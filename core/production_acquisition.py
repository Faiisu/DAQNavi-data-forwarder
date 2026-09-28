"""Durable physical DAQ acquisition, independent of the web process.

The public seam is ProductionPipeline.capture/flush_once/run.  A committed
SQLite batch is the recovery boundary; the destination may acknowledge a
commit ambiguously, so every database write is idempotent.
"""

from __future__ import annotations

import json
import csv
import fcntl
import io
import logging
import math
import os
import shutil
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
import hashlib
import shlex
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from collections import deque

import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

try:
    from .config_loader import AiSignalType, ValueRange, validate_config_values
except ImportError:
    from config_loader import AiSignalType, ValueRange, validate_config_values


log = logging.getLogger(__name__)
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_WRITER_BATCH_GROUP = 4
_WRITER_MAX_ROWS = 8000


class AcquisitionFault(RuntimeError):
    """Physical acquisition cannot preserve or trust further measurements."""


def validate_production_config(cfg):
    validate_config_values(cfg.raw)
    dest = str(cfg.DESTINATION).lower()
    if dest not in ("postgresql", "timescaledb", "influxdb", "mqtt"):
        raise ValueError("production destination must be PostgreSQL/TimescaleDB, InfluxDB, or MQTT")
    if not isinstance(cfg.DEVICE_ID, str) or not cfg.DEVICE_ID.strip():
        raise ValueError("DEVICE_ID is required")
    if cfg.SECTION_COUNT != 0:
        raise ValueError("SECTION_COUNT must be zero for continuous production acquisition")
    if cfg.START_CHANNEL < 0 or cfg.START_CHANNEL + cfg.CHANNEL_COUNT > 16:
        raise ValueError("configured DAQ channel span is invalid")
    if cfg.SPOOL_MAX_BYTES <= 0:
        raise ValueError("SPOOL_MAX_BYTES must be positive")
    if not _IDENTIFIER.fullmatch(cfg.DB_PRODUCTION_TABLE):
        raise ValueError("DB_PRODUCTION_TABLE must be a simple SQL identifier")
    if cfg.DB_PRODUCTION_TABLE == cfg.DB_TABLE:
        raise ValueError("production table must differ from the legacy table")
    if dest == "influxdb":
        parsed = urllib.parse.urlsplit(str(cfg.INFLUX_URL))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("INFLUX_URL must be a valid HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("INFLUX_URL must not contain credentials; use INFLUX_TOKEN")
        if not cfg.INFLUX_ORG.strip() or not cfg.INFLUX_BUCKET.strip() or not cfg.INFLUX_TOKEN.strip():
            raise ValueError("InfluxDB organization, bucket, and token are required")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg.INFLUX_MEASUREMENT):
            raise ValueError("INFLUX_MEASUREMENT must be a simple measurement name")
        if "\n" in cfg.DEVICE_ID or "\r" in cfg.DEVICE_ID:
            raise ValueError("DEVICE_ID cannot contain newline characters for InfluxDB")
    elif dest == "mqtt":
        if not getattr(cfg, "MQTT_TLS_ENABLED", False):
            raise ValueError("MQTT production destination requires TLS to be enabled")
        username = getattr(cfg, "MQTT_USERNAME", "")
        if not username or not str(username).strip():
            raise ValueError("MQTT production destination requires username")
        password = getattr(cfg, "MQTT_PASSWORD", "")
        if not password or not str(password).strip():
            raise ValueError("MQTT production destination requires password")
        qos = getattr(cfg, "MQTT_PRODUCTION_QOS", 1)
        if qos not in (0, 1):
            raise ValueError("MQTT production QoS must be 0 or 1")
        topic_prefix = getattr(cfg, "MQTT_PRODUCTION_TOPIC_PREFIX", "")
        if not topic_prefix or not str(topic_prefix).strip() or "+" in topic_prefix or "#" in topic_prefix:
            raise ValueError("MQTT production topic prefix must be non-empty and cannot contain wildcards (+ or #)")
    selected_span = range(cfg.START_CHANNEL, cfg.START_CHANNEL + cfg.CHANNEL_COUNT)
    for index, channel in cfg.channels.items():
        if channel.enabled and index not in selected_span:
            raise ValueError(f"enabled channel {index} is outside the configured DAQ span")
    enabled = []
    pci_1716 = re.match(r'^PCI-1716(?:H|L)?(?:,|$)', cfg.DEVICE_DESCRIPTION, re.IGNORECASE)
    for index in range(cfg.START_CHANNEL, cfg.START_CHANNEL + cfg.CHANNEL_COUNT):
        channel = cfg.channels.get(index)
        raw_channel = cfg.raw.get("CHANNELS", {}).get(str(index))
        if raw_channel is None:
            raise ValueError(f"channel {index} configuration is required")
        for setting, enum in (("signal_type", AiSignalType), ("value_range", ValueRange)):
            name = raw_channel.get(setting)
            members = getattr(enum, "__members__", vars(enum))
            if not isinstance(name, str) or name not in members:
                raise ValueError(f"channel {index} {setting} is invalid")
        signal_type = raw_channel['signal_type']
        if pci_1716:
            if signal_type == 'PseudoDifferential':
                raise ValueError(f"channel {index} PseudoDifferential is not supported by PCI-1716")
            if channel.enabled and signal_type == 'Differential':
                if index % 2:
                    raise ValueError(f"channel {index} Differential must use an even-numbered channel")
                other = cfg.channels.get(index + 1)
                if other is not None and other.enabled:
                    raise ValueError(f"channel {index + 1} cannot be enabled when channel {index} is Differential")
        if channel is None or not channel.enabled:
            continue
        enabled.append(index)
        if not isinstance(raw_channel.get("label"), str):
            raise ValueError(f"channel {index} label is required")
        if not channel.label.strip():
            raise ValueError(f"channel {index} label is required")
        if not isinstance(raw_channel.get("unit"), str) or not channel.unit:
            raise ValueError(f"channel {index} unit is required")
        if not channel.scale_enabled or not channel.has_scale:
            raise ValueError(f"channel {index} calibration must be enabled")
        if cfg.DESTINATION == "influxdb" and any("\n" in value or "\r" in value for value in (
                channel.unit, channel.label)):
            raise ValueError(f"channel {index} InfluxDB tag values cannot contain newline characters")
        scale = raw_channel.get("scale", {})
        if type(scale.get("enabled")) is not bool or any(key not in scale for key in (
            "low_voltage", "high_voltage", "low_value", "high_value"
        )):
            raise ValueError(f"channel {index} calibration fields are required")
        for key in ("low_voltage", "high_voltage", "low_value", "high_value"):
            if type(scale[key]) not in (int, float):
                raise ValueError(f"channel {index} calibration {key} must be a number")
            if not math.isfinite(getattr(channel, key)):
                raise ValueError(f"channel {index} calibration {key} must be finite")
        if channel.high_voltage == channel.low_voltage:
            raise ValueError(f"channel {index} calibration voltage span must be nonzero")
    if not enabled:
        raise ValueError("at least one physical channel must be enabled")
    return tuple(enabled)


