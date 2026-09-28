"""Fingerprint the selected production destination without exposing secrets."""

from __future__ import annotations

import hashlib
import json
import shlex
import urllib.parse

def destination_identity(cfg):
    """Opaque, non-secret fingerprint for the durable spool's sink ownership."""
    if cfg.DESTINATION == "influxdb":
        parsed = urllib.parse.urlsplit(cfg.INFLUX_URL)
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("INFLUX_URL must not contain credentials; use INFLUX_TOKEN")
        identity = ["influxdb", cfg.INFLUX_URL.rstrip("/"), cfg.INFLUX_ORG, cfg.INFLUX_BUCKET,
                    cfg.INFLUX_MEASUREMENT]
    elif cfg.DESTINATION == "mqtt":
        identity = ["mqtt", str(cfg.MQTT_BROKER), int(cfg.MQTT_PORT),
                    str(getattr(cfg, "MQTT_PRODUCTION_TOPIC_PREFIX", "daq/production/v1")),
                    int(getattr(cfg, "MQTT_PRODUCTION_QOS", 1))]
    else:
        dsn = str(cfg.DB_DSN)
        parsed = urllib.parse.urlsplit(dsn)
        if parsed.scheme in ("postgres", "postgresql"):
            query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            target = [parsed.hostname or query.get("host", ""), parsed.port or query.get("port", ""),
                      parsed.username or query.get("user", ""), query.get("dbname", parsed.path.lstrip("/")),
                      query.get("hostaddr", ""), query.get("service", "")]
        else:
            values = {}
            for token in shlex.split(dsn):
                if "=" not in token:
                    raise ValueError("DB_DSN must be a PostgreSQL URL or keyword DSN")
                key, value = token.split("=", 1)
                values[key.lower()] = value
            target = [values.get("host", ""), values.get("port", ""), values.get("user", ""),
                      values.get("dbname", ""), values.get("service", "")]
            if not any(target):
                raise ValueError("DB_DSN must identify a PostgreSQL host or service")
        mode = getattr(cfg, "DB_CONNECTION_MODE", "fields")
        identity = ["postgresql", mode, *target, cfg.DB_PRODUCTION_TABLE]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
