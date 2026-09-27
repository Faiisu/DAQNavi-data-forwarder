#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_docker_compose.py
───────────────────────
Tests for independent infrastructure, DAQ, and Portal Compose deployments.
- Verifies docker-compose.yml defines: timescaledb and influxdb
- Verifies TimescaleDB auto-initialization with db_setup.sql
- Verifies daq-navi container: privileged, /dev, /usr/lib, /etc/biobdaq, config.json bind-mount
- Verifies each deployment has its own environment template
- Verifies daq-navi Dockerfile and entrypoint script
- Verifies config_loader environment variable overrides
"""

import os
import sys
import unittest
from unittest.mock import patch
import yaml

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORE_DIR = os.path.join(SERVICE_DIR, "core")
PROJECT_ROOT = os.path.abspath(os.path.join(SERVICE_DIR, "..", ".."))
for p in (CORE_DIR, SERVICE_DIR, PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from config_loader import load_daq_config


class TestDockerComposeStack(unittest.TestCase):
    def setUp(self):
        self.project_root = PROJECT_ROOT
        self.compose_path = os.path.join(self.project_root, "docker-compose.yml")
        self.env_example_path = os.path.join(self.project_root, ".env.example")
        self.daq_compose_path = os.path.join(self.project_root, "deploy", "daq-navi", "compose.yml")
        self.portal_compose_path = os.path.join(self.project_root, "deploy", "portal", "compose.yml")
        self.dockerfile_path = os.path.join(self.project_root, "services", "daq_navi", "Dockerfile")
        self.entrypoint_path = os.path.join(self.project_root, "services", "daq_navi", "entrypoint.sh")

    def test_docker_compose_file_exists_and_parses(self):
        self.assertTrue(os.path.exists(self.compose_path), "docker-compose.yml does not exist")
        with open(self.compose_path, "r", encoding="utf-8") as f:
            compose_data = yaml.safe_load(f)
        self.assertIsInstance(compose_data, dict)
        self.assertIn("services", compose_data)

    def test_required_services_defined(self):
        with open(self.compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        services = data.get("services", {})
        required = ["timescaledb", "influxdb"]
        for req in required:
            self.assertIn(req, services, f"Required service '{req}' missing from docker-compose.yml")
        self.assertNotIn("daq-navi", services)
        self.assertNotIn("portal", services)

    def test_timescaledb_service_configuration(self):
        with open(self.compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        tsdb = data["services"]["timescaledb"]
        self.assertIn("timescale/timescaledb", tsdb.get("image", ""))
        self.assertIn("pg16", tsdb.get("image", ""))
        
        # Verify db_setup.sql mount in docker-entrypoint-initdb.d
        volumes = tsdb.get("volumes", [])
        has_init_sql = any("docker-entrypoint-initdb.d" in str(v) and "db_setup.sql" in str(v) for v in volumes)
        self.assertTrue(has_init_sql, "timescaledb must mount db_setup.sql into docker-entrypoint-initdb.d")

        # Verify healthcheck
        self.assertIn("healthcheck", tsdb, "timescaledb must configure healthcheck")

    def test_influxdb_service_configuration(self):
        with open(self.compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        influx = data["services"]["influxdb"]
        self.assertIn("influxdb", influx.get("image", ""))
        
        # Verify port 8086
        ports = [str(p) for p in influx.get("ports", [])]
        has_port = any("8086" in p for p in ports)
        self.assertTrue(has_port, "influxdb must expose port 8086")

    def test_daq_navi_service_configuration(self):
        with open(self.daq_compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        daq = data["services"]["daq-navi"]
        
        # Verify privileged: true
        self.assertTrue(daq.get("privileged", False), "daq-navi must have privileged: true")
        
        # Verify volume mounts: /dev, /usr/lib, /etc/biobdaq, config.json
        volumes = [str(v) for v in daq.get("volumes", [])]
        self.assertTrue(any("/dev" in v for v in volumes), "daq-navi must mount /dev")
        self.assertTrue(any("/usr/lib" in v for v in volumes), "daq-navi must mount /usr/lib")
        self.assertTrue(any("/etc/biobdaq" in v for v in volumes), "daq-navi must mount /etc/biobdaq")
        self.assertIn("./config:/app/config", volumes, "daq-navi must mount the private config directory for atomic replacement")
        self.assertTrue(data["volumes"]["daq_spool"]["external"])
        self.assertTrue(data["networks"]["infrastructure"]["external"])

    def test_portal_port(self):
        with open(self.portal_compose_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        portal = data["services"]["portal"]
        
        portal_ports = [str(p) for p in portal.get("ports", [])]
        self.assertTrue(any("8080" in p for p in portal_ports), "portal must expose port 8080")
        

    def test_env_example_file(self):
        self.assertTrue(os.path.exists(self.env_example_path), f"Missing {self.env_example_path}")
        with open(self.env_example_path, "r", encoding="utf-8") as f:
            content = f.read()
        for k in ["POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB", "DB_PORT", "INFLUX_PORT", "INFLUX_TOKEN"]:
            self.assertIn(k, content, f"Missing key '{k}' in .env.example")
        self.assertNotIn("MOCKUP_MODE", content)
        self.assertNotIn("PORTAL_PORT", content)
        for name, key in [("daq-navi", "DAQ_PORT"), ("portal", "PORTAL_PORT")]:
            path = os.path.join(self.project_root, "deploy", name, ".env.example")
            with open(path, "r", encoding="utf-8") as f:
                self.assertIn(key, f.read())

    def test_daq_navi_dockerfile_and_entrypoint(self):
        self.assertTrue(os.path.exists(self.dockerfile_path), f"Missing {self.dockerfile_path}")
        with open(self.dockerfile_path, "r", encoding="utf-8") as f:
            df_content = f.read()
        self.assertIn("FROM python:", df_content)
        self.assertIn("requirements.txt", df_content)
        self.assertIn("entrypoint.sh", df_content)

        self.assertTrue(os.path.exists(self.entrypoint_path), f"Missing {self.entrypoint_path}")
        with open(self.entrypoint_path, "r", encoding="utf-8") as f:
            ep_content = f.read()
        self.assertIn("MOCKUP_MODE", ep_content)
        self.assertIn("mockup_stream_to_db.py", ep_content)
        self.assertIn("buffered_daq_to_timescaledb.py", ep_content)

    def test_saved_daq_config_ignores_container_environment(self):
        config_path = os.path.join(SERVICE_DIR, "config.json")
        base_cfg = load_daq_config(config_path)
        overrides = {
            "MOCKUP_MODE": "true", "DESTINATION": "postgresql",
            "DB_DSN": "postgresql://test:test@timescaledb:5432/testdb",
            "INFLUX_URL": "http://influxdb:8086", "INFLUX_TOKEN": "token123",
        }
        with patch.dict(os.environ, overrides):
            cfg = load_daq_config()
            for name in overrides:
                self.assertEqual(getattr(cfg, name), getattr(base_cfg, name))


if __name__ == "__main__":
    unittest.main()