def destination_identity(cfg):
    """Opaque, non-secret fingerprint for the durable spool's sink ownership."""
    if cfg.DESTINATION == "influxdb":
        parsed = urllib.parse.urlsplit(cfg.INFLUX_URL)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("INFLUX_URL must not contain credentials; use INFLUX_TOKEN")
        identity = ["influxdb", cfg.INFLUX_URL.rstrip("/"), cfg.INFLUX_ORG, cfg.INFLUX_BUCKET,
                    cfg.INFLUX_MEASUREMENT]
    elif cfg.DESTINATION == "mqtt":
        identity = ["mqtt", str(cfg.MQTT_BROKER), int(cfg.MQTT_PORT),
                    str(getattr(cfg, "MQTT_PRODUCTION_TOPIC_PREFIX", "daq/production/v1")),
                    int(getattr(cfg, "MQTT_PRODUCTION_QOS", 1))]
    else:
        dsn = str(cfg.DB_DSN)
        parsed = urllib.parse.urlsplit(dsn)
        if parsed.scheme in ("postgres", "postgresql"):
            query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            target = [parsed.hostname or query.get("host", ""), parsed.port or query.get("port", ""),
                      parsed.username or query.get("user", ""), query.get("dbname", parsed.path.lstrip("/")),
                      query.get("hostaddr", ""), query.get("service", "")]
        else:
            values = {}
            for token in shlex.split(dsn):
                if "=" not in token:
                    raise ValueError("DB_DSN must be a PostgreSQL URL or keyword DSN")
                key, value = token.split("=", 1)
                values[key.lower()] = value
            target = [values.get("host", ""), values.get("port", ""), values.get("user", ""),
                      values.get("dbname", ""), values.get("service", "")]
            if not any(target):
                raise ValueError("DB_DSN must identify a PostgreSQL host or service")
        mode = getattr(cfg, "DB_CONNECTION_MODE", "fields")
        identity = ["postgresql", mode, *target, cfg.DB_PRODUCTION_TABLE]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


