"""Standalone production acquisition contract; all data stays in temporary storage."""

import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from core.production_acquisition import (
        AcquisitionFault,
        AdvantechDaq,
        DurableSpool,
        ProductionPipeline,
        InfluxProductionDestination,
        TimescaleProductionDestination,
        run_production,
        validate_production_config,
    )
    from core.config_loader import DaqNaviConfig
except ModuleNotFoundError:
    from core.production_acquisition import (
        AcquisitionFault,
        AdvantechDaq,
        DurableSpool,
        ProductionPipeline,
        InfluxProductionDestination,
        TimescaleProductionDestination,
        run_production,
        validate_production_config,
    )
    from core.config_loader import DaqNaviConfig


def fake_bdaq_modules(device):
    """Let adapter tests run without loading the host's vendor shared library."""
    automation = ModuleType('Automation')
    bdaq = ModuleType('Automation.BDaq')
    waveform = ModuleType('Automation.BDaq.WaveformAiCtrl')
    api = ModuleType('Automation.BDaq.BDaqApi')
    waveform.WaveformAiCtrl = lambda description: device
    api.BioFailed = lambda result: False
    return {
        'Automation': automation,
        'Automation.BDaq': bdaq,
        'Automation.BDaq.WaveformAiCtrl': waveform,
        'Automation.BDaq.BDaqApi': api,
    }


def configuration(**changes):
    channels = {
        str(index): {
            "enabled": True,
            "label": f"sensor-{index}",
            "unit": "kPa",
            "signal_type": "SingleEnded",
            "value_range": "V_0To5",
            "scale": {
                "enabled": True,
                "low_voltage": 0,
                "high_voltage": 5,
                "low_value": 0,
                "high_value": 100,
                "revision": "r1",
            },
        }
        for index in range(4)
    }
    data = {
        "DEVICE_ID": "test-device",
        "START_CHANNEL": 0,
        "CHANNEL_COUNT": 4,
        "CLOCK_RATE": 2000,
        "SECTION_LENGTH": 2,
        "DESTINATION": "postgresql",
        "DB_TABLE": "test_production_samples",
        "CHANNELS": channels,
        "SPOOL_MAX_BYTES": 1024 * 1024,
    }
    data.update(changes)
    return DaqNaviConfig(data, allow_env_overrides=False)


class FakeDaq:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.closed = False

    def read(self, count):
        return next(self.batches)

    def close(self):
        self.closed = True


class FakeDestination:
    def __init__(self):
        self.rows = {}
        self.gaps = {}
        self.fail = False
        self.uncertain = False

    def write(self, rows, gaps):
        if self.fail:
            raise ConnectionError("database unavailable")
        for row in rows:
            self.rows[(row.get("time_ns"), row["sample_id"])] = row
        for gap in gaps:
            self.gaps[gap["gap_id"]] = gap
        if self.uncertain:
            self.uncertain = False
            raise ConnectionError("commit response lost")


