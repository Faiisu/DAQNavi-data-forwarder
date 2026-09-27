"""Production schema contract against an explicitly isolated TimescaleDB database."""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import psycopg2
from psycopg2 import sql

try:
    from core.production_acquisition import (
        ProductionPipeline,
        TimescaleProductionDestination,
    )
    from tests.test_production_acquisition import configuration
except ModuleNotFoundError:
    from core.production_acquisition import (
        ProductionPipeline,
        TimescaleProductionDestination,
    )
    from tests.test_production_acquisition import configuration


class ProductionTimescaleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dsn = os.environ.get("DAQ_TEST_DB_DSN", "")
        dbname = psycopg2.extensions.parse_dsn(dsn).get("dbname", "")
        if not dbname.startswith("daq_navi_test_"):
            raise unittest.SkipTest("set DAQ_TEST_DB_DSN to a daq_navi_test_* database")
        cls.dsn = dsn
        cls.table = "test_samples_" + uuid.uuid4().hex[:10]
        cls.cfg = configuration(DB_DSN=dsn, DB_PRODUCTION_TABLE=cls.table)

    @classmethod
    def tearDownClass(cls):
        if not hasattr(cls, "dsn"):
            return
        with psycopg2.connect(cls.dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(cls.table)))

    def test_duplicate_batch_is_idempotent_and_retention_changes(self):
        sink = TimescaleProductionDestination(self.cfg)
        with tempfile.TemporaryDirectory() as directory:
            pipeline = ProductionPipeline(self.cfg, Path(directory), sink)
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_000_000_000_000)
            _, original_rows = pipeline.spool.oldest()
            pipeline.flush_once()
            sink.write(original_rows, [])
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("SELECT COUNT(*), MIN(raw_voltage), MAX(calibrated_value) FROM {}").format(sql.Identifier(self.table)))
                    self.assertEqual(cur.fetchone(), (4, 1.0, 80.0))
                    cur.execute(sql.SQL("""SELECT sample_id, session_id, device_id, channel,
                        sensor_name, raw_voltage, calibrated_value, unit,
                        provenance FROM {} WHERE channel=0""").format(
                        sql.Identifier(self.table)))
                    sample = cur.fetchone()
                    self.assertEqual(sample[0], f"{pipeline.session_id}:0:0")
                    self.assertEqual(str(sample[1]), pipeline.session_id)
                    self.assertEqual(sample[2:], ("test-device", 0, "sensor-0", 1.0,
                                                  20.0, "kPa", "physical_daq"))
                    cur.execute("SELECT config->>'drop_after' FROM timescaledb_information.jobs WHERE hypertable_name=%s AND proc_name='policy_retention'", (self.table,))
                    self.assertEqual(cur.fetchone()[0], "30 days")
            self.cfg.DB_RETENTION_DAYS = 45
            sink.ensure_schema()
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT config->>'drop_after' FROM timescaledb_information.jobs WHERE hypertable_name=%s AND proc_name='policy_retention'", (self.table,))
                    self.assertEqual(cur.fetchone()[0], "45 days")
            pipeline.close()

    def test_lost_commit_response_replays_oldest_batch_once(self):
        sink = TimescaleProductionDestination(self.cfg)

        class LostReply:
            lose_reply = True

            def write(self, rows, gaps):
                sink.write(rows, gaps)
                if self.lose_reply:
                    self.lose_reply = False
                    raise ConnectionError("commit response lost")

        with tempfile.TemporaryDirectory() as directory:
            pipeline = ProductionPipeline(self.cfg, Path(directory), LostReply())
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_001_000_000_000)
            with self.assertRaisesRegex(ConnectionError, "response lost"):
                pipeline.flush_once()
            self.assertEqual(pipeline.pending_batches, 1)
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_001_000_500_000)
            pipeline.flush_once()
            pipeline.flush_once()
            self.assertEqual(pipeline.pending_batches, 0)
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("""SELECT COUNT(*), COUNT(DISTINCT sample_id),
                        MIN(time), MAX(time) FROM {} WHERE session_id=%s""").format(
                        sql.Identifier(self.table)), (pipeline.session_id,))
                    count, unique, first, last = cur.fetchone()
                    self.assertEqual((count, unique), (8, 8))
                    self.assertLess(first, last)
            pipeline.close()

    def test_schema_migration_retires_calibration_revision_once_and_does_not_repeat(self):
        legacy_table = "test_legacy_" + uuid.uuid4().hex[:10]
        cfg = configuration(DB_DSN=self.dsn, DB_PRODUCTION_TABLE=legacy_table)
        sink = TimescaleProductionDestination(cfg)

        try:
            # 1. Create table with legacy calibration_revision column
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                    cur.execute(sql.SQL("""CREATE TABLE {} (
                        time TIMESTAMPTZ NOT NULL, sample_id TEXT NOT NULL,
                        session_id UUID NOT NULL, device_id TEXT NOT NULL,
                        channel SMALLINT NOT NULL, sensor_name TEXT NOT NULL,
                        raw_voltage DOUBLE PRECISION NOT NULL,
                        calibrated_value DOUBLE PRECISION NOT NULL,
                        unit TEXT NOT NULL,
                        calibration_revision TEXT NOT NULL,
                        provenance TEXT NOT NULL,
                        PRIMARY KEY (time, sample_id)
                    )""").format(sql.Identifier(legacy_table)))
                    cur.execute("""
                        SELECT 1 FROM information_schema.columns
                        WHERE lower(table_name) = %s AND lower(column_name) = 'calibration_revision'
                    """, (legacy_table.lower(),))
                    self.assertIsNotNone(cur.fetchone())

            # 2. First startup / ensure_schema executes migration once
            sink.ensure_schema()

            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    # Check column was dropped
                    cur.execute("""
                        SELECT 1 FROM information_schema.columns
                        WHERE lower(table_name) = %s AND lower(column_name) = 'calibration_revision'
                    """, (legacy_table.lower(),))
                    self.assertIsNone(cur.fetchone())

                    # Check migration was recorded in daq_schema_migrations
                    cur.execute("SELECT version, description FROM daq_schema_migrations WHERE version = %s",
                                ("0001_retire_calibration_revision",))
                    migration_row = cur.fetchone()
                    self.assertIsNotNone(migration_row)
                    self.assertEqual(migration_row[0], "0001_retire_calibration_revision")

            # 3. Repeated startup / ensure_schema must NOT execute destructive DDL again
            executed_statements = []
            orig_connect = sink._connect

            class CurProxy:
                def __init__(self, cur):
                    self._cur = cur
                def __enter__(self):
                    self._cur.__enter__()
                    return self
                def __exit__(self, et, ev, tb):
                    return self._cur.__exit__(et, ev, tb)
                def execute(self, query, vars=None):
                    executed_statements.append(str(query))
                    return self._cur.execute(query, vars)
                def __getattr__(self, name):
                    return getattr(self._cur, name)

            class ConnProxy:
                def __init__(self, target):
                    self._target = target
                def __enter__(self):
                    self._target.__enter__()
                    return self
                def __exit__(self, et, ev, tb):
                    return self._target.__exit__(et, ev, tb)
                def cursor(self, *args, **kwargs):
                    return CurProxy(self._target.cursor(*args, **kwargs))
                def __getattr__(self, name):
                    return getattr(self._target, name)

            sink._connect = lambda: ConnProxy(orig_connect())
            sink.ensure_schema()

            drop_col_statements = [s for s in executed_statements if "DROP COLUMN" in s.upper()]
            self.assertEqual(drop_col_statements, [], "Repeated ensure_schema must not execute DROP COLUMN")

        finally:
            with psycopg2.connect(self.dsn) as conn:
                with conn.cursor() as cur:
                    cur.execute(sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(sql.Identifier(legacy_table)))


if __name__ == "__main__":
    unittest.main()
