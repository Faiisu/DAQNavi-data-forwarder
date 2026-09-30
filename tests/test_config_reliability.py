import os
import json
import tempfile
import unittest
import importlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch, MagicMock

from web import auth
web_module = importlib.import_module('web.app')
from core.config_loader import DaqNaviConfig
from core.production_acquisition import destination_identity


class TestConfigReliabilityPhase1(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.json"
        self.sentinel_db_pw = "sentinel_db_secret_123"
        self.sentinel_influx = "sentinel_influx_token_456"
        self.sentinel_mqtt = "sentinel_mqtt_pw_789"
        self.config = {
            "START_CHANNEL": 0,
            "CHANNEL_COUNT": 4,
            "CLOCK_RATE": 2000,
            "SECTION_LENGTH": 500,
            "SECTION_COUNT": 0,
            "SPOOL_MAX_BYTES": 1048576,
            "SPOOL_DIR": os.path.join(self.temp_dir.name, "spool"),
            "DESTINATION": "postgresql",
            "DB_HOST": "10.0.0.99",
            "DB_PORT": 5432,
            "DB_NAME": "test_db",
            "DB_USER": "test_user",
            "DB_PASSWORD": self.sentinel_db_pw,
            "POSTGRES_PASSWORD": self.sentinel_db_pw,
            "DB_DSN": f"postgresql://test_user:{self.sentinel_db_pw}@10.0.0.99:5432/test_db",
            "DB_RETENTION_DAYS": 30,
            "DB_PRODUCTION_TABLE": "daq_production_samples",
            "INFLUX_URL": "http://10.0.0.99:8086",
            "INFLUX_ORG": "test_org",
            "INFLUX_BUCKET": "test_bucket",
            "INFLUX_TOKEN": self.sentinel_influx,
            "INFLUX_MEASUREMENT": "test_meas",
            "MQTT_BROKER": "10.0.0.99",
            "MQTT_PORT": 8883,
            "MQTT_USERNAME": "test_mqtt_user",
            "MQTT_PASSWORD": self.sentinel_mqtt,
            "MQTT_TLS_ENABLED": True,
            "AUTO_START_MODE": "production",
            "AUTO_START_ON_STARTUP": False,
            "CHANNELS": {
                str(i): {
                    "enabled": True,
                    "label": f"Ch{i}",
                    "unit": "V",
                    "signal_type": "SingleEnded",
                    "value_range": "V_0To5",
                    "scale": {
                        "enabled": True,
                        "low_voltage": 0.0,
                        "high_voltage": 5.0,
                        "low_value": 0.0,
                        "high_value": 100.0,
                    }
                } for i in range(4)
            }
        }
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        self.orig_config_path = web_module.CONFIG_PATH
        web_module.CONFIG_PATH = str(self.config_path)

        web_module.app.config["TESTING"] = True
        self.client = web_module.app.test_client()
        # Set up authenticated session
        web_module.configure_auth(
            users={"operator": auth.hash_password("operator-pass")},
            session_key="test-signing-key-for-reliability-32b"
        )
        _, cookie = web_module.session_store.create_session("operator")
        self.client.set_cookie(auth.COOKIE_NAME, cookie)

    def tearDown(self):
        web_module.CONFIG_PATH = self.orig_config_path
        self.temp_dir.cleanup()

    def test_redact_config_secrets_covers_all_secrets(self):
        """Ticket 01: redact_config_secrets masks DB_PASSWORD, tokens, and keyword DSNs."""
        redacted = auth.redact_config_secrets(self.config)
        self.assertEqual(redacted["DB_PASSWORD"], auth.REDACTED_PLACEHOLDER)
        self.assertEqual(redacted["POSTGRES_PASSWORD"], auth.REDACTED_PLACEHOLDER)
        self.assertEqual(redacted["INFLUX_TOKEN"], auth.REDACTED_PLACEHOLDER)
        self.assertEqual(redacted["MQTT_PASSWORD"], auth.REDACTED_PLACEHOLDER)
        self.assertNotIn(self.sentinel_db_pw, redacted["DB_DSN"])
        self.assertIn(auth.REDACTED_PLACEHOLDER, redacted["DB_DSN"])

        # Also test keyword DSN
        kw_config = dict(self.config)
        kw_config["DB_DSN"] = f"host=10.0.0.99 port=5432 user=test_user password={self.sentinel_db_pw} dbname=test_db"
        kw_redacted = auth.redact_config_secrets(kw_config)
        self.assertNotIn(self.sentinel_db_pw, kw_redacted["DB_DSN"])
        self.assertIn(auth.REDACTED_PLACEHOLDER, kw_redacted["DB_DSN"])

    def test_get_and_post_config_responses_never_echo_secrets(self):
        """Ticket 01: GET and POST responses never contain sentinels."""
        # 1. GET /api/config
        res = self.client.get("/api/config")
        self.assertEqual(res.status_code, 200)
        body_text = res.get_data(as_text=True)
        self.assertNotIn(self.sentinel_db_pw, body_text)
        self.assertNotIn(self.sentinel_influx, body_text)
        self.assertNotIn(self.sentinel_mqtt, body_text)
        data = res.get_json()
        self.assertEqual(data["DB_PASSWORD"], auth.REDACTED_PLACEHOLDER)

        # 2. POST /api/config success response
        payload = dict(data)
        payload["CLOCK_RATE"] = 1500
        with patch.object(web_module, "_test_destination", return_value="ok"), \
             patch.object(web_module, "effective_timescale_retention", return_value="30 days"):
            res = self.client.post("/api/config", json=payload)
        self.assertEqual(res.status_code, 200)
        body_text = res.get_data(as_text=True)
        self.assertNotIn(self.sentinel_db_pw, body_text)
        self.assertNotIn(self.sentinel_influx, body_text)
        self.assertNotIn(self.sentinel_mqtt, body_text)
        post_data = res.get_json()
        self.assertIn("config", post_data)
        self.assertEqual(post_data["config"]["DB_PASSWORD"], auth.REDACTED_PLACEHOLDER)
        self.assertEqual(post_data["config"]["INFLUX_TOKEN"], auth.REDACTED_PLACEHOLDER)
        self.assertEqual(post_data["config"]["MQTT_PASSWORD"], auth.REDACTED_PLACEHOLDER)

        # 3. Verify disk kept the real secrets
        saved_disk = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved_disk["DB_PASSWORD"], self.sentinel_db_pw)
        self.assertEqual(saved_disk["INFLUX_TOKEN"], self.sentinel_influx)
        self.assertEqual(saved_disk["MQTT_PASSWORD"], self.sentinel_mqtt)
        self.assertEqual(saved_disk["CLOCK_RATE"], 1500)

    def test_destination_preflight_error_does_not_echo_token(self):
        payload = self.client.get("/api/config").get_json()
        payload["DESTINATION"] = "influxdb"
        with patch.object(web_module, "_test_destination",
                          side_effect=RuntimeError(f"token {self.sentinel_influx} was rejected")):
            response = self.client.post("/api/config", json=payload)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(self.sentinel_influx, response.get_data(as_text=True))

    def test_destination_test_error_does_not_echo_token(self):
        payload = self.client.get('/api/config').get_json()
        payload['DESTINATION'] = 'influxdb'
        with patch.object(web_module, '_test_destination',
                          side_effect=RuntimeError(f'token {self.sentinel_influx} was rejected')):
            response = self.client.post('/api/test_destination', json=payload)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(self.sentinel_influx, response.get_data(as_text=True))

    def test_explicit_secret_replacement_and_clear(self):
        """Ticket 01: Replacement updates secret; deliberate clear clears optional secret."""
        # Replacement
        payload = self.client.get("/api/config").get_json()
        payload["DB_PASSWORD"] = "new_db_password_xyz"
        payload["MQTT_PASSWORD"] = auth.CLEAR_SECRET
        with patch.object(web_module, "_test_destination", return_value="ok"), \
             patch.object(web_module, "effective_timescale_retention", return_value="30 days"):
            res = self.client.post("/api/config", json=payload)
        self.assertEqual(res.status_code, 200)

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["DB_PASSWORD"], "new_db_password_xyz")
        self.assertEqual(saved["MQTT_PASSWORD"], "")

    def test_db_connection_mode_inference_and_explicit_setting(self):
        """Ticket 02: Legacy configs infer connection mode; explicit mode is persisted."""
        # Legacy config without DB_CONNECTION_MODE matching fields -> 'fields'
        legacy_cfg = dict(self.config)
        self.assertNotIn("DB_CONNECTION_MODE", legacy_cfg)
        mode = auth.infer_db_connection_mode(legacy_cfg)
        self.assertEqual(mode, "fields")

        # Legacy config with custom DSN -> 'dsn'
        custom_cfg = dict(self.config)
        custom_cfg["DB_DSN"] = "postgresql://other_user:pass@remote_db:5432/analytics"
        mode = auth.infer_db_connection_mode(custom_cfg)
        self.assertEqual(mode, "dsn")

    def test_fields_mode_derives_effective_dsn_on_save(self):
        """Ticket 02: In fields mode, changing host/port updates effective DSN."""
        payload = self.client.get("/api/config").get_json()
        payload["DB_CONNECTION_MODE"] = "fields"
        payload["DB_HOST"] = "192.168.1.200"
        payload["DB_PORT"] = 5433
        payload["DB_NAME"] = "new_daq_db"
        # Client sends masked DSN or stale DSN
        payload["DB_DSN"] = "postgresql://stale:pass@10.0.0.99:5432/stale_db"

        with patch.object(web_module, "_test_destination", return_value="ok"), \
             patch.object(web_module, "effective_timescale_retention", return_value="30 days"):
            res = self.client.post("/api/config", json=payload)
        self.assertEqual(res.status_code, 200)

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["DB_CONNECTION_MODE"], "fields")
        self.assertEqual(saved["DB_HOST"], "192.168.1.200")
        self.assertEqual(saved["DB_PORT"], 5433)
        self.assertEqual(saved["DB_NAME"], "new_daq_db")
        # Effective DSN derived from fields with preserved password
        expected_dsn = f"postgresql://test_user:{self.sentinel_db_pw}@192.168.1.200:5433/new_daq_db"
        self.assertEqual(saved["DB_DSN"], expected_dsn)

    def test_custom_dsn_mode_uses_custom_dsn(self):
        """Ticket 02: In dsn mode, custom DSN is preserved and effective."""
        payload = self.client.get("/api/config").get_json()
        payload["DB_CONNECTION_MODE"] = "dsn"
        payload["DB_DSN"] = "postgresql://custom_user:secret@custom_host:5432/custom_db"
        payload["DB_HOST"] = "ignore_this_host"

        with patch.object(web_module, "_test_destination", return_value="ok"), \
             patch.object(web_module, "effective_timescale_retention", return_value="30 days"):
            res = self.client.post("/api/config", json=payload)
        self.assertEqual(res.status_code, 200)

        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["DB_CONNECTION_MODE"], "dsn")
        self.assertEqual(saved["DB_DSN"], "postgresql://custom_user:secret@custom_host:5432/custom_db")

    def test_destination_identity_reflects_mode_and_target_changes(self):
        """Ticket 02: destination_identity changes on target and connection mode transitions."""
        cfg1 = DaqNaviConfig({**self.config, "DB_CONNECTION_MODE": "fields"}, allow_env_overrides=False)
        id1 = destination_identity(cfg1)

        # Change host in fields mode
        cfg2 = DaqNaviConfig({
            **self.config,
            "DB_CONNECTION_MODE": "fields",
            "DB_HOST": "192.168.1.201",
            "DB_DSN": f"postgresql://test_user:{self.sentinel_db_pw}@192.168.1.201:5432/test_db"
        }, allow_env_overrides=False)
        id2 = destination_identity(cfg2)
        self.assertNotEqual(id1, id2)

        # Change mode to dsn
        cfg3 = DaqNaviConfig({
            **self.config,
            "DB_CONNECTION_MODE": "dsn",
            "DB_DSN": "postgresql://test_user:pass@remote:5432/db"
        }, allow_env_overrides=False)
        id3 = destination_identity(cfg3)
        self.assertNotEqual(id1, id3)

    def test_stopped_process_reconciles_stale_spool_active_marker(self):
        spool = web_module.DurableSpool(Path(self.config['SPOOL_DIR']), self.config['SPOOL_MAX_BYTES'])
        try:
            spool.set_state('acquisition_active', '1')
        finally:
            spool.close()
        self.assertTrue(web_module.reconcile_stopped_spool(self.config))
        spool = web_module.DurableSpool(Path(self.config['SPOOL_DIR']), self.config['SPOOL_MAX_BYTES'])
        try:
            self.assertEqual(spool.state_value('acquisition_active'), '0')
        finally:
            spool.close()
        runtime = json.loads((Path(self.config['SPOOL_DIR']) / 'status.json').read_text())
        self.assertEqual(runtime['state'], 'stopped')
        self.assertEqual(runtime['pending_batches'], 0)

    # =========================================================================
    # Group Phase 2: Ticket 03 Atomic & Durable Config Storage Tests
    # =========================================================================

    def test_corrupted_config_fails_visibly_and_never_overwrites(self):
        """Ticket 03: Malformed JSON causes GET and POST to fail visibly, never overwriting with {}."""
        # Intentionally write invalid JSON
        self.config_path.write_text("{ this is malformed json", encoding="utf-8")

        # 1. GET /api/config should fail visibly
        res = self.client.get("/api/config")
        self.assertIn(res.status_code, (500, 503))
        self.assertIn("error", res.get_json())

        # 2. POST /api/config should reject rather than merging into {}
        res = self.client.post("/api/config", json={"CLOCK_RATE": 1200})
        self.assertIn(res.status_code, (500, 503))
        # File must remain untouched (still the corrupted file, not overwritten with empty defaults)
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), "{ this is malformed json")

    def test_write_config_preserves_old_file_on_write_interruption(self):
        """Ticket 03: Fault injected before replace leaves old config file completely intact."""
        initial_content = self.config_path.read_text(encoding="utf-8")

        # Invalidate os.replace by raising an OSError
        with patch("os.replace", side_effect=OSError("Disk write error")):
            success = web_module.write_config({"CLOCK_RATE": 1800})
            self.assertFalse(success)

        # Confirm old content is 100% intact
        self.assertEqual(self.config_path.read_text(encoding="utf-8"), initial_content)

    def test_write_config_preserves_file_permissions(self):
        """Ticket 03: Restrictive permissions (e.g. 0o600) are preserved after write."""
        self.config_path.chmod(0o600)
        success = web_module.write_config(self.config)
        self.assertTrue(success)
        # Check permissions
        current_mode = self.config_path.stat().st_mode & 0o777
        self.assertEqual(current_mode, 0o600)

    # =========================================================================
    # Group Phase 3: Tickets 04 & 05 Destination Switch Recovery & Retention Tests
    # =========================================================================

    def test_stale_config_submission_returns_409_conflict(self):
        """Ticket 04: Submitting an outdated revision returns 409 Conflict."""
        # Initial readback
        res = self.client.get("/api/config")
        data1 = res.get_json()
        rev1 = data1.get("_REV")

        # First save succeeds and advances revision
        data1["CLOCK_RATE"] = 1500
        res = self.client.post("/api/config", json=data1)
        self.assertEqual(res.status_code, 200)

        # Second save with stale revision rev1 should fail with 409
        stale_payload = dict(data1)
        stale_payload["_REV"] = rev1
        stale_payload["CLOCK_RATE"] = 1800
        res = self.client.post("/api/config", json=stale_payload)
        self.assertEqual(res.status_code, 409)
        self.assertIn("modified", res.get_json().get("message", "").lower())

    def test_concurrent_saves_serialize_and_reject_second_stale_revision(self):
        base = self.client.get("/api/config").get_json()
        cookie = self.client.get_cookie(auth.COOKIE_NAME).value
        def submit(rate):
            client = web_module.app.test_client()
            client.set_cookie(auth.COOKIE_NAME, cookie)
            payload = dict(base, CLOCK_RATE=rate)
            response = client.post("/api/config", json=payload)
            return response.status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, (1500, 1600)))
        self.assertCountEqual(statuses, (200, 409))
        self.assertIn(json.loads(self.config_path.read_text())["CLOCK_RATE"], (1500, 1600))

    def test_preflight_destination_failure_keeps_running_acquisition_untouched(self):
        """Ticket 04: If new destination cannot connect, preflight fails before stopping running acquisition."""
        payload = self.client.get("/api/config").get_json()
        payload["DESTINATION"] = "influxdb"
        payload["INFLUX_URL"] = "http://unreachable-host-9999:8086"
        payload["INFLUX_TOKEN"] = "token"

        with patch.object(web_module, "_test_destination", side_effect=ConnectionError("offline")), \
             patch.object(web_module, "get_running_process", return_value=(12345, "production")), \
             patch.object(web_module, "stop_acquisition") as mock_stop:
            res = self.client.post("/api/config", json=payload)
            # Must fail preflight
            self.assertIn(res.status_code, (400, 502))
            # Must NOT stop running acquisition
            mock_stop.assert_not_called()

    def test_new_destination_start_failure_reports_persisted_stopped_config(self):
        payload = self.client.get("/api/config").get_json()
        payload["DESTINATION"] = "influxdb"
        payload["INFLUX_URL"] = "http://localhost:8086"
        payload["INFLUX_TOKEN"] = "token"
        with patch.object(web_module, "_test_destination", return_value="ok"), \
             patch.object(web_module, "get_running_process", return_value=(12345, "production")), \
             patch.object(web_module, "stop_acquisition", return_value={"stopped": True}), \
             patch.object(web_module, "start_acquisition", return_value={"started": False, "message": "failed"}):
            res = self.client.post("/api/config", json=payload)
        self.assertEqual(res.status_code, 503)
        body = res.get_json()
        self.assertTrue(body["persisted"])
        self.assertEqual(body["runtime"], "stopped")
        self.assertEqual(body["config"]["DESTINATION"], "influxdb")
        self.assertEqual(json.loads(self.config_path.read_text())["DESTINATION"], "influxdb")

    def test_retention_compensation_on_config_write_failure(self):
        """Ticket 05: If write_config fails after retention applied, compensate policy back to prior."""
        payload = self.client.get("/api/config").get_json()
        payload["DB_RETENTION_DAYS"] = 90

        mock_dest = MagicMock()
        with patch.object(web_module, "TimescaleProductionDestination", return_value=mock_dest), \
             patch.object(web_module, "effective_timescale_retention", side_effect=["90 days", "30 days"]), \
             patch.object(web_module, "write_config", return_value=False):
            res = self.client.post("/api/config", json=payload)
            self.assertEqual(res.status_code, 500)
            # ensure_schema called twice: once for 90 days, once to compensate back to 30 days
            self.assertEqual(mock_dest.ensure_schema.call_count, 2)

    def test_combined_destination_and_retention_change_is_rejected_before_stop(self):
        payload = self.client.get("/api/config").get_json()
        payload["DB_DSN"] = "postgresql://operator:password@new-host:5432/new-db"
        payload["DB_CONNECTION_MODE"] = "dsn"
        payload["DB_RETENTION_DAYS"] = 90
        with patch.object(web_module, "get_running_process", return_value=(12345, "production")), \
             patch.object(web_module, "stop_acquisition") as stop, \
             patch.object(web_module, "_test_destination") as preflight, \
             patch.object(web_module.TimescaleProductionDestination, "ensure_schema") as ensure_schema:
            response = self.client.post("/api/config", json=payload)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["persisted"])
        stop.assert_not_called()
        preflight.assert_not_called()
        ensure_schema.assert_not_called()
        self.assertEqual(json.loads(self.config_path.read_text())["DB_RETENTION_DAYS"], 30)

    def test_new_postgresql_target_does_not_require_preexisting_retention_policy(self):
        payload = self.client.get("/api/config").get_json()
        payload["DB_DSN"] = "postgresql://operator:password@new-host:5432/new-db"
        payload["DB_CONNECTION_MODE"] = "dsn"
        with patch.object(web_module, "get_running_process", return_value=(None, None)), \
             patch.object(web_module, "_test_destination", return_value="ok"), \
             patch.object(web_module, "effective_timescale_retention", side_effect=RuntimeError("no policy")) as read_retention, \
             patch.object(web_module, "drain_spool_for_destination_switch"), \
             patch.object(web_module, "update_spool_owner"):
            response = self.client.post("/api/config", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(json.loads(self.config_path.read_text())["DB_DSN"], payload["DB_DSN"])
        read_retention.assert_not_called()

    def test_retention_readback_mismatch_compensates_and_reports_if_unverified(self):
        payload = self.client.get("/api/config").get_json()
        payload["DB_RETENTION_DAYS"] = 90
        mock_dest = MagicMock()
        with patch.object(web_module, "TimescaleProductionDestination", return_value=mock_dest), \
             patch.object(web_module, "effective_timescale_retention", side_effect=["45 days", "20 days"]):
            res = self.client.post("/api/config", json=payload)
        self.assertEqual(res.status_code, 503)
        body = res.get_json()
        self.assertIn("compensation failed", body["message"])
        self.assertIn("Operator action", body["message"])
        self.assertEqual(json.loads(self.config_path.read_text())["DB_RETENTION_DAYS"], 30)
        self.assertEqual(mock_dest.ensure_schema.call_count, 2)


if __name__ == "__main__":
    unittest.main()
