#!/bin/bash
set -e

# Select Python interpreter: prefer PYTHON_BIN or virtualenv if present, otherwise system python3
if [ -n "$PYTHON_BIN" ]; then
    PY="$PYTHON_BIN"
elif [ -f "../../.venv/bin/python" ]; then
    PY="../../.venv/bin/python"
elif [ -f ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

# The saved file is authoritative after initial setup.
CONFIG_FILE="${DAQ_CONFIG_PATH:-}"
if [ -z "$CONFIG_FILE" ]; then
    if [ -f "/app/config/config.json" ]; then
        CONFIG_FILE="/app/config/config.json"
    elif [ -f "config.json" ]; then
        CONFIG_FILE="config.json"
    fi
fi

if [ -n "$CONFIG_FILE" ] && [ ! -f "$CONFIG_FILE" ]; then
    CONFIG_DIR=$(dirname "$CONFIG_FILE")
    mkdir -p "$CONFIG_DIR"
    if [ -f "config.json" ]; then
        cp "config.json" "$CONFIG_FILE"
        chmod 600 "$CONFIG_FILE" 2>/dev/null || true
    fi
fi

# Pass through custom command if invoked with python, bash, sh, etc.
if [ "$#" -gt 0 ] && [ "$1" != "app.py" ]; then
    if [ "$1" = "python" ] || [ "$1" = "python3" ] || [ "$1" = "bash" ] || [ "$1" = "sh" ] || [ "$1" = "/bin/bash" ] || [ "$1" = "/bin/sh" ]; then
        exec "$@"
    fi
fi

# Web GUI mode vs Headless Daemon mode
# When ENABLE_WEB_UI is true (default), launches app.py which binds port 8081 and manages streaming
if [ "${ENABLE_WEB_UI:-true}" = "true" ] && [ "${HEADLESS:-false}" != "true" ]; then
    echo "[DAQ-Navi Entrypoint] Launching DAQ Control Panel Web GUI on port 8081 (app.py)..."
    exec $PY app.py "$@"
fi

if [ -n "$CONFIG_FILE" ] && [ -f "$CONFIG_FILE" ] && \
    "$PY" -c 'import json,sys; c=json.load(open(sys.argv[1])); sys.exit(0 if c.get("AUTO_START_MODE") == "mockup" else 1)' "$CONFIG_FILE"; then
    echo "[DAQ-Navi Entrypoint] ERROR: Retired mockup configuration cannot start headless physical acquisition. Review and save physical settings first." >&2
    exit 1
fi

STREAM_SCRIPT="core/buffered_daq_to_timescaledb.py"
[ -f "$STREAM_SCRIPT" ] || STREAM_SCRIPT="buffered_daq_to_timescaledb.py"

# Hardware acquisition must fail visibly if the driver is unavailable.
if [ ! -f "/usr/lib/libbiodaq.so" ] && [ ! -f "/opt/advantech/libs/libbiodaq.so" ] && [ ! -f "/usr/local/lib/libbiodaq.so" ]; then
    echo "[DAQ-Navi Entrypoint] ERROR: Advantech driver library (libbiodaq.so) not found in container paths." >&2
    exit 1
fi
echo "[DAQ-Navi Entrypoint] Launching hardware DAQ streaming pipeline ($STREAM_SCRIPT)..."
exec "$PY" "$STREAM_SCRIPT" "$@"
