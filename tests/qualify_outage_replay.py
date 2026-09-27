#!/usr/bin/env python3
"""Exercise physical DAQ buffering and restart replay in an isolated database."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from datetime import datetime, timezone

import psycopg2
from psycopg2 import sql

from qualify_standalone import (
    DEFAULT_REPORT_DIR,
    SCRIPT,
    SERVICE,
    make_qualification_config,
    require_isolated_dsn,
    write_private_json,
)


def run_for(config_path, seconds, log_path, dsn, crash_at_stop=False, status_path=None):
    env = os.environ.copy()
    env.update({"DB_DSN": dsn, "DESTINATION": "postgresql"})
    start = time.time()
    early = False
    killed = False
    observations = []
    started_ns = time.time_ns()
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--config", str(config_path)],
            cwd=SERVICE, env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                early = True
                break
            if status_path is not None:
                try:
                    status = json.loads(status_path.read_text())
                    if status.get("checked_at_ns", 0) >= started_ns:
                        observations.append({"observed_at": time.time(), **status})
                except (OSError, ValueError):
                    pass
            time.sleep(1 if status_path is not None else 0.1)
        stop = time.time()
        if process.poll() is None:
            process.send_signal(signal.SIGKILL if crash_at_stop else signal.SIGTERM)
        try:
            result = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            killed = True
            process.kill()
            result = process.wait(timeout=5)
    return {"start": start, "stop": stop, "exit_code": result, "early_exit": early,
            "forced_kill": killed, "observations": observations}


def assess_live_catchup(observations, window_seconds=15):
    """Judge delivery capacity while physical acquisition is still running."""
    if any(row.get("state") == "failed" or row.get("last_fault") for row in observations):
        return {"result": "fail", "reason": "DAQ reported an acquisition fault",
                "net_drain_bytes_per_second": None, "estimated_catchup_seconds": None}
    running = [row for row in observations if row.get("state") == "running"
               and row.get("source") == "physical_daq"
               and row.get("last_sample_ns") is not None
               and row.get("pending_bytes") is not None]
    result = {"result": "inconclusive", "reason": "too few live observations",
              "net_drain_bytes_per_second": None, "estimated_catchup_seconds": None}
    if len(running) < 3:
        return result
    session = running[-1]["session_id"]
    running = [row for row in running if row.get("session_id") == session]
    end = running[-1]["observed_at"]
    recent = [row for row in running if row["observed_at"] >= end - window_seconds]
    span = recent[-1]["observed_at"] - recent[0]["observed_at"]
    if span < min(10, window_seconds * 0.75) or len(recent) < 6:
        return result
    third = max(2, len(recent) // 3)
    first, middle, last = recent[:third], recent[third:2 * third], recent[-third:]
    if not middle or not (first[-1]["last_sample_ns"] < middle[-1]["last_sample_ns"]
                          < last[-1]["last_sample_ns"]):
        result["reason"] = "physical acquisition did not advance throughout observation"
        return result
    begin_bytes = statistics.median(row["pending_bytes"] for row in first)
    end_bytes = statistics.median(row["pending_bytes"] for row in last)
    result.update({"window_seconds": round(span, 2), "pending_bytes_start": begin_bytes,
                   "pending_bytes_end": end_bytes, "session_id": session})
    if max(row["pending_batches"] for row in last) <= 1:
        return {**result, "result": "pass", "reason": "caught up while DAQ kept acquiring",
                "estimated_catchup_seconds": 0}
    net_drain = (begin_bytes - end_bytes) / span
    result["net_drain_bytes_per_second"] = round(net_drain, 2)
    if net_drain > 0:
        return {**result, "result": "pass", "reason": "backlog shrank during live acquisition",
                "estimated_catchup_seconds": round(end_bytes / net_drain, 1)}
    return {**result, "result": "fail", "reason": "backlog did not shrink during live acquisition"}


def buffered_samples(spool_path):
    with sqlite3.connect(spool_path) as conn:
        payloads = [payload for (payload,) in conn.execute("SELECT payload FROM batches")]
        rows = [json.loads(zlib.decompress(payload)) for payload in payloads]
        gaps = conn.execute("SELECT cause, end_ns FROM gaps ORDER BY start_ns").fetchall()
    flat = [sample for batch in rows for sample in batch]
    sessions = {sample["session_id"] for sample in flat}
    return flat, sessions, gaps, sum(map(len, payloads))


def datetime_ns(value):
    elapsed = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return ((elapsed.days * 86400 + elapsed.seconds) * 1_000_000_000
            + elapsed.microseconds * 1000)


def compare_replayed_rows(expected_rows, stored_rows):
    """Compare every committed sample with its row after database replay."""
    expected = {row["sample_id"]: row for row in expected_rows}
    remaining = set(expected)
    max_time_error_ns = 0
    value_mismatches = 0
    unexpected_rows = 0
    for sample_id, recorded_at, voltage, scaled, unit, channel in stored_rows:
        row = expected.get(sample_id)
        if row is None:
            unexpected_rows += 1
            continue
        remaining.discard(sample_id)
        max_time_error_ns = max(max_time_error_ns,
                                abs(datetime_ns(recorded_at) - row["time_ns"]))
        if ((voltage, scaled, unit, channel) !=
                (row["raw_voltage"], row["calibrated_value"], row["unit"],
                 row["channel"])):
            value_mismatches += 1
    return {
        "missing_sample_ids": len(remaining),
        "unexpected_sample_ids": unexpected_rows,
        "value_mismatches": value_mismatches,
        "maximum_timestamp_rounding_ns": max_time_error_ns,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-dsn", default=os.getenv("DAQ_TEST_DB_DSN"))
    parser.add_argument("--base-config", type=Path, default=SERVICE / "config.json")
    parser.add_argument("--outage-seconds", type=int, default=30)
    parser.add_argument("--replay-seconds", type=int, default=60,
                        help="observe delivery while DAQ continues; backlog need not reach zero")
    parser.add_argument("--crash-first", action="store_true",
                        help="kill the disconnected first process to check crash recovery")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args()
    database = require_isolated_dsn(parser, args.test_dsn)
    if args.outage_seconds < 2 or args.replay_seconds < 2:
        parser.error("each phase must run for at least two seconds")
    with psycopg2.connect(args.test_dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            if cur.fetchone()[0] != database:
                parser.error("test database identity changed after connection")
            cur.execute("SELECT current_setting('server_version')")
            postgres_version = cur.fetchone()[0]
            cur.execute("SELECT extversion FROM pg_extension WHERE extname='timescaledb'")
            extension = cur.fetchone()
            timescaledb_version = extension[0] if extension else None

    run_id = uuid.uuid4().hex[:10]
    table = f"daq_replay_{run_id}"
    spool_dir = Path(tempfile.mkdtemp(prefix="daq_navi_replay_spool_"))
    config_dir = Path(tempfile.mkdtemp(prefix="daq_navi_replay_config_"))
    base = json.loads(args.base_config.read_text())
    config = make_qualification_config(base, args.test_dsn, spool_dir, table)
    args.report_dir.mkdir(parents=True, exist_ok=True)

    # Point only this test process at a closed local port. The test DB and all
    # production services keep running; no shared database is stopped.
    outage_dsn = psycopg2.extensions.make_dsn(args.test_dsn, host="127.0.0.1", port="1", connect_timeout="1")
    config["DB_DSN"] = outage_dsn
    outage_path = config_dir / "outage.json"
    write_private_json(outage_path, config)
    first = run_for(outage_path, args.outage_seconds, args.report_dir / f"{run_id}-outage.log",
                    outage_dsn, crash_at_stop=args.crash_first)
    spool_path = spool_dir / "production-spool.sqlite3"
    if not spool_path.exists():
        print("Outage acquisition created no durable spool", file=sys.stderr)
        return 1
    first_rows, first_sessions, first_gaps, outage_payload_bytes = buffered_samples(spool_path)

    config["DB_DSN"] = args.test_dsn
    replay_path = config_dir / "replay.json"
    write_private_json(replay_path, config)
    second = run_for(replay_path, args.replay_seconds, args.report_dir / f"{run_id}-replay.log",
                     args.test_dsn, status_path=spool_dir / "status.json")
    observations = second.pop("observations")
    catchup = assess_live_catchup(observations)
    pending_rows, _, final_gaps, _ = buffered_samples(spool_path)

    errors = []
    expected_first_exit = -signal.SIGKILL if args.crash_first else 0
    if first["exit_code"] != expected_first_exit or first["early_exit"] or first["forced_kill"]:
        errors.append("outage phase failed or ended early")
    if second["exit_code"] != 0 or second["early_exit"] or second["forced_kill"]:
        errors.append("replay phase failed or ended early")
    if not first_rows or len(first_sessions) != 1:
        errors.append("outage phase did not commit one session of physical samples")
    first_channel_counts = {channel: sum(row["channel"] == channel for row in first_rows)
                            for channel in range(4)}
    if len(set(first_channel_counts.values())) != 1:
        errors.append("outage phase committed unequal channel sample counts")
    if catchup["result"] == "fail":
        errors.append(catchup["reason"])

    database_rows = 0
    first_session_rows = 0
    unique_sample_ids = 0
    observed_sessions = 0
    replay_comparison = None
    try:
        with psycopg2.connect(args.test_dsn, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT count(*), count(DISTINCT sample_id), count(DISTINCT session_id) FROM {}").format(sql.Identifier(table)))
                database_rows, unique_sample_ids, observed_sessions = cur.fetchone()
                if first_sessions:
                    cur.execute(sql.SQL("SELECT count(*) FROM {} WHERE session_id=%s").format(sql.Identifier(table)), (next(iter(first_sessions)),))
                    first_session_rows = cur.fetchone()[0]
                    cur.execute(sql.SQL("""SELECT sample_id,time,raw_voltage,
                        calibrated_value,unit,channel FROM {}
                        WHERE session_id=%s""").format(sql.Identifier(table)),
                        (next(iter(first_sessions)),))
                    replay_comparison = compare_replayed_rows(first_rows, cur.fetchall())
    except psycopg2.Error as exc:
        errors.append(f"test database verification failed: {exc.pgcode or type(exc).__name__}")
    if first_session_rows > len(first_rows) or (not pending_rows and first_session_rows != len(first_rows)):
        errors.append("replay did not preserve every committed outage sample exactly once")
    if replay_comparison and ((not pending_rows and replay_comparison["missing_sample_ids"])
                              or replay_comparison["unexpected_sample_ids"]
                              or replay_comparison["value_mismatches"]
                              or replay_comparison["maximum_timestamp_rounding_ns"] > 1000):
        errors.append("replayed sample identity, value, or timestamp differs from durable spool")
    if unique_sample_ids != database_rows:
        errors.append("database contains duplicate sample identities")
    if observed_sessions != 2 and not pending_rows:
        errors.append("database does not show two distinct physical acquisition sessions")
    expected_gap = "process_restart" if args.crash_first else "requested_stop"
    if not any(cause == expected_gap and end_ns is not None for cause, end_ns in final_gaps):
        errors.append("stop-to-restart acquisition gap was not closed")

    integrity_errors = {"replay did not preserve every committed outage sample exactly once",
                        "replayed sample identity, value, or timestamp differs from durable spool",
                        "database contains duplicate sample identities",
                        "database does not show two distinct physical acquisition sessions"}
    integrity = ("failed" if any(error in integrity_errors or
                                 error.startswith("test database verification failed") for error in errors)
                 else "pending" if pending_rows else "verified")

    report = {
        "database": database,
        "database_conditions": {
            "postgres_version": postgres_version,
            "timescaledb_version": timescaledb_version,
        },
        "table": table,
        "hardware_conditions": {
            "device_description": config["DEVICE_DESCRIPTION"],
            "device_id": config["DEVICE_ID"],
            "kernel_release": platform.release(),
            "configured_channels": [0, 1, 2, 3],
        },
        "crash_first": args.crash_first,
        "outage_phase": first,
        "replay_phase": second,
        "live_catchup": catchup,
        "recovery_observations": [{key: row.get(key) for key in (
            "observed_at", "session_id", "state", "last_sample_ns", "last_delivery_ns",
            "pending_batches", "pending_bytes", "last_fault")}
            for row in observations],
        "committed_outage_rows": len(first_rows),
        "committed_outage_rows_by_channel": first_channel_counts,
        "outage_payload_bytes": outage_payload_bytes,
        "estimated_24h_payload_bytes_at_4ch_2khz": (
            round(outage_payload_bytes / len(first_rows) * 4 * 2000 * 86400)
            if first_rows else None
        ),
        "committed_outage_sessions": len(first_sessions),
        "database_rows": database_rows,
        "replayed_outage_rows": first_session_rows,
        "replay_comparison": replay_comparison,
        "unique_sample_ids": unique_sample_ids,
        "database_sessions": observed_sessions,
        "pending_rows_after_replay": len(pending_rows),
        "replay_integrity": integrity,
        "gaps_before_replay": [{"cause": cause, "closed": end_ns is not None} for cause, end_ns in first_gaps],
        "gaps_after_replay": [{"cause": cause, "closed": end_ns is not None} for cause, end_ns in final_gaps],
        "spool_dir": str(spool_dir),
        "errors": errors,
        "local_result": ("fail" if errors else "pass" if catchup["result"] == "pass"
                         else "inconclusive"),
    }
    report_path = args.report_dir / f"{run_id}-replay.json"
    write_private_json(report_path, report)
    print(f"Physical outage/replay: {report['local_result']} (report {report_path})")
    return {"pass": 0, "fail": 1, "inconclusive": 2}[report["local_result"]]


if __name__ == "__main__":
    raise SystemExit(main())
