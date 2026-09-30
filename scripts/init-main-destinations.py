#!/usr/bin/env python3
"""Create private secrets and a local TLS certificate for the main Compose stack."""

from __future__ import annotations

import os
from pathlib import Path
import secrets
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
CERT_DIR = ROOT / "config" / "mqtt"
SECRET_KEYS = (
    "DAQ_DB_PASSWORD",
    "DAQ_INFLUX_PASSWORD",
    "DAQ_INFLUX_TOKEN",
    "DAQ_MQTT_PASSWORD",
)


def main():
    if not ENV_FILE.exists():
        raise SystemExit("Create .env from .env.example before running this script.")
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    added = []
    for key in SECRET_KEYS:
        if not values.get(key):
            token = "adminpassword123" if key == "DAQ_INFLUX_PASSWORD" else secrets.token_urlsafe(32)
            replacement = f"{key}={token}"
            for index, line in enumerate(lines):
                if line.startswith(key + "="):
                    lines[index] = replacement
                    break
            else:
                lines.append(replacement)
            added.append(key)
    if added:
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)

    CERT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CERT_DIR, 0o700)
    cert = CERT_DIR / "server.crt"
    key = CERT_DIR / "server.key"
    if cert.exists() != key.exists():
        raise SystemExit("MQTT certificate pair is incomplete; restore the missing file first.")
    if not cert.exists():
        with tempfile.TemporaryDirectory(dir=CERT_DIR) as temporary:
            temp_cert = Path(temporary) / "server.crt"
            temp_key = Path(temporary) / "server.key"
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes",
                "-days", "825", "-keyout", str(temp_key), "-out", str(temp_cert),
                "-subj", "/CN=mqtt",
                "-addext", "subjectAltName=DNS:mqtt,DNS:localhost",
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.chmod(temp_key, 0o600)
            os.chmod(temp_cert, 0o644)
            os.replace(temp_key, key)
            os.replace(temp_cert, cert)
    print("Main destination secrets and MQTT certificate are ready.")


if __name__ == "__main__":
    main()
