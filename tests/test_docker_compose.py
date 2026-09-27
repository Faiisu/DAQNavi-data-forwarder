"""Deployment contract for the standalone DAQNavi Compose project."""

import os
from pathlib import Path
import unittest
from unittest.mock import patch

import yaml

from core.config_loader import load_daq_config


ROOT = Path(__file__).resolve().parents[1]


class StandaloneComposeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compose = yaml.safe_load((ROOT / "compose.yml").read_text(encoding="utf-8"))
        cls.service = cls.compose["services"]["daq-navi"]

    def test_compose_contains_only_the_daqnavi_service(self):
        self.assertEqual(set(self.compose["services"]), {"daq-navi"})
        self.assertEqual(self.service["build"], ".")

    def test_hardware_driver_and_device_nodes_are_passed_through(self):
        self.assertTrue(self.service["privileged"])
        volumes = self.service["volumes"]
        for mount in ("/dev:/dev", "/usr/lib:/usr/lib:ro",
                      "/opt/advantech:/opt/advantech:ro",
                      "/etc/biobdaq:/etc/biobdaq:ro", "/var/lib/daq:/var/lib/daq:ro"):
            self.assertIn(mount, volumes)

    def test_config_and_production_spool_are_persistent(self):
        self.assertIn("./config:/app/config", self.service["volumes"])
        self.assertIn("daq_spool:/var/lib/daq_navi/spool", self.service["volumes"])
        self.assertIn("daq_spool", self.compose["volumes"])

    def test_service_restarts_and_has_http_healthcheck(self):
        self.assertEqual(self.service["restart"], "unless-stopped")
        self.assertIn("healthcheck", self.service)
        self.assertIn("/api/health", " ".join(self.service["healthcheck"]["test"]))

    def test_environment_template_requires_session_secret(self):
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("DAQ_SESSION_KEY", env_example)
        self.assertIn("${DAQ_SESSION_KEY:?Set DAQ_SESSION_KEY in .env}",
                      self.service["environment"]["DAQ_SESSION_KEY"])

    def test_device_scan_dependency_is_declared(self):
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        self.assertIn("pyserial", requirements)

    def test_docker_image_installs_declared_runtime_dependencies(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.12", dockerfile)
        self.assertIn("COPY requirements.txt", dockerfile)
        self.assertIn("entrypoint.sh", dockerfile)

    def test_entrypoint_uses_web_service_by_default_and_only_hardware_headless(self):
        entrypoint = (ROOT / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertIn('ENABLE_WEB_UI:-true', entrypoint)
        self.assertIn('exec $PY app.py', entrypoint)
        self.assertNotIn('MOCKUP_MODE', entrypoint)
        self.assertIn('core/buffered_daq_to_timescaledb.py', entrypoint)
        self.assertNotIn('core/mockup_stream_to_db.py', entrypoint)
        self.assertNotIn('AUTO_FALLBACK', entrypoint)

    def test_saved_configuration_ignores_container_environment_overrides(self):
        base = load_daq_config(str(ROOT / "config.json"))
        overrides = {
            "DESTINATION": "postgresql",
            "DB_DSN": "postgresql://test:test@db.example:5432/test",
            "INFLUX_URL": "http://influx.example:8086", "INFLUX_TOKEN": "test-token",
        }
        with patch.dict(os.environ, overrides):
            loaded = load_daq_config(str(ROOT / "config.json"))
        for name in overrides:
            self.assertEqual(getattr(loaded, name), getattr(base, name))


if __name__ == "__main__":
    unittest.main()
