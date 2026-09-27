#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# See: docs/architecture/context.md

"""
mockup_stream_to_db.py
──────────────────────
Mock-up streaming pipeline for DAQNavi Universal Service.
Generates synthetic sensor data (sinusoid/noise) and writes to TimescaleDB
table (default 'daq_telemetry') with device_id, channel, and calibrated values.
"""

import sys
import os
import time
import math
import random
import signal
import threading
import logging
import queue
import csv
from datetime import datetime, timezone

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.dirname(CORE_DIR)
for p in (CORE_DIR, SERVICE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from .config_loader import load_daq_config
    from .buffered_daq_to_timescaledb import (
        Calibrator,
        DaqSampleParser,
        ensure_db_and_tables,
        create_destination_client,
        check_pipeline_watchdog
    )
except ImportError:
    from config_loader import load_daq_config
    from buffered_daq_to_timescaledb import (
        Calibrator,
        DaqSampleParser,
        ensure_db_and_tables,
        create_destination_client,
        check_pipeline_watchdog
    )

config = load_daq_config(os.getenv('DAQ_CONFIG_PATH'))
config.DB_TABLE = config.DB_MOCKUP_TABLE

# ─── Mock-up Tuning ──────────────────────────────────────────────────────────
# Waveform parameters per channel offset: (amplitude_V, frequency_Hz, dc_offset_V)
# Simulates DP-101A pressure sensors (typically 1V - 5V)
MOCKUP_CHANNEL_WAVEFORMS = [
    (1.5,  2.0,   3.0),   # ch0: 1.5 V amp,  2 Hz, centered at 3.0 V (range 1.5V - 4.5V)
    (1.2,  3.5,   3.0),   # ch1: 1.2 V amp, 3.5 Hz, centered at 3.0 V (range 1.8V - 4.2V)
    (1.0,  5.0,   2.5),   # ch2
    (1.8,  1.0,   2.5),   # ch3
    (0.8, 10.0,   2.5),   # ch4
    (0.5, 20.0,   2.5),   # ch5
    (1.0,  0.5,   2.5),   # ch6
    (1.0,  0.2,   2.5),   # ch7
]
MOCKUP_NOISE_STD_V  = 0.015   # Gaussian noise standard deviation (volts)
MOCKUP_CSV_PATH     = None
MOCKUP_PRINT_ROWS   = False
MOCKUP_SUMMARY_ROWS = 4

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)-12s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# ─── Shared state ─────────────────────────────────────────────────────────────
data_queue = queue.Queue(maxsize=config.QUEUE_MAXSIZE)
stop_event = threading.Event()

stats_lock = threading.Lock()
stats = {
    "polled":    0,   # total interleaved samples generated
    "enqueued":  0,   # total batches enqueued
    "written":   0,   # total rows written to DB
    "dropped":   0,   # batches dropped due to full queue
    "db_errors": 0,   # number of DB insert failures
    "last_polled_time": time.time(),
    "last_written_time": time.time(),
}

_recent_rows_lock = threading.Lock()
_recent_rows: list = []


# ─── Mock DAQ Generator Thread ───────────────────────────────────────────────
def mock_daq_reader_thread():
    """
    Simulates hardware acquisition without real DAQ card.
    Generates interleaved samples for configured active channels.
    """
    try:
        n_ch    = config.CHANNEL_COUNT
        sec_len = config.SECTION_LENGTH
        buf_sz  = config.USER_BUFFER_SIZE
        dt_s    = 1.0 / config.CLOCK_RATE

        waveforms = (MOCKUP_CHANNEL_WAVEFORMS * n_ch)[:n_ch]
        sample_counter = 0

        log.info(
            f"[MockDAQ] Started | device_id={config.DEVICE_ID} | channels={n_ch} "
            f"| clock={config.CLOCK_RATE} Hz | sectionLength={sec_len} | userBuffer={buf_sz}"
        )
        for i in range(n_ch):
            ch_idx = config.START_CHANNEL + i
            amp, freq, dc = waveforms[i]
            ch_cfg = config.channels.get(ch_idx)
            label = ch_cfg.label if ch_cfg else f"ch{ch_idx}"
            log.info(f"  ch{ch_idx} ({label}): {amp:.2f}V × sin(2π×{freq:.1f}Hz×t) + {dc:.2f}V")

        while not stop_event.is_set():
            # Simulate real hardware acquisition interval
            time.sleep(sec_len / config.CLOCK_RATE)

            if stop_event.is_set():
                break

            raw_data = []
            for s in range(sec_len):
                t = (sample_counter + s) * dt_s
                for ch_idx in range(n_ch):
                    amp, freq, dc = waveforms[ch_idx]
                    value = amp * math.sin(2 * math.pi * freq * t) + dc
                    value += random.gauss(0.0, MOCKUP_NOISE_STD_V)
                    # Clamp within 0-5V or configured range
                    value = max(0.0, min(5.0, value))
                    raw_data.append(value)

            sample_counter += sec_len
            returned_count = len(raw_data)
            batch_wall_ts_ns = time.time_ns()

            try:
                data_queue.put_nowait((batch_wall_ts_ns, raw_data, returned_count))
                with stats_lock:
                    stats["polled"] += returned_count
                    stats["enqueued"] += 1
                    stats["last_polled_time"] = time.time()
            except queue.Full:
                with stats_lock:
                    stats["dropped"] += 1
                log.warning(f"[MockDAQ] Queue full! Dropped 1 batch ({returned_count} samples).")

    except Exception as e:
        log.exception(f"Unhandled exception in Mock DAQ thread: {e}")
        stop_event.set()
    finally:
        log.info("[MockDAQ] Mock DAQ thread stopped.")


