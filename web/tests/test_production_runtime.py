"""Behavior checks for the standalone production container boundary."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT


class ProductionRuntimeTests(unittest.TestCase):
    def test_previous_production_launcher_remains_executable(self):
        result = subprocess.run(
            [sys.executable,
             str(SERVICE / "core" / "stream_to_db.py"), "--help"],
            cwd=SERVICE,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--config", result.stdout)

    def test_failed_hardware_run_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "invocations"
            python_stub = Path(directory) / "python-stub"
            python_stub.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >> "$INVOCATION_LOG"\n'
                "exit 17\n"
            )
            python_stub.chmod(0o755)

            env = os.environ.copy()
            env.update(
                {
                    "PYTHON_BIN": str(python_stub),
                    "INVOCATION_LOG": str(log),
                    "ENABLE_WEB_UI": "false",
                    "HEADLESS": "true",
                    "AUTO_FALLBACK": "true",
                }
            )
            result = subprocess.run(
                ["bash", str(SERVICE / "entrypoint.sh")],
                cwd=SERVICE,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            invocations = log.read_text().splitlines() if log.exists() else []
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(all("mockup_stream_to_db" not in call for call in invocations))

    def test_compose_persists_standalone_buffer(self):
        compose_file = ROOT / "compose.yml"
        if not compose_file.exists():
            self.skipTest("standalone compose.yml is not available")
        try:
            import yaml
            config = yaml.safe_load(compose_file.read_text())
            mounts = config["services"]["daq-navi"]["volumes"]
            self.assertIn("daq_spool:/var/lib/daq_navi/spool", mounts)
            self.assertIn("./config:/app/config", mounts)
            self.assertIn("daq_spool", config["volumes"])
        except ImportError:
            text = compose_file.read_text()
            self.assertIn("daq_spool:/var/lib/daq_navi/spool", text)
            self.assertIn("daq_spool:", text)


if __name__ == "__main__":
    unittest.main()
