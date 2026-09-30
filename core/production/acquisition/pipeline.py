"""Turn DAQ frames into durable samples and replay them to a destination."""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from collections import defaultdict, deque
from ..config import validate_production_config
from ..fault import AcquisitionFault
from ..spool import DurableSpool
from .counter import CycleCounter

class ProductionPipeline:
    """Transforms physical DAQ batches and commits them before remote transfer."""

    PREVIEW_SAMPLES_PER_CHANNEL = 100
    PREVIEW_HISTORY_SECONDS = 300
    PREVIEW_PUBLISH_INTERVAL_NS = 1_000_000_000

    def __init__(self, cfg, spool_dir: Path, destination, session_id=None):
        self.enabled = validate_production_config(cfg)
        self.cfg = cfg
        self.spool = DurableSpool(spool_dir, cfg.SPOOL_MAX_BYTES)
        self.destination = destination
        self.session_id = session_id or str(uuid.uuid4())
        self.counter = CycleCounter(cfg, self.session_id)
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
        self._preview_samples = defaultdict(lambda: deque(maxlen=self.PREVIEW_SAMPLES_PER_CHANNEL))
        self._preview_history = defaultdict(lambda: deque(maxlen=self.PREVIEW_HISTORY_SECONDS))
        self._preview_buckets = {}
        self._preview_published_ns = 0
        self._load_preview_history()
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
        self._publish_preview(force=True)
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

    def _publish_preview(self, rows=None, force=False):
        """Atomically publish a small, recent view of committed samples for the operator UI."""
        now_ns = time.time_ns()
        if rows:
            for row in rows:
                sample = {key: row[key] for key in (
                    "time_ns", "sample_id", "channel", "sensor_name",
                    "raw_voltage", "calibrated_value", "unit",
                )}
                channel = int(row["channel"])
                self._preview_samples[channel].append(sample)
                self._accumulate_preview(channel, row)
        if not force and now_ns - self._preview_published_ns < self.PREVIEW_PUBLISH_INTERVAL_NS:
            return
        history = sorted(
            (item for channel_history in self._preview_history.values() for item in channel_history),
            key=lambda item: item["time_ns"],
        )
        preview = {
            "session_id": self.session_id,
            "destination": self.cfg.DESTINATION,
            "checked_at_ns": now_ns,
            "samples": sorted(
                (sample for channel_samples in self._preview_samples.values()
                 for sample in channel_samples),
                key=lambda sample: sample["time_ns"], reverse=True,
            ),
            "history": history,
        }
        target = self.spool.directory / "preview.json"
        temporary = target.with_suffix(".json.tmp")
        try:
            with open(temporary, "w", encoding="utf-8") as output:
                json.dump(preview, output, separators=(",", ":"), allow_nan=False)
                output.flush()
            os.replace(temporary, target)
            self._preview_published_ns = now_ns
        except OSError:
            log.exception("Could not publish acquisition preview")

    def _load_preview_history(self):
        """Retain the last five minutes of compact history across browser and process restarts."""
        path = self.spool.directory / "preview.json"
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        cutoff_ns = time.time_ns() - self.PREVIEW_HISTORY_SECONDS * 1_000_000_000
        records = [item for item in existing.get("history", [])
                   if int(item.get("time_ns", 0)) >= cutoff_ns]
        for item in sorted(records, key=lambda record: record.get("time_ns", 0)):
            self._preview_history[int(item["channel"])].append(item)

    def _accumulate_preview(self, channel, row):
        bucket_ns = int(row["time_ns"]) // 1_000_000_000 * 1_000_000_000
        current = self._preview_buckets.get(channel)
        if current and current["bucket_start_ns"] != bucket_ns:
            self._finish_preview_bucket(channel, current)
            current = None
        if current is None:
            current = {
                "bucket_start_ns": bucket_ns,
                "time_ns": bucket_ns + 500_000_000,
                "channel": channel,
                "sensor_name": row["sensor_name"],
                "unit": row["unit"],
                "sample_id": row["sample_id"],
                "last_sample_ns": int(row["time_ns"]),
                "sample_count": 0,
            }
            for name in ("raw_voltage", "calibrated_value"):
                value = float(row[name])
                current[f"{name}_min"] = value
                current[f"{name}_max"] = value
                current[f"{name}_sum"] = 0.0
            self._preview_buckets[channel] = current
        current["sample_count"] += 1
        current["sensor_name"] = row["sensor_name"]
        current["unit"] = row["unit"]
        if int(row["time_ns"]) >= current["last_sample_ns"]:
            current["last_sample_ns"] = int(row["time_ns"])
            current["sample_id"] = row["sample_id"]
        for name in ("raw_voltage", "calibrated_value"):
            value = float(row[name])
            current[f"{name}_min"] = min(current[f"{name}_min"], value)
            current[f"{name}_max"] = max(current[f"{name}_max"], value)
            current[f"{name}_sum"] += value

    def _finish_preview_bucket(self, channel, bucket):
        item = {key: value for key, value in bucket.items() if not key.endswith("_sum") and key != "last_sample_ns"}
        for name in ("raw_voltage", "calibrated_value"):
            item[f"{name}_avg"] = bucket[f"{name}_sum"] / bucket["sample_count"]
        history = self._preview_history[channel]
        if history and history[-1]["bucket_start_ns"] == bucket["bucket_start_ns"]:
            previous = history.pop()
            combined_count = previous["sample_count"] + item["sample_count"]
            for name in ("raw_voltage", "calibrated_value"):
                item[f"{name}_min"] = min(previous[f"{name}_min"], item[f"{name}_min"])
                item[f"{name}_max"] = max(previous[f"{name}_max"], item[f"{name}_max"])
                item[f"{name}_avg"] = (
                    previous[f"{name}_avg"] * previous["sample_count"]
                    + item[f"{name}_avg"] * item["sample_count"]
                ) / combined_count
            item["sample_count"] = combined_count
        history.append(item)

    def _finish_preview_buckets(self):
        for channel, bucket in list(self._preview_buckets.items()):
            self._finish_preview_bucket(channel, bucket)
        self._preview_buckets.clear()

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
                count_rows = self.counter.add_samples(rows)
                self.spool.append(f"{self.session_id}:{self.frame_index}", rows + count_rows,
                                  first_sample_ns=rows[0]["time_ns"],
                                  last_sample_ns=rows[-1]["time_ns"])
            except AcquisitionFault as exc:
                self.fault(str(exc), end_time_ns)
            self.frame_index += frames
            self.phase_adjust_ns += phase_change_ns
            self.last_sample_ns = rows[-1]["time_ns"]
            self.state = "running"
            self._publish_preview(rows)
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
        self._finish_preview_buckets()
        self._publish_preview(force=True)
        if self.state != "failed":
            self.state = "stopped"
        self.publish_status()
        self.spool.set_state("acquisition_active", "0")
        self.spool.close()
