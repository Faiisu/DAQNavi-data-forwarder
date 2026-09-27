"""Automated tests for DAQ Navi operator access control, session lifecycle, and security."""

import os
import sys
import json
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import importlib

# Ensure repo root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from web import auth
    web = importlib.import_module('web.app')
except ModuleNotFoundError:
    from web import auth
    web = importlib.import_module('web.app')


class TestAccessControl(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

        # Set up test config
        self.config_path = Path(self.directory.name) / 'config.json'
        source = Path(web.SERVICE_DIR) / 'config.json'
        self.config = json.loads(source.read_text(encoding='utf-8'))
        self.config['POSTGRES_PASSWORD'] = 'secret-postgres-pw'
        self.config['INFLUX_TOKEN'] = 'secret-influx-token'
        self.config['MQTT_PASSWORD'] = 'secret-mqtt-pw'
        self.config['DB_DSN'] = 'postgresql://operator:secret-postgres-pw@localhost:5432/daq_test'
        self.config_path.write_text(json.dumps(self.config), encoding='utf-8')

        self.config_patch = patch.object(web, 'CONFIG_PATH', str(self.config_path))
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

        origins_patch = patch.object(web, 'ALLOWED_ORIGINS', [
            'http://localhost:8081', 'http://127.0.0.1:8081', 'http://localhost:8080'
        ])
        origins_patch.start()
        self.addCleanup(origins_patch.stop)

        for name in ('PID_PATH', 'MODE_PATH', 'LOG_PATH'):
            replacement = patch.object(web, name, str(Path(self.directory.name) / name))
            replacement.start()
            self.addCleanup(replacement.stop)

        # Set up auth test credentials
        self.operator_user = 'test_operator'
        self.operator_password = 'ValidPassword123!'
        self.password_hash = auth.hash_password(self.operator_password)
        self.session_key = 'test-session-signing-key-32-bytes-long!'

        # Configure web app auth
        web.app.config['TESTING'] = True
        web.configure_auth(
            users={self.operator_user: self.password_hash},
            session_key=self.session_key,
            ttl_seconds=3600
        )
        self.client = web.app.test_client()

    def _login(self, username=None, password=None):
        user = username if username is not None else self.operator_user
        pwd = password if password is not None else self.operator_password
        return self.client.post('/login', data={'username': user, 'password': pwd})

    def _authenticated_client(self):
        client = web.app.test_client()
        _, cookie_val = web.session_store.create_session(self.operator_user)
        client.set_cookie(auth.COOKIE_NAME, cookie_val)
        return client

    # =========================================================================
    # Group A: Provisioning & Fail-Closed Startup
    # =========================================================================

    def test_normal_startup_with_valid_secrets(self):
        """SEC-01: Valid credentials and session key permit normal startup."""
        users, key = auth.load_auth_secrets(env={
            'DAQ_OPERATOR_USER': 'op1',
            'DAQ_OPERATOR_HASH': self.password_hash,
            'DAQ_SESSION_KEY': self.session_key
        })
        self.assertEqual(users, {'op1': self.password_hash})
        self.assertEqual(key, self.session_key)

    def test_fail_closed_missing_password_hash(self):
        """SEC-02: Missing operator password hash raises RuntimeError."""
        with self.assertRaises(RuntimeError) as ctx:
            auth.load_auth_secrets(env={
                'DAQ_OPERATOR_USER': 'op1',
                'DAQ_SESSION_KEY': self.session_key
            })
        self.assertIn('Missing or empty DAQ operator password hash', str(ctx.exception))

    def test_fail_closed_missing_session_key(self):
        """SEC-03: Missing session signing key raises RuntimeError."""
        with self.assertRaises(RuntimeError) as ctx:
            auth.load_auth_secrets(env={
                'DAQ_OPERATOR_USER': 'op1',
                'DAQ_OPERATOR_HASH': self.password_hash
            })
        self.assertIn('Missing or empty DAQ session signing key', str(ctx.exception))

    def test_no_default_credentials(self):
        """SEC-04: No default accounts (admin/admin or blank) are permitted."""
        with self.assertRaises(RuntimeError):
            auth.load_auth_secrets(env={})

    # =========================================================================
    # Group B: Authentication Flow & Session Lifecycle
    # =========================================================================

    def test_successful_login(self):
        """AUTH-01: Correct operator credentials return session cookie and redirect."""
        res = self._login()
        self.assertIn(res.status_code, (200, 302))
        cookie_header = res.headers.get('Set-Cookie', '')
        self.assertIn(auth.COOKIE_NAME, cookie_header)
        self.assertIn('HttpOnly', cookie_header)

    def test_failed_login_bad_password(self):
        """AUTH-02: Wrong password returns 401 Unauthorized without session cookie."""
        res = self._login(password='WrongPassword!')
        self.assertEqual(res.status_code, 401)
        self.assertNotIn(auth.COOKIE_NAME, res.headers.get('Set-Cookie', ''))

    def test_anonymous_access_redirects_to_login(self):
        """AUTH-03: Unauthenticated visit to / redirects to /login."""
        res = self.client.get('/')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.headers.get('Location', ''))

    def test_authenticated_access_to_login_redirects_to_index(self):
        """AUTH-04: Authenticated operator visiting /login redirects to /."""
        client = self._authenticated_client()
        res = client.get('/login')
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res.headers.get('Location'), '/')

    def test_logout_invalidates_session(self):
        """AUTH-05: Logout clears cookie and invalidates session on server."""
        client = self._authenticated_client()
        res = client.post('/logout')
        self.assertEqual(res.status_code, 302)
        self.assertIn('/login', res.headers.get('Location', ''))
        # Try accessing / again -> must redirect to /login
        after_logout = client.get('/')
        self.assertEqual(after_logout.status_code, 302)
        self.assertIn('/login', after_logout.headers.get('Location', ''))

    def test_expired_session_rejected(self):
        """AUTH-06: Expired session cookie is rejected."""
        short_store = auth.SessionStore(self.session_key, ttl_seconds=1)
        _, cookie_val = short_store.create_session('temp_user')
        time.sleep(1.1)
        self.assertIsNone(short_store.validate_cookie(cookie_val))

    def test_tampered_cookie_rejected(self):
        """AUTH-07: Tampered session signature is rejected."""
        client = web.app.test_client()
        _, cookie_val = web.session_store.create_session(self.operator_user)
        tampered = cookie_val[:-4] + ('1234' if cookie_val[-4:] != '1234' else 'abcd')
        client.set_cookie(auth.COOKIE_NAME, tampered)
        res = client.get('/api/config')
        self.assertEqual(res.status_code, 401)

    # =========================================================================
    # Group C: Cookie Security & Isolation
    # =========================================================================

    def test_cookie_attributes_and_name(self):
        """ISO-01: Session cookie uses daq_session_id with HttpOnly and SameSite."""
        res = self._login()
        set_cookie = res.headers.get('Set-Cookie', '')
        self.assertIn(f'{auth.COOKIE_NAME}=', set_cookie)
        self.assertIn('HttpOnly', set_cookie)
        self.assertIn('SameSite=Lax', set_cookie)

    def test_cross_service_cookie_rejected(self):
        """ISO-02: Musashi or foreign cookies are rejected."""
        client = web.app.test_client()
        client.set_cookie('musashi_session_id', 'some-musashi-token')
        res = client.get('/api/config')
        self.assertEqual(res.status_code, 401)

    # =========================================================================
    # Group D: HTTP API Boundary Protection
    # =========================================================================

    def test_anonymous_api_calls_return_json_401(self):
        """API-01, API-02, API-03: All protected endpoints return JSON 401 when anonymous."""
        endpoints = [
            ('GET', '/api/config'),
            ('POST', '/api/config'),
            ('GET', '/api/status'),
            ('GET', '/api/samples'),
            ('GET', '/api/retention'),
            ('GET', '/api/scan_usb'),
            ('POST', '/api/start'),
            ('POST', '/api/stop'),
            ('POST', '/api/buffer/clear'),
            ('POST', '/api/test_destination'),
        ]
        for method, endpoint in endpoints:
            if method == 'GET':
                res = self.client.get(endpoint)
            else:
                res = self.client.post(endpoint, json={})
            self.assertEqual(res.status_code, 401, f'Endpoint {method} {endpoint} allowed anonymous access')
            self.assertTrue(res.is_json, f'Endpoint {method} {endpoint} did not return JSON')
            self.assertIn('error', res.get_json())

    def test_authenticated_api_calls_succeed(self):
        """API-04: Authenticated operator can call protected API endpoints."""
        client = self._authenticated_client()
        res = client.get('/api/config')
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.is_json)

    # =========================================================================
    # Group E: WebSocket / Socket.IO Boundary Protection
    # =========================================================================

    def test_anonymous_socketio_connect_rejected(self):
        """WS-01: Socket.IO handshake without session cookie is rejected."""
        socket_client = web.socketio.test_client(web.app)
        self.assertFalse(socket_client.is_connected())

    def test_authenticated_socketio_connect_succeeds(self):
        """WS-02: Socket.IO handshake with valid session cookie succeeds."""
        _, cookie_val = web.session_store.create_session(self.operator_user)
        flask_client = web.app.test_client()
        flask_client.set_cookie(auth.COOKIE_NAME, cookie_val)
        socket_client = web.socketio.test_client(web.app, flask_test_client=flask_client)
        self.assertTrue(socket_client.is_connected())

    def test_anonymous_socketio_commands_rejected(self):
        """WS-03: Socket.IO events start_daq/stop_daq require valid session."""
        socket_client = web.socketio.test_client(web.app)
        self.assertFalse(socket_client.is_connected())
        # If handler is reached without a valid session cookie, command must not run
        with patch.object(web, 'start_acquisition') as mock_start:
            with web.app.test_request_context():
                web.handle_start({'mode': 'production'})
            mock_start.assert_not_called()

    # =========================================================================
    # Group F: Cross-Site Request & Origin Validation
    # =========================================================================

    def test_disallowed_cross_site_post_rejected(self):
        """CSRF-01: POST request with disallowed Origin is rejected with 403."""
        client = self._authenticated_client()
        res = client.post('/api/config', json={}, headers={'Origin': 'http://evil.com'})
        self.assertEqual(res.status_code, 403)

    def test_disallowed_socketio_origin_rejected(self):
        """CSRF-02: Socket.IO connection with disallowed Origin is rejected."""
        _, cookie_val = web.session_store.create_session(self.operator_user)
        flask_client = web.app.test_client()
        flask_client.set_cookie(auth.COOKIE_NAME, cookie_val)
        socket_client = web.socketio.test_client(
            web.app,
            flask_test_client=flask_client,
            headers={'Origin': 'http://evil.com'}
        )
        self.assertFalse(socket_client.is_connected())

    # =========================================================================
    # Group G: Secret Redaction & Safe Config Mutation
    # =========================================================================

    def test_config_api_redacts_sensitive_fields(self):
        """SEC-05: GET /api/config redacts passwords, tokens, and DSN credentials."""
        client = self._authenticated_client()
        res = client.get('/api/config')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data.get('POSTGRES_PASSWORD'), auth.REDACTED_PLACEHOLDER)
        self.assertEqual(data.get('INFLUX_TOKEN'), auth.REDACTED_PLACEHOLDER)
        self.assertEqual(data.get('MQTT_PASSWORD'), auth.REDACTED_PLACEHOLDER)
        self.assertNotIn('secret-postgres-pw', data.get('DB_DSN', ''))
        self.assertIn(auth.REDACTED_PLACEHOLDER, data.get('DB_DSN', ''))

    def test_partial_config_save_preserves_secrets(self):
        """SEC-06: Saving config with masked/empty secret preserves existing secret."""
        client = self._authenticated_client()
        payload = dict(self.config)
        payload['POSTGRES_PASSWORD'] = auth.REDACTED_PLACEHOLDER
        payload['INFLUX_TOKEN'] = ''
        payload['CLOCK_RATE'] = 2000

        res = client.post('/api/config', json=payload)
        self.assertEqual(res.status_code, 200)

        # Inspect saved file on disk
        saved = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['POSTGRES_PASSWORD'], 'secret-postgres-pw')
        self.assertEqual(saved['INFLUX_TOKEN'], 'secret-influx-token')
        self.assertEqual(saved['CLOCK_RATE'], 2000)

    def test_explicit_secret_replacement(self):
        """SEC-07: Providing new password explicitly updates stored secret."""
        client = self._authenticated_client()
        payload = dict(self.config)
        payload['POSTGRES_PASSWORD'] = 'brand-new-db-password'

        res = client.post('/api/config', json=payload)
        self.assertEqual(res.status_code, 200)

        saved = json.loads(self.config_path.read_text(encoding='utf-8'))
        self.assertEqual(saved['POSTGRES_PASSWORD'], 'brand-new-db-password')

    def test_error_messages_do_not_leak_secrets(self):
        """SEC-08: Test destination / connection errors redact credentials."""
        client = self._authenticated_client()
        with patch.object(web, 'TimescaleProductionDestination', side_effect=Exception('connection failed to postgresql://user:mysecretpw@localhost:5432/db')):
            res = client.post('/api/test_destination', json={'DESTINATION': 'postgresql'})
            self.assertIn(res.status_code, (400, 502))
            self.assertNotIn('mysecretpw', res.get_data(as_text=True))

    # =========================================================================
    # Group H: Public Health & Portal Integration
    # =========================================================================

    def test_public_health_is_minimal_and_unauthenticated(self):
        """HLTH-01 & HLTH-02: /api/health is accessible anonymously and minimal."""
        res = self.client.get('/api/health')
        self.assertIn(res.status_code, (200, 503))
        data = res.get_json()
        self.assertIn('service', data)
        self.assertIn('status', data)
        self.assertNotIn('CHANNELS', data)
        self.assertNotIn('POSTGRES_PASSWORD', data)
        self.assertNotIn('spool_bytes', data)

    def test_public_health_portal_cors(self):
        """HLTH-03: /api/health allows Portal origin via CORS."""
        res = self.client.get('/api/health', headers={'Origin': 'http://localhost:8080'})
        self.assertEqual(res.headers.get('Access-Control-Allow-Origin'), 'http://localhost:8080')

    # =========================================================================
    # Group I: Password Change Lifecycle & Persistence
    # =========================================================================

    def test_change_password_requires_authentication(self):
        """PWD-01: Anonymous request to /api/auth/change-password returns 401."""
        res = self.client.post('/api/auth/change-password', json={
            'current_password': self.operator_password,
            'new_password': 'BrandNewPassword123!',
            'confirm_password': 'BrandNewPassword123!'
        })
        self.assertEqual(res.status_code, 401)

    def test_change_password_validation_errors(self):
        """PWD-02: Missing fields, short passwords, or mismatched confirmation return 400."""
        client = self._authenticated_client()

        # Missing current password
        res = client.post('/api/auth/change-password', json={
            'current_password': '',
            'new_password': 'ValidNewPassword123!',
            'confirm_password': 'ValidNewPassword123!'
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn('Current password is required', res.get_json()['message'])

        # New password too short (< 8 chars)
        res = client.post('/api/auth/change-password', json={
            'current_password': self.operator_password,
            'new_password': 'short',
            'confirm_password': 'short'
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn('at least 8 characters', res.get_json()['message'])

        # Confirmation mismatch
        res = client.post('/api/auth/change-password', json={
            'current_password': self.operator_password,
            'new_password': 'ValidNewPassword123!',
            'confirm_password': 'DifferentPassword123!'
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn('do not match', res.get_json()['message'])

        # Same as current
        res = client.post('/api/auth/change-password', json={
            'current_password': self.operator_password,
            'new_password': self.operator_password,
            'confirm_password': self.operator_password
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn('must be different from current', res.get_json()['message'])

    def test_change_password_invalid_current_password(self):
        """PWD-03: Wrong current password returns 400."""
        client = self._authenticated_client()
        res = client.post('/api/auth/change-password', json={
            'current_password': 'WrongPassword!',
            'new_password': 'BrandNewPassword123!',
            'confirm_password': 'BrandNewPassword123!'
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn('Incorrect current password', res.get_json()['message'])

    def test_change_password_success_and_persistence(self):
        """PWD-04: Successful password change updates memory, file, and allows login."""
        hash_file = Path(self.directory.name) / 'operator_hash'
        with patch.dict(os.environ, {'DAQ_OPERATOR_HASH_FILE': str(hash_file)}):
            client = self._authenticated_client()
            new_pwd = 'BrandNewPassword123!'
            res = client.post('/api/auth/change-password', json={
                'current_password': self.operator_password,
                'new_password': new_pwd,
                'confirm_password': new_pwd
            })
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.get_json()['status'], 'ok')

            # Verify persisted file content
            self.assertTrue(hash_file.exists())
            persisted_hash = hash_file.read_text(encoding='utf-8').strip()
            self.assertTrue(persisted_hash.startswith('pbkdf2_sha256:'))
            self.assertTrue(auth.verify_password(new_pwd, persisted_hash))

            # Old password can no longer log in
            login_old = self._login(username=self.operator_user, password=self.operator_password)
            self.assertEqual(login_old.status_code, 401)

            # New password logs in successfully
            login_new = self._login(username=self.operator_user, password=new_pwd)
            self.assertEqual(login_new.status_code, 302)

    def test_hash_file_precedence_in_load_auth_secrets(self):
        """PWD-05: Hash file takes precedence over DAQ_OPERATOR_HASH in env."""
        hash_file = Path(self.directory.name) / 'custom_operator_hash'
        custom_pwd = 'FileProvidedPassword999!'
        file_hash = auth.hash_password(custom_pwd)
        hash_file.write_text(file_hash, encoding='utf-8')

        users, key = auth.load_auth_secrets(env={
            'DAQ_OPERATOR_USER': 'admin',
            'DAQ_OPERATOR_HASH': self.password_hash,  # older env hash
            'DAQ_OPERATOR_HASH_FILE': str(hash_file),
            'DAQ_SESSION_KEY': self.session_key
        })
        self.assertEqual(users['admin'], file_hash)
        self.assertTrue(auth.verify_password(custom_pwd, users['admin']))


if __name__ == '__main__':
    unittest.main()
