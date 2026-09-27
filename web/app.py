#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
services.daq_navi.app
─────────────────────
Public entrypoint seam for the DAQ Navi Web Control Panel & REST API.
Delegates to `services.daq_navi.web.app` while preserving backward compatibility
for external runners (Docker Compose, Linux scripts, and unit tests).
"""

import os
import sys

# Ensure root directory and package directory are in sys.path
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(BASE_DIR, '..', '..'))
for path in (BASE_DIR, PROJECT_ROOT):
    if os.path.exists(path) and path not in sys.path:
        sys.path.insert(0, path)

try:
    from services.daq_navi.web.app import app, socketio, init_application
except ModuleNotFoundError:
    from web.app import app, socketio, init_application

__all__ = ["app", "socketio", "init_application"]

if __name__ == '__main__':
    init_application()
    # Served on Port 8081 (bound to all network interfaces for LAN edge access)
    socketio.run(app, host='0.0.0.0', port=8081, debug=False)