class DurableSpool:
    """SQLite WAL journal with durable, ordered batch commits."""

    def __init__(self, directory: Path, max_bytes: int):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock_file = open(self.directory / "acquisition.lock", "a+")
        try:
            fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._lock_file.close()
            raise AcquisitionFault("acquisition_already_running") from exc
        self.path = self.directory / "production-spool.sqlite3"
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("CREATE TABLE IF NOT EXISTS batches (batch_id TEXT PRIMARY KEY, created_ns INTEGER NOT NULL, payload BLOB NOT NULL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS gaps (gap_id TEXT PRIMARY KEY, start_ns INTEGER NOT NULL, end_ns INTEGER, cause TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, delivered_revision INTEGER NOT NULL DEFAULT 0)")
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(gaps)")}
        if "revision" not in columns:
            self.conn.execute("ALTER TABLE gaps ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        if "delivered_revision" not in columns:
            self.conn.execute("ALTER TABLE gaps ADD COLUMN delivered_revision INTEGER NOT NULL DEFAULT 0")
            if "delivered" in columns:
                self.conn.execute("UPDATE gaps SET delivered_revision = delivered")
        self.conn.execute("CREATE TABLE IF NOT EXISTS cutovers (id INTEGER PRIMARY KEY AUTOINCREMENT, time_ns INTEGER NOT NULL, old_destination TEXT, new_destination TEXT, pending_records INTEGER NOT NULL DEFAULT 0)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.conn.commit()
        # Rebuild once after recovery. Recounting all pending blobs on every
        # capture becomes quadratic during a day-long database outage.
        self._pending_count, self._pending_bytes = self.conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(length(payload)),0) FROM batches"
        ).fetchone()

    @property
    def pending_batches(self):
        with self._lock:
            return self._pending_count

    @property
    def usage_bytes(self):
        return sum(path.stat().st_size for path in (
            self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")
        ) if path.exists())

    @property
    def pending_bytes(self):
        with self._lock:
            return self._pending_bytes

    def append(self, batch_id: str, rows: list[dict],
               first_sample_ns: int | None = None, last_sample_ns: int | None = None):
        payload = zlib.compress(json.dumps(rows, separators=(",", ":"), allow_nan=False).encode(), 1)
        with self._lock:
            if self.pending_bytes + len(payload) > self.max_bytes:
                raise AcquisitionFault("buffer_full")
            if shutil.disk_usage(self.directory).free < len(payload) + 1024 * 1024:
                raise AcquisitionFault("buffer_unwritable")
            try:
                self.conn.execute("INSERT INTO batches VALUES (?, ?, ?)", (batch_id, time.time_ns(), payload))
                if last_sample_ns is not None:
                    self.conn.execute("INSERT INTO state VALUES ('last_sample_ns', ?) "
                                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                      (str(last_sample_ns),))
                if first_sample_ns is not None:
                    self.conn.execute("UPDATE gaps SET end_ns=MAX(start_ns, ?), revision=revision+1 "
                                      "WHERE end_ns IS NULL", (first_sample_ns,))
                self.conn.commit()
                self._pending_count += 1
                self._pending_bytes += len(payload)
            except sqlite3.Error as exc:
                self.conn.rollback()
                raise AcquisitionFault("buffer_unwritable") from exc

    def oldest(self):
        batches = self.oldest_many(1)
        return batches[0] if batches else None

    def oldest_many(self, limit: int):
        if limit < 1:
            raise ValueError("batch limit must be positive")
        with self._lock:
            # The SQLite rowid records commit order. Wall time can step back
            # during NTP adjustment and must never reorder replay.
            batches = self.conn.execute(
                "SELECT batch_id, payload FROM batches ORDER BY rowid LIMIT ?", (limit,)
            ).fetchall()
        return [(batch_id, json.loads(zlib.decompress(payload)))
                for batch_id, payload in batches]

    def acknowledge(self, batch_id: str):
        self.acknowledge_many([batch_id])

    def acknowledge_many(self, batch_ids: list[str]):
        if not batch_ids:
            return
        with self._lock:
            placeholders = ",".join("?" for _ in batch_ids)
            try:
                lengths = self.conn.execute(
                    f"SELECT length(payload) FROM batches WHERE batch_id IN ({placeholders})",
                    batch_ids,
                ).fetchall()
                self.conn.executemany(
                    "DELETE FROM batches WHERE batch_id=?", ((batch_id,) for batch_id in batch_ids)
                )
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise
            self._pending_count -= len(lengths)
            self._pending_bytes -= sum(length for (length,) in lengths)
            if self.pending_batches == 0:
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def clear_pending(self):
        """Discard undelivered batches and record their sample intervals as gaps."""
        with self._lock:
            count = self._pending_count
            if count == 0:
                self._reclaim_space()
                return {"cleared_batches": 0, "cleared_samples": 0,
                        "cleared_bytes": 0, "recorded_gaps": 0}

            intervals = []
            sample_count = 0
            timestamp_key = b'"time_ns":'
            for (payload,) in self.conn.execute("SELECT payload FROM batches ORDER BY rowid"):
                data = zlib.decompress(payload)
                first_pos = data.find(timestamp_key)
                last_pos = data.rfind(timestamp_key)
                if first_pos < 0:
                    raise ValueError("pending batch has no sample timestamps")
                sample_count += data.count(timestamp_key)

                def timestamp_at(position):
                    start = position + len(timestamp_key)
                    end = start
                    while end < len(data) and 48 <= data[end] <= 57:
                        end += 1
                    if end == start:
                        raise ValueError("pending batch has an invalid sample timestamp")
                    return int(data[start:end])

                first = timestamp_at(first_pos)
                last = timestamp_at(last_pos)
                intervals.append((min(first, last), max(first, last)))

            # Adjacent batches are separated by at most one sample period at
            # the supported 1–2 kHz rates. Preserve larger delivered intervals.
            merged = []
            for start, end in sorted(intervals):
                if merged and start <= merged[-1][1] + 1_000_000:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], end))
                else:
                    merged.append((start, end))

            cleared_bytes = self._pending_bytes
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                self.conn.executemany(
                    "INSERT INTO gaps (gap_id, start_ns, end_ns, cause, revision, delivered_revision) VALUES (?, ?, ?, ?, 1, 0)",
                    ((str(uuid.uuid4()), start, end, "operator_cleared_buffer")
                     for start, end in merged),
                )
                self.conn.execute("DELETE FROM batches")
                self.conn.commit()
            except sqlite3.Error:
                self.conn.rollback()
                raise

            self._pending_count = 0
            self._pending_bytes = 0
            self._reclaim_space()
            return {"cleared_batches": count, "cleared_samples": sample_count,
                    "cleared_bytes": cleared_bytes, "recorded_gaps": len(merged)}

    def _reclaim_space(self):
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.conn.execute("VACUUM")
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            log.exception("Pending batches cleared, but spool space could not be reclaimed")

    def record_gap(self, start_ns: int, end_ns: int, cause: str):
        with self._lock:
            gap_id = str(uuid.uuid4())
            self.conn.execute("INSERT INTO gaps (gap_id, start_ns, end_ns, cause, revision, delivered_revision) VALUES (?, ?, ?, ?, 1, 0)",
                              (gap_id, start_ns, max(start_ns, end_ns), cause))
            self.conn.commit()
            return gap_id

    def open_gap(self, start_ns: int, cause: str):
        with self._lock:
            existing = self.conn.execute("SELECT gap_id FROM gaps WHERE end_ns IS NULL LIMIT 1").fetchone()
            if existing:
                return existing[0]
            gap_id = str(uuid.uuid4())
            self.conn.execute("INSERT INTO gaps (gap_id, start_ns, end_ns, cause, revision, delivered_revision) VALUES (?, ?, NULL, ?, 1, 0)",
                              (gap_id, start_ns, cause))
            self.conn.commit()
            return gap_id

    def close_open_gaps_for_switch(self, end_ns: int):
        """Close open intervals at a stopped boundary and queue their final update."""
        with self._lock:
            self.conn.execute("UPDATE gaps SET end_ns=MAX(start_ns, ?), revision=revision+1 WHERE end_ns IS NULL", (end_ns,))
            self.conn.commit()

    def record_cutover(self, old_destination: str, new_destination: str, pending_records: int = 0, when_ns: int | None = None):
        when_ns = time.time_ns() if when_ns is None else int(when_ns)
        with self._lock:
            self.conn.execute(
                "INSERT INTO cutovers (time_ns, old_destination, new_destination, pending_records) VALUES (?, ?, ?, ?)",
                (when_ns, str(old_destination or ""), str(new_destination or ""), int(pending_records))
            )
            self.conn.commit()

    def recent_cutovers(self, limit: int = 5):
        with self._lock:
            rows = self.conn.execute(
                "SELECT time_ns, old_destination, new_destination, pending_records "
                "FROM cutovers ORDER BY time_ns DESC, id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [{"time_ns": r[0], "from": r[1], "to": r[2], "pending_records": r[3]} for r in rows]

    def state_value(self, key):
        with self._lock:
            row = self.conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set_state(self, key, value):
        with self._lock:
            self.conn.execute("INSERT INTO state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self.conn.commit()

    def pending_gaps(self):
        with self._lock:
            return [dict(zip(("gap_id", "revision", "start_ns", "end_ns", "cause"), row))
                    for row in self.conn.execute(
                        "SELECT gap_id, revision, start_ns, end_ns, cause FROM gaps "
                        "WHERE delivered_revision < revision ORDER BY start_ns, revision")]

    def acknowledge_gaps(self, gap_items):
        with self._lock:
            for item in gap_items:
                if isinstance(item, dict):
                    gap_id = item["gap_id"]
                    rev = item.get("revision")
                    if rev is not None:
                        self.conn.execute(
                            "UPDATE gaps SET delivered_revision=MAX(delivered_revision, ?) WHERE gap_id=?",
                            (rev, gap_id),
                        )
                    else:
                        self.conn.execute(
                            "UPDATE gaps SET delivered_revision=revision WHERE gap_id=?",
                            (gap_id,),
                        )
                else:
                    self.conn.execute(
                        "UPDATE gaps SET delivered_revision=revision WHERE gap_id=?",
                        (str(item),),
                    )
            self.conn.commit()

    def close(self):
        with self._lock:
            self.conn.close()
            fcntl.flock(self._lock_file, fcntl.LOCK_UN)
            self._lock_file.close()


def clear_spooled_data(directory: Path, max_bytes: int):
    """Clear a stopped production spool and refresh its runtime snapshot."""
    spool = DurableSpool(directory, max_bytes)
    try:
        result = spool.clear_pending()
        spool_bytes = spool.usage_bytes
    finally:
        spool.close()

    target = Path(directory) / "status.json"
    try:
        runtime = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        runtime = {}
    runtime.update(state="stopped", checked_at_ns=time.time_ns(),
                   pending_batches=0, pending_bytes=0, spool_bytes=spool_bytes,
                   last_writer_error=None)
    temporary = target.with_suffix(".json.tmp")
    try:
        temporary.write_text(json.dumps(runtime), encoding="utf-8")
        os.replace(temporary, target)
    except OSError:
        log.exception("Pending batches cleared, but DAQ status could not be updated")
    return {**result, "pending_batches": 0, "spool_bytes": spool_bytes}


class ProductionPipeline:
    """Transforms physical DAQ batches and commits them before remote transfer."""

    def __init__(self, cfg, spool_dir: Path, destination, session_id=None):
        self.enabled = validate_production_config(cfg)
        self.cfg = cfg
        self.spool = DurableSpool(spool_dir, cfg.SPOOL_MAX_BYTES)
        self.destination = destination
        self.session_id = session_id or str(uuid.uuid4())
        self.frame_index = 0
        self.anchor_ns = None
        self.last_sample_ns = None
        self.last_fault = None
        self.last_writer_error = None
        self.last_delivery_ns = None
        self.state = "starting"
        self.last_receive_offset_ns = 0
        self.phase_adjust_ns = 0
        self._receipt_lag_ns = None
        self.reliable_reads = 0
        self._capture_lock = threading.Lock()
        self._status_lock = threading.Lock()
        prior_active = self.spool.state_value("acquisition_active")
        prior_last = self.spool.state_value("last_sample_ns")
        if prior_active == "1" and prior_last:
            self.spool.open_gap(int(prior_last) + 1_000_000_000 // cfg.CLOCK_RATE,
                                "process_restart")
        elif prior_last and not self.spool.conn.execute("SELECT 1 FROM gaps WHERE end_ns IS NULL LIMIT 1").fetchone():
            last_gap_end = self.spool.conn.execute("SELECT MAX(end_ns) FROM gaps").fetchone()[0]
            start = max(int(prior_last) + 1_000_000_000 // cfg.CLOCK_RATE,
                        int(last_gap_end) if last_gap_end is not None else 0)
            self.spool.open_gap(start, "between_runs")
        self.spool.set_state("acquisition_active", "1")
        self.publish_status()

    @property
    def pending_batches(self):
        return self.spool.pending_batches

    @property
    def usage_bytes(self):
        return self.spool.usage_bytes

    def publish_status(self):
        """Atomic interprocess snapshot; age reveals a dead child process."""
        with self._status_lock:
            status = {
                "state": self.state,
                "source": "physical_daq",
                "destination": self.cfg.DESTINATION,
                "session_id": self.session_id,
                "pid": os.getpid(),
                "checked_at_ns": time.time_ns(),
                "last_sample_ns": self.last_sample_ns,
                "last_fault": self.last_fault,
                "last_writer_error": self.last_writer_error,
                "last_delivery_ns": self.last_delivery_ns,
                "pending_batches": self.spool.pending_batches,
                "pending_bytes": self.spool.pending_bytes,
                "spool_bytes": self.spool.usage_bytes,
                "receive_offset_ns": self.last_receive_offset_ns,
                "clock_phase_adjust_ns": self.phase_adjust_ns,
                "reliable_clock_reads": self.reliable_reads,
            }
            target = self.spool.directory / "status.json"
            temporary = target.with_suffix(".json.tmp")
            try:
                with open(temporary, "w", encoding="utf-8") as output:
                    json.dump(status, output)
                    output.flush()
                os.replace(temporary, target)
            except OSError:
                log.exception("Could not publish DAQ status")
            return status

    def _gap(self, start_ns, end_ns, cause):
        try:
            self.spool.record_gap(start_ns, end_ns, cause)
        except sqlite3.Error:
            log.exception("Could not persist acquisition gap: %s", cause)

    def open_gap(self, cause: str, when_ns=None):
        point = when_ns or time.time_ns()
        start_ns = self.last_sample_ns
        if start_ns is None:
            persisted = self.spool.state_value("last_sample_ns")
            start_ns = int(persisted) if persisted else point
        try:
            self.spool.open_gap(start_ns + 1_000_000_000 // self.cfg.CLOCK_RATE, cause)
        except sqlite3.Error:
            log.exception("Could not persist open acquisition gap: %s", cause)

    def fault(self, cause: str, when_ns=None):
        self.last_fault = cause
        self.state = "failed"
        self.open_gap(cause, when_ns)
        self.publish_status()
        raise AcquisitionFault(cause)

    def capture(self, raw_data, end_time_ns: int, hardware_start_ns: int | None = None,
                timing_reliable: bool = False):
        with self._capture_lock:
            count = len(raw_data)
            channel_count = self.cfg.CHANNEL_COUNT
            if count == 0 or count % channel_count:
                self.fault("invalid_daq_batch", end_time_ns)
            frames = count // channel_count
            dt_ns = 1_000_000_000 // self.cfg.CLOCK_RATE
            if self.anchor_ns is None:
                self.anchor_ns = (hardware_start_ns if hardware_start_ns is not None
                                  else end_time_ns - (frames - 1) * dt_ns)
            expected_last_ns = (self.anchor_ns + (self.frame_index + frames - 1) * dt_ns
                                + self.phase_adjust_ns)
            # Receipt time can lag hardware samples when its ring buffer has a
            # backlog. Never infer a missing batch or re-anchor from this alone.
            self.last_receive_offset_ns = end_time_ns - expected_last_ns
            if expected_last_ns - end_time_ns > 1_000_000_000:
                self.fault("clock_alignment_lost", end_time_ns)
            phase_change_ns = 0
            if timing_reliable:
                self.reliable_reads += 1
                if self._receipt_lag_ns is None:
                    self._receipt_lag_ns = self.last_receive_offset_ns
                else:
                    residual = self.last_receive_offset_ns - self._receipt_lag_ns
                    # Read calls that block for new samples provide a usable
                    # wall-clock observation. Slew across every frame so a
                    # backward host adjustment cannot reverse sample time.
                    wanted = round(residual * 0.05)
                    limit = frames * dt_ns // 2
                    phase_change_ns = max(-limit, min(limit, wanted))
            rows = []
            for frame in range(frames):
                sequence = self.frame_index + frame
                sample_ns = (self.anchor_ns + sequence * dt_ns + self.phase_adjust_ns
                             + round((frame + 1) * phase_change_ns / frames))
                for physical_channel in self.enabled:
                    offset = physical_channel - self.cfg.START_CHANNEL
                    voltage = float(raw_data[frame * channel_count + offset])
                    if not math.isfinite(voltage):
                        self.fault("invalid_voltage", sample_ns)
                    channel = self.cfg.channels[physical_channel]
                    scaled = channel.low_value + (voltage - channel.low_voltage) * (
                        channel.high_value - channel.low_value
                    ) / (channel.high_voltage - channel.low_voltage)
                    rows.append({
                        "time_ns": sample_ns,
                        "sample_id": f"{self.session_id}:{sequence}:{physical_channel}",
                        "session_id": self.session_id,
                        "device_id": self.cfg.DEVICE_ID,
                        "channel": physical_channel,
                        "sensor_name": channel.label,
                        "raw_voltage": voltage,
                        "calibrated_value": scaled,
                        "unit": channel.unit,
                        "provenance": "physical_daq",
                    })
            try:
                self.spool.append(f"{self.session_id}:{self.frame_index}", rows,
                                  first_sample_ns=rows[0]["time_ns"],
                                  last_sample_ns=rows[-1]["time_ns"])
            except AcquisitionFault as exc:
                self.fault(str(exc), end_time_ns)
            self.frame_index += frames
            self.phase_adjust_ns += phase_change_ns
            self.last_sample_ns = rows[-1]["time_ns"]
            self.state = "running"
            self.publish_status()
            return len(rows)

    def flush_once(self):
        return self.flush_batches(1)

    def flush_batches(self, limit: int):
        pending = self.spool.oldest_many(limit)
        gaps = self.spool.pending_gaps()
        if not pending and not gaps:
            return False
        rows = [row for _, batch_rows in pending for row in batch_rows]
        self.destination.write(rows, gaps)
        self.spool.acknowledge_many([batch_id for batch_id, _ in pending])
        self.spool.acknowledge_gaps(gaps)
        self.last_delivery_ns = time.time_ns()
        self.last_writer_error = None
        self.publish_status()
        return True

    def close(self):
        if self.state != "failed":
            self.state = "stopped"
        self.publish_status()
        self.spool.set_state("acquisition_active", "0")
        self.spool.close()


class TimescaleProductionDestination:
    """TimescaleDB sink for the new production schema, separate from legacy data."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._initialized = False

    def _connect(self):
        return psycopg2.connect(self.cfg.DB_DSN, connect_timeout=3, options="-c statement_timeout=10000")

    def _apply_migrations(self, cur, table):
        cur.execute("""CREATE TABLE IF NOT EXISTS daq_schema_migrations (
            version TEXT NOT NULL,
            table_name TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL,
            description TEXT NOT NULL,
            PRIMARY KEY (version, table_name)
        )""")
        migration_version = "0001_retire_calibration_revision"
        pure_table = table.split(".")[-1].lower()
        cur.execute(
            "SELECT 1 FROM daq_schema_migrations WHERE version = %s AND table_name = %s",
            (migration_version, pure_table)
        )
        if not cur.fetchone():
            cur.execute("""
                SELECT 1 FROM information_schema.columns
                WHERE lower(table_name) = %s AND lower(column_name) = 'calibration_revision'
            """, (pure_table,))
            if cur.fetchone():
                cur.execute(sql.SQL("ALTER TABLE {} DROP COLUMN calibration_revision").format(sql.Identifier(table)))
            cur.execute(
                "INSERT INTO daq_schema_migrations (version, table_name, applied_at, description) VALUES (%s, %s, %s, %s)",
                (migration_version, pure_table, datetime.now(timezone.utc),
                 "Retire calibration_revision column from production samples")
            )

    def ensure_schema(self):
        table = self.cfg.DB_PRODUCTION_TABLE
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                cur.execute(sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                    time TIMESTAMPTZ NOT NULL, sample_id TEXT NOT NULL,
                    session_id UUID NOT NULL, device_id TEXT NOT NULL,
                    channel SMALLINT NOT NULL, sensor_name TEXT NOT NULL,
                    raw_voltage DOUBLE PRECISION NOT NULL,
                    calibrated_value DOUBLE PRECISION NOT NULL,
                    unit TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    PRIMARY KEY (time, sample_id)
                )""").format(sql.Identifier(table)))
                self._apply_migrations(cur, table)
                cur.execute("SELECT create_hypertable(%s, 'time', if_not_exists => TRUE, chunk_time_interval => INTERVAL '1 hour')", (table,))
                cur.execute(sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} (device_id, channel, time DESC)").format(
                    sql.Identifier("idx_" + table + "_device_channel_time"), sql.Identifier(table)))
                cur.execute("""CREATE TABLE IF NOT EXISTS daq_production_gaps (
                    gap_id UUID PRIMARY KEY, start_time TIMESTAMPTZ NOT NULL,
                    end_time TIMESTAMPTZ, cause TEXT NOT NULL
                )""")
                cur.execute("ALTER TABLE daq_production_gaps ALTER COLUMN end_time DROP NOT NULL")
                cur.execute("SELECT job_id, config->>'drop_after' FROM timescaledb_information.jobs WHERE hypertable_name=%s AND proc_name='policy_retention'", (table,))
                jobs = cur.fetchall()
                desired = f"{self.cfg.DB_RETENTION_DAYS} days"
                if not jobs or any(interval != desired for _, interval in jobs):
                    cur.execute("SELECT remove_retention_policy(%s, if_exists => TRUE)", (table,))
                    cur.execute("SELECT add_retention_policy(%s, %s::interval)", (table, desired))
                cur.execute(sql.SQL("ALTER TABLE {} SET (timescaledb.enable_columnstore=true, timescaledb.segmentby='channel')").format(
                    sql.Identifier(table)))
                cur.execute("CALL add_columnstore_policy(%s, after => INTERVAL '1 day', if_not_exists => TRUE)",
                            (table,))
        self._initialized = True

    def write(self, rows, gaps):
        if not self._initialized:
            self.ensure_schema()
        table = self.cfg.DB_PRODUCTION_TABLE
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    if rows:
                        cur.execute(sql.SQL("CREATE TEMP TABLE daq_ingest_stage (LIKE {} INCLUDING DEFAULTS) ON COMMIT DROP").format(
                            sql.Identifier(table)))
                        buffer = io.StringIO()
                        writer = csv.writer(buffer)
                        for row in rows:
                            writer.writerow((
                                datetime.fromtimestamp(row["time_ns"] / 1e9, timezone.utc).isoformat(),
                                row["sample_id"], row["session_id"], row["device_id"],
                                row["channel"], row["sensor_name"], row["raw_voltage"],
                                row["calibrated_value"], row["unit"], row["provenance"],
                            ))
                        buffer.seek(0)
                        cur.copy_expert("""COPY daq_ingest_stage (
                            time,sample_id,session_id,device_id,channel,sensor_name,
                            raw_voltage,calibrated_value,unit,provenance
                        ) FROM STDIN WITH (FORMAT csv)""", buffer)
                        cur.execute(sql.SQL("""INSERT INTO {} (
                            time,sample_id,session_id,device_id,channel,sensor_name,
                            raw_voltage,calibrated_value,unit,provenance
                        ) SELECT time,sample_id,session_id,device_id,channel,sensor_name,
                            raw_voltage,calibrated_value,unit,provenance
                          FROM daq_ingest_stage ON CONFLICT (time,sample_id) DO NOTHING""").format(
                            sql.Identifier(table)))
                    if gaps:
                        execute_values(cur, """INSERT INTO daq_production_gaps
                            (gap_id,start_time,end_time,cause) VALUES %s
                            ON CONFLICT (gap_id) DO UPDATE SET end_time=EXCLUDED.end_time""", [(
                            gap["gap_id"], datetime.fromtimestamp(gap["start_ns"] / 1e9, timezone.utc),
                            datetime.fromtimestamp(gap["end_ns"] / 1e9, timezone.utc) if gap["end_ns"] is not None else None,
                            gap["cause"]
                        ) for gap in gaps])
        except Exception:
            self._initialized = False
            raise


