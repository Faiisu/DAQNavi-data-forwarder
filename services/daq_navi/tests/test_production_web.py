"""Public web API checks for saved production settings and control."""

import os
import json
import importlib
import sqlite3
import tempfile
import unittest
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

try:
    web = importlib.import_module('services.daq_navi.web.app')
except ModuleNotFoundError:
    web = importlib.import_module('web.app')


class ProductionWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'config.json'
        source = Path(web.SERVICE_DIR) / 'config.json'
        self.config = json.loads(source.read_text(encoding='utf-8'))
        self.config['DESTINATION'] = 'postgresql'
        self.config['SPOOL_DIR'] = self.directory.name
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        self.config_patch = patch.object(web, 'CONFIG_PATH', str(self.path))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        for name in ('PID_PATH', 'MODE_PATH', 'LOG_PATH'):
            replacement = patch.object(web, name, str(Path(self.directory.name) / name))
            replacement.start()
            self.addCleanup(replacement.stop)

        os.environ['DAQ_OPERATOR_HASH'] = web.auth.hash_password('test-pass')
        os.environ['DAQ_SESSION_KEY'] = 'test-signing-key-for-existing-suite-32b'
        self.addCleanup(lambda: os.environ.pop('DAQ_OPERATOR_HASH', None))
        self.addCleanup(lambda: os.environ.pop('DAQ_SESSION_KEY', None))
        web.configure_auth(
            users={'operator': os.environ['DAQ_OPERATOR_HASH']},
            session_key=os.environ['DAQ_SESSION_KEY'],
            ttl_seconds=3600
        )
        self.client = web.app.test_client()
        _, cookie_val = web.session_store.create_session('operator')
        self.client.set_cookie(web.COOKIE_NAME, cookie_val)

    def test_influx_samples_api_preserves_stored_metadata(self):
        config = dict(self.config, DESTINATION='influxdb', INFLUX_URL='http://influx.test:8086',
                      INFLUX_ORG='org', INFLUX_BUCKET='bucket', INFLUX_TOKEN='token',
                      INFLUX_MEASUREMENT='daq_telemetry', DEVICE_ID='test-device')
        csv_data = (',result,table,_time,device_id,channel,session_id,unit,raw_voltage,calibrated_value\n'
                    ',_result,0,2026-09-26T00:00:00Z,test-device,0,session-a,kPa,1.0,10.0\n'
                    ',_result,1,2026-09-26T00:00:00Z,test-device,0,session-b,kPa,3.0,30.0\n')
        response_mock = MagicMock()
        response_mock.__enter__.return_value.read.return_value = csv_data.encode()
        requests = []
        def capture(request, timeout):
            requests.append(request)
            return response_mock
        with patch.object(web, 'read_config', return_value=config), \
             patch('urllib.request.urlopen', side_effect=capture):
            response = self.client.get('/api/samples?channel=0')
        self.assertEqual(response.status_code, 200, response.get_json())
        points = response.get_json()['points']
        self.assertEqual([(point['unit'], point['session_id']) for point in points],
                         [('kPa', 'session-a'), ('kPa', 'session-b')])
        self.assertEqual([point['calibrated_value'] for point in points], [10.0, 30.0])
        query = requests[0].data.decode()
        self.assertIn('aggregateWindow(every: 1s', query)
        self.assertIn('r.provenance == "physical_daq"', query)
        self.assertIn('r.device_id == "test-device"', query)

    def test_destination_switch_keeps_pending_spool_for_new_destination(self):
        old = dict(self.config, DESTINATION='postgresql', SPOOL_DIR=self.directory.name)
        spool = web.DurableSpool(Path(self.directory.name), old['SPOOL_MAX_BYTES'])
        spool.append('batch', [{'sample_id': 's', 'time_ns': 100}])
        gap_id = spool.open_gap(50, 'requested_stop')
        spool.close()
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, '_test_destination', return_value='ok'), \
             patch.object(web.TimescaleProductionDestination, 'write') as old_write:
            response = self.client.post('/api/config', json={'DESTINATION': 'influxdb'})
        self.assertEqual(response.status_code, 200, response.get_json())
        old_write.assert_not_called()
        reopened = web.DurableSpool(Path(self.directory.name), old['SPOOL_MAX_BYTES'])
        self.assertEqual(reopened.pending_batches, 1)
        self.assertEqual(reopened.oldest()[1][0]['sample_id'], 's')
        self.assertEqual(reopened.pending_gaps()[0]['gap_id'], gap_id)
        reopened.close()

    def test_destination_switch_saves_then_restarts_running_acquisition(self):
        with patch.object(web, 'get_running_process', side_effect=[(123, 'production'), (None, None)]), \
             patch.object(web, '_test_destination', return_value='ok'), \
             patch.object(web, 'stop_acquisition', return_value={'stopped': True}), \
             patch.object(web, 'start_acquisition', return_value={'started': True}) as start:
            response = self.client.post('/api/config', json={'DESTINATION': 'influxdb'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(self.client.get('/api/config').get_json()['DESTINATION'], 'influxdb')
        start.assert_called_once_with('production')

    def test_same_backend_target_change_saves_with_pending_records(self):
        spool = web.DurableSpool(Path(self.directory.name), self.config['SPOOL_MAX_BYTES'])
        spool.append('pending', [{'sample_id': 's', 'time_ns': 100}])
        spool.close()
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, '_test_destination', return_value='ok'), \
             patch.object(web, 'effective_timescale_retention', return_value='30 days'), \
             patch.object(web.TimescaleProductionDestination, 'write') as old_write:
            response = self.client.post('/api/config', json={'DB_DSN': 'postgresql://other-host/testdb'})
        self.assertEqual(response.status_code, 200, response.get_json())
        old_write.assert_not_called()
        spool = web.DurableSpool(Path(self.directory.name), self.config['SPOOL_MAX_BYTES'])
        self.assertEqual(spool.pending_batches, 1)
        spool.close()

        influx_config = dict(self.config, DESTINATION='influxdb', INFLUX_URL='http://influx.test:8086',
                             INFLUX_ORG='org', INFLUX_BUCKET='bucket', INFLUX_TOKEN='old-token',
                             INFLUX_MEASUREMENT='daq_telemetry')
        self.path.write_text(json.dumps(influx_config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.InfluxProductionDestination, 'write') as old_write:
            rotated = self.client.post('/api/config', json={'INFLUX_TOKEN': 'new-token'})
        self.assertEqual(rotated.status_code, 200, rotated.get_json())
        old_write.assert_not_called()

    def test_config_rejects_spool_directory_change(self):
        original = self.path.read_text(encoding='utf-8')
        response = self.client.post('/api/config', json={'SPOOL_DIR': str(Path(self.directory.name) / 'new-spool')})
        self.assertEqual(response.status_code, 400)
        self.assertIn('SPOOL_DIR changes are unsupported', response.get_json()['message'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), original)

    def test_switch_uses_saved_config_even_when_spool_has_old_owner_metadata(self):
        old = dict(self.config, DESTINATION='influxdb', SPOOL_DIR=self.directory.name,
                   INFLUX_URL='http://influx.test:8086', INFLUX_ORG='org',
                   INFLUX_BUCKET='current-bucket', INFLUX_TOKEN='token',
                   INFLUX_MEASUREMENT='daq_telemetry')
        changed = dict(old, INFLUX_BUCKET='edited-out-of-band')
        spool = web.DurableSpool(Path(self.directory.name), old['SPOOL_MAX_BYTES'])
        spool.set_state('destination', 'influxdb')
        spool.set_state('destination_identity', web.destination_identity(
            web.DaqNaviConfig(old, allow_env_overrides=False)))
        spool.append('pending', [{'sample_id': 's', 'time_ns': 100}])
        spool.close()
        self.path.write_text(json.dumps(changed), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, '_test_destination', return_value='ok'), \
             patch.object(web.InfluxProductionDestination, 'write') as old_write:
            response = self.client.post('/api/config', json={'INFLUX_BUCKET': 'next-bucket'})
        self.assertEqual(response.status_code, 200, response.get_json())
        old_write.assert_not_called()
        reopened = web.DurableSpool(Path(self.directory.name), old['SPOOL_MAX_BYTES'])
        self.assertEqual(reopened.pending_batches, 1)
        reopened.close()

    def test_partial_channel_save_preserves_other_settings_and_zero(self):
        with patch.object(web, 'get_running_process', return_value=(None, None)):
            response = self.client.post('/api/config', json={
                'CHANNELS': {'0': {'scale': {'low_value': 0.0}}},
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = self.client.get('/api/config').get_json()
        self.assertEqual(saved['CHANNELS']['0']['scale']['low_value'], 0.0)
        disk_config = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(disk_config['DB_DSN'], self.config['DB_DSN'])
        self.assertEqual(saved['DB_DSN'], web.auth.redact_dsn_password(self.config['DB_DSN']))

    def test_invalid_enabled_channel_is_rejected_without_saving(self):
        before = self.path.read_text(encoding='utf-8')
        response = self.client.post('/api/config', json={
            'CHANNELS': {'0': {'unit': ''}},
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('unit', response.get_json()['message'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), before)

    def test_enabled_channel_outside_span_is_rejected(self):
        outside = str(self.config.get('START_CHANNEL', 0) + self.config.get('CHANNEL_COUNT', 4))
        response = self.client.post('/api/config', json={'CHANNELS': {outside: {'enabled': True}}})
        self.assertEqual(response.status_code, 400)
        self.assertIn('outside', response.get_json()['message'])

    def test_retention_save_applies_policy_and_reports_failure(self):
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.TimescaleProductionDestination, 'ensure_schema') as apply, \
             patch.object(web, 'effective_timescale_retention', return_value='14 days'):
            response = self.client.post('/api/config', json={'DB_RETENTION_DAYS': 14})
        self.assertEqual(response.status_code, 200, response.get_json())
        apply.assert_called_once()
        self.assertEqual(response.get_json()['retention_days'], 14)
        with patch.object(web.TimescaleProductionDestination, 'ensure_schema',
                          side_effect=OSError('database offline')):
            failed = self.client.post('/api/config', json={'DB_RETENTION_DAYS': 7})
        self.assertEqual(failed.status_code, 503)
        self.assertEqual(self.client.get('/api/config').get_json()['DB_RETENTION_DAYS'], 14)

    def test_retention_save_fresh_policy_applies_and_preserves_other_settings(self):
        del self.config['DB_RETENTION_DAYS']
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.TimescaleProductionDestination, 'ensure_schema') as apply, \
             patch.object(web, 'effective_timescale_retention', return_value='30 days'):
            response = self.client.post('/api/config', json={'DB_RETENTION_DAYS': 30})
        self.assertEqual(response.status_code, 200, response.get_json())
        apply.assert_called_once()
        self.assertEqual(response.get_json()['retention_days'], 30)
        saved = self.client.get('/api/config').get_json()
        self.assertEqual(saved['DB_RETENTION_DAYS'], 30)
        self.assertEqual(saved['CLOCK_RATE'], self.config['CLOCK_RATE'])
        self.assertEqual(saved['CHANNELS']['0'], self.config['CHANNELS']['0'])

    def test_retention_save_changing_existing_policy_updates_timescaledb(self):
        self.config['DB_RETENTION_DAYS'] = 30
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.TimescaleProductionDestination, 'ensure_schema') as apply, \
             patch.object(web, 'effective_timescale_retention', return_value='60 days'):
            response = self.client.post('/api/config', json={'DB_RETENTION_DAYS': 60})
        self.assertEqual(response.status_code, 200, response.get_json())
        apply.assert_called_once()
        self.assertEqual(response.get_json()['retention_days'], 60)
        saved = self.client.get('/api/config').get_json()
        self.assertEqual(saved['DB_RETENTION_DAYS'], 60)

    def test_retention_save_unchanged_does_not_reapply_policy(self):
        self.config['DB_RETENTION_DAYS'] = 30
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.TimescaleProductionDestination, 'ensure_schema') as apply:
            response = self.client.post('/api/config', json={'CLOCK_RATE': 1500})
        self.assertEqual(response.status_code, 200, response.get_json())
        apply.assert_not_called()
        self.assertEqual(response.get_json()['config']['DB_RETENTION_DAYS'], 30)

    def test_retention_validation_rejects_non_positive_integers(self):
        invalid_values = [0, -1, -30, "thirty", 14.5, True, False, None, []]
        for val in invalid_values:
            with self.subTest(val=val):
                before = self.path.read_text(encoding='utf-8')
                response = self.client.post('/api/config', json={'DB_RETENTION_DAYS': val})
                self.assertEqual(response.status_code, 400, f"Expected 400 for {val}")
                self.assertEqual(self.path.read_text(encoding='utf-8'), before)

    def test_retention_database_error_reports_503_and_prevents_save(self):
        self.config['DB_RETENTION_DAYS'] = 30
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.TimescaleProductionDestination, 'ensure_schema',
                          side_effect=Exception('TimescaleDB connection refused')):
            response = self.client.post('/api/config', json={'DB_RETENTION_DAYS': 45})
        self.assertEqual(response.status_code, 503)
        self.assertIn('compensation failed', response.get_json()['message'])
        self.assertEqual(self.client.get('/api/config').get_json()['DB_RETENTION_DAYS'], 30)

    def test_get_retention_effective_policy(self):
        mock_cursor = MagicMock()
        mock_cursor.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = ('30 days',)
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        with patch('psycopg2.connect', return_value=mock_conn):
            response = self.client.get('/api/retention')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['saved_days'], 30)
        self.assertEqual(data['effective'], '30 days')

    def test_get_retention_missing_policy_reports_503(self):
        mock_cursor = MagicMock()
        mock_cursor.__enter__.return_value = mock_cursor
        mock_cursor.fetchone.return_value = None
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        with patch('psycopg2.connect', return_value=mock_conn):
            response = self.client.get('/api/retention')
        self.assertEqual(response.status_code, 503)
        data = response.get_json()
        self.assertEqual(data['saved_days'], 30)
        self.assertIn('No active production retention policy', data['message'])

    def test_get_retention_database_error_reports_503(self):
        with patch('psycopg2.connect', side_effect=Exception('connection timed out')):
            response = self.client.get('/api/retention')
        self.assertEqual(response.status_code, 503)
        data = response.get_json()
        self.assertIn('Could not read retention policy', data['message'])

    def test_start_validates_production_channels(self):
        self.config['CHANNELS']['0']['label'] = ''
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)):
            result = self.client.post('/api/start', json={'mode': 'production'})
        self.assertEqual(result.status_code, 400)
        self.assertIn('label', result.get_json()['message'])

    def test_stop_reports_pending_replay(self):
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'terminate_pid', return_value=True), \
             patch.object(web, 'read_runtime_status', return_value={'pending_batches': 2}):
            result = self.client.post('/api/stop')
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()['pending_replay'])

    def test_health_fails_when_auto_start_expects_acquisition(self):
        self.config['AUTO_START_ON_STARTUP'] = True
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={}):
            result = self.client.get('/api/health')
        self.assertEqual(result.status_code, 503)
        self.assertFalse(result.get_json()['healthy'])

    def test_failed_child_remains_faulted_and_unhealthy(self):
        Path(web.PID_PATH).write_text('999999', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={'last_fault': 'daq_open_failed'}):
            result = self.client.get('/api/health')
        self.assertEqual(result.status_code, 503)
        self.assertEqual(result.get_json()['status'], 'faulted')
        self.assertEqual(result.get_json()['fault'], 'daq_open_failed')

    def test_status_exposes_recorded_acquisition_gap(self):
        spool = Path(self.directory.name) / 'production-spool.sqlite3'
        with sqlite3.connect(spool) as conn:
            conn.execute('CREATE TABLE gaps (start_ns INTEGER, end_ns INTEGER, cause TEXT)')
            conn.execute('INSERT INTO gaps VALUES (1000000000, 2000000000, ?)', ('daq_read_failed',))
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={}):
            status = self.client.get('/api/status').get_json()
        self.assertEqual(status['gaps'][0]['cause'], 'daq_read_failed')

    def test_save_running_session_drains_then_restarts_saved_mode(self):
        calls = []
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'stop_acquisition', side_effect=lambda manual: calls.append('stop') or {'stopped': True}), \
             patch.object(web, 'start_acquisition', side_effect=lambda mode: calls.append(mode) or {'started': True}):
            response = self.client.post('/api/config', json={'CLOCK_RATE': 1500})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(calls, ['stop', 'production'])
        self.assertEqual(self.client.get('/api/config').get_json()['CLOCK_RATE'], 1500)

    def test_reboot_uses_saved_auto_start_even_after_manual_stop(self):
        self.config['AUTO_START_ON_STARTUP'] = True
        self.config['AUTO_START_MODE'] = 'production'
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'start_acquisition', return_value={'started': True}) as start:
            web.init_application()
        start.assert_called_once_with('production')

    def test_invalid_pci_signal_edit_does_not_save_or_restart_running_daq(self):
        original = self.path.read_text(encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'stop_acquisition') as stop:
            response = self.client.post('/api/config', json={
                'CHANNELS': {'0': {'signal_type': 'PseudoDifferential'}},
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn('PseudoDifferential', response.get_json()['message'])
        self.assertEqual(self.path.read_text(encoding='utf-8'), original)
        stop.assert_not_called()

    def test_full_round_trip_channel_editing_and_production_start_validation(self):
        updated_payload = {
            'CLOCK_RATE': 1500,
            'CHANNELS': {
                '1': {'enabled': False},
                '0': {
                    'enabled': True,
                    'label': 'bearing-vibration',
                    'unit': 'mm/s',
                    'signal_type': 'Differential',
                    'value_range': 'V_Neg5To5',
                    'scale': {
                        'enabled': True,
                        'low_voltage': -5.0,
                        'high_voltage': 5.0,
                        'low_value': 0.0,
                        'high_value': 250.0,
                        'revision': 'rev-2026-vibe',
                    },
                },
            },
        }
        with patch.object(web, 'get_running_process', return_value=(None, None)):
            save_response = self.client.post('/api/config', json=updated_payload)
        self.assertEqual(save_response.status_code, 200, save_response.get_json())

        # Verify round-trip readback via GET /api/config
        saved = self.client.get('/api/config').get_json()
        self.assertEqual(saved['CLOCK_RATE'], 1500)
        ch0 = saved['CHANNELS']['0']
        self.assertTrue(ch0['enabled'])
        self.assertEqual(ch0['label'], 'bearing-vibration')
        self.assertEqual(ch0['unit'], 'mm/s')
        self.assertEqual(ch0['signal_type'], 'Differential')
        self.assertEqual(ch0['value_range'], 'V_Neg5To5')
        self.assertTrue(ch0['scale']['enabled'])
        self.assertEqual(ch0['scale']['low_voltage'], -5.0)
        self.assertEqual(ch0['scale']['high_voltage'], 5.0)
        self.assertEqual(ch0['scale']['low_value'], 0.0)
        self.assertEqual(ch0['scale']['high_value'], 250.0)
        self.assertEqual(ch0['scale']['revision'], 'rev-2026-vibe')

        # Verify production start validation passes when configured properly
        mock_proc = MagicMock()
        mock_proc.pid = 4321
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.subprocess, 'Popen', return_value=mock_proc), \
             patch.object(web, 'start_tailing'):
            start_response = self.client.post('/api/start', json={'mode': 'production'})
        self.assertEqual(start_response.status_code, 200, start_response.get_json())
        self.assertTrue(start_response.get_json()['started'])
        self.assertEqual(start_response.get_json()['mode'], 'production')
        self.assertEqual(start_response.get_json()['pid'], 4321)

    def test_start_already_running_rejected(self):
        with patch.object(web, 'get_running_process', return_value=(123, 'production')):
            result = self.client.post('/api/start', json={'mode': 'production'})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.get_json()['started'])
        self.assertIn('already running', result.get_json()['message'])

    def test_start_invalid_mode_rejected(self):
        with patch.object(web, 'get_running_process', return_value=(None, None)):
            result = self.client.post('/api/start', json={'mode': 'invalid_mode'})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(result.get_json()['started'])
        self.assertIn('mode must be production or mockup', result.get_json()['message'])

    def test_start_rejected_while_buffer_clear_is_running(self):
        job = Path(self.directory.name) / 'buffer-clear-job.json'
        job.write_text(json.dumps({'state': 'starting', 'checked_at_ns': time.time_ns()}))
        with patch.object(web, 'get_running_process', return_value=(None, None)):
            response = self.client.post('/api/start', json={'mode': 'production'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('clearing', response.get_json()['message'])

    def test_start_explicit_mockup_launches_mockup_script(self):
        mock_proc = MagicMock()
        mock_proc.pid = 5555
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.subprocess, 'Popen', return_value=mock_proc) as mock_popen, \
             patch.object(web, 'start_tailing'):
            res = self.client.post('/api/start', json={'mode': 'mockup'})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['started'])
        self.assertEqual(res.get_json()['mode'], 'mockup')
        # Ensure mockup script was targeted
        popen_args = mock_popen.call_args[0][0]
        self.assertTrue(any('mockup_stream_to_db.py' in str(arg) for arg in popen_args))

    def test_start_default_mode_uses_saved_configuration(self):
        mock_proc = MagicMock()
        mock_proc.pid = 6666
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.subprocess, 'Popen', return_value=mock_proc) as mock_popen, \
             patch.object(web, 'start_tailing'):
            res = self.client.post('/api/start', json={})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['started'])
        self.assertEqual(res.get_json()['mode'], 'production')
        popen_args = mock_popen.call_args[0][0]
        self.assertTrue(any('buffered_daq_to_timescaledb.py' in str(arg) for arg in popen_args))

    def test_stop_when_not_running(self):
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={'pending_batches': 0}):
            result = self.client.post('/api/stop')
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.get_json()['stopped'])
        self.assertTrue(result.get_json()['drained'])
        self.assertFalse(result.get_json()['pending_replay'])

    def test_stop_drain_timeout_reports_503(self):
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'terminate_pid', return_value=False):
            result = self.client.post('/api/stop')
        self.assertEqual(result.status_code, 503)
        self.assertFalse(result.get_json()['stopped'])
        self.assertIn('Drain timeout', result.get_json()['message'])

    def test_clear_buffer_requires_confirmation_and_stopped_acquisition(self):
        spool = web.DurableSpool(Path(self.directory.name), 4096)
        spool.append('batch-1', [{'time_ns': 1_000_000_000}])
        spool.close()
        with patch.object(web, 'get_running_process', return_value=(123, 'production')):
            active = self.client.post('/api/buffer/clear', json={'confirm': 'CLEAR BUFFER'})
        self.assertEqual(active.status_code, 409)
        with patch.object(web, 'get_running_process', return_value=(None, None)):
            unconfirmed = self.client.post('/api/buffer/clear', json={})
        self.assertEqual(unconfirmed.status_code, 400)
        reopened = web.DurableSpool(Path(self.directory.name), 4096)
        self.assertEqual(reopened.pending_batches, 1)
        reopened.close()

    def test_clear_buffer_discards_pending_batches_and_updates_status(self):
        spool = web.DurableSpool(Path(self.directory.name), 4096)
        spool.append('batch-1', [{'time_ns': 1_000_000_000}])
        spool.close()
        with patch.object(web, 'get_running_process', return_value=(None, None)):
            response = self.client.post('/api/buffer/clear',
                                        json={'confirm': 'CLEAR BUFFER'})
        self.assertEqual(response.status_code, 202, response.get_json())
        self.assertEqual(self.client.get('/api/status').status_code, 200)
        for _ in range(100):
            result = self.client.get('/api/buffer/clear').get_json()
            if result['state'] in ('complete', 'failed'):
                break
            time.sleep(0.05)
        self.assertEqual(result['state'], 'complete', result)
        self.assertEqual(result['cleared_batches'], 1)
        self.assertEqual(result['pending_batches'], 0)
        self.assertEqual(web.read_runtime_status(self.config)['pending_batches'], 0)
        reopened = web.DurableSpool(Path(self.directory.name), 4096)
        self.assertEqual(reopened.pending_batches, 0)
        self.assertEqual(reopened.pending_gaps()[0]['cause'], 'operator_cleared_buffer')
        reopened.close()

    def test_clear_buffer_rejects_locked_spool(self):
        spool = web.DurableSpool(Path(self.directory.name), 4096)
        try:
            spool.append('batch-1', [{'time_ns': 1_000_000_000}])
            with patch.object(web, 'get_running_process', return_value=(None, None)):
                response = self.client.post('/api/buffer/clear',
                                            json={'confirm': 'CLEAR BUFFER'})
            self.assertEqual(response.status_code, 409)
            self.assertEqual(spool.pending_batches, 1)
        finally:
            spool.close()

    def test_save_running_session_with_pending_replay_reports_not_drained(self):
        calls = []
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'stop_acquisition', side_effect=lambda manual: calls.append('stop') or {
                 'stopped': True, 'drained': False, 'pending_replay': True, 'pending_batches': 4
             }), \
             patch.object(web, 'start_acquisition', side_effect=lambda mode: calls.append(mode) or {'started': True}):
            response = self.client.post('/api/config', json={'CLOCK_RATE': 1500})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, ['stop', 'production'])
        json_data = response.get_json()
        self.assertFalse(json_data['drained'])
        self.assertTrue(json_data['pending_replay'])
        self.assertEqual(json_data['pending_batches'], 4)

    def test_save_running_session_stop_timeout_aborts_without_restart(self):
        calls = []
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'stop_acquisition', return_value={'stopped': False, 'message': 'Drain timeout'}), \
             patch.object(web, 'start_acquisition', side_effect=lambda mode: calls.append(mode) or {'started': True}):
            response = self.client.post('/api/config', json={'CLOCK_RATE': 1500})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(calls, [])  # start_acquisition must NOT be called

    def test_stale_pid_process_exit_reports_faulted_without_mockup_fallback(self):
        Path(web.PID_PATH).write_text('999999', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={}):
            status = self.client.get('/api/status').get_json()
        self.assertFalse(status['is_running'])
        self.assertEqual(status['status'], 'faulted')
        self.assertEqual(status['fault'], 'acquisition_process_exited')
        self.assertEqual(status['mode'], 'production')
        self.assertNotEqual(status['mode'], 'mockup')
        self.assertFalse(status['healthy'])

    def test_handle_connect_in_faulted_state_preserves_configured_mode(self):
        Path(web.PID_PATH).write_text('999999', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        emitted = []
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'emit', side_effect=lambda event, data: emitted.append((event, data))):
            web.handle_connect()
        status_change = next(data for event, data in emitted if event == 'status_change')
        self.assertFalse(status_change['is_running'])
        self.assertEqual(status_change['mode'], 'production')
        self.assertNotEqual(status_change['mode'], 'mockup')

    def test_restart_after_process_failure_recovers_saved_mode(self):
        Path(web.PID_PATH).write_text('999999', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        mock_proc = MagicMock()
        mock_proc.pid = 7777
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.subprocess, 'Popen', return_value=mock_proc) as mock_popen, \
             patch.object(web, 'start_tailing'):
            res = self.client.post('/api/start', json={'mode': 'production'})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['started'])
        self.assertEqual(res.get_json()['mode'], 'production')
        popen_args = mock_popen.call_args[0][0]
        self.assertTrue(any('buffered_daq_to_timescaledb.py' in str(arg) for arg in popen_args))
        self.assertEqual(Path(web.PID_PATH).read_text(encoding='utf-8'), '7777')
        self.assertEqual(Path(web.MODE_PATH).read_text(encoding='utf-8'), 'production')

    def test_reboot_auto_start_after_manual_stop_lifecycle(self):
        # 1. Configure auto-start
        self.config['AUTO_START_ON_STARTUP'] = True
        self.config['AUTO_START_MODE'] = 'production'
        self.path.write_text(json.dumps(self.config), encoding='utf-8')

        # 2. Simulate process running
        Path(web.PID_PATH).write_text('1234', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')

        # 3. Manual Stop by operator
        with patch.object(web, 'get_running_process', return_value=(1234, 'production')), \
             patch.object(web, 'terminate_pid', return_value=True), \
             patch.object(web, 'read_runtime_status', return_value={'pending_batches': 0}):
            stop_res = self.client.post('/api/stop')
        self.assertEqual(stop_res.status_code, 200)
        self.assertTrue(stop_res.get_json()['stopped'])
        self.assertFalse(Path(web.PID_PATH).exists())

        # Verify configuration was NOT modified by manual stop
        saved_cfg = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertTrue(saved_cfg['AUTO_START_ON_STARTUP'])

        # 4. Simulate reboot - init_application runs with process not running
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'start_acquisition', return_value={'started': True}) as mock_start:
            web.init_application()
        mock_start.assert_called_once_with('production')

    def test_reboot_auto_start_disabled_does_not_start(self):
        self.config['AUTO_START_ON_STARTUP'] = False
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'start_acquisition') as mock_start:
            web.init_application()
        mock_start.assert_not_called()

    def test_stop_reports_drained_and_pending_replay(self):
        # Case A: 0 pending batches -> drained=True, pending_replay=False
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'terminate_pid', return_value=True), \
             patch.object(web, 'read_runtime_status', return_value={'pending_batches': 0}):
            res1 = self.client.post('/api/stop')
        self.assertEqual(res1.status_code, 200)
        self.assertTrue(res1.get_json()['drained'])
        self.assertFalse(res1.get_json()['pending_replay'])
        self.assertEqual(res1.get_json()['pending_batches'], 0)

        # Case B: 3 pending batches -> drained=False, pending_replay=True
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'terminate_pid', return_value=True), \
             patch.object(web, 'read_runtime_status', return_value={'pending_batches': 3}):
            res2 = self.client.post('/api/stop')
        self.assertEqual(res2.status_code, 200)
        self.assertFalse(res2.get_json()['drained'])
        self.assertTrue(res2.get_json()['pending_replay'])

    def test_healthy_acquisition_state_in_status_and_health(self):
        Path(web.PID_PATH).write_text('1234', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        runtime_data = {
            'state': 'running',
            'checked_at_ns': time.time_ns(),
            'last_fault': None,
            'last_writer_error': None,
            'pending_batches': 0,
            'pending_bytes': 0,
            'spool_bytes': 2048,
            'last_sample_ns': 1700000000000000000,
        }
        with patch.object(web, 'get_running_process', return_value=(1234, 'production')), \
             patch.object(web, 'read_runtime_status', return_value=runtime_data):
            status_res = self.client.get('/api/status')
            health_res = self.client.get('/api/health')

        self.assertEqual(status_res.status_code, 200)
        status = status_res.get_json()
        self.assertTrue(status['is_running'])
        self.assertEqual(status['status'], 'running')
        self.assertTrue(status['healthy'])
        self.assertEqual(status['mode'], 'production')
        self.assertEqual(status['pid'], 1234)
        self.assertEqual(status['pending_batches'], 0)
        self.assertEqual(status['spool_bytes'], 2048)
        self.assertIsNone(status['fault'])
        self.assertIsNone(status['writer_error'])

        self.assertEqual(health_res.status_code, 200)
        health = health_res.get_json()
        self.assertTrue(health['healthy'])
        self.assertEqual(health['status'], 'running')

    def test_database_outage_with_buffering_reports_unhealthy_and_buffer_stats(self):
        Path(web.PID_PATH).write_text('1234', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        runtime_data = {
            'state': 'running',
            'checked_at_ns': time.time_ns(),
            'last_fault': None,
            'last_writer_error': 'database connection refused',
            'pending_batches': 42,
            'pending_bytes': 420000,
            'spool_bytes': 5242880,
            'last_sample_ns': 1700000000000000000,
        }
        with patch.object(web, 'get_running_process', return_value=(1234, 'production')), \
             patch.object(web, 'read_runtime_status', return_value=runtime_data):
            status_res = self.client.get('/api/status')
            health_res = self.client.get('/api/health')

        self.assertEqual(status_res.status_code, 200)
        status = status_res.get_json()
        self.assertTrue(status['is_running'])
        self.assertEqual(status['status'], 'buffering')
        self.assertFalse(status['healthy'])
        self.assertEqual(status['pending_batches'], 42)
        self.assertEqual(status['pending_bytes'], 420000)
        self.assertEqual(status['spool_bytes'], 5242880)
        self.assertEqual(status['writer_error'], 'database connection refused')

        self.assertEqual(health_res.status_code, 503)
        health = health_res.get_json()
        self.assertFalse(health['healthy'])
        self.assertEqual(health['status'], 'buffering')
        self.assertEqual(health['writer_error'], 'database connection refused')
        self.assertEqual(health['pending_batches'], 42)
        self.assertEqual(health['spool_bytes'], 5242880)

    def test_stopped_acquisition_healthy_when_not_expected(self):
        self.config['AUTO_START_ON_STARTUP'] = False
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={'state': 'stopped', 'pending_batches': 0}):
            status_res = self.client.get('/api/status')
            health_res = self.client.get('/api/health')

        self.assertEqual(status_res.status_code, 200)
        status = status_res.get_json()
        self.assertFalse(status['is_running'])
        self.assertEqual(status['status'], 'stopped')
        self.assertFalse(status['expected_running'])
        self.assertTrue(status['healthy'])

        self.assertEqual(health_res.status_code, 200)
        health = health_res.get_json()
        self.assertTrue(health['healthy'])

    def test_stopped_acquisition_ignores_previous_writer_error(self):
        self.config['AUTO_START_ON_STARTUP'] = False
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={
                 'state': 'stopped', 'pending_batches': 42,
                 'last_writer_error': 'previous statement timeout',
             }):
            status_response = self.client.get('/api/status')
            health_response = self.client.get('/api/health')
        status = status_response.get_json()
        self.assertEqual(status['status'], 'stopped')
        self.assertEqual(status['pending_batches'], 42)
        self.assertIsNone(status['writer_error'])
        self.assertTrue(status['healthy'])
        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(health_response.get_json()['status'], 'stopped')

    def test_stopped_acquisition_unhealthy_when_expected(self):
        self.config['AUTO_START_ON_STARTUP'] = True
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={'state': 'stopped'}):
            status_res = self.client.get('/api/status')
            health_res = self.client.get('/api/health')

        self.assertEqual(status_res.status_code, 200)
        status = status_res.get_json()
        self.assertFalse(status['is_running'])
        self.assertEqual(status['status'], 'stopped')
        self.assertTrue(status['expected_running'])
        self.assertFalse(status['healthy'])

        self.assertEqual(health_res.status_code, 503)
        health = health_res.get_json()
        self.assertFalse(health['healthy'])
        self.assertEqual(health['status'], 'stopped')

    def test_recorded_acquisition_gap_exposure_in_status_and_samples(self):
        spool = Path(self.directory.name) / 'production-spool.sqlite3'
        with sqlite3.connect(spool) as conn:
            conn.execute('CREATE TABLE gaps (start_ns INTEGER, end_ns INTEGER, cause TEXT)')
            conn.execute('INSERT INTO gaps VALUES (1000000000, 2000000000, ?)', ('buffer_overflow_dropped_samples',))
            conn.execute('INSERT INTO gaps VALUES (3000000000, 4000000000, ?)', ('daq_read_timeout',))

        # Check in /api/status
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, 'read_runtime_status', return_value={}):
            status = self.client.get('/api/status').get_json()

        self.assertEqual(len(status['gaps']), 2)
        self.assertEqual(status['gaps'][0]['cause'], 'daq_read_timeout')
        self.assertEqual(status['gaps'][0]['start_ns'], 3000000000)
        self.assertEqual(status['gaps'][0]['end_ns'], 4000000000)
        self.assertEqual(status['gaps'][1]['cause'], 'buffer_overflow_dropped_samples')

        # Check in /api/samples
        mock_cursor = MagicMock()
        mock_cursor.__enter__.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor
        with patch('psycopg2.connect', return_value=mock_conn):
            samples_res = self.client.get('/api/samples?channel=0')

        self.assertEqual(samples_res.status_code, 200)
        samples = samples_res.get_json()
        self.assertEqual(samples['channel'], 0)
        self.assertEqual(len(samples['gaps']), 2)
        self.assertEqual(samples['gaps'][0]['cause'], 'daq_read_timeout')
        self.assertEqual(samples['gaps'][1]['cause'], 'buffer_overflow_dropped_samples')

    def test_samples_query_associates_unit(self):
        t1 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 9, 24, 12, 0, 1, tzinfo=timezone.utc)
        mock_rows = [
            (t1, 1.25, 25.0, 'bar'),
            (t2, 2.50, 50.0, 'bar'),
        ]
        mock_cursor = MagicMock()
        mock_cursor.__enter__.return_value = mock_cursor
        mock_cursor.fetchall.return_value = mock_rows
        mock_conn = MagicMock()
        mock_conn.__enter__.return_value = mock_conn
        mock_conn.cursor.return_value = mock_cursor

        config = dict(self.config, DESTINATION='postgresql')
        with patch.object(web, 'read_config', return_value=config), \
             patch('psycopg2.connect', return_value=mock_conn):
            res = self.client.get('/api/samples?channel=1')

        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data['channel'], 1)
        points = data['points']
        self.assertEqual(len(points), 2)
        self.assertEqual(points[0]['time'], t1.isoformat())
        self.assertEqual(points[0]['raw_voltage'], 1.25)
        self.assertEqual(points[0]['calibrated_value'], 25.0)
        self.assertEqual(points[0]['unit'], 'bar')

        self.assertEqual(points[1]['time'], t2.isoformat())
        self.assertEqual(points[1]['raw_voltage'], 2.50)
        self.assertEqual(points[1]['calibrated_value'], 50.0)
        self.assertEqual(points[1]['unit'], 'bar')

    def test_samples_channel_validation_and_db_failure(self):
        # Invalid channel arguments
        res1 = self.client.get('/api/samples?channel=-1')
        self.assertEqual(res1.status_code, 400)
        self.assertIn('channel must be 0–15', res1.get_json()['message'])

        res2 = self.client.get('/api/samples?channel=16')
        self.assertEqual(res2.status_code, 400)
        self.assertIn('channel must be 0–15', res2.get_json()['message'])

        res3 = self.client.get('/api/samples?channel=invalid')
        self.assertEqual(res3.status_code, 400)

        # Database failure returns 503
        with patch('psycopg2.connect', side_effect=Exception('connection timed out')):
            res_fail = self.client.get('/api/samples?channel=0')
        self.assertEqual(res_fail.status_code, 503)
        self.assertIn('Production samples unavailable', res_fail.get_json()['message'])
if __name__ == '__main__':
    unittest.main()
