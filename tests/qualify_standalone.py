#!/usr/bin/env python3
"""Run a physical DAQ smoke check against an explicitly isolated TimescaleDB.

This checks the standalone path, sample counts, local host timing, calibration,
and gaps. Fault injection is a separate gate item.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

import psycopg2
from psycopg2 import sql


SERVICE = Path(__file__).resolve().parents[1]
ROOT = SERVICE
SCRIPT = SERVICE / "core" / "buffered_daq_to_timescaledb.py"
DEFAULT_REPORT_DIR = ROOT / ".scratch" / "daq-navi-production" / "qualification"
IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test-dsn", default=os.getenv("DAQ_TEST_DB_DSN"))
    parser.add_argument("--base-config", type=Path, default=SERVICE / "config.json")
    parser.add_argument("--duration", type=int, default=60, help="physical acquisition seconds")
    parser.add_argument("--rate", type=int, choices=(1000, 2000), default=2000,
                        help="samples per second per channel")
    parser.add_argument("--prepare-only", action="store_true", help="write an isolated config without using DAQ or DB")
    parser.add_argument("--output-config", type=Path)
    parser.add_argument("--spool-dir", type=Path)
    parser.add_argument("--table")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    return parser, parser.parse_args()


def require_isolated_dsn(parser, dsn):
    if not dsn:
        parser.error("--test-dsn or DAQ_TEST_DB_DSN is required")
    try:
        database = psycopg2.extensions.parse_dsn(dsn).get("dbname", "")
    except psycopg2.ProgrammingError:
        parser.error("invalid test database DSN")
    if not database.startswith("daq_navi_test_"):
        parser.error("test DSN must name an isolated daq_navi_test_* database")
    return database


def write_private_json(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(content, file, indent=2)
        file.write("\n")


def make_qualification_config(base, dsn, spool_dir, table, rate=2000):
    config = json.loads(json.dumps(base))
    config.update({
        "DESTINATION": "postgresql",
        "START_CHANNEL": 0,
        "CHANNEL_COUNT": 4,
        "CLOCK_RATE": rate,
        "SECTION_LENGTH": 500,
        "SECTION_COUNT": 0,
        "DB_DSN": dsn,
        "DB_PRODUCTION_TABLE": table,
        "DB_RETENTION_DAYS": 30,
        "SPOOL_DIR": str(spool_dir),
        "SPOOL_MAX_BYTES": 4 * 1024**3,
    })
    channels = config.setdefault("CHANNELS", {})
    for index in range(4):
        channel = dict(channels.get(str(index), {}))
        channel.update({
            "enabled": True,
            "label": f"qualification-ch{index}",
            "unit": "V",
            "signal_type": channel.get("signal_type", "SingleEnded"),
            "value_range": channel.get("value_range", "V_0To5"),
            "scale": {
                "enabled": True,
                "low_voltage": 0.0,
                "high_voltage": 5.0,
                "low_value": 0.0,
                "high_value": 5.0,
                "revision": "qualification-identity-v1",
            },
        })
        channels[str(index)] = channel
    return config


def read_results(dsn, table, start_time, stop_time):
    with psycopg2.connect(dsn, connect_timeout=5) as conn:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("""SELECT channel, count(*), count(DISTINCT sample_id),
                min(time), max(time), max(abs(raw_voltage-calibrated_value)),
                count(DISTINCT session_id),
                count(*) FILTER (WHERE provenance <> 'physical_daq')
                FROM {} GROUP BY channel ORDER BY channel""").format(sql.Identifier(table)))
            channels = []
            for channel, count, unique, first, last, scale_error, sessions, wrong_source in cur.fetchall():
                span = (last - first).total_seconds() if count > 1 else 0.0
                channels.append({
                    "channel": channel,
                    "count": count,
                    "unique_sample_ids": unique,
                    "first_utc": first.isoformat(),
                    "last_utc": last.isoformat(),
                    "rate_from_timestamps_hz": round((count - 1) / span, 3) if span > 0 else 0,
                    "max_calibration_error_v": float(scale_error or 0),
                    "sessions": sessions,
                    "wrong_provenance_rows": wrong_source,
                })
            cur.execute("SELECT to_regclass('public.daq_production_gaps')")
            if cur.fetchone()[0] is None:
                gaps = []
            else:
                cur.execute("""SELECT cause, start_time, end_time FROM daq_production_gaps
                    WHERE start_time BETWEEN %s AND %s ORDER BY start_time""", (
                    datetime.fromtimestamp(start_time - 1, timezone.utc),
                    datetime.fromtimestamp(stop_time + 1, timezone.utc),
                ))
                gaps = [{"cause": cause, "start_utc": start.isoformat(),
                         "end_utc": end.isoformat() if end is not None else None}
                        for cause, start, end in cur.fetchall()]
    return channels, gaps


def assess(channels, gaps, duration, rate, start_time, stop_time, exit_code, exited_early, forced_kill):
    errors = []
    if exit_code != 0:
        errors.append(f"standalone process exited with code {exit_code}")
    if exited_early:
        errors.append("standalone process exited before requested duration")
    if forced_kill:
        errors.append("standalone process did not stop within 30 seconds")
    if [row["channel"] for row in channels] != [0, 1, 2, 3]:
        errors.append("database does not contain all four qualification channels")
    for row in channels:
        if row["count"] < duration * rate - rate // 2:
            errors.append(f"channel {row['channel']} is missing over half a second of samples")
        if row["unique_sample_ids"] != row["count"]:
            errors.append(f"channel {row['channel']} has duplicate sample identities")
        if not rate - 2 <= row["rate_from_timestamps_hz"] <= rate + 2:
            errors.append(f"channel {row['channel']} timestamp rate differs from {rate:,} Hz")
        if row["max_calibration_error_v"] > 1e-6:
            errors.append(f"channel {row['channel']} identity calibration differs from raw voltage")
        if row["sessions"] != 1 or row["wrong_provenance_rows"]:
            errors.append(f"channel {row['channel']} has invalid session or provenance")
        if abs(datetime.fromisoformat(row["first_utc"]).timestamp() - start_time) > 1.0:
            errors.append(f"channel {row['channel']} first sample differs from capture-ready time by over one second")
        if abs(stop_time - datetime.fromisoformat(row["last_utc"]).timestamp()) > 0.5:
            errors.append(f"channel {row['channel']} last sample differs from host stop time by over half a second")
    for gap in gaps:
        if gap["cause"] not in {"requested_stop", "operator_stop", "manual_stop"}:
            errors.append(f"unexpected acquisition gap: {gap['cause']}")
    return errors


def main():
    parser, args = parse_args()
    database = require_isolated_dsn(parser, args.test_dsn)
    if args.duration < 2:
        parser.error("--duration must be at least two seconds")
    run_id = uuid.uuid4().hex[:10]
    table = args.table or f"daq_qualification_{run_id}"
    if not IDENTIFIER.fullmatch(table) or len(table) > 63:
        parser.error("--table must be a simple SQL identifier")
    spool_dir = args.spool_dir or Path(tempfile.mkdtemp(prefix="daq_navi_qualification_spool_"))
    config_path = args.output_config or Path(tempfile.mkdtemp(prefix="daq_navi_qualification_config_")) / "config.json"
    base = json.loads(args.base_config.read_text())
    config = make_qualification_config(base, args.test_dsn, spool_dir, table, args.rate)
    write_private_json(config_path, config)
    if args.prepare_only:
        print(f"Prepared isolated acquisition config: {config_path}")
        return 0

    # Verify the target before opening the physical DAQ. All following SQL is
    # confined to a database whose name passed require_isolated_dsn().
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

    args.report_dir.mkdir(parents=True, exist_ok=True)
    log_path = args.report_dir / f"{run_id}.log"
    report_path = args.report_dir / f"{run_id}.json"
    env = os.environ.copy()
    env.update({
        "DB_DSN": args.test_dsn,
        "DESTINATION": "postgresql",
        "DB_RETENTION_DAYS": "30",
    })
    launch_time = time.time()
    start_time = launch_time
    exited_early = False
    forced_kill = False
    capture_ready = False
    status_path = spool_dir / "status.json"
    with log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            [sys.executable, str(SCRIPT), "--config", str(config_path)],
            cwd=SERVICE,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        startup_deadline = time.monotonic() + 30
        while time.monotonic() < startup_deadline:
            if process.poll() is not None:
                exited_early = True
                break
            try:
                live_status = json.loads(status_path.read_text())
                capture_ready = (live_status.get("state") == "running"
                                 and live_status.get("checked_at_ns", 0) >= int(launch_time * 1e9)
                                 and bool(live_status.get("last_sample_ns")))
            except (FileNotFoundError, json.JSONDecodeError):
                pass
            if capture_ready:
                start_time = time.time()
                break
            time.sleep(0.1)
        deadline = time.monotonic() + args.duration if capture_ready else time.monotonic()
        while time.monotonic() < deadline:
            if process.poll() is not None:
                exited_early = True
                break
            time.sleep(0.1)
        stop_time = time.time()
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            exit_code = process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            forced_kill = True
            process.kill()
            exit_code = process.wait(timeout=5)

    try:
        channels, gaps = read_results(args.test_dsn, table, launch_time, stop_time)
        errors = assess(channels, gaps, args.duration, args.rate, start_time, stop_time,
                        exit_code, exited_early, forced_kill)
    except psycopg2.Error as exc:
        channels, gaps = [], []
        errors = [f"database verification failed: {exc.pgcode or type(exc).__name__}"]
    if not capture_ready:
        errors.append("physical acquisition did not produce a sample within 30 seconds")
    status = json.loads(status_path.read_text()) if status_path.exists() else None
    if not status or status.get("reliable_clock_reads", 0) == 0:
        errors.append("physical run yielded no reliable DAQ timing reads")
    if status and status.get("pending_batches", 0):
        errors.append(f"{status['pending_batches']} committed batches remain undelivered")
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
        "launch_utc": datetime.fromtimestamp(launch_time, timezone.utc).isoformat(),
        "start_utc": datetime.fromtimestamp(start_time, timezone.utc).isoformat(),
        "stop_utc": datetime.fromtimestamp(stop_time, timezone.utc).isoformat(),
        "duration_requested_seconds": args.duration,
        "rate_requested_hz": args.rate,
        "exit_code": exit_code,
        "spool_dir": str(spool_dir),
        "config_path": str(config_path),
        "channels": channels,
        "gaps": gaps,
        "pipeline_status": status,
        "clock_reference": "local_database_host",
        "errors": errors,
        "local_result": "pass" if not errors else "fail",
    }
    write_private_json(report_path, report)
    print(f"Local DAQ qualification: {report['local_result']} (report {report_path}, log {log_path})")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
