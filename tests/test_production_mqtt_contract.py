"""Observable contract for the production MQTT destination.

Tests use the writer, durable spool, configuration, and HTTP API as public seams.
The Paho client stands in for the external broker; no network service is used.
"""

import importlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.config_loader import DaqNaviConfig
from core import production_acquisition as production

web = importlib.import_module('web.app')


def mqtt_config(**changes):
    channels = {
        str(i): {
            'enabled': True, 'label': f'Sensor {i}', 'unit': 'kPa',
            'signal_type': 'SingleEnded', 'value_range': 'V_0To5',
            'scale': {'enabled': True, 'low_voltage': 0, 'high_voltage': 5,
                      'low_value': 0, 'high_value': 100},
        } for i in range(4)
    }
    data = {
        'DEVICE_ID': 'rack/one',
        'START_CHANNEL': 0, 'CHANNEL_COUNT': 4, 'CLOCK_RATE': 2000,
        'SECTION_LENGTH': 2, 'SECTION_COUNT': 0, 'CHANNELS': channels,
        'DESTINATION': 'mqtt', 'SPOOL_MAX_BYTES': 1024 * 1024,
        'MQTT_BROKER': 'broker.example', 'MQTT_PORT': 8883,
        'MQTT_USERNAME': 'daq', 'MQTT_PASSWORD': 'secret',
        'MQTT_TLS_ENABLED': True, 'MQTT_PRODUCTION_TOPIC_PREFIX': 'daq/production/v1',
        'MQTT_PRODUCTION_QOS': 1,
    }
    data.update(changes)
    return DaqNaviConfig(data, allow_env_overrides=False)


SAMPLE = {
    'time_ns': 1_700_000_000_000_000_000, 'sample_id': 'run:0:0',
    'session_id': 'run', 'device_id': 'rack/one', 'channel': 0,
    'sensor_name': 'Sensor 0', 'raw_voltage': 1.25,
    'calibrated_value': 25.0, 'unit': 'kPa', 'provenance': 'physical_daq',
}


class Broker:
    """External broker boundary with acknowledgments controlled by the test."""

    def __init__(self):
        self.published = []
        self.username = None
        self.tls = None
        self.publish_rc = 0
        self._lock = threading.Lock()

    def username_pw_set(self, username, password):
        self.username = (username, password)

    def tls_set(self, **kwargs):
        self.tls = kwargs

    def connect(self, host, port, keepalive=10):
        self.endpoint = (host, port)
        return 0

    def loop_start(self):
        self.on_connect(self, None, None, 0, None)

    def publish(self, topic, payload, qos=0, retain=False):
        with self._lock:
            mid = len(self.published) + 1
            self.published.append((topic, payload, qos, retain, mid))
        return MagicMock(rc=self.publish_rc, mid=mid)

    def ack(self, mid):
        self.on_publish(self, None, mid, 0, None)

    def wait_for_messages(self, count):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.published) >= count:
                    return list(self.published)
            time.sleep(0.005)
        raise AssertionError(f'expected {count} MQTT publications, got {len(self.published)}')

    def loop_stop(self):
        pass

    def disconnect(self):
        pass


class ProductionMqttConfigTests(unittest.TestCase):
    def test_secure_production_mqtt_accepts_both_qos_values(self):
        for qos in (0, 1):
            with self.subTest(qos=qos):
                self.assertEqual(production.validate_production_config(
                    mqtt_config(MQTT_PRODUCTION_QOS=qos)), (0, 1, 2, 3))

    def test_production_qos_defaults_to_one(self):
        raw = mqtt_config().raw.copy()
        del raw['MQTT_PRODUCTION_QOS']
        cfg = DaqNaviConfig(raw, allow_env_overrides=False)
        self.assertEqual(cfg.MQTT_PRODUCTION_QOS, 1)

    def test_production_mqtt_rejects_insecure_or_invalid_delivery_settings(self):
        invalid = (
            ({'MQTT_TLS_ENABLED': False}, 'TLS'),
            ({'MQTT_USERNAME': ''}, 'username'),
            ({'MQTT_PASSWORD': ''}, 'password'),
            ({'MQTT_PRODUCTION_QOS': 2}, 'QoS'),
            ({'MQTT_PRODUCTION_TOPIC_PREFIX': 'daq/+/samples'}, 'topic'),
        )
        for changes, reason in invalid:
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, f'(?i){reason}'):
                production.validate_production_config(mqtt_config(**changes))


