#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Standalone configuration loading tests."""

import os
import sys
import unittest

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_DIR = os.path.join(SERVICE_DIR, "core")
PROJECT_ROOT = SERVICE_DIR
for p in (CORE_DIR, SERVICE_DIR, PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.config_loader import load_daq_config, DaqNaviConfig, ChannelConfig

class TestConfigAndSchema(unittest.TestCase):
    def test_load_default_config(self):
        config_path = os.path.join(SERVICE_DIR, "config.json")
        cfg = load_daq_config(config_path)
        
        self.assertEqual(cfg.DEVICE_ID, "pci1716-0")
        self.assertEqual(cfg.DEVICE_DESCRIPTION, "PCI-1716,BID#0")
        self.assertEqual(cfg.CHANNEL_COUNT, 8)
        self.assertEqual(cfg.CLOCK_RATE, 1000)
        self.assertEqual(cfg.DB_TABLE, "daq_telemetry")
        self.assertEqual(cfg.DESTINATION, "postgresql")
        self.assertEqual(cfg.DB_RETENTION_DAYS, 30)
        self.assertEqual(cfg.DB_COMPRESSION_INTERVAL, "1 hour")
        
        # Verify all configured channels are parsed, including disabled inputs.
        self.assertEqual(set(cfg.channels), set(range(8)))
        
        ch0 = cfg.channels[0]
        self.assertEqual(ch0.label, "pressure-ch0")
        self.assertTrue(ch0.enabled)
        self.assertTrue(ch0.scale_enabled)
        self.assertEqual(ch0.low_voltage, 1.0)
        self.assertEqual(ch0.high_voltage, 5.0)
        self.assertEqual(ch0.low_value, 0.0)
        self.assertEqual(ch0.high_value, 1000.0)

    @unittest.skip("the monorepo SQL bootstrap file is not part of the standalone project")
    def test_sql_schema_file_exists_and_contains_table(self):
        sql_path = os.path.join(PROJECT_ROOT, "scripts", "sql", "db_setup.sql")
        self.assertTrue(os.path.exists(sql_path), "db_setup.sql must exist")
        with open(sql_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("daq_telemetry", content)
        self.assertIn("device_id", content)
        self.assertIn("create_hypertable", content)
        self.assertIn("idx_daq_telemetry_device_channel_time", content)
        self.assertIn("timescaledb.compress", content)
        self.assertIn("compress_segmentby", content)
        self.assertIn("add_compression_policy", content)
        self.assertIn("add_retention_policy", content)
        self.assertIn("daq_telemetry_1s", content)
        self.assertIn("daq_telemetry_1m", content)
        self.assertIn("add_continuous_aggregate_policy", content)

if __name__ == "__main__":
    unittest.main()
