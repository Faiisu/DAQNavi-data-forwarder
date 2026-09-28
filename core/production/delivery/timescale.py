"""Idempotent TimescaleDB production writer and schema setup."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import io
import psycopg2
from psycopg2 import sql
from psycopg2.extras import execute_values

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
