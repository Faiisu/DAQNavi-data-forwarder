# test_app.py
# Verification tests for DAQ USB-4716 Web API (CORS, Status Schema, Interface Binding)

import os
import sys
import tempfile
import unittest
import importlib
from unittest.mock import patch

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = SERVICE_DIR
for path in (PROJECT_ROOT, SERVICE_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

web = importlib.import_module('web.app')
app = web.app


class TestDaqUsb4716App(unittest.TestCase):

    def setUp(self):
        self.previous_auth = (web.session_store, web.operator_users)
        web.configure_auth(
            users={'api_test_operator': web.auth.hash_password('api-test-password')},
            session_key='api-test-session-key-at-least-32-bytes'
        )
        _, cookie = web.session_store.create_session('api_test_operator')
        self.client = app.test_client()
        self.client.set_cookie(web.COOKIE_NAME, cookie)

        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self._restore_auth)
        for name in ('PID_PATH', 'MODE_PATH'):
            p = patch.object(web, name, os.path.join(self.temp_dir.name, name))
            p.start()
            self.addCleanup(p.stop)
        for name, value in (
            ('get_running_process', (None, None)),
            ('read_config', {'AUTO_START_ON_STARTUP': False, 'AUTO_START_MODE': 'production',
                             'DESTINATION': 'influxdb'}),
            ('read_runtime_status', {}),
            ('read_recent_gaps', []),
        ):
            p = patch.object(web, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def _restore_auth(self):
        web.session_store, web.operator_users = self.previous_auth

    def test_cors_headers_on_get(self):
        """Verify CORS headers are present on GET responses."""
        res = self.client.get('/api/status', headers={'Origin': 'http://localhost:8081'})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get('Access-Control-Allow-Origin'), 'http://localhost:8081')
        self.assertEqual(res.headers.get('Access-Control-Allow-Methods'), 'GET, POST, OPTIONS')
        self.assertEqual(res.headers.get('Access-Control-Allow-Headers'), 'Content-Type, Authorization')

    def test_options_preflight(self):
        """Verify OPTIONS preflight request returns 204 with CORS headers."""
        res = self.client.options('/api/status', headers={'Origin': 'http://localhost:8081'})
        self.assertEqual(res.status_code, 204)
        self.assertEqual(res.headers.get('Access-Control-Allow-Origin'), 'http://localhost:8081')
        self.assertEqual(res.headers.get('Access-Control-Allow-Methods'), 'GET, POST, OPTIONS')
        self.assertEqual(res.headers.get('Access-Control-Allow-Headers'), 'Content-Type, Authorization')

    def test_standardized_status_schema(self):
        """Verify /api/status returns standardized fields matching portal contract."""
        res = self.client.get('/api/status', headers={'Origin': 'http://localhost:8081'})
        self.assertEqual(res.status_code, 200)
        data = res.get_json()

        # Required standardized top-level keys
        self.assertEqual(data.get('service_name'), 'DAQ USB-4716')
        self.assertEqual(data.get('port'), 8081)
        self.assertIsInstance(data.get('is_running'), bool)
        self.assertIn('status', data)
        self.assertEqual(data.get('status'), 'running' if data['is_running'] else 'stopped')
        self.assertIn('mode', data)
        self.assertIn('run_mode', data)

        # Service specific retained fields
        self.assertIn('destination', data)
        self.assertIn('pid', data)

    def test_host_binding_verification(self):
        """Verify entrypoint specifies host='0.0.0.0' for LAN edge deployment."""
        app_py_path = os.path.join(SERVICE_DIR, 'app.py')
        if not os.path.exists(app_py_path):
            app_py_path = os.path.join(SERVICE_DIR, 'web', 'app.py')
        with open(app_py_path, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn("host='0.0.0.0'", content)
        self.assertIn("port=8081", content)


if __name__ == '__main__':
    unittest.main()
