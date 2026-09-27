#!/usr/bin/env python3
"""Initialize the default table used by the E2E Config Center switch check."""

import json
from pathlib import Path

from core.config_loader import DaqNaviConfig
from core.production_acquisition import TimescaleProductionDestination


config_path = Path("/app/config/config.json")
config = json.loads(config_path.read_text(encoding="utf-8"))
config.update(
    {
        "DESTINATION": "postgresql",
        "DB_HOST": "pg",
        "DB_PORT": 5432,
        "DB_NAME": "daq_navi_test_local",
        "DB_USER": "admin",
        "DB_PASSWORD": "testpass",
        "DB_DSN": "postgresql://admin:testpass@pg:5432/daq_navi_test_local",
        "DB_PRODUCTION_TABLE": "daq_production_samples",
        "DB_RETENTION_DAYS": 30,
    }
)

destination = TimescaleProductionDestination(
    DaqNaviConfig(config, allow_env_overrides=False)
)
destination.ensure_schema()
