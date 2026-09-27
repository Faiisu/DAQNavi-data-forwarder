#!/usr/bin/env python3
"""Compatibility launcher for web processes started before the script rename."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    runpy.run_path(
        str(Path(__file__).with_name("buffered_daq_to_timescaledb.py")),
        run_name="__main__",
    )
