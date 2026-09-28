"""Acquisition process transitions and reported runtime state."""

import importlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


web = importlib.import_module('web.app')


class AcquisitionLifecycleTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config = json.loads((Path(web.SERVICE_DIR) / 'config.json').read_text(encoding='utf-8'))
        config['SPOOL_DIR'] = str(root)
        config['AUTO_START_ON_STARTUP'] = True
        config_path = root / 'config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        for name, value in [('CONFIG_PATH', config_path), ('PID_PATH', root / 'pid'),
                            ('MODE_PATH', root / 'mode'), ('LOG_PATH', root / 'log')]:
            replacement = patch.object(web, name, str(value))
            replacement.start()
            self.addCleanup(replacement.stop)
        no_auth = patch.object(web, 'session_store', None)
        no_auth.start()
        self.addCleanup(no_auth.stop)
        self.client = web.app.test_client()

    def test_socket_start_waits_for_config_transition(self):
        entered = threading.Event()
        finished = threading.Event()
        result = []

        def start(_mode):
            entered.set()
            return {'started': True}

        def invoke():
            try:
                with web.app.test_request_context('/'):
                    web.handle_start({'mode': 'production'})
            finally:
                finished.set()

        with patch.object(web, 'start_acquisition', side_effect=start):
            with web.CONFIG_TRANSITION_LOCK:
                worker = threading.Thread(target=invoke, daemon=True)
                worker.start()
                self.assertFalse(entered.wait(0.05), 'socket start bypassed the config transition lock')
            self.assertTrue(finished.wait(2), 'socket start never resumed after the lock was released')
        self.assertTrue(entered.is_set())

    def test_public_transitions_wait_for_config_commit(self):
        for operation, argument in ((web.start_acquisition, 'production'),
                                    (web.stop_acquisition, True)):
            with self.subTest(operation=operation.__name__):
                entered = threading.Event()
                finished = threading.Event()

                def observe_process():
                    entered.set()
                    return (1234, 'production')

                def invoke():
                    try:
                        operation(argument)
                    finally:
                        finished.set()

                with patch.object(web, 'get_running_process', side_effect=observe_process), \
                     patch.object(web, 'terminate_pid', return_value=False):
                    with web.CONFIG_TRANSITION_LOCK:
                        threading.Thread(target=invoke, daemon=True).start()
                        self.assertFalse(entered.wait(0.05))
                    self.assertTrue(finished.wait(2))
                self.assertTrue(entered.is_set())

    def test_old_snapshot_is_not_reported_for_new_process(self):
        Path(web.PID_PATH).write_text('1234', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        launch_ns = Path(web.PID_PATH).stat().st_mtime_ns
        old_runtime = {'state': 'running', 'checked_at_ns': launch_ns - 1,
                       'last_fault': 'previous_session_fault', 'last_writer_error': 'old outage'}
        with patch.object(web, 'get_running_process', return_value=(1234, 'production')), \
             patch.object(web, 'read_runtime_status', return_value=old_runtime):
            response = self.client.get('/api/status')
        status = response.get_json()
        self.assertEqual(status['status'], 'starting')
        self.assertFalse(status['healthy'])
        self.assertIsNone(status['fault'])
        self.assertIsNone(status['writer_error'])
        self.assertEqual(status['pending_batches'], 0)

    def test_stale_heartbeat_is_faulted_after_process_was_running(self):
        Path(web.PID_PATH).write_text('1234', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        runtime = {'pid': 1234, 'state': 'running',
                   'checked_at_ns': time.time_ns() - 20_000_000_000}
        with patch.object(web, 'get_running_process', return_value=(1234, 'production')), \
             patch.object(web, 'read_runtime_status', return_value=runtime):
            status = self.client.get('/api/status').get_json()
        self.assertEqual(status['status'], 'faulted')
        self.assertEqual(status['fault'], 'acquisition_status_stale')
        self.assertFalse(status['healthy'])

    def test_new_snapshot_makes_process_running(self):
        Path(web.PID_PATH).write_text('1234', encoding='utf-8')
        Path(web.MODE_PATH).write_text('production', encoding='utf-8')
        runtime = {'state': 'running', 'checked_at_ns': time.time_ns(),
                   'last_fault': None, 'last_writer_error': None}
        with patch.object(web, 'get_running_process', return_value=(1234, 'production')), \
             patch.object(web, 'read_runtime_status', return_value=runtime):
            status = self.client.get('/api/status').get_json()
        self.assertEqual(status['status'], 'running')
        self.assertTrue(status['healthy'])

    def test_shutdown_uses_stop_transition_and_defers_exit_on_timeout(self):
        with patch.object(web, 'stop_acquisition', return_value={'stopped': False, 'message': 'Drain timeout'}) as stop, \
             patch.object(web.sys, 'exit') as exit_process:
            web.handle_shutdown(None, None)
        stop.assert_called_once_with(manual=False)
        exit_process.assert_not_called()

    def test_shutdown_exits_after_reconciled_stop(self):
        with patch.object(web, 'stop_acquisition', return_value={'stopped': True}) as stop, \
             patch.object(web.sys, 'exit') as exit_process:
            web.handle_shutdown(None, None)
        stop.assert_called_once_with(manual=False)
        exit_process.assert_called_once_with(0)

    def test_failed_pid_recording_stops_spawned_child(self):
        process = MagicMock(pid=4321)
        missing_pid_path = str(Path(web.PID_PATH).parent / 'missing' / 'pid')
        with patch.object(web, 'PID_PATH', missing_pid_path), \
             patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.subprocess, 'Popen', return_value=process), \
             patch.object(web, 'terminate_pid', return_value=True) as terminate:
            result = web.start_acquisition('production')
        self.assertFalse(result['started'])
        terminate.assert_called_once_with(4321)

    def test_failed_pid_recording_reports_uncertain_process_when_stop_fails(self):
        process = MagicMock(pid=4321)
        missing_pid_path = str(Path(web.PID_PATH).parent / 'missing' / 'pid')
        with patch.object(web, 'PID_PATH', missing_pid_path), \
             patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web.subprocess, 'Popen', return_value=process), \
             patch.object(web, 'terminate_pid', return_value=False):
            result = web.start_acquisition('production')
        self.assertFalse(result['started'])
        self.assertTrue(result['process_may_be_running'])


if __name__ == '__main__':
    unittest.main()
