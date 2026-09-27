#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# See: docs/architecture/context.md

"""Run physical DAQ acquisition with a durable buffer and TimescaleDB output.

The command-line entry point delegates to production_acquisition.run_production.
That path commits batches to a persistent SQLite spool before database delivery,
so it can replay pending batches after an outage or restart.

Legacy in-memory pipeline helpers remain here for the mockup acquisition path
and existing imports. They are not used by the production entry point.
"""

import sys
import os
import time
import signal
import threading
import logging
import queue
from datetime import datetime, timezone
import json
import urllib.request
import urllib.parse

import psycopg2
import psycopg2.extras

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.dirname(CORE_DIR)
for p in (CORE_DIR, SERVICE_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    from Automation.BDaq import *
    from Automation.BDaq.WaveformAiCtrl import WaveformAiCtrl
    from Automation.BDaq.BDaqApi import AdxEnumToString, BioFailed
except (ImportError, OSError) as bdaq_err:
    WaveformAiCtrl = None
    def BioFailed(ret):
        return False
    def AdxEnumToString(*args):
        return "BDaq_NOT_LOADED"

try:
    from .config_loader import load_daq_config
except ImportError:
    from config_loader import load_daq_config
config = load_daq_config(os.path.join(SERVICE_DIR, 'config.json'))

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)-12s] %(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# ─── Shared state ─────────────────────────────────────────────────────────────
# Each item in queue: (batch_wall_ts_ns: int, raw_data: list[float], returned_count: int)
#   batch_wall_ts_ns = time.time_ns() captured right after getDataF64() returns
#   Per-sample ts is forward-computed in DB writer relative to periodic anchor:
#       sample_ts = anchor_time_ns + (samples_since_anchor + s) * dt_ns
data_queue = queue.Queue(maxsize=config.QUEUE_MAXSIZE)
stop_event = threading.Event()
daq_error_occurred = threading.Event()

stats_lock = threading.Lock()
stats = {
    "polled":   0,   # total interleaved samples polled from DAQ
    "enqueued": 0,   # total batches enqueued
    "written":  0,   # total rows written to DB
    "dropped":  0,   # batches dropped due to full queue
    "db_errors": 0,  # number of DB insert failures
    "last_polled_time": time.time(),
    "last_written_time": time.time(),
}


# ─── DAQ Reader Thread ────────────────────────────────────────────────────────
def daq_reader_thread():
    """
    Responsibility: poll hardware as fast as possible, enqueue raw data.
    """
    try:
        if WaveformAiCtrl is None:
            log.error("Advantech BDaq library is not available in this environment.")
            daq_error_occurred.set()
            stop_event.set()
            return

        wf = WaveformAiCtrl(config.DEVICE_DESCRIPTION)
        if config.PROFILE_PATH and os.path.exists(config.PROFILE_PATH):
            wf.loadProfile = config.PROFILE_PATH

        wf.conversion.channelStart = config.START_CHANNEL
        wf.conversion.channelCount = config.CHANNEL_COUNT
        wf.conversion.clockRate    = config.CLOCK_RATE
        wf.record.sectionCount     = config.SECTION_COUNT
        wf.record.sectionLength    = config.SECTION_LENGTH

        log.info(f"[DAQ] Configuring {config.CHANNEL_COUNT} channels on {config.DEVICE_DESCRIPTION} ({config.DEVICE_ID}):")
        for i in range(config.CHANNEL_COUNT):
            ch_idx = config.START_CHANNEL + i
            ch_cfg = config.channels.get(ch_idx)
            if ch_cfg:
                wf.channels[ch_idx].signalType = ch_cfg.signal_type
                wf.channels[ch_idx].valueRange = ch_cfg.value_range
                log.info(f"  ch{ch_idx} ({ch_cfg.label}): signalType={ch_cfg.signal_type_str}, range={ch_cfg.value_range_str}")
            else:
                wf.channels[ch_idx].signalType = getattr(AiSignalType, "SingleEnded", 0)
                wf.channels[ch_idx].valueRange = getattr(ValueRange, "V_0To5", 0)

        ret = wf.prepare()
        if BioFailed(ret):
            log.error(f"DAQ prepare() failed on {config.DEVICE_DESCRIPTION} — check device connection and permissions")
            daq_error_occurred.set()
            stop_event.set()
            return

        ret = wf.start()
        if BioFailed(ret):
            log.error(f"DAQ start() failed on {config.DEVICE_DESCRIPTION}")
            daq_error_occurred.set()
            stop_event.set()
            return

        log.info(
            f"DAQ started | device={config.DEVICE_DESCRIPTION} (ID: {config.DEVICE_ID}) | "
            f"channels={config.CHANNEL_COUNT} | clock={config.CLOCK_RATE} Hz | "
            f"sectionLength={config.SECTION_LENGTH} | userBuffer={config.USER_BUFFER_SIZE}"
        )

        try:
            log.info("DAQ loop started — periodic wall-clock re-anchoring active")

            while not stop_event.is_set():
                # Block until USER_BUFFER_SIZE interleaved samples are ready
                # timeout=-1 means wait indefinitely for requested count
                result = wf.getDataF64(config.USER_BUFFER_SIZE, -1)

                # ── Capture wall-clock timestamp IMMEDIATELY after getDataF64() returns ──
                # This is the best approximation of when the LAST sample in this batch
                # was produced by the hardware. OS scheduling jitter is typically ~1 ms.
                batch_wall_ts_ns = time.time_ns()

                ret, returned_count, raw_data = result[0], result[1], result[2]

                if BioFailed(ret):
                    log.error("getDataF64() error — stopping DAQ thread")
                    stop_event.set()
                    break

                if returned_count <= 0:
                    continue

                # ── Minimal work: copy raw list + enqueue immediately ──
                # DO NOT loop/parse here — let DB writer handle it
                raw_copy = list(raw_data[:returned_count])

                try:
                    data_queue.put_nowait((batch_wall_ts_ns, raw_copy, returned_count))
                    with stats_lock:
                        stats["polled"]   += returned_count
                        stats["enqueued"] += 1
                        stats["last_polled_time"] = time.time()
                except queue.Full:
                    with stats_lock:
                        stats["dropped"] += 1
                    log.warning(
                        f"Queue full! Dropped 1 batch ({returned_count} samples). "                     
                    )

        finally:
            try:
                if hasattr(wf, 'stop'):
                    wf.stop()
            except Exception:
                pass
            try:
                if hasattr(wf, 'dispose'):
                    wf.dispose()
            except Exception:
                pass
            log.info("DAQ thread stopped and device released.")
    except Exception as e:
        log.exception(f"Unhandled exception in DAQ Reader thread: {e}")
        daq_error_occurred.set()
        stop_event.set()



# ─── Data Extraction, Calibration, and Storage Components (SRP Design) ───────

class Calibrator:
    """
    Responsibility: Handle calibration configuration parsing and scaling calculations.
    """
    def __init__(self, start_channel, channel_count, channel_configs):
        self.calibrations = {}
        if isinstance(channel_configs, dict):
            for ch in range(channel_count):
                ch_num = start_channel + ch
                cfg_entry = channel_configs.get(ch_num) or channel_configs.get(str(ch_num))
                if cfg_entry is None:
                    continue
                if hasattr(cfg_entry, "scale_enabled"):
                    if cfg_entry.scale_enabled:
                        low_volt = cfg_entry.low_voltage
                        high_volt = cfg_entry.high_voltage
                        low_val = cfg_entry.low_value
                        high_val = cfg_entry.high_value
                        denom = high_volt - low_volt
                        if abs(denom) > 1e-9:
                            slope = (high_val - low_val) / denom
                            self.calibrations[ch] = (low_volt, low_val, slope)
                elif isinstance(cfg_entry, dict):
                    scale_cfg = cfg_entry.get("scale", cfg_entry)
                    if scale_cfg.get("enabled", False):
                        low_volt = float(scale_cfg.get("low_voltage", 0.0))
                        high_volt = float(scale_cfg.get("high_voltage", 5.0))
                        low_val = float(scale_cfg.get("low_value", 0.0))
                        high_val = float(scale_cfg.get("high_value", 100.0))
                        denom = high_volt - low_volt
                        if abs(denom) > 1e-9:
                            slope = (high_val - low_val) / denom
                            self.calibrations[ch] = (low_volt, low_val, slope)

    def calibrate(self, ch, value):
        if ch in self.calibrations:
            low_volt, low_val, slope = self.calibrations[ch]
            return low_val + (value - low_volt) * slope
        return value


class DaqSampleParser:
    """
    Responsibility: Parse interleaved raw DAQ data and compute timestamps relative to a periodic anchor.
    Outputs (time, device_id, channel, value) tuples.
    """
    def __init__(self, start_channel, channel_count, clock_rate, calibrator, device_id="pci1716-0", recalibrate_interval_hr=24.0):
        self.start_channel = start_channel
        self.channel_count = channel_count
        self.dt_ns = int(1_000_000_000 / clock_rate)
        self.calibrator = calibrator
        self.device_id = device_id
        
        # Periodic anchor state configuration
        self.recalibrate_interval_ns = int(recalibrate_interval_hr * 3600 * 1_000_000_000)
        self.anchor_time_ns = None
        self.samples_since_anchor = 0

    def parse_batch(self, batch_wall_ts_ns, raw_data, returned_count):
        samples_per_channel = returned_count // self.channel_count
        
        # Re-anchor the base timestamp if not yet set or if the configured interval has elapsed
        current_time_ns = time.time_ns()
        if self.anchor_time_ns is None or (current_time_ns - self.anchor_time_ns) >= self.recalibrate_interval_ns:
            self.anchor_time_ns = batch_wall_ts_ns
            self.samples_since_anchor = 0
            
        rows = []
        for s in range(samples_per_channel):
            # Calculate forward timestamp based on cumulative samples since the last anchor
            sample_ts_ns = self.anchor_time_ns + (self.samples_since_anchor + s) * self.dt_ns
            sample_ts = datetime.fromtimestamp(sample_ts_ns / 1_000_000_000, tz=timezone.utc)
            for ch in range(self.channel_count):
                value = raw_data[s * self.channel_count + ch]
                value = self.calibrator.calibrate(ch, value)
                value = round(value, 3)
                rows.append((sample_ts, self.device_id, self.start_channel + ch, value))
                
        # Advance cumulative sample count for the next batch
        self.samples_since_anchor += samples_per_channel
        return rows


def _create_cagg_with_policy(cur, table_name: str, bucket_size: str, suffix: str, start_offset: str, end_offset: str, schedule_interval: str):
    cagg_name = f"{table_name}_{suffix}"
    cur.execute(f"""
        CREATE MATERIALIZED VIEW IF NOT EXISTS {cagg_name}
        WITH (timescaledb.continuous) AS
        SELECT
            time_bucket('{bucket_size}', time) AS bucket,
            device_id,
            channel,
            AVG(value) AS avg,
            MIN(value) AS min,
            MAX(value) AS max,
            COUNT(value) AS count
        FROM {table_name}
        GROUP BY bucket, device_id, channel
        WITH NO DATA;
    """)
    cur.execute(f"""
        SELECT add_continuous_aggregate_policy('{cagg_name}',
            start_offset => INTERVAL '{start_offset}',
            end_offset => INTERVAL '{end_offset}',
            schedule_interval => INTERVAL '{schedule_interval}',
            if_not_exists => TRUE);
    """)


def ensure_db_and_tables(dsn, table_name="daq_telemetry", retention_days=None, compression_interval=None):
    """
    Auto-creates database if missing and auto-creates required tables/hypertable/indexes/policies if missing.
    Supports configurable table_name with device_id column, configurable retention_days, and compression_interval.
    """
    try:
        import psycopg2.extensions
        parsed = psycopg2.extensions.make_dsn(dsn)
        parts = psycopg2.extensions.parse_dsn(parsed)
        target_dbname = parts.get("dbname")

        if target_dbname:
            maint_parts = dict(parts)
            maint_parts["dbname"] = "postgres"
            maint_dsn = psycopg2.extensions.make_dsn(**maint_parts)
            try:
                maint_conn = psycopg2.connect(maint_dsn, connect_timeout=5)
                maint_conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
                with maint_conn.cursor() as cur:
                    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_dbname,))
                    if not cur.fetchone():
                        log.info(f"[DBSetup] Database '{target_dbname}' does not exist. Creating database...")
                        cur.execute(f'CREATE DATABASE "{target_dbname}"')
                        log.info(f"[DBSetup] Database '{target_dbname}' created successfully.")
                maint_conn.close()
            except Exception as me:
                log.warning(f"[DBSetup] Maintenance DB check/creation warning: {me}")

        target_conn = psycopg2.connect(dsn, connect_timeout=5)
        target_conn.autocommit = True
        with target_conn.cursor() as cur:
            has_timescale = False
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")
                has_timescale = True
            except Exception:
                try:
                    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb';")
                    has_timescale = bool(cur.fetchone())
                except Exception:
                    has_timescale = False

            cur.execute(f"""
                CREATE TABLE IF NOT EXISTS {table_name} (
                    time        TIMESTAMPTZ      NOT NULL,
                    device_id   VARCHAR(32)      NOT NULL,
                    channel     SMALLINT         NOT NULL,
                    value       DOUBLE PRECISION NOT NULL
                );
            """)

            if has_timescale:
                try:
                    cur.execute(f"SELECT create_hypertable('{table_name}', 'time', chunk_time_interval => INTERVAL '1 hour', if_not_exists => TRUE);")
                except Exception:
                    pass

            cur.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_{table_name}_device_channel_time
                    ON {table_name} (device_id, channel, time DESC);
            """)

            if has_timescale:
                # Compression Policy
                try:
                    resolved_compression_interval = str(compression_interval) if compression_interval is not None else getattr(config, "DB_COMPRESSION_INTERVAL", "1 hour")
                    cur.execute(f"""
                        ALTER TABLE {table_name} SET (
                            timescaledb.compress,
                            timescaledb.compress_segmentby = 'device_id, channel',
                            timescaledb.compress_orderby = 'time DESC'
                        );
                    """)
                    cur.execute(f"SELECT add_compression_policy('{table_name}', INTERVAL '{resolved_compression_interval}', if_not_exists => TRUE);")
                except Exception as comp_err:
                    log.warning(f"[DBSetup] Compression policy setup warning: {comp_err}")

                # Retention Policy
                try:
                    resolved_retention_days = int(retention_days) if retention_days is not None else getattr(config, "DB_RETENTION_DAYS", 90)
                    cur.execute(f"SELECT add_retention_policy('{table_name}', INTERVAL '{resolved_retention_days} days', if_not_exists => TRUE);")
                except Exception as ret_err:
                    log.warning(f"[DBSetup] Retention policy setup warning: {ret_err}")

                # Continuous Aggregates: 1s and 1m downsampling
                try:
                    _create_cagg_with_policy(
                        cur=cur,
                        table_name=table_name,
                        bucket_size="1 second",
                        suffix="1s",
                        start_offset="1 hour",
                        end_offset="1 second",
                        schedule_interval="10 seconds"
                    )
                except Exception as cagg_err:
                    log.warning(f"[DBSetup] Continuous aggregate {table_name}_1s warning: {cagg_err}")

                try:
                    _create_cagg_with_policy(
                        cur=cur,
                        table_name=table_name,
                        bucket_size="1 minute",
                        suffix="1m",
                        start_offset="1 day",
                        end_offset="1 minute",
                        schedule_interval="1 minute"
                    )
                except Exception as cagg_err:
                    log.warning(f"[DBSetup] Continuous aggregate {table_name}_1m warning: {cagg_err}")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS daq_sessions (
                    session_id    VARCHAR(64) PRIMARY KEY,
                    device_id     VARCHAR(32),
                    start_time    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    end_time      TIMESTAMPTZ,
                    channel_count SMALLINT    NOT NULL,
                    clock_rate    INTEGER     NOT NULL,
                    config_snapshot JSONB,
                    mode          VARCHAR(32)
                );
            """)
        target_conn.close()
        log.info(f"[DBSetup] Database and table schema '{table_name}' verified/auto-created.")
        return True
    except Exception as e:
        log.error(f"[DBSetup] Auto creation of database/tables failed: {e}")
        return False


