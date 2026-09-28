"""Validate physical acquisition configuration and identify spool ownership."""

from __future__ import annotations

import re
import urllib.parse
try:
    from ..config_loader import AiSignalType, ValueRange, validate_config_values, validate_finite_number
except ImportError:
    from config_loader import AiSignalType, ValueRange, validate_config_values, validate_finite_number
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")

def validate_production_config(cfg):
    validate_config_values(cfg.raw)
    dest = str(cfg.DESTINATION).lower()
    if dest not in ("postgresql", "timescaledb", "influxdb", "mqtt"):
        raise ValueError("production destination must be PostgreSQL/TimescaleDB, InfluxDB, or MQTT")
    if not isinstance(cfg.DEVICE_ID, str) or not cfg.DEVICE_ID.strip():
        raise ValueError("DEVICE_ID is required")
    if cfg.SECTION_COUNT != 0:
        raise ValueError("SECTION_COUNT must be zero for continuous production acquisition")
    if cfg.START_CHANNEL < 0 or cfg.START_CHANNEL + cfg.CHANNEL_COUNT > 16:
        raise ValueError("configured DAQ channel span is invalid")
    if cfg.SPOOL_MAX_BYTES <= 0:
        raise ValueError("SPOOL_MAX_BYTES must be positive")
    if not _IDENTIFIER.fullmatch(cfg.DB_PRODUCTION_TABLE):
        raise ValueError("DB_PRODUCTION_TABLE must be a simple SQL identifier")
    if cfg.DB_PRODUCTION_TABLE == cfg.DB_TABLE:
        raise ValueError("production table must differ from the legacy table")
    if dest == "influxdb":
        parsed = urllib.parse.urlsplit(str(cfg.INFLUX_URL))
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("INFLUX_URL must be a valid HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("INFLUX_URL must not contain credentials; use INFLUX_TOKEN")
        if not cfg.INFLUX_ORG.strip() or not cfg.INFLUX_BUCKET.strip() or not cfg.INFLUX_TOKEN.strip():
            raise ValueError("InfluxDB organization, bucket, and token are required")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", cfg.INFLUX_MEASUREMENT):
            raise ValueError("INFLUX_MEASUREMENT must be a simple measurement name")
        if "\n" in cfg.DEVICE_ID or "\r" in cfg.DEVICE_ID:
            raise ValueError("DEVICE_ID cannot contain newline characters for InfluxDB")
    elif dest == "mqtt":
        if not getattr(cfg, "MQTT_TLS_ENABLED", False):
            raise ValueError("MQTT production destination requires TLS to be enabled")
        username = getattr(cfg, "MQTT_USERNAME", "")
        if not username or not str(username).strip():
            raise ValueError("MQTT production destination requires username")
        password = getattr(cfg, "MQTT_PASSWORD", "")
        if not password or not str(password).strip():
            raise ValueError("MQTT production destination requires password")
        qos = getattr(cfg, "MQTT_PRODUCTION_QOS", 1)
        if qos not in (0, 1):
            raise ValueError("MQTT production QoS must be 0 or 1")
        topic_prefix = getattr(cfg, "MQTT_PRODUCTION_TOPIC_PREFIX", "")
        if not topic_prefix or not str(topic_prefix).strip() or "+" in topic_prefix or "#" in topic_prefix:
            raise ValueError("MQTT production topic prefix must be non-empty and cannot contain wildcards (+ or #)")
    selected_span = range(cfg.START_CHANNEL, cfg.START_CHANNEL + cfg.CHANNEL_COUNT)
    for index, channel in cfg.channels.items():
        if channel.enabled and index not in selected_span:
            raise ValueError(f"enabled channel {index} is outside the configured DAQ span")
    enabled = []
    pci_1716 = re.match(r'^PCI-1716(?:H|L)?(?:,|$)', cfg.DEVICE_DESCRIPTION, re.IGNORECASE)
    for index in range(cfg.START_CHANNEL, cfg.START_CHANNEL + cfg.CHANNEL_COUNT):
        channel = cfg.channels.get(index)
        raw_channel = cfg.raw.get("CHANNELS", {}).get(str(index))
        if raw_channel is None:
            raise ValueError(f"channel {index} configuration is required")
        for setting, enum in (("signal_type", AiSignalType), ("value_range", ValueRange)):
            name = raw_channel.get(setting)
            members = getattr(enum, "__members__", vars(enum))
            if not isinstance(name, str) or name not in members:
                raise ValueError(f"channel {index} {setting} is invalid")
        signal_type = raw_channel['signal_type']
        if pci_1716:
            if signal_type == 'PseudoDifferential':
                raise ValueError(f"channel {index} PseudoDifferential is not supported by PCI-1716")
            if channel.enabled and signal_type == 'Differential':
                if index % 2:
                    raise ValueError(f"channel {index} Differential must use an even-numbered channel")
                other = cfg.channels.get(index + 1)
                if other is not None and other.enabled:
                    raise ValueError(f"channel {index + 1} cannot be enabled when channel {index} is Differential")
        if channel is None or not channel.enabled:
            continue
        enabled.append(index)
        if not isinstance(raw_channel.get("label"), str):
            raise ValueError(f"channel {index} label is required")
        if not channel.label.strip():
            raise ValueError(f"channel {index} label is required")
        if not isinstance(raw_channel.get("unit"), str) or not channel.unit:
            raise ValueError(f"channel {index} unit is required")
        if not channel.scale_enabled or not channel.has_scale:
            raise ValueError(f"channel {index} calibration must be enabled")
        if cfg.DESTINATION == "influxdb" and any("\n" in value or "\r" in value for value in (
                channel.unit, channel.label)):
            raise ValueError(f"channel {index} InfluxDB tag values cannot contain newline characters")
        scale = raw_channel.get("scale", {})
        if type(scale.get("enabled")) is not bool or any(key not in scale for key in (
            "low_voltage", "high_voltage", "low_value", "high_value"
        )):
            raise ValueError(f"channel {index} calibration fields are required")
        for key in ("low_voltage", "high_voltage", "low_value", "high_value"):
            validate_finite_number(getattr(channel, key), f"channel {index} calibration {key}")
        if channel.high_voltage == channel.low_voltage:
            raise ValueError(f"channel {index} calibration voltage span must be nonzero")
    if not enabled:
        raise ValueError("at least one physical channel must be enabled")
    return tuple(enabled)