class ProductionAcquisitionTests(unittest.TestCase):
    def test_influx_line_protocol_uses_ns_identity_and_field_sample_id(self):
        cfg = configuration(DESTINATION='influxdb', INFLUX_TOKEN='test')
        rows = []
        for rate in (1000, 2000):
            step = 1_000_000_000 // rate
            for index in range(3):
                rows.append({"time_ns": 1_700_000_000_000_000_000 + index * step,
                             "sample_id": f"run:{rate}:{index}", "device_id": "device,1",
                             "channel": 2, "session_id": "session 1", "sensor_name": "sensor x",
                             "raw_voltage": 1.25, "calibrated_value": 25.0, "unit": "kPa",
                             "provenance": "physical_daq"})
        encoded = InfluxProductionDestination.points(rows, [], cfg.INFLUX_MEASUREMENT).splitlines()
        self.assertEqual(len(encoded), 6)
        self.assertIn('device_id=device\\,1,channel=2,session_id=session\\ 1', encoded[0])
        self.assertIn('unit=kPa,provenance=physical_daq sample_id=', encoded[0])
        self.assertIn('sample_id="run:1000:0"', encoded[0])
        self.assertNotIn('sample_id=', encoded[0].split(' ', 1)[0])
        self.assertNotIn('calibration_revision', encoded[0])
        self.assertEqual([int(line.rsplit(' ', 1)[1]) for line in encoded[:3]],
                         [1_700_000_000_000_000_000 + i * 1_000_000 for i in range(3)])
        self.assertEqual([int(line.rsplit(' ', 1)[1]) for line in encoded[3:]],
                         [1_700_000_000_000_000_000 + i * 500_000 for i in range(3)])

    def test_influx_production_config_requires_token_and_accepts_valid_settings(self):
        valid = configuration(DESTINATION='influxdb', INFLUX_TOKEN='scoped-token')
        self.assertEqual(validate_production_config(valid), (0, 1, 2, 3))
        invalid = configuration(DESTINATION='influxdb', INFLUX_TOKEN='')
        with self.assertRaisesRegex(ValueError, 'organization, bucket, and token'):
            validate_production_config(invalid)
        unsafe = configuration(DESTINATION='influxdb', INFLUX_TOKEN='token', DEVICE_ID='bad\ndevice')
        with self.assertRaisesRegex(ValueError, 'DEVICE_ID cannot contain newline'):
            validate_production_config(unsafe)
        for setting, unsafe_value in (('unit', 'kP\na'), ('label', 'press\rure')):
            raw = {**configuration(DESTINATION='influxdb', INFLUX_TOKEN='token').raw}
            raw['CHANNELS'] = {key: dict(value) for key, value in raw['CHANNELS'].items()}
            raw['CHANNELS']['0'] = dict(raw['CHANNELS']['0'])
            raw['CHANNELS']['0'][setting] = unsafe_value
            with self.assertRaisesRegex(ValueError, 'InfluxDB tag values'):
                validate_production_config(DaqNaviConfig(raw, allow_env_overrides=False))

    def test_influx_write_failure_keeps_spool_batch_pending_for_replay(self):
        cfg = configuration(DESTINATION='influxdb', INFLUX_TOKEN='test')
        with tempfile.TemporaryDirectory() as directory:
            def fail(*args, **kwargs):
                raise TimeoutError('ambiguous response')
            destination = InfluxProductionDestination(cfg, opener=fail)
            pipeline = ProductionPipeline(cfg, Path(directory), destination, session_id='test-session')
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_000_000_000_000)
            with self.assertRaises(TimeoutError):
                pipeline.flush_once()
            self.assertEqual(pipeline.pending_batches, 1)
            pipeline.close()

    def test_startup_replays_pending_spool_to_current_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = DurableSpool(Path(directory), 1024 * 1024)
            spool.set_state('destination', 'postgresql')
            spool.append('old-batch', [{'sample_id': 'old'}])
            spool.close()
            cfg = configuration(DESTINATION='influxdb', INFLUX_TOKEN='test', SPOOL_DIR=directory)
            destination = FakeDestination()
            stopped = threading.Event()
            stopped.set()
            run_production(cfg, stop_event=stopped, destination=destination,
                           daq_factory=lambda _: FakeDaq([]), spool_dir=directory)
            self.assertEqual(destination.rows[(None, 'old')]['sample_id'], 'old')
            reopened = DurableSpool(Path(directory), cfg.SPOOL_MAX_BYTES)
            self.assertEqual(reopened.pending_batches, 0)
            reopened.close()

    def test_startup_replays_pending_spool_after_target_change(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = DurableSpool(Path(directory), 1024 * 1024)
            old_cfg = configuration(DESTINATION='influxdb', INFLUX_TOKEN='old-token',
                                    INFLUX_BUCKET='old-bucket')
            from core.production_acquisition import destination_identity
            spool.set_state('destination', 'influxdb')
            spool.set_state('destination_identity', destination_identity(old_cfg))
            spool.append('old-batch', [{'sample_id': 'old'}])
            spool.close()
            new_cfg = configuration(DESTINATION='influxdb', INFLUX_TOKEN='new-token',
                                    INFLUX_BUCKET='new-bucket', SPOOL_DIR=directory)
            destination = FakeDestination()
            stopped = threading.Event()
            stopped.set()
            run_production(new_cfg, stop_event=stopped, destination=destination,
                           daq_factory=lambda _: FakeDaq([]), spool_dir=directory)
            self.assertEqual(destination.rows[(None, 'old')]['sample_id'], 'old')

    def test_target_identity_ignores_rotated_credentials(self):
        from core.production_acquisition import destination_identity
        influx_a = configuration(DESTINATION='influxdb', INFLUX_TOKEN='token-a')
        influx_b = configuration(DESTINATION='influxdb', INFLUX_TOKEN='token-b')
        self.assertEqual(destination_identity(influx_a), destination_identity(influx_b))
        pg_a = configuration(DB_DSN='postgresql://daq:old-secret@db.example:5432/sensors')
        pg_b = configuration(DB_DSN='postgresql://daq:new-secret@db.example:5432/sensors')
        self.assertEqual(destination_identity(pg_a), destination_identity(pg_b))

    def test_section_longer_than_read_timeout_does_not_fault_before_data_arrives(self):
        cfg = configuration(CLOCK_RATE=1000, SECTION_LENGTH=5000)
        adapter = object.__new__(AdvantechDaq)
        adapter.cfg = cfg
        adapter._bio_failed = lambda result: False
        adapter.empty_reads = 0
        adapter.device = MagicMock()
        adapter.device.getDataF64.side_effect = [
            (SimpleNamespace(name="WarningFuncTimeout"), 0, []) for _ in range(5)
        ] + [(SimpleNamespace(name="Success"), cfg.USER_BUFFER_SIZE,
               [0.0] * cfg.USER_BUFFER_SIZE)]

        for _ in range(5):
            samples, _ = adapter.read(cfg.USER_BUFFER_SIZE)
            self.assertEqual(samples, [])
        samples, _ = adapter.read(cfg.USER_BUFFER_SIZE)
        self.assertEqual(len(samples), cfg.USER_BUFFER_SIZE)
        self.assertEqual(adapter.empty_reads, 0)

    def test_section_read_still_faults_after_expected_wait(self):
        cfg = configuration(CLOCK_RATE=1000, SECTION_LENGTH=5000)
        adapter = object.__new__(AdvantechDaq)
        adapter.cfg = cfg
        adapter._bio_failed = lambda result: False
        adapter.empty_reads = 0
        adapter.device = MagicMock()
        adapter.device.getDataF64.return_value = (
            SimpleNamespace(name="WarningFuncTimeout"), 0, []
        )

        with self.assertRaisesRegex(AcquisitionFault, "daq_stalled"):
            for _ in range(7):
                adapter.read(cfg.USER_BUFFER_SIZE)

    def test_spool_capacity_counter_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = DurableSpool(Path(directory), 4096)
            spool.append("batch-1", [{"sample_id": "first"}])
            pending = spool.pending_bytes
            self.assertGreater(pending, 0)
            self.assertEqual(spool.pending_batches, 1)
            spool.close()
            reopened = DurableSpool(Path(directory), 4096)
            self.assertEqual(reopened.pending_bytes, pending)
            self.assertEqual(reopened.pending_batches, 1)
            reopened.acknowledge("batch-1")
            self.assertEqual(reopened.pending_bytes, 0)
            self.assertEqual(reopened.pending_batches, 0)
            reopened.close()

    def test_clear_pending_records_discarded_samples_as_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = DurableSpool(Path(directory), 4096)
            spool.append("batch-1", [{"time_ns": 1_000_000_000},
                                      {"time_ns": 1_000_500_000}])
            spool.append("batch-2", [{"time_ns": 1_001_000_000}])
            result = spool.clear_pending()
            self.assertEqual(result["cleared_batches"], 2)
            self.assertEqual(result["cleared_samples"], 3)
            self.assertGreater(result["cleared_bytes"], 0)
            self.assertEqual(result["recorded_gaps"], 1)
            self.assertEqual(spool.pending_batches, 0)
            self.assertEqual(spool.pending_bytes, 0)
            self.assertIsNone(spool.oldest())
            gap = spool.pending_gaps()[0]
            self.assertEqual((gap["start_ns"], gap["end_ns"], gap["cause"]),
                             (1_000_000_000, 1_001_000_000, "operator_cleared_buffer"))
            spool.close()
            reopened = DurableSpool(Path(directory), 4096)
            self.assertEqual(reopened.pending_batches, 0)
            self.assertEqual(len(reopened.pending_gaps()), 1)
            reopened.close()

    def test_clear_empty_spool_reclaims_space_after_interrupted_compaction(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = DurableSpool(Path(directory), 1024 * 1024)
            spool.append('batch-1', [{'time_ns': 1_000_000_000,
                                      'raw': os.urandom(256_000).hex()}])
            spool.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            spool.conn.execute('DELETE FROM batches')
            spool.conn.commit()
            spool.close()
            reopened = DurableSpool(Path(directory), 1024 * 1024)
            before = reopened.usage_bytes
            self.assertEqual(reopened.pending_batches, 0)
            self.assertEqual(reopened.clear_pending()['cleared_batches'], 0)
            self.assertLess(reopened.usage_bytes, before)
            reopened.close()

    def test_spool_replays_commit_order_when_wall_clock_moves_back(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = DurableSpool(Path(directory), 4096)
            with patch("core.production_acquisition.time.time_ns",
                       side_effect=(200, 100)):
                spool.append("first", [{"sample_id": "first"}])
                spool.append("second", [{"sample_id": "second"}])
            spool.close()
            restarted = DurableSpool(Path(directory), 4096)
            self.assertEqual(restarted.oldest()[0], "first")
            restarted.acknowledge("first")
            self.assertEqual(restarted.oldest()[0], "second")
            restarted.close()

    def test_spool_commits_sample_position_and_gap_boundary_with_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = DurableSpool(Path(directory), 4096)
            spool.open_gap(100, "daq_read_failed")
            spool.append("batch-1", [{"time_ns": 200}, {"time_ns": 300}],
                         first_sample_ns=200, last_sample_ns=300)
            spool.close()
            restarted = DurableSpool(Path(directory), 4096)
            self.assertEqual(restarted.state_value("last_sample_ns"), "300")
            gap = restarted.pending_gaps()[0]
            self.assertEqual((gap["start_ns"], gap["end_ns"]), (100, 200))
            restarted.close()

    def test_four_channels_keep_raw_calibration_and_last_sample_time(self):
        cfg = configuration()
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            pipeline = ProductionPipeline(cfg, Path(directory), destination, session_id="test-session")
            pipeline.capture([0, 1, 2, 3, 5, 4, 3, 2], end_time_ns=1_700_000_000_000_500_000)
            pipeline.flush_once()
            first = destination.rows[(1_700_000_000_000_000_000, "test-session:0:0")]
            self.assertEqual((first["raw_voltage"], first["calibrated_value"], first["unit"]), (0, 0, "kPa"))
            self.assertEqual(len(destination.rows), 8)
            pipeline.close()

    def test_new_calibration_changes_only_future_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            old = ProductionPipeline(configuration(), Path(directory), destination,
                                     session_id="00000000-0000-0000-0000-000000000001")
            old.capture([2, 2, 2, 2], end_time_ns=1_700_000_000_000_000_000)
            old.flush_once()
            old.close()
            raw = configuration().raw
            raw["CHANNELS"]["0"]["scale"]["high_value"] = 200
            new = ProductionPipeline(DaqNaviConfig(raw), Path(directory), destination,
                                     session_id="00000000-0000-0000-0000-000000000002")
            new.capture([2, 2, 2, 2], end_time_ns=1_700_000_001_000_000_000)
            new.flush_once()
            samples = sorted((row for row in destination.rows.values() if row["channel"] == 0),
                             key=lambda row: row["time_ns"])
            self.assertEqual([row["calibrated_value"] for row in samples], [40, 80])
            new.close()

    def test_outage_restart_and_uncertain_commit_replay_once(self):
        cfg = configuration()
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            pipeline = ProductionPipeline(cfg, Path(directory), destination, session_id="test-session")
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_000_000_000_000)
            destination.fail = True
            with self.assertRaises(ConnectionError):
                pipeline.flush_once()
            pipeline.close()
            destination.fail = False
            destination.uncertain = True
            restarted = ProductionPipeline(cfg, Path(directory), destination, session_id="new-session")
            with self.assertRaises(ConnectionError):
                restarted.flush_once()
            restarted.flush_once()
            self.assertEqual(len(destination.rows), 4)
            self.assertEqual(restarted.pending_batches, 0)
            restarted.close()

    def test_grouped_flush_commits_four_batches_and_keeps_the_next_one(self):
        class RecordingDestination(FakeDestination):
            def __init__(self):
                super().__init__()
                self.write_sizes = []

            def write(self, rows, gaps):
                self.write_sizes.append(len(rows))
                super().write(rows, gaps)

        with tempfile.TemporaryDirectory() as directory:
            destination = RecordingDestination()
            pipeline = ProductionPipeline(configuration(), Path(directory), destination,
                                          session_id="test-session")
            self.assertIsNone(pipeline.last_delivery_ns)
            for index in range(5):
                pipeline.capture([1, 2, 3, 4],
                                 end_time_ns=1_700_000_000_000_000_000 + index * 500_000)
            self.assertTrue(pipeline.flush_batches(4))
            self.assertIsInstance(pipeline.last_delivery_ns, int)
            self.assertEqual(pipeline.last_delivery_ns,
                             json.loads((Path(directory) / 'status.json').read_text())['last_delivery_ns'])
            self.assertEqual(destination.write_sizes, [16])
            self.assertEqual(pipeline.pending_batches, 1)
            self.assertEqual(pipeline.spool.oldest()[0], "test-session:4")
            pipeline.close()

    def test_grouped_flush_replays_every_batch_after_uncertain_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            pipeline = ProductionPipeline(configuration(), Path(directory), destination,
                                          session_id="test-session")
            for index in range(3):
                pipeline.capture([1, 2, 3, 4],
                                 end_time_ns=1_700_000_000_000_000_000 + index * 500_000)
            destination.uncertain = True
            with self.assertRaises(ConnectionError):
                pipeline.flush_batches(4)
            self.assertIsNone(pipeline.last_delivery_ns)
            self.assertEqual(pipeline.pending_batches, 3)
            pipeline.flush_batches(4)
            self.assertEqual(pipeline.pending_batches, 0)
            self.assertEqual(len(destination.rows), 12)
            pipeline.close()

    def test_late_read_does_not_retimestamp_continuous_samples(self):
        cfg = configuration()
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            pipeline = ProductionPipeline(cfg, Path(directory), destination, session_id="test-session")
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_000_000_000_000)
            # A stalled reader may receive an older queued hardware batch late.
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_003_000_000_000)
            pipeline.flush_once()
            pipeline.flush_once()
            second = destination.rows[(1_700_000_000_000_500_000, "test-session:1:0")]
            self.assertEqual(second["channel"], 0)
            pipeline.close()

    def test_reliable_read_timing_slews_clock_without_backward_samples(self):
        cfg = configuration()
        anchor = 1_700_000_000_000_000_000
        frames_per_batch = 20
        dt_ns = 500_000
        with tempfile.TemporaryDirectory() as directory:
            pipeline = ProductionPipeline(cfg, Path(directory), FakeDestination())
            previous = None
            for batch in range(240):
                first_frame = batch * frames_per_batch
                last_frame = first_frame + frames_per_batch - 1
                # An exaggerated 0.5% drift makes the correction observable
                # in a short test. At batch 40 the wall clock steps back.
                drift_ns = round(last_frame * dt_ns * 0.005)
                wall_step_ns = -500_000_000 if batch >= 40 else 0
                end_ns = anchor + last_frame * dt_ns + 10_000_000 + drift_ns + wall_step_ns
                pipeline.capture([1.0] * (frames_per_batch * 4), end_ns,
                                 hardware_start_ns=anchor if batch == 0 else None,
                                 timing_reliable=True)
                if previous is not None:
                    self.assertGreater(pipeline.last_sample_ns, previous)
                previous = pipeline.last_sample_ns
            self.assertLess(abs(end_ns - pipeline.last_sample_ns - 10_000_000), 50_000_000)
            pipeline.close()

    def test_first_read_delay_uses_hardware_start_boundary(self):
        cfg = configuration()
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            pipeline = ProductionPipeline(cfg, Path(directory), destination, session_id="test-session")
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_003_000_000_000,
                             hardware_start_ns=1_700_000_000_000_000_000)
            pipeline.flush_once()
            self.assertIn((1_700_000_000_000_000_000, "test-session:0:0"), destination.rows)
            pipeline.close()

    def test_full_buffer_stops_capture_with_gap(self):
        cfg = configuration(SPOOL_MAX_BYTES=1)
        with tempfile.TemporaryDirectory() as directory:
            pipeline = ProductionPipeline(cfg, Path(directory), FakeDestination(), session_id="test-session")
            with self.assertRaises(AcquisitionFault):
                pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_000_000_000_000)
            self.assertEqual(pipeline.last_fault, "buffer_full")
            self.assertEqual(pipeline.spool.pending_gaps()[0]["cause"], "buffer_full")
            pipeline.close()

    def test_unwritable_buffer_stops_capture_with_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            pipeline = ProductionPipeline(configuration(), Path(directory), FakeDestination())
            with patch("core.production_acquisition.shutil.disk_usage",
                       return_value=SimpleNamespace(free=0)):
                with self.assertRaisesRegex(AcquisitionFault, "buffer_unwritable"):
                    pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_000_000_000_000)
            self.assertEqual(pipeline.spool.pending_gaps()[0]["cause"], "buffer_unwritable")
            pipeline.close()

    def test_incomplete_enabled_channel_is_rejected(self):
        cfg = configuration()
        cfg.channels[2].unit = ""
        with self.assertRaisesRegex(ValueError, "channel 2.*unit"):
            validate_production_config(cfg)

    def test_null_metadata_and_boolean_calibration_are_rejected(self):
        cases = (("label", None, "label"),
                 ("unit", None, "unit"),
                 ("scale.low_voltage", True, "low_voltage"))
        for key, value, error_field in cases:
            with self.subTest(key=key):
                raw = configuration().raw
                target = raw["CHANNELS"]["2"]
                if key.startswith("scale."):
                    target["scale"][key.split(".", 1)[1]] = value
                else:
                    target[key] = value
                with self.assertRaisesRegex(ValueError, f"channel 2.*{error_field}"):
                    validate_production_config(DaqNaviConfig(raw))

    def test_malformed_scale_identifies_channel(self):
        raw = configuration().raw
        raw["CHANNELS"]["2"]["scale"] = None
        with self.assertRaisesRegex(ValueError, "channel 2 scale"):
            DaqNaviConfig(raw)

    def test_finite_section_count_is_rejected_for_continuous_acquisition(self):
        with self.assertRaisesRegex(ValueError, "SECTION_COUNT"):
            validate_production_config(configuration(SECTION_COUNT=1))

    def test_fractional_rate_is_not_silently_truncated(self):
        with self.assertRaisesRegex(ValueError, "CLOCK_RATE must be an integer"):
            validate_production_config(configuration(CLOCK_RATE=1999.9))

    def test_string_false_cannot_silently_enable_a_channel(self):
        cfg = configuration()
        cfg.raw["CHANNELS"]["2"]["enabled"] = "false"
        with self.assertRaisesRegex(ValueError, "channel 2 enabled"):
            validate_production_config(cfg)

    def test_unknown_daq_signal_type_and_range_are_rejected(self):
        for setting, invalid in (("signal_type", "UnknownSignal"),
                                 ("value_range", "UnknownRange")):
            with self.subTest(setting=setting):
                raw = configuration().raw
                raw["CHANNELS"]["2"][setting] = invalid
                with self.assertRaisesRegex(ValueError, f"channel 2 {setting}"):
                    validate_production_config(DaqNaviConfig(raw))

    def test_pci_1716_rejects_unsupported_and_conflicting_signal_types(self):
        cases = (
            (0, 'PseudoDifferential', 'PseudoDifferential'),
            (1, 'Differential', 'even-numbered'),
            (0, 'Differential', 'channel 1'),
        )
        for channel_number, signal_type, message in cases:
            with self.subTest(channel=channel_number, signal_type=signal_type):
                raw = configuration().raw
                raw['DEVICE_DESCRIPTION'] = 'PCI-1716,BID#0'
                raw['CHANNELS'][str(channel_number)]['signal_type'] = signal_type
                with self.assertRaisesRegex(ValueError, message):
                    validate_production_config(DaqNaviConfig(raw, allow_env_overrides=False))

    def test_pci_1716_allows_one_differential_pair_with_unused_odd_channel(self):
        raw = configuration().raw
        raw['DEVICE_DESCRIPTION'] = 'PCI-1716,BID#0'
        raw['CHANNELS']['0']['signal_type'] = 'Differential'
        raw['CHANNELS']['1']['enabled'] = False
        self.assertEqual(validate_production_config(
            DaqNaviConfig(raw, allow_env_overrides=False)), (0, 2, 3))

    def test_hardware_adapter_applies_edited_signal_type_without_overwriting_pair(self):
        raw = configuration().raw
        raw['DEVICE_DESCRIPTION'] = 'PCI-1716,BID#0'
        raw['CHANNELS']['0']['signal_type'] = 'Differential'
        raw['CHANNELS']['1']['enabled'] = False
        raw['CHANNELS']['0']['value_range'] = 'V_Neg5To5'
        cfg = DaqNaviConfig(raw, allow_env_overrides=False)
        channels = [SimpleNamespace(signalType=None, valueRange=None) for _ in range(16)]
        device = MagicMock()
        device.channels = channels
        device.prepare.return_value = 0
        device.start.return_value = 0

        with patch.dict(sys.modules, fake_bdaq_modules(device)):
            adapter = AdvantechDaq(cfg)
            adapter.close()

        self.assertEqual(channels[0].signalType, cfg.channels[0].signal_type)
        self.assertEqual(channels[0].valueRange, cfg.channels[0].value_range)
        self.assertIsNone(channels[1].signalType)
        self.assertEqual(channels[2].signalType, cfg.channels[2].signal_type)

    def test_hardware_adapter_applies_single_ended_mode_to_both_channels(self):
        raw = configuration().raw
        raw['DEVICE_DESCRIPTION'] = 'PCI-1716,BID#0'
        cfg = DaqNaviConfig(raw, allow_env_overrides=False)
        channels = [SimpleNamespace(signalType=None, valueRange=None) for _ in range(16)]
        device = MagicMock()
        device.channels = channels
        device.prepare.return_value = 0
        device.start.return_value = 0

        with patch.dict(sys.modules, fake_bdaq_modules(device)):
            adapter = AdvantechDaq(cfg)
            adapter.close()

        self.assertEqual(channels[0].signalType, cfg.channels[0].signal_type)
        self.assertEqual(channels[1].signalType, cfg.channels[1].signal_type)

    def test_disabled_channel_is_not_persisted(self):
        raw = configuration().raw
        raw["CHANNELS"]["2"]["enabled"] = False
        cfg = DaqNaviConfig(raw)
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            pipeline = ProductionPipeline(cfg, Path(directory), destination)
            pipeline.capture([1, 2, 3, 4], end_time_ns=1_700_000_000_000_000_000)
            pipeline.flush_once()
            self.assertEqual(sorted(row["channel"] for row in destination.rows.values()), [0, 1, 3])
            pipeline.close()

    def test_hardware_fault_fails_run_and_records_gap(self):
        cfg = configuration()

        class FaultyDaq:
            def __init__(self, _cfg):
                pass

            def read(self, _count):
                raise AcquisitionFault("daq_read_failed")

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            with self.assertRaisesRegex(AcquisitionFault, "daq_read_failed"):
                run_production(cfg, daq_factory=FaultyDaq, destination=destination, spool_dir=directory)
            self.assertEqual(next(iter(destination.gaps.values()))["cause"], "daq_read_failed")

    def test_fault_gap_closes_at_first_sample_after_restart(self):
        cfg = configuration()
        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            first = ProductionPipeline(cfg, Path(directory), destination, session_id="first-session")
            first.capture([1, 2, 3, 4], end_time_ns=1_700_000_000_000_000_000)
            first.flush_once()
            with self.assertRaises(AcquisitionFault):
                first.fault("daq_read_failed", 1_700_000_000_100_000_000)
            first.close()
            second = ProductionPipeline(cfg, Path(directory), destination, session_id="second-session")
            second.capture([1, 2, 3, 4], end_time_ns=1_700_000_005_000_000_000)
            second.flush_once()
            gap = next(iter(destination.gaps.values()))
            self.assertEqual(gap["cause"], "daq_read_failed")
            self.assertEqual(gap["end_ns"], 1_700_000_005_000_000_000)
            second.close()

    def test_requested_stop_preserves_pending_batch_when_database_is_down(self):
        cfg = configuration()
        from threading import Event
        stop = Event()

        class StoppingDaq:
            def __init__(self, _cfg):
                pass

            def read(self, _count):
                stop.set()
                return [1, 2, 3, 4], 1_700_000_000_000_000_000

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            destination = FakeDestination()
            destination.fail = True
            outcome = run_production(cfg, stop_event=stop, daq_factory=StoppingDaq,
                                     destination=destination, spool_dir=directory)
            self.assertEqual(outcome["pending_batches"], 1)
            destination.fail = False
            restarted = ProductionPipeline(cfg, Path(directory), destination)
            restarted.flush_once()
            self.assertEqual(len(destination.rows), 4)
            restarted.close()


