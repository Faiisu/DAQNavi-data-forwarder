#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_destinations.py
────────────────────
Tests for legacy database and InfluxDB mockup destinations.
- InfluxDBClient tags points with device_id in line protocol
- DESTINATION config selection and shared send_samples interface
- Reading connection parameters from config
- Mockup pipeline verification for PostgreSQL and InfluxDB destinations
"""

import json
import os
import sys
import time
import threading
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_DIR = os.path.join(SERVICE_DIR, "core")
PROJECT_ROOT = os.path.abspath(os.path.join(SERVICE_DIR, "..", ".."))
for p in (CORE_DIR, SERVICE_DIR, PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_loader import load_daq_config, DaqNaviConfig
from buffered_daq_to_timescaledb import (
    InfluxDBClient,
    TimescaleDBClient,
    create_destination_client,
    Calibrator,
    DaqSampleParser
)
import mockup_stream_to_db


class TestDestinations(unittest.TestCase):
    def setUp(self):
        self.config_path = os.path.join(SERVICE_DIR, "config.json")
        self.cfg = load_daq_config(self.config_path)
        while not mockup_stream_to_db.data_queue.empty():
            try:
                mockup_stream_to_db.data_queue.get_nowait()
            except Exception:
                break
        mockup_stream_to_db.stop_event.clear()
        self.addCleanup(self._clear_mockup_queue)

    def _clear_mockup_queue(self):
        mockup_stream_to_db.stop_event.set()
        while not mockup_stream_to_db.data_queue.empty():
            try:
                mockup_stream_to_db.data_queue.get_nowait()
            except Exception:
                break

    def test_destination_selection_factory(self):
        # Test postgresql
        cfg_pg = DaqNaviConfig({**self.cfg.raw, "DESTINATION": "postgresql"})
        client_pg = create_destination_client(cfg_pg)
        self.assertIsInstance(client_pg, TimescaleDBClient)

        # Test influxdb
        cfg_influx = DaqNaviConfig({
            **self.cfg.raw,
            "DESTINATION": "influxdb",
            "INFLUX_URL": "http://influx.example.com:8086",
            "INFLUX_ORG": "test-org",
            "INFLUX_BUCKET": "test-bucket",
            "INFLUX_TOKEN": "secret-token"
        })
        client_influx = create_destination_client(cfg_influx)
        self.assertIsInstance(client_influx, InfluxDBClient)
        self.assertEqual(client_influx.url, "http://influx.example.com:8086")
        self.assertEqual(client_influx.org, "test-org")
        self.assertEqual(client_influx.bucket, "test-bucket")
        self.assertEqual(client_influx.token, "secret-token")

    def test_shared_client_interface(self):
        # All three clients must have send_samples, rollback, and disconnect methods
        clients = [
            TimescaleDBClient("postgresql://localhost/dummy", MagicMock()),
            InfluxDBClient("http://localhost:8086", "token", "org", "bucket")
        ]
        for c in clients:
            self.assertTrue(callable(getattr(c, "send_samples", None)), f"{c.__class__.__name__} missing send_samples")
            self.assertTrue(callable(getattr(c, "rollback", None)), f"{c.__class__.__name__} missing rollback")
            self.assertTrue(callable(getattr(c, "disconnect", None)), f"{c.__class__.__name__} missing disconnect")

    def test_influxdb_client_line_protocol_tags_device_id(self):
        client = InfluxDBClient(
            url="http://localhost:8086",
            token="token123",
            org="mddp",
            bucket="daq_telemetry",
            measurement="daq_telemetry"
        )

        test_ts = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        ts_sec = int(test_ts.timestamp())

        # Test with 5-tuple: (time, device_id, channel, voltage, scaled)
        row_5 = (test_ts, "pci1716-0", 0, 2.45, 50.0)
        line_5 = client.format_line(row_5)
        expected_5 = f"daq_telemetry,device_id=pci1716-0,ch=0 voltage=2.45,scaled=50.0 {ts_sec}"
        self.assertEqual(line_5, expected_5)

        # Test with 4-tuple: (time, device_id, channel, value)
        row_4 = (test_ts, "pci1716-0", 1, 2.45)
        line_4 = client.format_line(row_4)
        self.assertIn("device_id=pci1716-0", line_4)
        self.assertIn("ch=1", line_4)
        self.assertIn(str(ts_sec), line_4)

    @patch("urllib.request.urlopen")
    def test_influxdb_client_send_samples_http_write(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        client = InfluxDBClient(
            url="http://localhost:8086",
            token="my-token",
            org="mddp",
            bucket="daq_telemetry",
            measurement="daq_telemetry"
        )

        test_ts = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        rows = [
            (test_ts, "pci1716-0", 0, 2.45, 50.0),
            (test_ts, "pci1716-0", 1, 1.20, 25.0)
        ]

        client.send_samples(rows)

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        self.assertIn("http://localhost:8086/api/v2/write", req.full_url)
        self.assertIn("org=mddp", req.full_url)
        self.assertIn("bucket=daq_telemetry", req.full_url)
        self.assertEqual(req.headers.get("Authorization"), "Token my-token")
        body_text = req.data.decode("utf-8")
        self.assertIn("daq_telemetry,device_id=pci1716-0,ch=0 voltage=2.45,scaled=50.0", body_text)
        self.assertIn("daq_telemetry,device_id=pci1716-0,ch=1 voltage=1.2,scaled=25.0", body_text)

    def test_mockup_pipeline_with_destinations(self):
        calibrator = Calibrator(
            start_channel=self.cfg.START_CHANNEL,
            channel_count=self.cfg.CHANNEL_COUNT,
            channel_configs=self.cfg.channels
        )
        parser = DaqSampleParser(
            start_channel=self.cfg.START_CHANNEL,
            channel_count=self.cfg.CHANNEL_COUNT,
            clock_rate=self.cfg.CLOCK_RATE,
            calibrator=calibrator,
            device_id=self.cfg.DEVICE_ID
        )

        start_ns = 1_770_000_000_000_000_000
        raw_data = [3.0] * self.cfg.CHANNEL_COUNT  # one interleaved frame for every configured input
        rows = parser.parse_batch(start_ns, raw_data, len(raw_data))
        self.assertEqual(len(rows), self.cfg.CHANNEL_COUNT)
        self.assertEqual(rows[0][1], "pci1716-0")

        # Test passing rows to InfluxDBClient formatting
        influx_client = InfluxDBClient("http://localhost:8086", "token", "org", "bucket")
        line = influx_client.format_line(rows[0])
        self.assertIn("device_id=pci1716-0", line)

    @patch("mockup_stream_to_db.create_destination_client")
    def test_mockup_writer_thread_influxdb_execution(self, mock_factory):
        mock_client = MagicMock()
        mock_client.connect.return_value = True
        mock_factory.return_value = mock_client

        test_batch = (time.time_ns(), [2.5] * self.cfg.CHANNEL_COUNT, self.cfg.CHANNEL_COUNT)
        mockup_stream_to_db.data_queue.put(test_batch)

        with patch.object(mockup_stream_to_db, 'config', self.cfg):
            mockup_stream_to_db.stop_event.clear()
            writer_t = threading.Thread(target=mockup_stream_to_db.db_writer_thread)
            writer_t.start()

            time.sleep(0.3)
            mockup_stream_to_db.stop_event.set()
            writer_t.join(timeout=2.0)

        self.assertTrue(mock_client.send_samples.called)
        sent_rows = mock_client.send_samples.call_args[0][0]
        self.assertGreater(len(sent_rows), 0)
        self.assertEqual(sent_rows[0][1], "pci1716-0")


    def test_live_influxdb_http_transport(self):
        from http.server import HTTPServer, BaseHTTPRequestHandler
        received_bodies = []

        class InfluxMockHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/health":
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"status":"pass"}')
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                received_bodies.append(body)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), InfluxMockHandler)
        port = httpd.server_port
        http_thread = threading.Thread(target=httpd.handle_request)
        http_thread.start()

        client = InfluxDBClient(url=f"http://127.0.0.1:{port}", token="tok", org="mddp", bucket="daq_telemetry")
        connected = client.connect()
        self.assertTrue(connected)
        http_thread.join(timeout=2.0)

        # Handle POST write
        http_thread_post = threading.Thread(target=httpd.handle_request)
        http_thread_post.start()

        sample_ts = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)
        client.send_samples([(sample_ts, "pci1716-0", 0, 2.45, 50.0)])
        http_thread_post.join(timeout=2.0)
        httpd.server_close()

        self.assertEqual(len(received_bodies), 1)
        self.assertIn("daq_telemetry,device_id=pci1716-0,ch=0 voltage=2.45,scaled=50.0", received_bodies[0])


if __name__ == "__main__":
    unittest.main()
