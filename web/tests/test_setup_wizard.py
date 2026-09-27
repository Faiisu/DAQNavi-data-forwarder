import os
import subprocess
import unittest

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = SERVICE_DIR
WIZARD_SCRIPT = os.path.join(PROJECT_ROOT, "scripts", "setup_wizard.sh")
CONFIG_JSON_PATH = os.path.join(SERVICE_DIR, "config.json")


@unittest.skip("the setup wizard is not part of the standalone DAQNavi project")
class TestSetupWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        """Read wizard script once for all inspection tests."""
        cls.script_content = ""
        if os.path.isfile(WIZARD_SCRIPT):
            with open(WIZARD_SCRIPT, "r", encoding="utf-8") as f:
                cls.script_content = f.read()

    def test_wizard_file_exists_and_executable(self):
        """Wizard script must exist in scripts/setup_wizard.sh and be executable."""
        self.assertTrue(
            os.path.isfile(WIZARD_SCRIPT),
            f"Wizard script not found at {WIZARD_SCRIPT}",
        )
        self.assertTrue(
            os.access(WIZARD_SCRIPT, os.X_OK),
            f"Wizard script {WIZARD_SCRIPT} is not marked executable",
        )

    def test_bash_syntax_validity(self):
        """Wizard bash script must pass bash -n syntax check cleanly."""
        result = subprocess.run(
            ["bash", "-n", WIZARD_SCRIPT],
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"bash -n syntax check failed:\nStdout: {result.stdout}\nStderr: {result.stderr}",
        )

    def test_wizard_library_and_stages_structure(self):
        """Wizard must preserve the standard wizard library and declare TOTAL_STAGES=6."""
        # Check library helper functions are present
        self.assertIn("banner()", self.script_content)
        self.assertIn("stage()", self.script_content)
        self.assertIn("say()", self.script_content)
        self.assertIn("step()", self.script_content)
        self.assertIn("note()", self.script_content)
        self.assertIn("warn()", self.script_content)
        self.assertIn("open_url()", self.script_content)
        self.assertIn("pause()", self.script_content)
        self.assertIn("confirm()", self.script_content)
        self.assertIn("ask()", self.script_content)
        self.assertIn("ask_secret()", self.script_content)
        self.assertIn("write_env()", self.script_content)
        self.assertIn("finish()", self.script_content)

        # Check TOTAL_STAGES
        self.assertIn("TOTAL_STAGES=6", self.script_content)

    def test_stage_1_os_and_kernel_detection(self):
        """Stage 1 must detect OS distribution (Ubuntu/Debian) and kernel version."""
        self.assertIn("/etc/os-release", self.script_content)
        self.assertIn("uname -r", self.script_content)
        self.assertIn("Ubuntu", self.script_content)
        self.assertIn("Debian", self.script_content)

    def test_stage_2_pci_card_detection(self):
        """Stage 2 must check for PCI-1716 card presence via lspci and offer recovery steps."""
        self.assertIn("lspci", self.script_content)
        self.assertTrue("13fe" in self.script_content or "1716" in self.script_content or "Advantech" in self.script_content)
        self.assertIn("PCI", self.script_content)

    def test_stage_3_driver_guidance(self):
        """Stage 3 must guide DAQNavi driver install, check dpkg/rpm, lsmod, and permissions."""
        self.assertIn("advantech.com", self.script_content)
        self.assertIn("dpkg -i", self.script_content)
        self.assertIn("rpm -ivh", self.script_content)
        self.assertIn("lsmod", self.script_content)
        self.assertTrue("libbiodaq" in self.script_content or "dev_enum" in self.script_content)
        self.assertIn("dialout", self.script_content)

    def test_stage_4_docker_setup(self):
        """Stage 4 must check and guide Docker & Docker Compose installation."""
        self.assertIn("docker", self.script_content)
        self.assertIn("compose", self.script_content)

    def test_stage_5_config_customization(self):
        """Stage 5 must customize config.json and .env variables including DB host and port."""
        self.assertIn("config.json", self.script_content)
        self.assertIn("DEVICE_DESCRIPTION", self.script_content)
        self.assertIn("CHANNEL_COUNT", self.script_content)
        self.assertIn("DB_HOST", self.script_content)
        self.assertIn("DB_PORT", self.script_content)
        self.assertIn("POSTGRES_USER", self.script_content)
        self.assertIn("MOCKUP_MODE", self.script_content)

    def test_stage_6_docker_compose_verification(self):
        """Stage 6 must offer docker-compose up verification and status inspection."""
        self.assertTrue("docker compose up" in self.script_content or "docker-compose up" in self.script_content)
        self.assertIn("ALL_HEALTHY", self.script_content)

    def test_check_only_flag_execution(self):
        """Running the wizard with --check-only runs prerequisites inspection without blocking."""
        result = subprocess.run(
            ["bash", WIZARD_SCRIPT, "--check-only"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
        )
        self.assertEqual(result.returncode, 0, f"Error running --check-only:\n{result.stderr}")
        self.assertIn("Prerequisite Check", result.stdout)
        self.assertIn("Kernel", result.stdout)


if __name__ == "__main__":
    unittest.main()
