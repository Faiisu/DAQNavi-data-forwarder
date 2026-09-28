"""SQLite journal for committed batches, gaps, and destination cutovers."""

from __future__ import annotations

import fcntl
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid
import zlib
from .fault import AcquisitionFault

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