class TimescaleDBClient:
    """
    Responsibility: Manage TimescaleDB connection lifecycle, transactions, and execution.
    """
    def __init__(self, dsn, stop_event=None, dbname=None, table_name="daq_telemetry", retention_days=None, compression_interval=None):
        self.dsn = dsn
        self.stop_event = stop_event or threading.Event()
        self.dbname = dbname
        self.table_name = table_name
        self.retention_days = retention_days if retention_days is not None else getattr(config, "DB_RETENTION_DAYS", 90)
        self.compression_interval = compression_interval if compression_interval is not None else getattr(config, "DB_COMPRESSION_INTERVAL", "1 hour")
        self.conn = None
        self.cur = None

    def connect(self):
        while not self.stop_event.is_set():
            try:
                ensure_db_and_tables(self.dsn, self.table_name, self.retention_days, self.compression_interval)
                self.conn = psycopg2.connect(
                    self.dsn,
                    connect_timeout=10,
                    options="-c statement_timeout=15000"
                )
                self.conn.autocommit = False
                self.cur = self.conn.cursor()
                db_desc = f" '{self.dbname}'" if self.dbname else ""
                log.info(f"Connected to database{db_desc} (table: {self.table_name})")
                return True
            except Exception as e:
                log.error(f"DB connection failed: {e} — retrying in 5s")
                for _ in range(50):
                    if self.stop_event.is_set():
                        return False
                    time.sleep(0.1)
        return False

    def insert_samples(self, rows, page_size):
        if not self.conn or not self.cur:
            raise RuntimeError("Not connected to database")
        INSERT_SQL = f"INSERT INTO {self.table_name} (time, device_id, channel, value) VALUES %s"
        try:
            psycopg2.extras.execute_values(
                self.cur, INSERT_SQL, rows, page_size=page_size
            )
            self.conn.commit()
        except Exception as e:
            self.rollback()
            log.warning(f"Database insert failed ({e}). Auto-creating database/tables and retrying...")
            if ensure_db_and_tables(self.dsn, self.table_name):
                try:
                    self.conn = psycopg2.connect(
                        self.dsn,
                        connect_timeout=10,
                        options="-c statement_timeout=15000"
                    )
                    self.conn.autocommit = False
                    self.cur = self.conn.cursor()
                    psycopg2.extras.execute_values(
                        self.cur, INSERT_SQL, rows, page_size=page_size
                    )
                    self.conn.commit()
                    log.info("Insertion succeeded after auto-creating database/tables.")
                    return
                except Exception as retry_err:
                    log.error(f"Retry insertion after auto-creation failed: {retry_err}")
            raise

    @property
    def is_connected(self):
        return self.conn is not None and not getattr(self.conn, 'closed', False)

    def send_samples(self, rows, page_size=1000):
        self.insert_samples(rows, page_size=page_size)

    def rollback(self):
        if self.conn:
            self.conn.rollback()

    def disconnect(self):
        if self.cur:
            try:
                self.cur.close()
            except:
                pass
            self.cur = None
        if self.conn:
            try:
                self.conn.close()
            except:
                pass
            self.conn = None
        log.info("Disconnected from database.")


