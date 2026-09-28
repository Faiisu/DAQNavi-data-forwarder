"""The config form receives the effective database mode for legacy settings."""

import importlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch


web = importlib.import_module('web.app')


class ConfigCenterModeTests(unittest.TestCase):
    def setUp(self):
        web.configure_auth(
            users={'operator': web.auth.hash_password('test-pass')},
            session_key='test-signing-key-for-config-center-32b',
        )
        self.client = web.app.test_client()
        _, cookie = web.session_store.create_session('operator')
        self.client.set_cookie(web.COOKIE_NAME, cookie)
        self.config = json.loads((Path(web.SERVICE_DIR) / 'config.json').read_text(encoding='utf-8'))
        self.config.update(DB_USER='operator', DB_PASSWORD='secret-pass', DB_HOST='db.internal',
                           DB_PORT=5432, DB_NAME='daq',
                           DB_DSN='postgresql://operator:secret-pass@db.internal:5432/daq')
        self.config.pop('DB_CONNECTION_MODE', None)

    def test_legacy_fields_mode_survives_secret_redaction(self):
        saved_revision = web.config_revision(self.config)
        with patch.object(web, 'read_config', return_value=self.config):
            response = self.client.get('/api/config')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['DB_CONNECTION_MODE'], 'fields')
        self.assertEqual(body['_REV'], saved_revision)
        self.assertNotIn('secret-pass', response.get_data(as_text=True))

    def test_legacy_custom_dsn_uses_dsn_mode(self):
        self.config['DB_DSN'] = 'host=custom.internal dbname=daq user=operator password=secret-pass'
        saved_revision = web.config_revision(self.config)
        with patch.object(web, 'read_config', return_value=self.config):
            response = self.client.get('/api/config')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['DB_CONNECTION_MODE'], 'dsn')
        self.assertEqual(body['_REV'], saved_revision)
        self.assertNotIn('secret-pass', response.get_data(as_text=True))

    def test_config_center_serves_its_modules(self):
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('<script type="module" src="/static/config_center/app.js">',
                      page.get_data(as_text=True))
        for name in ('app', 'ui', 'config_form', 'runtime', 'password'):
            response = self.client.get(f'/static/config_center/{name}.js')
            self.assertEqual(response.status_code, 200, name)
            response.close()


if __name__ == '__main__':
    unittest.main()