class TestTimescaleMigration(unittest.TestCase):
    def test_apply_migrations_drops_column_when_unapplied_and_records(self):
        mock_cur = MagicMock()
        mock_cur.fetchone.side_effect = [
            None,  # daq_schema_migrations does not have version
            (1,),  # information_schema.columns has calibration_revision
        ]
        cfg = configuration(DB_PRODUCTION_TABLE="daq_production_samples")
        sink = TimescaleProductionDestination(cfg)
        sink._apply_migrations(mock_cur, "daq_production_samples")

        executed = [str(call[0][0]) for call in mock_cur.execute.call_args_list]
        self.assertTrue(any("DROP COLUMN calibration_revision" in s for s in executed))
        self.assertTrue(any("INSERT INTO daq_schema_migrations" in s for s in executed))

    def test_apply_migrations_skips_destructive_changes_when_already_applied(self):
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = (1,)  # already recorded
        cfg = configuration(DB_PRODUCTION_TABLE="daq_production_samples")
        sink = TimescaleProductionDestination(cfg)
        sink._apply_migrations(mock_cur, "daq_production_samples")

        executed = [str(call[0][0]) for call in mock_cur.execute.call_args_list]
        self.assertFalse(any("DROP COLUMN" in s for s in executed))
        self.assertFalse(any("INSERT INTO daq_schema_migrations" in s for s in executed))


