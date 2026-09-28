"""Compatibility imports for production acquisition callers and launchers."""

from __future__ import annotations

import shutil  # Preserve the existing patch point for spool disk checks.
import time  # Preserve the existing patch point for spool timestamps.

try:
    from .production.acquisition.daq import AdvantechDaq
    from .production.acquisition.pipeline import ProductionPipeline
    from .production.acquisition.runner import run_production
    from .production.config import validate_production_config
    from .production.delivery.identity import destination_identity
    from .production.delivery.influx import InfluxProductionDestination
    from .production.delivery.mqtt import MQTTProductionDestination
    from .production.delivery.timescale import TimescaleProductionDestination
    from .production.fault import AcquisitionFault
    from .production.spool import DurableSpool, clear_spooled_data
except ImportError:
    from production.acquisition.daq import AdvantechDaq
    from production.acquisition.pipeline import ProductionPipeline
    from production.acquisition.runner import run_production
    from production.config import validate_production_config
    from production.delivery.identity import destination_identity
    from production.delivery.influx import InfluxProductionDestination
    from production.delivery.mqtt import MQTTProductionDestination
    from production.delivery.timescale import TimescaleProductionDestination
    from production.fault import AcquisitionFault
    from production.spool import DurableSpool, clear_spooled_data
