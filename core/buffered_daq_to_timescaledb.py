#!/usr/bin/env python3
"""Start the durable physical DAQ acquisition pipeline."""

import argparse
import logging
import signal
import sys
import threading

try:
    from .config_loader import load_daq_config
    from .production_acquisition import run_production
except ImportError:
    from config_loader import load_daq_config
    from production_acquisition import run_production


def main():
    parser = argparse.ArgumentParser(description="Standalone physical DAQ acquisition")
    parser.add_argument("--config", help="DAQ configuration JSON path")
    args = parser.parse_args()
    config = load_daq_config(args.config)
    requested_stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: requested_stop.set())
    signal.signal(signal.SIGINT, lambda *_: requested_stop.set())
    try:
        run_production(config, stop_event=requested_stop)
    except Exception as exc:
        logging.getLogger(__name__).error("Physical production acquisition failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