class TestCalibrationRevisionRetirementVerification(unittest.TestCase):
    def test_pipeline_rows_omit_calibration_revision(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = configuration()
            pipeline = ProductionPipeline(cfg, Path(d), FakeDestination())
            pipeline.capture([1.0, 2.0, 3.0, 4.0], end_time_ns=1_700_000_000_000_000_000)
            _, rows = pipeline.spool.oldest()
            self.assertGreater(len(rows), 0)
            for row in rows:
                self.assertNotIn("calibration_revision", row)
            pipeline.close()

    def test_influx_write_payload_omits_calibration_revision(self):
        cfg = configuration(DESTINATION="influxdb", INFLUX_TOKEN="test-token")
        posted_data = []

        class MockResponse:
            status = 204
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        def mock_opener(req, timeout=5):
            posted_data.append(req.data.decode("utf-8"))
            return MockResponse()

        sink = InfluxProductionDestination(cfg, opener=mock_opener)
        rows = [{
            "time_ns": 1_700_000_000_000_000_000,
            "sample_id": "test:0:0",
            "session_id": "sess-1",
            "device_id": "dev-1",
            "channel": 0,
            "sensor_name": "ch0",
            "raw_voltage": 2.5,
            "calibrated_value": 50.0,
            "unit": "kPa",
            "provenance": "physical_daq",
        }]
        sink.write(rows, [])
        self.assertEqual(len(posted_data), 1)
        self.assertNotIn("calibration_revision", posted_data[0])


if __name__ == "__main__":
    unittest.main()
