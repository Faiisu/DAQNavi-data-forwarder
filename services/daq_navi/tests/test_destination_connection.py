"""Connection checks use unsaved form values without writing telemetry or config."""

import io
import importlib
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

web = importlib.import_module('services.daq_navi.web.app')


class DestinationConnectionTests(unittest.TestCase):
    def setUp(self):
        self.client = web.app.test_client()
        cookie = web.create_test_session('operator')
        self.client.set_cookie(web.COOKIE_NAME, cookie)
        self.saved = {
            'DESTINATION': 'postgresql',
            'DB_DSN': 'postgresql://saved.example/test',
            'INFLUX_URL': 'http://saved.example:8086',
            'INFLUX_ORG': 'saved',
            'INFLUX_BUCKET': 'saved',
            'INFLUX_TOKEN': 'saved-token',
        }
        config_patch = patch.object(web, 'read_config', return_value=self.saved)
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_postgres_uses_unsaved_dsn_and_runs_read_only_query(self):
        connection = MagicMock()
        with patch('psycopg2.connect', return_value=connection) as connect:
            response = self.client.post('/api/test_destination', json={
                'DESTINATION': 'postgresql',
                'DB_DSN': 'postgresql://draft.example/test',
            })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        connect.assert_called_once_with('postgresql://draft.example/test', connect_timeout=3)
        connection.cursor.return_value.__enter__.return_value.execute.assert_called_once_with('SELECT 1')
        connection.close.assert_called_once()

    def test_influx_checks_unsaved_org_and_bucket_with_token(self):
        requests = []

        def open_url(request, timeout):
            requests.append(request)
            payload = ({'orgs': [{'id': 'org-1', 'name': 'draft'}]} if len(requests) == 1 else
                       {'buckets': [{'name': 'draft-bucket', 'orgID': 'org-1'}]})
            return io.BytesIO(json.dumps(payload).encode())

        with patch('urllib.request.urlopen', side_effect=open_url):
            response = self.client.post('/api/test_destination', json={
                'DESTINATION': 'influxdb', 'INFLUX_URL': 'http://draft.example:8086',
                'INFLUX_ORG': 'draft', 'INFLUX_BUCKET': 'draft-bucket', 'INFLUX_TOKEN': 'draft-token',
            })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        self.assertEqual(len(requests), 2)
        self.assertIn('draft.example:8086/api/v2/orgs?org=draft', requests[0].full_url)
        self.assertIn('orgID=org-1', requests[1].full_url)
        self.assertEqual(requests[0].get_header('Authorization'), 'Token draft-token')

    def test_influx_missing_bucket_is_failure(self):
        responses = [io.BytesIO(b'{"orgs":[{"id":"org-1","name":"draft"}]}'),
                     io.BytesIO(b'{"buckets":[]}')]
        with patch('urllib.request.urlopen', side_effect=responses):
            response = self.client.post('/api/test_destination', json={
                'DESTINATION': 'influxdb', 'INFLUX_URL': 'http://draft.example:8086',
                'INFLUX_ORG': 'draft', 'INFLUX_BUCKET': 'missing', 'INFLUX_TOKEN': 'draft-token',
            })

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])

    def test_influx_reports_metadata_permission_failure(self):
        error = urllib.error.HTTPError('http://draft.example:8086/api/v2/orgs', 401, 'Unauthorized', {}, None)
        self.addCleanup(error.close)
        with patch('urllib.request.urlopen', side_effect=error):
            response = self.client.post('/api/test_destination', json={
                'DESTINATION': 'influxdb', 'INFLUX_URL': 'http://draft.example:8086',
                'INFLUX_ORG': 'draft', 'INFLUX_BUCKET': 'draft-bucket', 'INFLUX_TOKEN': 'draft-token',
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn('metadata access', response.get_json()['message'])

    def test_mqtt_requires_broker_acknowledgement(self):
        mqtt_client = MagicMock()
        mqtt_client.connect.return_value = 0
        mqtt_client.loop_start.side_effect = lambda: mqtt_client.on_connect(
            mqtt_client, None, None, 0, None)
        with patch('paho.mqtt.client.Client', return_value=mqtt_client):
            response = self.client.post('/api/test_destination', json={
                'DESTINATION': 'mqtt', 'MQTT_BROKER': 'draft-broker', 'MQTT_PORT': '8883',
                'MQTT_USERNAME': 'operator', 'MQTT_PASSWORD': 'secret', 'MQTT_TLS_ENABLED': True,
            })

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()['success'])
        mqtt_client.connect.assert_called_once_with('draft-broker', 8883, keepalive=10)
        mqtt_client.username_pw_set.assert_called_once_with('operator', 'secret')
        mqtt_client.tls_set.assert_called_once()
        mqtt_client.disconnect.assert_called_once()
        mqtt_client.publish.assert_not_called()

    def test_mqtt_rejected_connection_is_failure(self):
        mqtt_client = MagicMock()
        mqtt_client.connect.return_value = 0
        mqtt_client.loop_start.side_effect = lambda: mqtt_client.on_connect(
            mqtt_client, None, None, 5, None)
        with patch('paho.mqtt.client.Client', return_value=mqtt_client):
            response = self.client.post('/api/test_destination', json={
                'DESTINATION': 'mqtt', 'MQTT_BROKER': 'draft-broker', 'MQTT_PORT': 8883,
                'MQTT_USERNAME': 'operator', 'MQTT_PASSWORD': 'secret', 'MQTT_TLS_ENABLED': True,
            })

        self.assertEqual(response.status_code, 502)
        self.assertFalse(response.get_json()['success'])

    def test_unknown_destination_is_rejected(self):
        response = self.client.post('/api/test_destination', json={'DESTINATION': 'unknown'})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])


if __name__ == '__main__':
    unittest.main()