# ─── Data Writer Thread ───────────────────────────────────────────────────────
def db_writer_thread():
    """
    Dequeues raw batches, parses into (time, device_id, channel, value),
    calibrates, and writes to TimescaleDB or chosen destination.
    """
    calibrator = Calibrator(
        start_channel=config.START_CHANNEL,
        channel_count=config.CHANNEL_COUNT,
        channel_configs=config.channels
    )

    parser = DaqSampleParser(
        start_channel=config.START_CHANNEL,
        channel_count=config.CHANNEL_COUNT,
        clock_rate=config.CLOCK_RATE,
        calibrator=calibrator,
        device_id=config.DEVICE_ID,
        recalibrate_interval_hr=getattr(config, 'ANCHOR_RECALIBRATE_INTERVAL_HR', 24.0)
    )

    destination = getattr(config, 'DESTINATION', 'postgresql').lower()
    client = create_destination_client(config, stop_event=stop_event)

    if not client.connect():
        log.info(f"[MockWriter] Writer exiting (connection to {destination} failed).")
        return

    csv_file = None
    csv_writer = None
    if MOCKUP_CSV_PATH:
        try:
            csv_file = open(MOCKUP_CSV_PATH, "w", newline="")
            csv_writer = csv.writer(csv_file)
            csv_writer.writerow(["time_utc", "device_id", "channel", "value"])
            log.info(f"[MockWriter] CSV output enabled → {MOCKUP_CSV_PATH}")
        except Exception as e:
            log.error(f"[MockWriter] Failed to open CSV: {e}")

    try:
        while not stop_event.is_set() or not data_queue.empty():
            try:
                batch_wall_ts_ns, raw_data, returned_count = data_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            # 1. Parse raw data into sample rows (time, device_id, channel, value)
            rows = parser.parse_batch(batch_wall_ts_ns, raw_data, returned_count)

            # 2. Write rows to TimescaleDB or chosen client
            try:
                client.send_samples(rows, page_size=config.DB_PAGE_SIZE)
                with stats_lock:
                    stats["written"] += len(rows)
                    stats["last_written_time"] = time.time()

                if csv_writer is not None:
                    for ts, dev, ch, val in rows:
                        csv_writer.writerow([ts.isoformat(), dev, ch, f"{val:.4f}"])
                    csv_file.flush()

                with _recent_rows_lock:
                    _recent_rows.clear()
                    _recent_rows.extend(rows[-MOCKUP_SUMMARY_ROWS:])

            except Exception as e:
                with stats_lock:
                    stats["db_errors"] += 1
                log.error(f"[MockWriter] Output error ({destination}): {e} — attempting recovery...")
                try:
                    client.rollback()
                except Exception as rb_err:
                    log.error(f"[MockWriter] Rollback error: {rb_err}")
                    client.disconnect()

                try:
                    data_queue.put_nowait((batch_wall_ts_ns, raw_data, returned_count))
                except queue.Full:
                    with stats_lock:
                        stats["dropped"] += 1

                if not getattr(client, 'is_connected', False):
                    log.info(f"[MockWriter] Reconnecting to {destination}...")
                    if not client.connect():
                        log.error(f"[MockWriter] Reconnection failed. Stopping writer.")
                        return

                time.sleep(1.0)

    finally:
        client.disconnect()
        if csv_file:
            try:
                csv_file.close()
            except Exception:
                pass
        log.info(f"[MockWriter] Writer flushed and stopped.")