class ProductionMqttWriterTests(unittest.TestCase):
    def write_in_thread(self, cfg, rows, gaps, broker=None):
        broker = broker or Broker()
        result = {'error': None}
        client_patch = patch('paho.mqtt.client.Client', return_value=broker)
        client_patch.start()
        self.addCleanup(client_patch.stop)
        writer = production.MQTTProductionDestination(cfg)

        def run():
            try:
                writer.write(rows, gaps)
            except Exception as exc:
                result['error'] = exc

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return broker, thread, result

    def test_sample_wire_record_preserves_physical_metadata_and_separates_topic(self):
        broker, thread, result = self.write_in_thread(mqtt_config(), [SAMPLE], [])
        topic, payload, qos, retain, mid = broker.wait_for_messages(1)[0]
        record = json.loads(payload)
        self.assertTrue(topic.startswith('daq/production/v1/'))
        self.assertTrue(topic.endswith('/samples'))
        self.assertNotIn('/rack/one/', topic)  # Device ID is one safe topic segment.
        self.assertEqual(record['schema_version'], 1)
        self.assertEqual(record['device_id'], 'rack/one')
        self.assertEqual(record['samples'], [SAMPLE])
        self.assertTrue(record['batch_id'])
        self.assertTrue(record['chunk_id'])
        self.assertEqual((qos, retain), (1, False))
        self.assertEqual(broker.endpoint, ('broker.example', 8883))
        self.assertEqual(broker.username, ('daq', 'secret'))
        self.assertIsNotNone(broker.tls)
        broker.ack(mid)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(result['error'])

    def test_qos_zero_and_one_wait_for_on_publish_before_writer_returns(self):
        for qos in (0, 1):
            with self.subTest(qos=qos):
                broker, thread, result = self.write_in_thread(
                    mqtt_config(MQTT_PRODUCTION_QOS=qos), [SAMPLE], [])
                message = broker.wait_for_messages(1)[0]
                self.assertTrue(thread.is_alive())
                self.assertEqual(message[2], qos)
                broker.ack(message[4])
                thread.join(2)
                self.assertFalse(thread.is_alive())
                self.assertIsNone(result['error'])

    def test_writer_waits_for_every_sample_and_gap_publication(self):
        gap = {'gap_id': 'gap-1', 'revision': 1, 'start_ns': 100,
               'end_ns': None, 'cause': 'broker_outage'}
        broker, thread, result = self.write_in_thread(mqtt_config(), [SAMPLE], [gap])
        first = broker.wait_for_messages(1)[0]
        broker.ack(first[4])
        messages = broker.wait_for_messages(2)
        self.assertEqual({message[0].rsplit('/', 1)[-1] for message in messages},
                         {'samples', 'gaps'})
        self.assertTrue(thread.is_alive())
        broker.ack(messages[1][4])
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(result['error'])

    def test_publish_rejection_is_reported_to_spool_caller(self):
        broker = Broker()
        broker.publish_rc = 4
        broker, thread, result = self.write_in_thread(mqtt_config(), [SAMPLE], [], broker)
        broker.wait_for_messages(1)
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result['error'], Exception)

    def test_oversized_single_sample_is_rejected_before_any_publication(self):
        class ImmediateAckBroker(Broker):
            def publish(self, *args, **kwargs):
                result = super().publish(*args, **kwargs)
                self.ack(result.mid)
                return result

        oversized = dict(SAMPLE, sensor_name='sensor-' + 'x' * 1000)
        broker, thread, result = self.write_in_thread(
            mqtt_config(MQTT_PRODUCTION_MAX_PAYLOAD_BYTES=600), [SAMPLE, oversized], [],
            ImmediateAckBroker())
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result['error'], ValueError)
        self.assertIn('maximum payload size', str(result['error']).lower())
        self.assertIn(oversized['sample_id'], str(result['error']))
        self.assertEqual(broker.published, [])

    def test_open_and_closed_gap_have_same_id_and_increasing_revision(self):
        open_gap = {'gap_id': 'gap-1', 'revision': 1, 'start_ns': 100,
                    'end_ns': None, 'cause': 'broker_outage'}
        closed_gap = dict(open_gap, revision=2, end_ns=200)
        records = []
        for gap in (open_gap, closed_gap):
            broker, thread, result = self.write_in_thread(mqtt_config(), [], [gap])
            topic, payload, _, retain, mid = broker.wait_for_messages(1)[0]
            self.assertTrue(topic.endswith('/gaps'))
            self.assertFalse(retain)
            records.append(json.loads(payload))
            broker.ack(mid)
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(result['error'])
        self.assertEqual([(r['gap_id'], r['revision'], r['end_ns']) for r in records],
                         [('gap-1', 1, None), ('gap-1', 2, 200)])
        self.assertEqual([r['cause'] for r in records], ['broker_outage'] * 2)
        self.assertEqual([r['schema_version'] for r in records], [1, 1])

    def test_replay_keeps_chunk_ids_and_messages_within_byte_limit(self):
        cfg = mqtt_config(MQTT_PRODUCTION_MAX_PAYLOAD_BYTES=600)
        rows = [dict(SAMPLE, sample_id=f'run:0:{i}', time_ns=SAMPLE['time_ns'] + i)
                for i in range(10)]
        attempts = []
        for _ in range(2):
            broker, thread, result = self.write_in_thread(cfg, rows, [])
            broker.wait_for_messages(2)
            acknowledged = set()
            deadline = time.monotonic() + 2
            while thread.is_alive() and time.monotonic() < deadline:
                for _, _, _, _, mid in list(broker.published):
                    if mid not in acknowledged:
                        broker.ack(mid)
                        acknowledged.add(mid)
                thread.join(0.01)
            self.assertFalse(thread.is_alive())
            self.assertIsNone(result['error'])
            attempts.append(broker.published)
        self.assertEqual(len(attempts[0]), len(attempts[1]))
        self.assertEqual(
            [(topic, json.loads(payload)['chunk_id']) for topic, payload, *_ in attempts[0]],
            [(topic, json.loads(payload)['chunk_id']) for topic, payload, *_ in attempts[1]])
        self.assertTrue(all(len(payload.encode() if isinstance(payload, str) else payload) <= 600
                            for _, payload, *_ in attempts[0]))


