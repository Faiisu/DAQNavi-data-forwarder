#!/usr/bin/env python3
"""Show the physical AI0 voltage in the terminal. Stop with Ctrl+C."""

import json
import sys
import time
from pathlib import Path

from Automation.BDaq import AiSignalType, ValueRange
from Automation.BDaq.BDaqApi import BioFailed
from Automation.BDaq.InstantAiCtrl import InstantAiCtrl


def main():
    config_path = Path(__file__).resolve().parents[1] / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    ai0 = config["CHANNELS"]["0"]

    device = InstantAiCtrl(config["DEVICE_DESCRIPTION"])
    try:
        device.channels[0].signalType = getattr(AiSignalType, ai0["signal_type"])
        device.channels[0].valueRange = getattr(ValueRange, ai0["value_range"])
        print("Monitoring AI0 voltage. Press Ctrl+C to stop.")
        while True:
            result, values = device.readDataF64(0, 1)
            if BioFailed(result) or not values:
                raise RuntimeError(f"AI0 read failed: {result}")
            print(f"\rAI0: {values[0]:8.4f} V", end="", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        print()
    finally:
        device.dispose()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"\nAI0 monitor failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
