#!/usr/bin/env python3
"""Fill first-run DAQNavi config with the main Compose destination credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import quote


def main(path: Path):
    config = json.loads(path.read_text(encoding="utf-8"))
    required = ("DAQ_DB_PASSWORD", "DAQ_INFLUX_TOKEN", "DAQ_MQTT_PASSWORD")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise SystemExit("Missing main destination secrets: " + ", ".join(missing))

    db_name = os.environ.get("DAQ_DB_NAME", "daq_db")
    db_user = os.environ.get("DAQ_DB_USER", "admin")
    db_password = os.environ["DAQ_DB_PASSWORD"]
    config.update({
        "DB_CONNECTION_MODE": "fields",
        "DB_HOST": "timescaledb",
        "DB_PORT": 5432,
        "DB_NAME": db_name,
        "DB_USER": db_user,
        "DB_PASSWORD": db_password,
        "DB_DSN": "postgresql://{}:{}@timescaledb:5432/{}".format(
            quote(db_user, safe=""), quote(db_password, safe=""),
            quote(db_name, safe="")),
        "INFLUX_URL": "http://influxdb:8086",
        "INFLUX_ORG": os.environ.get("DAQ_INFLUX_ORG", "mddp"),
        "INFLUX_BUCKET": os.environ.get("DAQ_INFLUX_BUCKET", "daq_telemetry"),
        "INFLUX_TOKEN": os.environ["DAQ_INFLUX_TOKEN"],
        "MQTT_BROKER": "mqtt",
        "MQTT_PORT": 8883,
        "MQTT_USERNAME": os.environ.get("DAQ_MQTT_USER", "daqnavi"),
        "MQTT_PASSWORD": os.environ["DAQ_MQTT_PASSWORD"],
        "MQTT_TLS_ENABLED": True,
        "MQTT_CA_CERTS": "/main-mqtt/server.crt",
    })
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".config-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(config, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