class ProductionMqttSpoolTests(unittest.TestCase):
    def test_oversized_single_sample_stays_pending_in_durable_spool(self):
        channels = json.loads(json.dumps(mqtt_config().raw['CHANNELS']))
        channels['3']['label'] = 'sensor-' + 'x' * 1000
        cfg = mqtt_config(CHANNELS=channels, MQTT_PRODUCTION_MAX_PAYLOAD_BYTES=600)
        with tempfile.TemporaryDirectory() as directory, \
             patch('paho.mqtt.client.Client', return_value=Broker()) as client_factory:
            writer = production.MQTTProductionDestination(cfg)
            broker = client_factory.return_value
            pipeline = production.ProductionPipeline(cfg, Path(directory), writer)
            try:
                pipeline.capture([1.0, 2.0, 3.0, 4.0], 1_700_000_000_000_000_000)
                with self.assertRaisesRegex(ValueError, 'maximum payload size'):
                    pipeline.flush_once()
                self.assertEqual(pipeline.pending_batches, 1)
                self.assertEqual(broker.published, [])
            finally:
                pipeline.close()
                writer.close()

    def test_gap_close_during_open_delivery_remains_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            spool = production.DurableSpool(Path(directory), 1024 * 1024)
            try:
                gap_id = spool.open_gap(100, 'broker_outage')
                opened = spool.pending_gaps()[0]
                spool.close_open_gaps_for_switch(200)
                spool.acknowledge_gaps([opened])
                pending = spool.pending_gaps()
                self.assertEqual([(g['gap_id'], g['revision'], g['end_ns']) for g in pending],
                                 [(gap_id, 2, 200)])
            finally:
                spool.close()

    def test_ambiguous_publish_leaves_committed_batch_for_replay(self):
        class UncertainWriter:
            def write(self, rows, gaps):
                raise TimeoutError('publish completion unknown')

        with tempfile.TemporaryDirectory() as directory:
            cfg = mqtt_config(SPOOL_DIR=directory)
            pipeline = production.ProductionPipeline(
                cfg, Path(directory), UncertainWriter(), session_id='run')
            pipeline.capture([1.0, 2.0, 3.0, 4.0],
                             end_time_ns=1_700_000_000_000_000_000)
            with self.assertRaises(TimeoutError):
                pipeline.flush_once()
            self.assertEqual(pipeline.pending_batches, 1)
            pipeline.close()


class ProductionMqttWebTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        source = Path(web.SERVICE_DIR) / 'config.json'
        self.config = json.loads(source.read_text(encoding='utf-8'))
        self.config.update(mqtt_config().raw)
        self.config.update(DB_HOST='localhost', DB_PORT=5432, DB_NAME='daq_test',
                           DB_USER='test', DB_PASSWORD='test',
                           DB_DSN='postgresql://test:test@localhost:5432/daq_test',
                           INFLUX_URL='http://influx.test:8086', INFLUX_ORG='test-org',
                           INFLUX_BUCKET='test-bucket', INFLUX_TOKEN='test-token')
        self.config['SPOOL_DIR'] = self.directory.name
        self.path = Path(self.directory.name) / 'config.json'
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        config_patch = patch.object(web, 'CONFIG_PATH', str(self.path))
        config_patch.start()
        self.addCleanup(config_patch.stop)
        web.configure_auth(users={'operator': web.auth.hash_password('test-pass')},
                           session_key='test-signing-key-for-mqtt-contract-32b')
        self.client = web.app.test_client()
        _, cookie = web.session_store.create_session('operator')
        self.client.set_cookie(web.COOKIE_NAME, cookie)

    def test_mqtt_history_and_retention_are_consumer_managed(self):
        with patch('psycopg2.connect', side_effect=AssertionError('queried TimescaleDB')):
            samples = self.client.get('/api/samples?channel=0')
            retention = self.client.get('/api/retention')
        self.assertEqual(samples.status_code, 200)
        self.assertEqual(samples.get_json()['destination'], 'mqtt')
        self.assertEqual(samples.get_json()['points'], [])
        self.assertIn('consumer', samples.get_json()['message'].lower())
        self.assertEqual(retention.status_code, 200)
        self.assertEqual(retention.get_json()['destination'], 'mqtt')
        self.assertIn('consumer', retention.get_json()['retention'].lower())

    def test_mqtt_status_reports_broker_boundary_and_local_spool(self):
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'read_runtime_status', return_value={
                 'state': 'running', 'destination': 'mqtt', 'pending_batches': 3, 'pending_bytes': 1024,
                 'last_writer_error': 'broker offline', 'checked_at_ns': time.time_ns(),
             }):
            response = self.client.get('/api/status')
        self.assertEqual(response.status_code, 200)
        status = response.get_json()
        self.assertEqual(status['destination'], 'mqtt')
        self.assertEqual(status['pending_batches'], 3)
        self.assertEqual(status['status'], 'buffering')
        self.assertEqual(status['mqtt_delivery']['state'], 'error')
        self.assertIsNone(status['mqtt_delivery']['last_completed_ns'])
        self.assertEqual(status['mqtt_delivery']['qos'], 1)
        self.assertIn('consumer', status['retention'].lower())
        self.assertIsNone(status['retention_days'])

    def test_mqtt_status_reports_completed_delivery_without_claiming_live_connection(self):
        completed_ns = time.time_ns() - 1_000_000_000
        with patch.object(web, 'get_running_process', return_value=(123, 'production')), \
             patch.object(web, 'read_runtime_status', return_value={
                 'state': 'running', 'destination': 'mqtt', 'pending_batches': 2,
                 'last_delivery_ns': completed_ns, 'checked_at_ns': time.time_ns(),
             }):
            status = self.client.get('/api/status').get_json()
        self.assertEqual(status['mqtt_delivery'], {
            'state': 'completed', 'last_completed_ns': completed_ns, 'qos': 1,
        })
        self.assertNotIn('broker_connected', status)

    def test_mqtt_status_does_not_present_stale_or_idle_run_as_delivered(self):
        for runtime, expected in (
            ({'state': 'running', 'destination': 'mqtt', 'pending_batches': 0, 'checked_at_ns': time.time_ns()}, 'idle'),
            ({'state': 'running', 'destination': 'mqtt', 'pending_batches': 2, 'checked_at_ns': time.time_ns()}, 'pending'),
            ({'state': 'running', 'destination': 'mqtt', 'last_delivery_ns': 123, 'checked_at_ns': 1}, 'unavailable'),
            ({'state': 'running', 'destination': 'influxdb', 'last_delivery_ns': 123,
              'checked_at_ns': time.time_ns()}, 'unavailable'),
        ):
            with self.subTest(expected=expected), \
                 patch.object(web, 'get_running_process', return_value=(123, 'production')), \
                 patch.object(web, 'read_runtime_status', return_value=runtime):
                status = self.client.get('/api/status').get_json()
            self.assertEqual(status['mqtt_delivery']['state'], expected)
            if runtime.get('destination') != 'mqtt':
                self.assertIsNone(status['mqtt_delivery']['last_completed_ns'])

    def test_config_readback_keeps_mqtt_password_private_and_shows_qos(self):
        response = self.client.get('/api/config')
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data['MQTT_PRODUCTION_QOS'], 1)
        self.assertEqual(data['MQTT_PRODUCTION_TOPIC_PREFIX'], 'daq/production/v1')
        self.assertNotIn('secret', response.get_data(as_text=True))

    def test_save_new_broker_and_qos_keeps_pending_records_for_latest_config(self):
        spool = production.DurableSpool(Path(self.directory.name), 1024 * 1024)
        spool.append('pending-batch', [SAMPLE])
        spool.close()
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch.object(web, '_test_destination', return_value='ok'):
            response = self.client.post('/api/config', json={
                'MQTT_BROKER': 'new-broker.example', 'MQTT_PRODUCTION_QOS': 0,
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual((saved['MQTT_BROKER'], saved['MQTT_PRODUCTION_QOS']),
                         ('new-broker.example', 0))
        reopened = production.DurableSpool(Path(self.directory.name), 1024 * 1024)
        self.assertEqual(reopened.oldest(), ('pending-batch', [SAMPLE]))
        reopened.close()

    def test_save_database_to_mqtt_routes_pending_records_by_new_selection(self):
        self.config['DESTINATION'] = 'postgresql'
        self.path.write_text(json.dumps(self.config), encoding='utf-8')
        spool = production.DurableSpool(Path(self.directory.name), 1024 * 1024)
        spool.append('pending-batch', [SAMPLE])
        spool.close()
        broker = Broker()
        with patch.object(web, 'get_running_process', return_value=(None, None)), \
             patch('paho.mqtt.client.Client', return_value=broker), \
             patch('psycopg2.connect', side_effect=AssertionError('queried old database')):
            response = self.client.post('/api/config', json={'DESTINATION': 'mqtt'})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8'))['DESTINATION'], 'mqtt')
        self.assertEqual(broker.endpoint, ('broker.example', 8883))
        self.assertEqual(broker.published, [])
        reopened = production.DurableSpool(Path(self.directory.name), 1024 * 1024)
        self.assertEqual(reopened.oldest(), ('pending-batch', [SAMPLE]))
        reopened.close()

    def test_preflight_checks_authenticated_tls_connection_without_publishing(self):
        broker = Broker()
        with patch('paho.mqtt.client.Client', return_value=broker):
            response = self.client.post('/api/test_destination', json={
                'DESTINATION': 'mqtt', 'MQTT_BROKER': 'draft.example', 'MQTT_PORT': 8883,
                'MQTT_USERNAME': 'draft-user', 'MQTT_PASSWORD': 'draft-secret',
                'MQTT_TLS_ENABLED': True,
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(broker.endpoint, ('draft.example', 8883))
        self.assertEqual(broker.username, ('draft-user', 'draft-secret'))
        self.assertIsNotNone(broker.tls)
        self.assertEqual(broker.published, [])

    def test_preflight_rejects_insecure_production_mqtt(self):
        broker = Broker()
        with patch('paho.mqtt.client.Client', return_value=broker):
            response = self.client.post('/api/test_destination', json={
                'DESTINATION': 'mqtt', 'MQTT_TLS_ENABLED': False,
                'MQTT_USERNAME': '', 'MQTT_PASSWORD': '',
            })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()['success'])
        self.assertFalse(hasattr(broker, 'endpoint'))


class ProductionMqttContractFixtureTests(unittest.TestCase):
    """Verifies that documented JSON fixtures match contract expectations."""

    def setUp(self):
        self.fixtures_dir = Path(__file__).resolve().parents[1] / 'docs' / 'contracts' / 'fixtures'

    def test_sample_wire_record_fixture(self):
        fixture_path = self.fixtures_dir / 'production-sample-envelope.json'
        self.assertTrue(fixture_path.exists(), f'Missing fixture: {fixture_path}')
        data = json.loads(fixture_path.read_text(encoding='utf-8'))
        self.assertEqual(data['schema_version'], 1)
        self.assertEqual(data['device_id'], 'rack/one')
        self.assertTrue(data['batch_id'])
        self.assertTrue(data['chunk_id'])
        self.assertEqual(data['samples'], [SAMPLE])

    def test_gap_fixtures_match_open_and_closed_expectations(self):
        open_path = self.fixtures_dir / 'production-gap-open.json'
        closed_path = self.fixtures_dir / 'production-gap-closed.json'
        self.assertTrue(open_path.exists())
        self.assertTrue(closed_path.exists())

        opened = json.loads(open_path.read_text(encoding='utf-8'))
        closed = json.loads(closed_path.read_text(encoding='utf-8'))

        self.assertEqual(opened, {
            'schema_version': 1, 'device_id': 'rack/one', 'gap_id': 'gap-1',
            'revision': 1, 'start_ns': 100, 'end_ns': None, 'cause': 'broker_outage'
        })
        self.assertEqual(closed, {
            'schema_version': 1, 'device_id': 'rack/one', 'gap_id': 'gap-1',
            'revision': 2, 'start_ns': 100, 'end_ns': 200, 'cause': 'broker_outage'
        })

    def test_bounded_chunks_fixture_and_consumer_deduplication(self):
        chunks_path = self.fixtures_dir / 'bounded-chunks.json'
        self.assertTrue(chunks_path.exists())
        chunks = json.loads(chunks_path.read_text(encoding='utf-8'))
        self.assertIsInstance(chunks, list)
        self.assertGreater(len(chunks), 1)

        # Verify consumer deduplication by sample_id
        consumed_samples = {}
        for chunk in chunks:
            self.assertEqual(chunk['schema_version'], 1)
            self.assertEqual(chunk['device_id'], 'rack/one')
            self.assertTrue(chunk['chunk_id'])
            for sample in chunk['samples']:
                consumed_samples[sample['sample_id']] = sample

        # Replaying identical chunks produces identical deduplicated set
        for chunk in chunks:
            for sample in chunk['samples']:
                consumed_samples[sample['sample_id']] = sample

        self.assertEqual(len(consumed_samples), 3)


if __name__ == '__main__':
    unittest.main()