# ─── Monitor Thread ───────────────────────────────────────────────────────────
def monitor_thread():
    """Logs pipeline statistics, verifies health, and touches heartbeat file."""
    dest = getattr(config, 'DESTINATION', 'postgresql').lower()
    watchdog_timeout = getattr(config, 'WATCHDOG_TIMEOUT_SEC', 30)
    heartbeat_path = getattr(config, 'HEARTBEAT_FILE', '/tmp/daq_navi_heartbeat')

    while not stop_event.is_set():
        time.sleep(config.STATS_INTERVAL_SEC)
        with stats_lock:
            s = dict(stats)
        q_len = data_queue.qsize()
        loss_pct = (s["dropped"] / s["enqueued"] * 100) if s["enqueued"] > 0 else 0.0

        log.info(
            f"[STATS] [{config.DEVICE_ID} -> {config.DB_TABLE}] "
            f"polled={s['polled']:,} | written={s['written']:,} | "
            f"dropped={s['dropped']} ({loss_pct:.1f}%) | "
            f"errors={s['db_errors']} | queue={q_len}/{config.QUEUE_MAXSIZE}"
        )

        with _recent_rows_lock:
            snap = list(_recent_rows)
        if snap:
            log.info(f"[STATS] Sample rows [{dest.upper()}]:")
            for ts, dev, ch, val in snap:
                log.info(f"  {ts.strftime('%H:%M:%S.%f')[:-3]} [{dev}] ch{ch} = {val:+.3f}")

        is_healthy, warning = check_pipeline_watchdog(s, q_len, watchdog_timeout)
        if not is_healthy:
            log.warning(f"⚠ [WATCHDOG] {warning}")
        else:
            try:
                with open(heartbeat_path, "w") as f:
                    f.write(f"{time.time():.2f}\n")
            except Exception as hb_err:
                log.debug(f"Heartbeat write warning: {hb_err}")


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    def handle_signal(sig, frame):
        log.info(f"Signal {sig} received — shutting down gracefully...")
        stop_event.set()

    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, handle_signal)

    dest = getattr(config, 'DESTINATION', 'postgresql').lower()
    log.info("=" * 60)
    log.info(f"DAQNavi Universal MOCKUP Starting [Destination: {dest.upper()}]")
    log.info(f"  Device ID   : {config.DEVICE_ID}")
    log.info(f"  Channels    : {config.CHANNEL_COUNT} (ch{config.START_CHANNEL}–ch{config.START_CHANNEL + config.CHANNEL_COUNT - 1})")
    log.info(f"  Clock rate  : {config.CLOCK_RATE} Hz")
    log.info(f"  Section Len : {config.SECTION_LENGTH} samples/ch")
    log.info(f"  Batch size  : {config.USER_BUFFER_SIZE} interleaved samples")
    log.info(f"  Target Table: {config.DB_TABLE}")
    if dest in ('influxdb', 'influx'):
        log.info(f"  Influx URL  : {getattr(config, 'INFLUX_URL', 'http://localhost:8086')}")
        log.info(f"  Influx Org  : {getattr(config, 'INFLUX_ORG', 'mddp')}")
        log.info(f"  Influx Bucket: {getattr(config, 'INFLUX_BUCKET', 'daq_telemetry')}")
    else:
        log.info(f"  DB DSN      : {config.DB_DSN}")
    log.info("=" * 60)

    # Auto-bootstrap DB table if using PostgreSQL/TimescaleDB
    if dest in ('database', 'postgresql'):
        try:
            ensure_db_and_tables(
                config.DB_DSN,
                config.DB_TABLE,
                retention_days=getattr(config, 'DB_RETENTION_DAYS', 90),
                compression_interval=getattr(config, 'DB_COMPRESSION_INTERVAL', '1 hour')
            )
        except Exception as e:
            log.warning(f"Initial DB check warning ({e}) — writer will retry on connect.")

    daq_thread = threading.Thread(target=mock_daq_reader_thread, name="MockDAQ", daemon=True)
    db_thread  = threading.Thread(target=db_writer_thread, name="MockWriter", daemon=False)
    mon_thread = threading.Thread(target=monitor_thread, name="Monitor", daemon=True)

    daq_thread.start()
    db_thread.start()
    mon_thread.start()

    stop_event.wait()

    log.info("Flushing queue...")
    db_thread.join(timeout=30)

    with stats_lock:
        s = dict(stats)
    log.info("=" * 60)
    log.info("Mockup pipeline stopped.")
    log.info(f"  Device ID  : {config.DEVICE_ID}")
    log.info(f"  Total polled: {s['polled']:,}")
    log.info(f"  Total wrote : {s['written']:,}")
    log.info(f"  Dropped     : {s['dropped']}")
    log.info(f"  Errors      : {s['db_errors']}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