def _lp_escape(value):
    value = str(value)
    if "\n" in value or "\r" in value:
        raise ValueError("InfluxDB tag values cannot contain newline or carriage return")
    return value.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _lp_string(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n") + '"'


class InfluxProductionDestination:
    """InfluxDB 2.x line protocol sink; retries overwrite identical point identities."""

    def __init__(self, cfg, opener=None):
        self.cfg = cfg
        self.opener = opener or urllib.request.urlopen

    @staticmethod
    def points(rows, gaps, measurement):
        output = []
        for row in rows:
            tags = ",".join(f"{key}={_lp_escape(row[key])}" for key in (
                "device_id", "channel", "session_id", "unit", "provenance"))
            fields = {
                "sample_id": _lp_string(row["sample_id"]),
                "sensor_name": _lp_string(row["sensor_name"]),
                "raw_voltage": repr(float(row["raw_voltage"])),
                "calibrated_value": repr(float(row["calibrated_value"])),
            }
            encoded = ",".join(f"{_lp_escape(key)}={value}" for key, value in fields.items())
            output.append(f"{_lp_escape(measurement)},{tags} {encoded} {int(row['time_ns'])}")
        for gap in gaps:
            tags = f"gap_id={_lp_escape(gap['gap_id'])}"
            end = gap["end_ns"]
            fields = f"start_ns={int(gap['start_ns'])}i,open={str(end is None).lower()},cause={_lp_string(gap['cause'])}"
            if end is not None:
                fields += f",end_ns={int(end)}i"
            output.append(f"daq_acquisition_gaps,{tags} {fields} {int(gap['start_ns'])}")
        return "\n".join(output)

    def write(self, rows, gaps):
        payload = self.points(rows, gaps, self.cfg.INFLUX_MEASUREMENT)
        if not payload:
            return
        query = urllib.parse.urlencode({"org": self.cfg.INFLUX_ORG, "bucket": self.cfg.INFLUX_BUCKET, "precision": "ns"})
        url = f"{self.cfg.INFLUX_URL.rstrip('/')}/api/v2/write?{query}"
        headers = {"Authorization": f"Token {self.cfg.INFLUX_TOKEN}", "Content-Type": "text/plain; charset=utf-8",
                   "Accept": "application/json"}
        current, size = [], 0
        for line in payload.splitlines():
            line_size = len(line.encode()) + 1
            if current and size + line_size > 512 * 1024:
                self._post(url, "\n".join(current), headers)
                current, size = [], 0
            current.append(line)
            size += line_size
        if current:
            self._post(url, "\n".join(current), headers)

    def _post(self, url, payload, headers):
        request = urllib.request.Request(url, data=payload.encode(), method="POST", headers=headers)
        try:
            with self.opener(request, timeout=5) as response:
                if getattr(response, "status", 204) not in (204, 200):
                    raise RuntimeError(f"InfluxDB write returned HTTP {response.status}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"InfluxDB write returned HTTP {exc.code}") from exc


class MQTTProductionDestination:
    """Production MQTT destination delivering physical samples and gaps over authenticated TLS."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.broker = str(cfg.MQTT_BROKER)
        self.port = int(cfg.MQTT_PORT)
        self.username = str(cfg.MQTT_USERNAME)
        self.password = str(cfg.MQTT_PASSWORD)
        self.tls_enabled = bool(cfg.MQTT_TLS_ENABLED)
        self.ca_certs = getattr(cfg, "MQTT_CA_CERTS", "") or None
        self.client_cert = getattr(cfg, "MQTT_CLIENT_CERT", "") or None
        self.client_key = getattr(cfg, "MQTT_CLIENT_KEY", "") or None
        self.qos = int(getattr(cfg, "MQTT_PRODUCTION_QOS", 1))
        self.topic_prefix = str(getattr(cfg, "MQTT_PRODUCTION_TOPIC_PREFIX", "daq/production/v1")).rstrip("/")
        self.max_payload_bytes = int(getattr(cfg, "MQTT_PRODUCTION_MAX_PAYLOAD_BYTES", 256 * 1024))

        self._lock = threading.Lock()
        self._pending_mids = {}
        self._acked_mids = set()
        self._client_id = f"daq_prod_{uuid.uuid4().hex[:8]}"

        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(client_id=self._client_id)
        if self.username or self.password:
            self.client.username_pw_set(self.username, self.password)
        if self.tls_enabled:
            tls_kwargs = {}
            if self.ca_certs:
                tls_kwargs["ca_certs"] = self.ca_certs
            if self.client_cert:
                tls_kwargs["certfile"] = self.client_cert
            if self.client_key:
                tls_kwargs["keyfile"] = self.client_key
            self.client.tls_set(**tls_kwargs)

        self.client.on_connect = lambda *args, **kwargs: None
        self.client.on_publish = self._on_publish
        self.client.connect(self.broker, self.port)
        self.client.loop_start()

    def _on_publish(self, client, userdata, mid, *args, **kwargs):
        with self._lock:
            self._acked_mids.add(mid)
            event = self._pending_mids.get(mid)
            if event is not None:
                event.set()

    def ensure_schema(self):
        pass

    def write(self, rows: list[dict], gaps: list[dict]):
        if not rows and not gaps:
            return

        events_to_wait = []
        safe_device = urllib.parse.quote(str(self.cfg.DEVICE_ID), safe="")
        sample_topic = f"{self.topic_prefix}/{safe_device}/samples"
        gap_topic = f"{self.topic_prefix}/{safe_device}/gaps"

        # 1. Publish sample chunks if any
        if rows:
            batch_id = hashlib.sha256(
                f"{rows[0].get('sample_id')}:{rows[-1].get('sample_id')}:{rows[0].get('time_ns')}:{len(rows)}".encode()
            ).hexdigest()[:16]

            chunks = []
            curr_chunk = []
            base_envelope = {
                "schema_version": 1,
                "device_id": self.cfg.DEVICE_ID,
                "batch_id": batch_id,
            }

            for row in rows:
                if curr_chunk:
                    test_chunk = curr_chunk + [row]
                    test_chunk_id = f"{batch_id}:c{len(chunks)}"
                    envelope = dict(base_envelope, chunk_id=test_chunk_id, samples=test_chunk)
                    payload_len = len(json.dumps(
                        envelope, separators=(',', ':'), allow_nan=False).encode("utf-8"))
                    if payload_len <= self.max_payload_bytes:
                        curr_chunk.append(row)
                        continue
                    chunks.append(curr_chunk)
                    curr_chunk = []

                chunk_id = f"{batch_id}:c{len(chunks)}"
                envelope = dict(base_envelope, chunk_id=chunk_id, samples=[row])
                payload_len = len(json.dumps(
                    envelope, separators=(',', ':'), allow_nan=False).encode("utf-8"))
                if payload_len > self.max_payload_bytes:
                    raise ValueError(
                        "MQTT sample envelope exceeds maximum payload size "
                        f"(sample_id={row.get('sample_id')}, actual_bytes={payload_len}, "
                        f"max_bytes={self.max_payload_bytes})")
                curr_chunk.append(row)
            if curr_chunk:
                chunks.append(curr_chunk)

            for idx, chunk in enumerate(chunks):
                chunk_id = f"{batch_id}:c{idx}"
                envelope = dict(base_envelope, chunk_id=chunk_id, samples=chunk)
                payload_str = json.dumps(envelope, separators=(',', ':'), allow_nan=False)
                res = self.client.publish(sample_topic, payload_str, qos=self.qos, retain=False)
                if getattr(res, "rc", 0) != 0:
                    raise RuntimeError(f"MQTT publish rejected with code {res.rc}")
                mid = res.mid
                event = threading.Event()
                with self._lock:
                    if mid in self._acked_mids:
                        event.set()
                    else:
                        self._pending_mids[mid] = event
                events_to_wait.append((mid, event))

        # 2. Publish gaps if any
        for gap in gaps:
            payload = {
                "schema_version": 1,
                "device_id": self.cfg.DEVICE_ID,
                "gap_id": gap["gap_id"],
                "revision": gap.get("revision", 1),
                "start_ns": gap["start_ns"],
                "end_ns": gap.get("end_ns"),
                "cause": gap["cause"],
            }
            payload_str = json.dumps(payload, separators=(',', ':'), allow_nan=False)
            res = self.client.publish(gap_topic, payload_str, qos=self.qos, retain=False)
            if getattr(res, "rc", 0) != 0:
                raise RuntimeError(f"MQTT publish rejected with code {res.rc}")
            mid = res.mid
            event = threading.Event()
            with self._lock:
                if mid in self._acked_mids:
                    event.set()
                else:
                    self._pending_mids[mid] = event
            events_to_wait.append((mid, event))

        # 3. Wait for all messages to complete
        deadline = time.monotonic() + 10.0
        for mid, event in events_to_wait:
            remaining = max(0.01, deadline - time.monotonic())
            if not event.wait(remaining):
                raise TimeoutError(f"MQTT publish timed out waiting for mid {mid}")

        with self._lock:
            for mid, _ in events_to_wait:
                self._pending_mids.pop(mid, None)
                self._acked_mids.discard(mid)

    def close(self):
        try:
            self.client.loop_stop()
        except Exception:
            pass
        try:
            self.client.disconnect()
        except Exception:
            pass


class AdvantechDaq:
    """Small hardware adapter. getDataF64 count is interleaved scalar values."""

    def __init__(self, cfg):
        from Automation.BDaq.WaveformAiCtrl import WaveformAiCtrl
        from Automation.BDaq.BDaqApi import BioFailed
        self._bio_failed = BioFailed
        self.cfg = cfg
        self.device = WaveformAiCtrl(cfg.DEVICE_DESCRIPTION)
        if cfg.PROFILE_PATH:
            self.device.loadProfile = cfg.PROFILE_PATH
        self.device.conversion.channelStart = cfg.START_CHANNEL
        self.device.conversion.channelCount = cfg.CHANNEL_COUNT
        self.device.conversion.clockRate = cfg.CLOCK_RATE
        self.device.record.sectionCount = cfg.SECTION_COUNT
        self.device.record.sectionLength = cfg.SECTION_LENGTH
        for index in range(cfg.START_CHANNEL, cfg.START_CHANNEL + cfg.CHANNEL_COUNT):
            ch = cfg.channels[index]
            if (re.match(r'^PCI-1716(?:H|L)?(?:,|$)', cfg.DEVICE_DESCRIPTION, re.IGNORECASE)
                    and index % 2 and not ch.enabled
                    and cfg.channels[index - 1].signal_type_str == 'Differential'):
                # The card configures the odd input as the differential negative leg.
                continue
            self.device.channels[index].signalType = ch.signal_type
            self.device.channels[index].valueRange = ch.value_range
        if BioFailed(self.device.prepare()):
            self.close()
            raise AcquisitionFault("daq_start_failed")
        before_start_ns = time.time_ns()
        start_result = self.device.start()
        after_start_ns = time.time_ns()
        if BioFailed(start_result):
            self.close()
            raise AcquisitionFault("daq_start_failed")
        self.start_time_ns = (before_start_ns + after_start_ns) // 2
        self.empty_reads = 0
        self.last_read_timing_reliable = False

    def read(self, count):
        # The SDK accepts milliseconds. A finite timeout lets SIGTERM finish
        # even if the card stops producing samples; timeout may return data.
        before_read_ns = time.monotonic_ns()
        result, returned, data = self.device.getDataF64(count, 1000)[:3]
        read_duration_ns = time.monotonic_ns() - before_read_ns
        end_ns = time.time_ns()
        if self._bio_failed(result):
            raise AcquisitionFault("daq_read_failed")
        status = getattr(result, "name", str(result))
        if status not in ("Success", "WarningFuncTimeout"):
            raise AcquisitionFault("daq_cache_overflow" if status == "WarningCacheOverflow"
                                   else f"daq_warning_{status}")
        if returned < 0 or returned > count:
            raise AcquisitionFault("invalid_daq_count")
        if returned % self.cfg.CHANNEL_COUNT:
            raise AcquisitionFault("unaligned_daq_batch")
        expected_read_ns = returned // self.cfg.CHANNEL_COUNT * 1_000_000_000 // self.cfg.CLOCK_RATE
        self.last_read_timing_reliable = (
            status == "Success" and returned == count
            and read_duration_ns >= expected_read_ns // 2
        )
        self.empty_reads = 0 if returned else self.empty_reads + 1
        # getDataF64 can return an empty timeout while the SDK is filling a
        # section. Allow its expected collection time plus two polling cycles.
        max_empty_reads = max(3, math.ceil(self.cfg.SECTION_LENGTH / self.cfg.CLOCK_RATE) + 2)
        if self.empty_reads >= max_empty_reads:
            raise AcquisitionFault("daq_stalled")
        return list(data[:returned]), end_ns

    def close(self):
        if getattr(self, "device", None):
            try:
                self.device.stop()
            finally:
                self.device.dispose()


def run_production(cfg, stop_event=None, daq_factory=AdvantechDaq, destination=None, spool_dir=None):
    """Run until stop or fault; recover committed batches on every start."""
    validate_production_config(cfg)
    stop_event = stop_event or threading.Event()
    guard_spool = DurableSpool(Path(spool_dir or cfg.SPOOL_DIR), cfg.SPOOL_MAX_BYTES)
    try:
        prior_destination = guard_spool.state_value("destination")
        prior_identity = guard_spool.state_value("destination_identity")
        current_identity = destination_identity(cfg)
        if prior_destination and (prior_destination != cfg.DESTINATION or (prior_identity and prior_identity != current_identity)):
            pending = guard_spool.pending_batches + len(guard_spool.pending_gaps())
            guard_spool.record_cutover(prior_destination, cfg.DESTINATION, pending)
        guard_spool.set_state("destination", cfg.DESTINATION)
        guard_spool.set_state("destination_identity", current_identity)
    finally:
        guard_spool.close()
    if destination is None:
        if cfg.DESTINATION == "mqtt":
            destination = MQTTProductionDestination(cfg)
        elif cfg.DESTINATION == "influxdb":
            destination = InfluxProductionDestination(cfg)
        else:
            destination = TimescaleProductionDestination(cfg)
    pipeline = ProductionPipeline(cfg, Path(spool_dir or cfg.SPOOL_DIR), destination)
    writer_stop = threading.Event()
    writer_deadline = [None]
    writer_error = deque(maxlen=10)
    section_rows = cfg.SECTION_LENGTH * len(pipeline.enabled)
    writer_batch_group = max(1, min(_WRITER_BATCH_GROUP, _WRITER_MAX_ROWS // section_rows))

    def writer():
        while not writer_stop.is_set() or (
            (pipeline.pending_batches or pipeline.spool.pending_gaps())
            and time.monotonic() < writer_deadline[0]
        ):
            try:
                if not pipeline.flush_batches(writer_batch_group):
                    writer_stop.wait(0.1)
            except Exception as exc:
                writer_error.append(str(exc))
                pipeline.last_writer_error = str(exc)
                pipeline.publish_status()
                if writer_stop.is_set():
                    break
                writer_stop.wait(0.5)

    thread = threading.Thread(target=writer, name="ProductionWriter", daemon=True)
    thread.start()
    device = None
    fault = None
    try:
        device = daq_factory(cfg)
        while not stop_event.is_set():
            raw, end_ns = device.read(cfg.USER_BUFFER_SIZE)
            if raw:
                pipeline.capture(raw, end_ns,
                                 hardware_start_ns=getattr(device, "start_time_ns", None)
                                 if pipeline.frame_index == 0 else None,
                                 timing_reliable=getattr(device, "last_read_timing_reliable", False))
    except Exception as exc:
        fault = str(exc)
        pipeline.last_fault = fault
        pipeline.state = "failed"
        pipeline.open_gap(fault)
        pipeline.publish_status()
        log.exception("Production DAQ stopped: %s", fault)
    finally:
        if device is not None:
            try:
                device.close()
            except Exception as exc:
                log.exception("DAQ release failed")
                fault = fault or f"daq_release_failed: {exc}"
        writer_deadline[0] = time.monotonic() + 30
        writer_stop.set()
        thread.join(timeout=35)
        if thread.is_alive():
            log.warning("Writer timed out; committed batches remain in spool")
        pending = pipeline.pending_batches
        if fault is None:
            pipeline.open_gap("requested_stop")
        log.info("Production stopped; pending_batches=%s spool_bytes=%s", pending, pipeline.usage_bytes)
        if not thread.is_alive():
            pipeline.close()
    if fault:
        raise AcquisitionFault(fault)
    return {"pending_batches": pending, "writer_errors": list(writer_error)}