def _chunk_rows(rows, page_size=1000):
    if not rows:
        return
    step = page_size if page_size and page_size > 0 else len(rows)
    for i in range(0, len(rows), step):
        yield rows[i:i + step]


class InfluxDBClient:
    """
    Responsibility: Manage InfluxDB HTTP Line Protocol telemetry writes.
    """
    def __init__(self, url="http://localhost:8086", token=None, org="mddp", bucket="daq_telemetry", measurement="daq_telemetry", stop_event=None):
        self.url = (url or "http://localhost:8086").rstrip('/')
        self.token = token or ""
        self.org = org or "mddp"
        self.bucket = bucket or "daq_telemetry"
        self.measurement = measurement or "daq_telemetry"
        self.stop_event = stop_event or threading.Event()
        self.write_url = f"{self.url}/api/v2/write?org={urllib.parse.quote(self.org)}&bucket={urllib.parse.quote(self.bucket)}&precision=s"
        self.is_connected = False

    def connect(self):
        target_url = f"{self.url}/health"
        headers = {"User-Agent": "USB4716-Writer"}
        if self.token:
            headers["Authorization"] = f"Token {self.token}"
        try:
            req = urllib.request.Request(target_url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status == 200:
                    self.is_connected = True
                    log.info(f"Connected to InfluxDB at {self.url} (Org: {self.org}, Bucket: {self.bucket})")
                    return True
                else:
                    self.is_connected = False
                    log.error(f"InfluxDB health check returned status {resp.status}")
                    return False
        except Exception as e:
            self.is_connected = False
            log.warning(f"InfluxDB connection failed ({e}).")
            return False

    def format_line(self, row):
        """
        Formats a single sample tuple into an InfluxDB line protocol string.
        Accepts:
          - 5-tuple: (ts, device_id, channel, voltage, scaled)
          - 4-tuple: (ts, device_id, channel, value)
          - legacy 4-tuple: (ts_ns, ch, volt, scaled)
        """
        if len(row) >= 5:
            ts, dev_id, ch, volt, scaled = row[0], row[1], row[2], row[3], row[4]
            fields = f"voltage={volt},scaled={scaled}"
        elif len(row) == 4:
            if isinstance(row[1], str):
                ts, dev_id, ch, val = row[0], row[1], row[2], row[3]
                fields = f"voltage={val},scaled={val}"
            else:
                ts, ch, volt, scaled = row[0], row[1], row[2], row[3]
                dev_id = "default"
                fields = f"voltage={volt},scaled={scaled}"
        elif len(row) == 3:
            ts, ch, val = row[0], row[1], row[2]
            dev_id = "default"
            fields = f"voltage={val},scaled={val}"
        else:
            raise ValueError(f"Unsupported row format: {row}")

        if hasattr(ts, "timestamp"):
            ts_sec = int(ts.timestamp())
        elif isinstance(ts, (int, float)):
            ts_sec = int(ts / 1e9) if ts > 1e11 else int(ts)
        else:
            ts_sec = int(datetime.fromisoformat(str(ts)).timestamp())

        return f"{self.measurement},device_id={dev_id},ch={ch} {fields} {ts_sec}"

    def send_samples(self, rows, page_size=1000):
        if not rows:
            return
        for chunk in _chunk_rows(rows, page_size):
            lines = [self.format_line(r) for r in chunk]
            body = "\n".join(lines).encode('utf-8')
            headers = {
                "Content-Type": "text/plain; charset=utf-8",
                "Accept": "application/json"
            }
            if self.token:
                headers["Authorization"] = f"Token {self.token}"

            req = urllib.request.Request(self.write_url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                if resp.status not in (200, 204):
                    raise RuntimeError(f"InfluxDB HTTP status {resp.status}")

    def rollback(self):
        pass

    def disconnect(self):
        self.is_connected = False
        log.info("InfluxDB client disconnected.")


def create_destination_client(cfg, stop_event=None):
    """
    Factory function to instantiate the active destination client based on config.DESTINATION.
    Supports PostgreSQL/TimescaleDB and InfluxDB mockup destinations.
    """
    dest = getattr(cfg, 'DESTINATION', 'postgresql').lower()
    if dest == 'influxdb':
        return InfluxDBClient(
            url=getattr(cfg, 'INFLUX_URL', 'http://localhost:8086'),
            token=getattr(cfg, 'INFLUX_TOKEN', ''),
            org=getattr(cfg, 'INFLUX_ORG', 'mddp'),
            bucket=getattr(cfg, 'INFLUX_BUCKET', 'daq_telemetry'),
            measurement=getattr(cfg, 'INFLUX_MEASUREMENT', 'daq_telemetry'),
            stop_event=stop_event
        )
    elif dest in ('postgresql', 'timescaledb', 'database'):
        dsn = getattr(cfg, 'DB_DSN', None) or f"postgresql://{getattr(cfg, 'DB_USER', 'postgres')}:{getattr(cfg, 'DB_PASSWORD', '')}@{getattr(cfg, 'DB_HOST', 'localhost')}:{getattr(cfg, 'DB_PORT', 5432)}/{getattr(cfg, 'DB_NAME', 'daq_telemetry')}"
        return TimescaleDBClient(
            dsn=dsn,
            stop_event=stop_event,
            dbname=getattr(cfg, 'DB_NAME', 'daq_telemetry'),
            table_name=getattr(cfg, 'DB_TABLE', 'daq_telemetry'),
            retention_days=getattr(cfg, 'DB_RETENTION_DAYS', 90),
            compression_interval=getattr(cfg, 'DB_COMPRESSION_INTERVAL', '1 hour')
        )
    raise ValueError(f"Unsupported mockup destination: {dest}")


# ─── Data Writer Thread ───────────────────────────────────────────────────────
def db_writer_thread():
    """
    Responsibility: dequeue raw batches, delegate parsing, and write to a mockup database.
    Non-daemon thread — will flush remaining queue items before process exits.
    """
    calibrator = Calibrator(
        start_channel=config.START_CHANNEL,
        channel_count=config.CHANNEL_COUNT,
        channel_configs=getattr(config, 'channels', getattr(config, 'CHANNELS', {}))
    )

    parser = DaqSampleParser(
        start_channel=config.START_CHANNEL,
        channel_count=config.CHANNEL_COUNT,
        clock_rate=config.CLOCK_RATE,
        calibrator=calibrator,
        device_id=getattr(config, 'DEVICE_ID', 'pci1716-0'),
        recalibrate_interval_hr=getattr(config, 'ANCHOR_RECALIBRATE_INTERVAL_HR', 24.0)
    )

    destination = getattr(config, 'DESTINATION', 'database').lower()
    client = create_destination_client(config, stop_event=stop_event)

    if not client.connect():
        log.info(f"Writer thread exiting ({destination} connection failed).")
        return

    log.info(f"Writer thread ready ({destination} mode)...")

    while not stop_event.is_set() or not data_queue.empty():
        try:
            batch_wall_ts_ns, raw_data, returned_count = data_queue.get(timeout=1.0)
        except queue.Empty:
            continue

        # 1. Parse raw data into sample rows
        rows = parser.parse_batch(batch_wall_ts_ns, raw_data, returned_count)

        # 2. Write/Publish samples (SRP delegation)
        try:
            client.send_samples(rows, page_size=config.DB_PAGE_SIZE)
            with stats_lock:
                stats["written"] += len(rows)
                stats["last_written_time"] = time.time()
        except Exception as e:
            with stats_lock:
                stats["db_errors"] += 1
            log.error(f"Writer output error ({destination}): {e} — attempting recovery...")

            # Attempt rollback
            try:
                client.rollback()
            except Exception as rb_err:
                log.error(f"Rollback failed: {rb_err} — connection is dead. Closing resources.")
                client.disconnect()

            # Re-enqueue so data is not lost (best-effort)
            try:
                data_queue.put_nowait((batch_wall_ts_ns, raw_data, returned_count))
            except queue.Full:
                with stats_lock:
                    stats["dropped"] += 1
                log.error("Queue full on re-queue — batch permanently lost!")

            # Reconnect if connection was lost
            if not getattr(client, 'is_connected', False):
                log.info(f"Reconnecting to {destination}...")
                if not client.connect():
                    log.error(f"Failed to reconnect to {destination}. Writer thread stopping.")
                    return

            # Apply rate limiting to prevent tight CPU looping when writer has persistent errors
            time.sleep(1.0)

    client.disconnect()



# ─── Watchdog & Monitoring ───────────────────────────────────────────────────
def check_pipeline_watchdog(stats_snapshot, qsize, timeout_sec, current_time=None):
    """
    Checks if DAQ acquisition or DB writing is stalled.
    Returns (status: bool, warning_msg: str | None).
    """
    now = time.time() if current_time is None else current_time
    last_polled = stats_snapshot.get("last_polled_time", now)
    last_written = stats_snapshot.get("last_written_time", now)

    warnings = []
    # If DAQ has not polled within timeout_sec
    if (now - last_polled) > timeout_sec:
        warnings.append(f"DAQ acquisition stalled: no samples polled in {now - last_polled:.1f}s (threshold: {timeout_sec}s)")

    # If queue has pending items but writer hasn't written within timeout_sec
    if qsize > 0 and (now - last_written) > timeout_sec:
        warnings.append(f"Writer stalled: {qsize} batches queued but no samples written in {now - last_written:.1f}s (threshold: {timeout_sec}s)")

    if warnings:
        return False, " | ".join(warnings)
    return True, None


def monitor_thread():
    """Logs pipeline statistics, verifies health, and touches heartbeat file."""
    watchdog_timeout = getattr(config, 'WATCHDOG_TIMEOUT_SEC', 30)
    heartbeat_path = getattr(config, 'HEARTBEAT_FILE', '/tmp/daq_navi_heartbeat')

    while not stop_event.is_set():
        time.sleep(config.STATS_INTERVAL_SEC)
        with stats_lock:
            s = dict(stats)
        q_len = data_queue.qsize()
        loss_pct = (s["dropped"] / s["enqueued"] * 100) if s["enqueued"] > 0 else 0.0
        log.info(
            f"[STATS] polled={s['polled']:,} | written={s['written']:,} | "
            f"dropped_batches={s['dropped']} ({loss_pct:.1f}%) | "
            f"db_errors={s['db_errors']} | queue={q_len}/{config.QUEUE_MAXSIZE}"
        )

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
        log.info(f"Signal {sig} received — initiating graceful shutdown...")
        stop_event.set()

    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Windows: SIGBREAK fires on console close / taskkill without /f
    if sys.platform == "win32":
        signal.signal(signal.SIGBREAK, handle_signal)

    dest = getattr(config, 'DESTINATION', 'database').lower()
    log.info("=" * 60)
    log.info(f"DAQ Streaming Pipeline Starting [Destination: {dest.upper()}]")
    log.info(f"  Channels    : {config.CHANNEL_COUNT} (ch{config.START_CHANNEL}–ch{config.START_CHANNEL + config.CHANNEL_COUNT - 1})")
    log.info(f"  Clock rate  : {config.CLOCK_RATE} Hz")
    log.info(f"  sectionLength: {config.SECTION_LENGTH} samples/ch")
    log.info(f"  Batch size  : {config.USER_BUFFER_SIZE} interleaved samples (~{config.SECTION_LENGTH / config.CLOCK_RATE * 1000:.0f}ms)")
    log.info(f"  DB DSN      : {config.DB_DSN}")
    log.info("=" * 60)

    daq_thread = threading.Thread(
        target=daq_reader_thread, name="DAQ-Reader", daemon=True
    )
    db_thread = threading.Thread(
        target=db_writer_thread, name="DB-Writer", daemon=False  # non-daemon: flushes on exit
    )
    mon_thread = threading.Thread(
        target=monitor_thread, name="Monitor", daemon=True
    )

    daq_thread.start()
    db_thread.start()
    mon_thread.start()

    # Wait until stop_event is set (Ctrl+C or DAQ error)
    stop_event.wait()

    log.info("Waiting for data writer to flush remaining queue...")
    db_thread.join(timeout=60)

    if db_thread.is_alive():
        log.warning("Data writer did not finish within 60s timeout.")

    with stats_lock:
        s = dict(stats)
    log.info("=" * 60)
    log.info("Pipeline stopped.")
    log.info(f"  Total polled   : {s['polled']:,} samples")
    log.info(f"  Total sent/wrote: {s['written']:,} rows")
    log.info(f"  Dropped        : {s['dropped']} batches")
    log.info(f"  Errors         : {s['db_errors']}")
    log.info("=" * 60)

    if daq_error_occurred.is_set():
        log.error("Pipeline stopped due to DAQ hardware error.")
        sys.exit(1)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Standalone physical DAQ acquisition")
    parser.add_argument("--config", help="DAQ configuration JSON path")
    args = parser.parse_args()
    selected_config = load_daq_config(args.config) if args.config else config
    try:
        from .production_acquisition import run_production
    except ImportError:
        from production_acquisition import run_production
    requested_stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: requested_stop.set())
    signal.signal(signal.SIGINT, lambda *_: requested_stop.set())
    try:
        run_production(selected_config, stop_event=requested_stop)
    except Exception as exc:
        log.error("Physical production acquisition failed: %s", exc)
        sys.exit(1)
