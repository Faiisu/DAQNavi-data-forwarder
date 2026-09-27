# web_gui.py
# See: docs/architecture/context.md
# English comments only

try:
    import eventlet
    eventlet.monkey_patch()
except Exception as _e:
    print(f"[WARNING] Eventlet initialization warning: {_e}")

import os
import sys
import stat
import json
import subprocess
import threading
import re
import time
import signal
import tempfile
import math
import uuid
import hashlib
from datetime import datetime
import csv
import io
import urllib.parse
import urllib.request
from pathlib import Path
from flask import Flask, render_template, jsonify, request, redirect, url_for, has_request_context
from flask_socketio import SocketIO, emit

from . import auth

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.dirname(WEB_DIR)
for p in (SERVICE_DIR, WEB_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)
PROJECT_ROOT = os.path.abspath(os.path.join(SERVICE_DIR, '..', '..'))
if os.path.exists(PROJECT_ROOT) and PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from daq_navi.core.config_loader import DaqNaviConfig
    from daq_navi.core.production_acquisition import (
        AcquisitionFault, DurableSpool, TimescaleProductionDestination, InfluxProductionDestination,
        validate_production_config, destination_identity,
    )
except ModuleNotFoundError:
    from core.config_loader import DaqNaviConfig
    from core.production_acquisition import (
        AcquisitionFault, DurableSpool, TimescaleProductionDestination, InfluxProductionDestination,
        validate_production_config, destination_identity,
    )

COOKIE_NAME = auth.COOKIE_NAME
session_store = None
operator_users = {}

PORTAL_ORIGIN = os.getenv("PORTAL_ORIGIN", "http://localhost:8080").strip()
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "http://localhost:8081,http://127.0.0.1:8081").split(",") if o.strip()]
if PORTAL_ORIGIN and PORTAL_ORIGIN not in ALLOWED_ORIGINS:
    ALLOWED_ORIGINS.append(PORTAL_ORIGIN)

def configure_auth(users=None, session_key=None, ttl_seconds=auth.DEFAULT_SESSION_TTL_SECONDS):
    global session_store, operator_users
    if users is None or session_key is None:
        users, session_key = auth.load_auth_secrets()
    session_store = auth.SessionStore(session_key, ttl_seconds=ttl_seconds)
    operator_users = users

def get_current_session():
    if session_store is None:
        return None
    cookie_val = request.cookies.get(COOKIE_NAME)
    return session_store.validate_cookie(cookie_val)

def create_test_session(username="operator"):
    if session_store is None:
        configure_auth(users={username: auth.hash_password("test-pass")}, session_key="test-key-32-bytes-long-secret-key")
    _, cookie_val = session_store.create_session(username)
    return cookie_val

app = Flask(__name__, template_folder='templates', static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*")

@app.before_request
def handle_preflight_and_auth():
    if request.method == "OPTIONS":
        return ('', 204)

    # 1. CSRF / Origin check for mutating state changes
    if request.method in ('POST', 'PUT', 'DELETE'):
        origin = request.headers.get('Origin')
        if origin and not auth.is_allowed_origin(origin, ALLOWED_ORIGINS):
            return jsonify({'error': 'Forbidden: Invalid Origin'}), 403

    # 2. Public endpoints and static assets
    public_paths = {'/login', '/logout', '/api/health', '/favicon.ico'}
    if request.path in public_paths or request.path.startswith('/static/'):
        return None

    # 3. Enforce session authentication if session store is active
    if session_store is not None:
        session = get_current_session()
        if not session:
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            next_url = request.full_path if request.query_string else request.path
            return redirect(url_for('login', next=next_url))

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin')
    if request.path == '/api/health':
        if origin and auth.is_allowed_origin(origin, ALLOWED_ORIGINS):
            response.headers['Access-Control-Allow-Origin'] = origin
            response.headers['Access-Control-Allow-Methods'] = 'GET, OPTIONS'
            response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        elif not origin:
            response.headers['Access-Control-Allow-Origin'] = '*'
    elif origin and auth.is_allowed_origin(origin, ALLOWED_ORIGINS):
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    return response

@app.route('/', defaults={'path': ''}, methods=['OPTIONS'])
@app.route('/<path:path>', methods=['OPTIONS'])
def options_preflight(path=''):
    return ('', 204)

# Service directory paths
WEB_DIR = os.path.dirname(os.path.abspath(__file__))
SERVICE_DIR = os.path.dirname(WEB_DIR)
CORE_DIR = os.path.join(SERVICE_DIR, 'core')

def get_config_path():
    env_path = os.getenv("DAQ_CONFIG_PATH", "").strip()
    if env_path:
        return env_path
    mount_dir = "/app/config"
    if os.path.isdir(mount_dir):
        return os.path.join(mount_dir, "config.json")
    etc_dir = "/etc/daq-navi"
    if os.path.isdir(etc_dir):
        return os.path.join(etc_dir, "config.json")
    return os.path.join(SERVICE_DIR, "config.json")

CONFIG_PATH = get_config_path()
PID_PATH = os.path.join(SERVICE_DIR, '.daq_process.pid')
MODE_PATH = os.path.join(SERVICE_DIR, '.daq_process.mode')
LOG_PATH = os.path.join(SERVICE_DIR, 'daq_pipeline.log')
PROC_ROOT = Path('/proc')

# Global monitoring variables
tail_thread = None
stop_tail_event = threading.Event()
last_stats = {}
_clear_processes = {}
# Serializes config commits, spool ownership changes, and acquisition transitions
# within this service process. The deployment runs a single web worker.
CONFIG_TRANSITION_LOCK = threading.RLock()

def config_revision(config):
    stable = {key: value for key, value in config.items() if key not in ('_REV', '_WEB_MANAGED')}
    encoded = json.dumps(stable, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()

def redact_response_values(value, *configs):
    if isinstance(value, dict):
        return {key: redact_response_values(item, *configs) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_response_values(item, *configs) for item in value]
    if isinstance(value, str):
        return auth.redact_error_message(value, *configs)
    return value

def read_config():
    """Reads configuration parameters from config.json. Raises on missing or malformed file."""
    path = CONFIG_PATH
    if not os.path.exists(path):
        raise FileNotFoundError(f"Configuration file does not exist: {path}")
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)

def write_config(config_data):
    """Atomically and durably replace the saved configuration without truncation fallback."""
    temporary = None
    replaced = False
    target_path = CONFIG_PATH
    parent_dir = os.path.dirname(os.path.abspath(target_path)) or '.'
    os.makedirs(parent_dir, exist_ok=True)

    target_mode = 0o600
    original_exists = os.path.exists(target_path)
    original_content = None
    if original_exists:
        with open(target_path, 'rb') as original_file:
            original_content = original_file.read()
    if os.path.exists(target_path):
        try:
            target_mode = stat.S_IMODE(os.stat(target_path).st_mode)
        except OSError:
            pass

    try:
        config_data = {**config_data, '_WEB_MANAGED': True}
        with tempfile.NamedTemporaryFile('w', dir=parent_dir,
                                         prefix='.config.tmp.',
                                         encoding='utf-8', delete=False) as f:
            temporary = f.name
            json.dump(config_data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.chmod(temporary, target_mode)

        os.replace(temporary, target_path)
        replaced = True

        parent_fd = os.open(parent_dir, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

        return True
    except Exception as e:
        app.logger.error("Failed to write config atomically: %s", e)
        if replaced:
            try:
                if original_exists:
                    with tempfile.NamedTemporaryFile('wb', dir=parent_dir, prefix='.config.restore.', delete=False) as restore:
                        restore.write(original_content)
                        restore.flush()
                        os.fsync(restore.fileno())
                        restore_path = restore.name
                    os.chmod(restore_path, target_mode)
                    os.replace(restore_path, target_path)
                else:
                    os.unlink(target_path)
                parent_fd = os.open(parent_dir, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except Exception as restore_error:
                app.logger.critical("Config commit failed and previous-file restoration failed: %s", restore_error)
        return False
    finally:
        if temporary and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def merge_config(current, changes):
    if not isinstance(changes, dict):
        raise ValueError('Configuration must be an object')
    merged = dict(current)
    for key, value in changes.items():
        if key == 'CHANNELS':
            if not isinstance(value, dict):
                raise ValueError('CHANNELS must be an object')
            channels = {name: dict(channel) for name, channel in current.get('CHANNELS', {}).items()}
            for name, update in value.items():
                if not name.isdecimal() or not isinstance(update, dict):
                    raise ValueError('Each channel must be an object with a numeric key')
                channel = dict(channels.get(name, {}))
                for field, field_value in update.items():
                    if field == 'scale':
                        if not isinstance(field_value, dict):
                            raise ValueError(f'channel {name} scale must be an object')
                        channel[field] = {**channel.get(field, {}), **field_value}
                    else:
                        channel[field] = field_value
                channels[name] = channel
            merged[key] = channels
        elif key in current or key in ('AUTO_START_ON_STARTUP', 'AUTO_START_MODE', 'DB_RETENTION_DAYS', 'DB_CONNECTION_MODE', 'POSTGRES_PASSWORD', 'DB_PASSWORD', 'INFLUX_TOKEN', 'MQTT_PASSWORD'):
            merged[key] = value
        else:
            raise ValueError(f'Unsupported setting: {key}')
    for key in ('START_CHANNEL', 'CHANNEL_COUNT', 'CLOCK_RATE', 'SECTION_LENGTH',
                'SECTION_COUNT', 'SPOOL_MAX_BYTES', 'DB_RETENTION_DAYS'):
        if key in merged and type(merged[key]) is not int:
            raise ValueError(f'{key} must be an integer')
    if type(merged.get('AUTO_START_ON_STARTUP', False)) is not bool:
        raise ValueError('AUTO_START_ON_STARTUP must be a boolean')
    if merged.get('AUTO_START_MODE', 'production') not in ('production', 'mockup'):
        raise ValueError('AUTO_START_MODE must be production or mockup')
    if 'DB_CONNECTION_MODE' in merged:
        if merged['DB_CONNECTION_MODE'] not in ('fields', 'dsn'):
            raise ValueError('DB_CONNECTION_MODE must be fields or dsn')
    else:
        merged['DB_CONNECTION_MODE'] = auth.infer_db_connection_mode(merged)
    if merged.get('DB_CONNECTION_MODE') == 'fields':
        user = urllib.parse.quote(str(merged.get('DB_USER', 'admin')), safe='')
        password = urllib.parse.quote(str(merged.get('DB_PASSWORD', merged.get('POSTGRES_PASSWORD', 'admin'))), safe='')
        host = str(merged.get('DB_HOST', 'localhost'))
        port = str(merged.get('DB_PORT', 5432))
        dbname = urllib.parse.quote(str(merged.get('DB_NAME', 'daq_db')), safe='')
        merged['DB_DSN'] = f"postgresql://{user}:{password}@{host}:{port}/{dbname}"
    if merged.get('DB_RETENTION_DAYS', 30) < 1:
        raise ValueError('DB_RETENTION_DAYS must be at least one day')
    if merged.get('CHANNEL_COUNT', 0) < 1 or merged.get('SECTION_LENGTH', 0) < 1:
        raise ValueError('CHANNEL_COUNT and SECTION_LENGTH must be positive')
    if not 1000 <= merged.get('CLOCK_RATE', 0) <= 2000:
        raise ValueError('CLOCK_RATE must be 1000–2000 Hz per channel')
    for name, channel in merged.get('CHANNELS', {}).items():
        if type(channel.get('enabled')) is not bool:
            raise ValueError(f'channel {name} enabled must be a boolean')
        scale = channel.get('scale', {})
        for field in ('low_voltage', 'high_voltage', 'low_value', 'high_value'):
            if field in scale and (type(scale[field]) not in (int, float) or
                                   not math.isfinite(scale[field])):
                raise ValueError(f'channel {name} {field} must be a finite number')
    cfg = DaqNaviConfig(merged, allow_env_overrides=False)
    if merged.get('AUTO_START_MODE', 'production') == 'production':
        validate_production_config(cfg)
    return merged

# Cross-platform utility to check if a process is still active on the host OS
def is_pid_running(pid):
    if sys.platform == "win32":
        try:
            # Query tasklist on Windows
            output = subprocess.check_output(f'tasklist /fi "PID eq {pid}"', shell=True)
            return str(pid) in str(output)
        except Exception:
            return False
    else:
        try:
            stat_path = f'/proc/{pid}/stat'
            if os.path.exists(stat_path) and Path(stat_path).read_text().split(') ', 1)[1][0] == 'Z':
                return False
            # Query signal 0 (null signal) on POSIX
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

def acquisition_mode_for_pid(pid):
    """Identify a process launched by this service from its exact command arguments."""
    if not is_pid_running(pid):
        return None
    if sys.platform == 'win32':
        return None
    try:
        arguments = (PROC_ROOT / str(pid) / 'cmdline').read_bytes().split(b'\0')
    except OSError:
        return None
    if len(arguments) < 2:
        return None
    script = arguments[1].decode(errors='replace')
    if script == os.path.join(CORE_DIR, 'mockup_stream_to_db.py'):
        return 'mockup'
    # A process started before the script rename may still be running.
    if script in (
        os.path.join(CORE_DIR, 'buffered_daq_to_timescaledb.py'),
        os.path.join(CORE_DIR, 'stream_to_db.py'),
    ):
        for index, argument in enumerate(arguments[:-1]):
            if argument == b'--config' and arguments[index + 1] == os.fsencode(CONFIG_PATH):
                return 'production'
    return None


def find_acquisition_child():
    """Recover a live acquisition child when its PID file was overwritten."""
    if sys.platform == 'win32':
        return None, None
    try:
        tasks = (PROC_ROOT / 'self' / 'task').iterdir()
        children = set()
        for task in tasks:
            children.update(int(pid) for pid in (task / 'children').read_text().split())
    except (OSError, ValueError):
        return None, None
    for pid in sorted(children):
        mode = acquisition_mode_for_pid(pid)
        if mode:
            return pid, mode
    return None, None


# Retrieve the running process info if active
def get_running_process():
    if os.path.exists(PID_PATH) and os.path.exists(MODE_PATH):
        try:
            with open(PID_PATH, 'r') as f:
                pid = int(f.read().strip())
            with open(MODE_PATH, 'r') as f:
                mode = f.read().strip()
            
            if (sys.platform == 'win32' and is_pid_running(pid)) or acquisition_mode_for_pid(pid) == mode:
                return pid, mode
        except Exception as e:
            print(f"Error checking active PID file: {e}")
    pid, mode = find_acquisition_child()
    if pid is not None:
        Path(PID_PATH).write_text(str(pid), encoding='utf-8')
        Path(MODE_PATH).write_text(mode, encoding='utf-8')
        return pid, mode
    return None, None

# Terminate process by PID cross-platform
def terminate_pid(pid):
    if sys.platform == "win32":
        try:
            subprocess.run(f"taskkill /pid {pid} /t /f", shell=True)
        except Exception as e:
            print(f"Error terminating Windows PID {pid}: {e}")
    else:
        try:
            os.kill(pid, 15) # SIGTERM (graceful exit)
            # The production writer has a 10-second drain window and a 15-second join.
            for _ in range(250):
                if not is_pid_running(pid):
                    return True
                time.sleep(0.1)
            return False
        except ProcessLookupError:
            return True
        except OSError as e:
            print(f"Error terminating Unix PID {pid}: {e}")
            return False

# Regex to extract statistics from the log file
# E.g.: [STATS] polled=1,024 | written=1,024 | dropped_batches=0 (0.0%) | db_errors=0 | queue=0/200
STATS_REGEX = re.compile(
    r"\[STATS\] polled=(?P<polled>[0-9,]+) \| written=(?P<written>[0-9,]+) \| dropped_batches=(?P<dropped>[0-9]+) \((?P<loss_pct>[0-9\.]+)%\) \| db_errors=(?P<errors>[0-9]+) \| queue=(?P<qsize>[0-9]+)/(?P<qmax>[0-9]+)"
)

def parse_and_emit_stats(line):
    """Parses stats from a line and updates global caches."""
    global last_stats
    match = STATS_REGEX.search(line)
    if match:
        last_stats = {
            'polled': match.group('polled'),
            'written': match.group('written'),
            'dropped': match.group('dropped'),
            'loss_pct': match.group('loss_pct'),
            'errors': match.group('errors'),
            'queue_util': f"{match.group('qsize')}/{match.group('qmax')}"
        }
        socketio.emit('stats_update', last_stats)

def tail_log_file():
    """Background loop tailing the physical log file to feed sockets."""
    global last_stats
    print("[SYSTEM] Log tailing thread started.")
    
    # Wait until log file is created
    while not os.path.exists(LOG_PATH) and not stop_tail_event.is_set():
        time.sleep(0.2)
        
    try:
        with open(LOG_PATH, 'r', errors='replace') as f:
            # Start tailing from the end of the file
            f.seek(0, os.SEEK_END)
            
            while not stop_tail_event.is_set():
                pid, _ = get_running_process()
                if pid is None:
                    # DAQ process stopped; close tailing thread and notify client
                    cfg = read_config()
                    try:
                        rec_mode = Path(MODE_PATH).read_text(encoding='utf-8').strip()
                    except OSError:
                        rec_mode = None
                    stopped_mode = rec_mode or cfg.get('AUTO_START_MODE', 'production')
                    socketio.emit('status_change', {'is_running': False, 'mode': stopped_mode})
                    break
                    
                line = f.readline()
                if not line:
                    time.sleep(0.1)
                    continue
                
                decoded_line = line.strip()
                # Broadcast log line to connected sockets
                socketio.emit('log_update', {'log': decoded_line})
                parse_and_emit_stats(decoded_line)
                
    except Exception as e:
        print(f"Error tailing log file: {e}")
    finally:
        print("[SYSTEM] Log tailing thread finished.")

def start_tailing():
    """Starts a new background tailing thread if not active."""
    global tail_thread, stop_tail_event
    stop_tail_event.clear()
    if tail_thread is None or not tail_thread.is_alive():
        tail_thread = threading.Thread(target=tail_log_file, daemon=True)
        tail_thread.start()

def get_last_logs(count=50):
    """Retrieves last few log lines for newly connected clients."""
    if not os.path.exists(LOG_PATH):
        return []
    try:
        with open(LOG_PATH, 'r', errors='replace') as f:
            lines = f.readlines()
            return [line.strip() for line in lines[-count:]]
    except Exception as e:
        print(f"Error reading historical logs: {e}")
        return []

def scan_host_usb_devices():
    """Scan installed Advantech DAQ cards and serial ports on this host."""
    detected = []
    warnings = []

    # Use the installed DAQNavi enumerator so the device ID matches acquisition.
    try:
        result = subprocess.run(
            ['/opt/advantech/tools/dev_enum'],
            capture_output=True, text=True, timeout=5, check=True,
        )
        for line in result.stdout.splitlines():
            row = re.match(r'^\|\s*\d+\s*\|\s*\d+\s*\|\s*([^|]+?)\s*\|', line)
            if not row:
                continue
            description = row.group(1).strip()
            board_id = re.search(r'BID#\d+', description)
            detected.append({
                'id': description,
                'name': f"Advantech {description}",
                'type': 'Advantech DAQ Card',
                'port': board_id.group(0) if board_id else '',
                'vendor': 'Advantech',
                'is_daq': True
            })
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[SCAN] Advantech device enumeration failed: {exc}")
        warnings.append('Advantech DAQ scan failed; check that DAQNavi is installed and the device is accessible.')

    # Serial ports may contain devices outside the Advantech DAQNavi driver.
    try:
        import serial.tools.list_ports
        ports = serial.tools.list_ports.comports()
        for p in ports:
            desc = p.description if p.description else p.device
            mfg = p.manufacturer if hasattr(p, 'manufacturer') and p.manufacturer else 'USB Serial'
            detected.append({
                'id': p.device,
                'name': f"{p.device} ({desc})",
                'type': 'USB Serial Port',
                'port': p.device,
                'vendor': mfg,
                'hwid': p.hwid if hasattr(p, 'hwid') else '',
                'is_daq': False
            })
    except Exception as exc:
        print(f"[SCAN] Serial port scan failed: {exc}")
        warnings.append('Serial port scan failed.')

    # Deduplicate by 'id' while retaining order
    seen = set()
    unique_detected = []
    for d in detected:
        if d['id'] not in seen:
            seen.add(d['id'])
            unique_detected.append(d)

    return unique_detected, warnings

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'GET':
        if get_current_session() is not None:
            next_url = request.args.get('next', '/')
            if not next_url.startswith('/'):
                next_url = '/'
            return redirect(next_url)
        return render_template('login.html', next_url=request.args.get('next', ''))

    # POST login
    next_url = request.args.get('next') or request.form.get('next') or '/'
    if not next_url.startswith('/'):
        next_url = '/'

    data = request.get_json(silent=True) if request.is_json else request.form
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    stored_hash = operator_users.get(username) if operator_users else None
    if not stored_hash or not auth.verify_password(password, stored_hash):
        if request.is_json:
            return jsonify({'error': 'Invalid username or password'}), 401
        return render_template('login.html', error='Invalid username or password', next_url=next_url), 401

    if session_store is None:
        return jsonify({'error': 'Authentication service not initialized'}), 500

    _, cookie_val = session_store.create_session(username)
    if request.is_json:
        resp = jsonify({'status': 'ok', 'redirect': next_url})
    else:
        resp = redirect(next_url)

    is_secure = request.is_secure or request.headers.get('X-Forwarded-Proto') == 'https'
    resp.set_cookie(
        COOKIE_NAME,
        cookie_val,
        httponly=True,
        samesite='Lax',
        secure=is_secure,
        path='/'
    )
    return resp


@app.route('/logout', methods=['GET', 'POST'])
def logout():
    cookie_val = request.cookies.get(COOKIE_NAME)
    if session_store and cookie_val:
        session_store.invalidate_session(cookie_val)
    resp = redirect(url_for('login'))
    resp.delete_cookie(COOKIE_NAME, path='/')
    return resp


@app.route('/api/auth/change-password', methods=['POST'])
def change_password():
    session = get_current_session()
    if session is None and session_store is not None:
        return jsonify({'status': 'error', 'error': 'Unauthorized', 'message': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or request.form
    if not data:
        return jsonify({'status': 'error', 'error': 'Invalid request body', 'message': 'Invalid request body'}), 400

    current_password = data.get('current_password') or ''
    new_password = data.get('new_password') or ''
    confirm_password = data.get('confirm_password') or ''

    if not current_password:
        return jsonify({'status': 'error', 'error': 'Current password is required', 'message': 'Current password is required'}), 400

    if not new_password or len(new_password) < 8:
        return jsonify({'status': 'error', 'error': 'New password must be at least 8 characters long', 'message': 'New password must be at least 8 characters long'}), 400

    if new_password != confirm_password:
        return jsonify({'status': 'error', 'error': 'New password and confirmation do not match', 'message': 'New password and confirmation do not match'}), 400

    if current_password == new_password:
        return jsonify({'status': 'error', 'error': 'New password must be different from current password', 'message': 'New password must be different from current password'}), 400

    username = session.get('username', 'operator') if session else 'operator'
    stored_hash = operator_users.get(username) if operator_users else None

    if not stored_hash or not auth.verify_password(current_password, stored_hash):
        return jsonify({'status': 'error', 'error': 'Incorrect current password', 'message': 'Incorrect current password'}), 400

    new_hash = auth.hash_password(new_password)
    try:
        auth.save_operator_hash(new_hash)
    except Exception as exc:
        app.logger.error("Failed to persist updated operator password hash: %s", exc)
        return jsonify({'status': 'error', 'error': str(exc), 'message': f'Failed to persist password: {exc}'}), 500

    if operator_users is not None:
        operator_users[username] = new_hash

    app.logger.info("Operator '%s' successfully changed password.", username)
    return jsonify({'status': 'ok', 'message': 'Password changed successfully'})


@app.route('/')
def home():
    session = get_current_session()
    operator_user = session.get('username', 'operator') if session else 'operator'
    return render_template('index.html', operator_user=operator_user)

@app.route('/favicon.ico')
def favicon():
    return redirect(url_for('static', filename='config_center/favicon.svg'))

@app.route('/api/config', methods=['GET'])
def get_config():
    try:
        config = read_config()
        config['_REV'] = config_revision(config)
        return jsonify(auth.redact_config_secrets(config))
    except Exception as exc:
        return jsonify({'error': f'Failed to read configuration: {exc}'}), 500


def drain_spool_for_destination_switch(old_config, new_config):
    """While stopped, close open gaps and record cutover for the new destination without draining to old sink."""
    old_cfg = DaqNaviConfig(old_config, allow_env_overrides=False)
    new_cfg = DaqNaviConfig(new_config, allow_env_overrides=False)
    if destination_identity(old_cfg) == destination_identity(new_cfg):
        return
    spool = DurableSpool(Path(old_cfg.SPOOL_DIR), old_cfg.SPOOL_MAX_BYTES)
    try:
        spool.close_open_gaps_for_switch(time.time_ns())
        pending = spool.pending_batches + len(spool.pending_gaps())
        spool.record_cutover(old_cfg.DESTINATION, new_cfg.DESTINATION, pending)
    finally:
        spool.close()

def effective_timescale_retention(config):
    """Read the selected production hypertable's active retention policy."""
    import psycopg2
    cfg = DaqNaviConfig(config, allow_env_overrides=False)
    with psycopg2.connect(cfg.DB_DSN, connect_timeout=3) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT config->>'drop_after' FROM timescaledb_information.jobs WHERE hypertable_name=%s AND proc_name='policy_retention'", (cfg.DB_PRODUCTION_TABLE,))
            rows = [row[0] for row in cursor.fetchall()]
    if not rows:
        raise RuntimeError('No active production retention policy')
    if len(set(rows)) != 1:
        raise RuntimeError('Multiple conflicting production retention policies are active')
    return rows[0]

def update_spool_owner(config, destination, identity):
    cfg = DaqNaviConfig(config, allow_env_overrides=False)
    spool = DurableSpool(Path(cfg.SPOOL_DIR), cfg.SPOOL_MAX_BYTES)
    try:
        spool.set_state('destination', destination)
        if identity:
            spool.set_state('destination_identity', identity)
    finally:
        spool.close()

def restore_previous_retention(config, days):
    """Compensate a policy update and verify its effective value."""
    TimescaleProductionDestination(DaqNaviConfig(config, allow_env_overrides=False)).ensure_schema()
    effective = effective_timescale_retention(config)
    if effective != f"{days} days":
        raise RuntimeError(f'readback after compensation was {effective}')

def resume_after_failed_switch(was_running, destination_changed, mode, current):
    if not (was_running and destination_changed):
        return None
    try:
        return start_acquisition(mode or current.get('AUTO_START_MODE', 'production'))
    except Exception as exc:
        return {'started': False, 'message': auth.redact_error_message(str(exc), current)}

@app.route('/api/config', methods=['POST'])
def save_config():
    with CONFIG_TRANSITION_LOCK:
        return _save_config_locked()

def _save_config_locked():
    try:
        current = read_config()
    except Exception as exc:
        return jsonify({'status': 'error', 'message': f'Cannot save: existing configuration file is invalid or missing ({exc})'}), 500
    payload = {}
    try:
        payload = request.get_json(silent=True) or {}
        submitted_revision = payload.pop('_REV', None)
        if submitted_revision is not None and submitted_revision != config_revision(current):
            return jsonify({'status': 'error', 'message': 'Configuration was modified since it was loaded. Reload before saving.'}), 409
        payload = auth.merge_preserved_secrets(payload, current)
        updated = merge_config(current, payload)
    except (ValueError, TypeError, KeyError) as exc:
        return jsonify({'status': 'error', 'message': auth.redact_error_message(str(exc), current, payload)}), 400
    if current.get('SPOOL_DIR', '/var/lib/daq_navi/spool') != updated.get('SPOOL_DIR', '/var/lib/daq_navi/spool'):
        return jsonify({'status': 'error', 'message': 'SPOOL_DIR changes are unsupported; pending records and gap history must remain in the existing spool.'}), 400
    previous_retention = current.get('DB_RETENTION_DAYS', 30)
    pid, mode = get_running_process()
    was_running = pid is not None
    switch_stop_result = None
    supported_sinks = {'postgresql', 'timescaledb', 'database', 'influxdb', 'mqtt'}
    needs_identity = (
        current.get('AUTO_START_MODE', 'production') == 'production' or
        updated.get('AUTO_START_MODE', 'production') == 'production'
    ) and current.get('DESTINATION', 'postgresql') in supported_sinks and updated.get('DESTINATION', 'postgresql') in supported_sinks
    current_identity = updated_identity = None
    destination_changed = False
    if needs_identity:
        try:
            current_identity = destination_identity(DaqNaviConfig(current, allow_env_overrides=False))
            updated_identity = destination_identity(DaqNaviConfig(updated, allow_env_overrides=False))
        except (ValueError, TypeError) as exc:
            return jsonify({'status': 'error', 'message': auth.redact_error_message(str(exc), current, updated)}), 400
        destination_changed = current_identity != updated_identity
    retention_changed = ('DB_RETENTION_DAYS' in updated and
                         ('DB_RETENTION_DAYS' not in current or updated['DB_RETENTION_DAYS'] != previous_retention))
    if destination_changed and updated.get('DESTINATION', 'postgresql') in ('postgresql', 'timescaledb', 'database'):
        if retention_changed:
            return jsonify({'status': 'error', 'persisted': False,
                            'message': 'Save the destination and retention days in separate operations; no changes were applied.'}), 400
        try:
            effective = effective_timescale_retention(updated)
        except Exception as exc:
            return jsonify({'status': 'error', 'persisted': False,
                            'message': f'Could not verify retention on the new destination: {auth.redact_error_message(str(exc), current, updated)}'}), 502
        expected = f'{previous_retention} days'
        if effective != expected:
            return jsonify({'status': 'error', 'persisted': False,
                            'message': f'New destination retention is {effective}; expected {expected}. Set the target policy before switching.'}), 400
    if destination_changed:
        try:
            _test_destination(updated)
        except Exception as exc:
            return jsonify({'status': 'error', 'message': f'New destination preflight failed: {auth.redact_error_message(str(exc), current, updated)}'}), 502
        if pid is not None:
            switch_stop_result = stop_acquisition(manual=False)
            if not switch_stop_result['stopped']:
                return jsonify({'status': 'error', 'message': 'Could not stop acquisition before switching destination.', 'persisted': False, 'runtime': 'prior acquisition may still be stopping'}), 503
        try:
            drain_spool_for_destination_switch(current, updated)
        except Exception as exc:
            recovered = None
            if was_running:
                recovered = start_acquisition(mode or current.get('AUTO_START_MODE', 'production'))
            outcome = 'resumed on previous configuration' if recovered and recovered.get('started') else 'recovery failed; acquisition remains stopped'
            return jsonify({'status': 'error', 'message': f'Could not drain the old destination spool: {auth.redact_error_message(str(exc), current, updated)}; {outcome}.', 'persisted': False, 'runtime': 'old configuration resumed' if recovered and recovered.get('started') else 'stopped', 'resumed': bool(recovered and recovered.get('started')), 'recovery': redact_response_values(recovered, current, updated)}), 503
    retention_applied = False
    if retention_changed and updated.get('DESTINATION', 'postgresql') in ('postgresql', 'timescaledb', 'database'):
        try:
            new_cfg = DaqNaviConfig(updated, allow_env_overrides=False)
            TimescaleProductionDestination(new_cfg).ensure_schema()
            effective = effective_timescale_retention(updated)
            if effective != f"{updated['DB_RETENTION_DAYS']} days":
                raise RuntimeError(f'Policy readback is {effective}; expected {updated["DB_RETENTION_DAYS"]} days')
            retention_applied = True
        except Exception as exc:
            # ensure_schema may have changed the policy before raising, so always
            # try to restore and verify the previous setting.
            restore_error = None
            try:
                restore_previous_retention(current, previous_retention)
            except Exception as restore_exc:
                restore_error = auth.redact_error_message(str(restore_exc), current, updated)
            recovered = resume_after_failed_switch(was_running, destination_changed, mode, current)
            resumed = bool(recovered and recovered.get('started'))
            recovery = (' Previous acquisition resumed.' if resumed else
                        ' Previous acquisition remains stopped.' if was_running and destination_changed else '')
            if restore_error:
                message = (f'Retention apply failed ({auth.redact_error_message(str(exc), current, updated)}); compensation failed ({restore_error}). '
                           f'Saved config remains {previous_retention} days; effective policy may differ. '
                           f'Operator action: set retention on {current.get("DB_PRODUCTION_TABLE")} to {previous_retention} days and verify it.{recovery}')
            else:
                message = f'Could not apply retention policy; previous {previous_retention}-day policy was restored: {auth.redact_error_message(str(exc), current, updated)}.{recovery}'
            return jsonify({'status': 'error', 'persisted': False, 'message': message,
                            'runtime': 'old configuration resumed' if resumed else 'stopped' if was_running and destination_changed else 'unchanged',
                            'resumed': resumed, 'recovery': redact_response_values(recovered, current, updated),
                            'retention_compensated': restore_error is None}), 503
    if destination_changed:
        try:
            update_spool_owner(updated, updated.get('DESTINATION', 'postgresql'), updated_identity)
        except Exception as exc:
            restored = True
            try:
                update_spool_owner(current, current.get('DESTINATION', 'postgresql'), current_identity)
            except Exception:
                restored = False
            retention_restore_error = None
            if retention_applied:
                try:
                    restore_previous_retention(current, previous_retention)
                except Exception as restore_exc:
                    retention_restore_error = auth.redact_error_message(str(restore_exc), current, updated)
            recovered = None
            if was_running and restored:
                recovered = resume_after_failed_switch(was_running, destination_changed, mode, current)
            return jsonify({'status': 'error', 'persisted': False,
                            'runtime': 'old configuration resumed' if recovered and recovered.get('started') else 'stopped' if was_running else 'unchanged',
                            'resumed': bool(recovered and recovered.get('started')),
                            'spool_owner_restored': restored,
                            'retention_compensated': retention_restore_error is None,
                            'message': (f'Could not update spool destination ownership: {auth.redact_error_message(str(exc), current, updated)}; '
                                        + ('old owner restored.' if restored else 'spool ownership restoration failed; inspect spool destination metadata before restarting acquisition.')
                                        + (f' Retention compensation failed ({retention_restore_error}); verify the effective policy before retrying.' if retention_restore_error else ''))}), 503
    if not write_config(updated):
        spool_restored = True
        if destination_changed:
            try:
                update_spool_owner(current, current.get('DESTINATION', 'postgresql'), current_identity)
            except Exception:
                spool_restored = False
        compensation = 'not required'
        if retention_applied:
            try:
                restore_previous_retention(current, previous_retention)
                compensation = f'previous {previous_retention}-day policy restored'
            except Exception as exc:
                compensation = f'FAILED ({auth.redact_error_message(str(exc), current, updated)}); set {current.get("DB_PRODUCTION_TABLE")} retention to {previous_retention} days and verify'
        recovered = None
        if was_running and destination_changed and spool_restored:
            recovered = start_acquisition(mode or current.get('AUTO_START_MODE', 'production'))
        recovery_text = ('; prior acquisition resumed' if recovered and recovered.get('started') else
                         '; prior acquisition recovery failed and it remains stopped' if was_running and destination_changed else '')
        if not spool_restored:
            recovery_text += '; spool ownership restoration failed; inspect spool metadata before restarting acquisition'
        return jsonify({'status': 'error', 'persisted': False, 'runtime': 'old configuration resumed' if recovered and recovered.get('started') else 'stopped' if was_running and destination_changed else 'unchanged', 'resumed': bool(recovered and recovered.get('started')), 'recovery': redact_response_values(recovered, current, updated), 'spool_owner_restored': spool_restored,
                        'message': f'Failed to save configuration; {compensation}{recovery_text}.'}), 500
    safe_config = auth.redact_config_secrets({**updated, '_REV': config_revision(updated)})
    if was_running and switch_stop_result:
        target_mode = updated.get('AUTO_START_MODE') if ('AUTO_START_MODE' in (payload or {})) else (mode or updated.get('AUTO_START_MODE', 'production'))
        start_result = start_acquisition(target_mode)
        if not start_result['started']:
            return jsonify({'status': 'error', 'persisted': True, 'runtime': 'stopped', 'message': 'Configuration is saved, but acquisition did not restart.', 'config': safe_config, **redact_response_values(start_result, current, updated)}), 503
        return jsonify({'status': 'success', 'config': safe_config,
                        'retention': ('managed by InfluxDB bucket' if updated.get('DESTINATION') == 'influxdb'
                                      else 'managed by MQTT consumer / downstream broker' if updated.get('DESTINATION') == 'mqtt'
                                      else 'TimescaleDB policy'),
                        'previous_session': {'stopped': True, 'drained': True,
                                             'pending_replay': False, 'pending_batches': 0},
                        'drained': True, 'pending_replay': False, 'pending_batches': 0})
    pid, mode = get_running_process()
    if pid is not None:
        stop_result = stop_acquisition(manual=False)
        if not stop_result['stopped']:
            return jsonify({'status': 'error', 'message': 'Saved; prior session is still stopping',
                            'config': safe_config, **redact_response_values(stop_result, current, updated)}), 503
        target_mode = updated.get('AUTO_START_MODE') if ('AUTO_START_MODE' in (payload or {})) else (mode or updated.get('AUTO_START_MODE', 'production'))
        start_result = start_acquisition(target_mode)
        if not start_result['started']:
            return jsonify({'status': 'error', 'message': 'Saved; restart failed',
                            'persisted': True, 'runtime': 'stopped', 'config': safe_config, **redact_response_values(start_result, current, updated)}), 503
        drained = stop_result.get('drained', not stop_result.get('pending_replay', False))
        pending_replay = stop_result.get('pending_replay', False)
        pending_batches = stop_result.get('pending_batches', 0)
        return jsonify({
            'status': 'success',
            'config': safe_config,
            'retention_days': updated['DB_RETENTION_DAYS'],
            'previous_session': {
                'stopped': True,
                'drained': drained,
                'pending_replay': pending_replay,
                'pending_batches': pending_batches,
            },
            'drained': drained,
            'pending_replay': pending_replay,
            'pending_batches': pending_batches,
        })
    runtime = read_runtime_status(updated)
    pending = runtime.get('pending_batches', 0)
    return jsonify({
        'status': 'success',
        'config': safe_config,
        'retention_days': updated['DB_RETENTION_DAYS'],
        'drained': pending == 0,
        'pending_replay': pending > 0,
        'pending_batches': pending,
    })


@app.route('/api/retention', methods=['GET'])
def get_retention():
    config = read_config()
    dest = config.get('DESTINATION', 'postgresql')
    if dest == 'influxdb':
        return jsonify({'destination': 'influxdb', 'retention': 'managed by InfluxDB bucket',
                        'saved_days': None, 'message': 'Configure retention in the InfluxDB bucket settings.'})
    if dest == 'mqtt':
        return jsonify({'destination': 'mqtt', 'retention': 'managed by MQTT consumer / downstream broker',
                        'saved_days': None, 'message': 'Retention and history are consumer managed.'})
    try:
        import psycopg2
        with psycopg2.connect(config['DB_DSN'], connect_timeout=3) as conn:
            with conn.cursor() as cursor:
                cursor.execute("""SELECT config->>'drop_after' FROM timescaledb_information.jobs
                    WHERE hypertable_name=%s AND proc_name='policy_retention'""",
                               (config['DB_PRODUCTION_TABLE'],))
                row = cursor.fetchone()
        if row is None:
            return jsonify({'saved_days': config.get('DB_RETENTION_DAYS', 30),
                            'message': 'No active production retention policy'}), 503
        return jsonify({'saved_days': config.get('DB_RETENTION_DAYS', 30), 'effective': row[0]})
    except Exception as exc:
        return jsonify({'saved_days': config.get('DB_RETENTION_DAYS'),
                        'message': f'Could not read retention policy: {auth.redact_error_message(str(exc), config)}'}), 503

def _test_destination(settings):
    import urllib.error
    import urllib.parse
    import urllib.request

    saved = read_config()
    value = lambda key, default='': settings[key] if key in settings else saved.get(key, default)
    destination = settings.get('DESTINATION', settings.get('destination', saved.get('DESTINATION', 'postgresql')))

    if destination in ('postgresql', 'timescaledb', 'database'):
        import psycopg2

        mode = settings.get('DB_CONNECTION_MODE', saved.get('DB_CONNECTION_MODE', auth.infer_db_connection_mode(saved)))
        if mode == 'fields':
            connection = psycopg2.connect(
                host=value('DB_HOST', 'localhost'), port=value('DB_PORT', 5432),
                user=value('DB_USER', 'admin'), password=value('DB_PASSWORD'),
                dbname=value('DB_NAME', 'daq_db'), connect_timeout=3,
            )
        else:
            dsn = value('DB_DSN')
            if dsn:
                connection = psycopg2.connect(dsn, connect_timeout=3)
            else:
                connection = psycopg2.connect(
                    host=value('DB_HOST', 'localhost'), port=value('DB_PORT', 5432),
                    user=value('DB_USER', 'admin'), password=value('DB_PASSWORD'),
                    dbname=value('DB_NAME', 'daq_db'), connect_timeout=3,
                )
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1')
                cursor.fetchone()
        finally:
            connection.close()
        return 'PostgreSQL connection and query succeeded. No sample data was written.'

    if destination == 'influxdb':
        url = str(value('INFLUX_URL')).rstrip('/')
        org = str(value('INFLUX_ORG'))
        bucket = str(value('INFLUX_BUCKET'))
        token = str(value('INFLUX_TOKEN'))
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Enter a valid InfluxDB HTTP(S) URL.')
        if not org or not bucket or not token:
            raise ValueError('InfluxDB organization, bucket, and token are required.')
        headers = {'Authorization': f'Token {token}', 'Accept': 'application/json'}

        def get_resource(path, query):
            target = f"{url}{path}?{urllib.parse.urlencode(query)}"
            probe = urllib.request.Request(target, headers=headers, method='GET')
            try:
                with urllib.request.urlopen(probe, timeout=4.0) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    raise ValueError(
                        f'InfluxDB denied metadata access (HTTP {exc.code}). '
                        'Check the token and grant read access to organizations and buckets.'
                    ) from exc
                raise

        orgs = get_resource('/api/v2/orgs', {'org': org}).get('orgs', [])
        selected_org = next((entry for entry in orgs if entry.get('name') == org), None)
        if not selected_org:
            raise ValueError('InfluxDB organization is unavailable to this token.')
        buckets = get_resource('/api/v2/buckets', {'name': bucket, 'orgID': selected_org['id']}).get('buckets', [])
        if not any(entry.get('name') == bucket and entry.get('orgID') == selected_org['id'] for entry in buckets):
            raise ValueError('InfluxDB bucket is unavailable to this token.')
        return 'InfluxDB connection, token, organization, and bucket verified. No sample data was written.'

    if destination == 'mqtt':
        import paho.mqtt.client as mqtt

        broker = str(value('MQTT_BROKER'))
        if not broker:
            raise ValueError('MQTT broker host is required.')
        port = int(value('MQTT_PORT', 8883))
        if not 1 <= port <= 65535:
            raise ValueError('MQTT port must be between 1 and 65535.')
        username = str(value('MQTT_USERNAME'))
        password = str(value('MQTT_PASSWORD'))
        if not username or not password:
            raise ValueError('Production MQTT requires a broker username and password.')
        if not value('MQTT_TLS_ENABLED', False):
            raise ValueError('Production MQTT requires verified TLS.')
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f'daq-connection-test-{uuid.uuid4().hex[:8]}',
            reconnect_on_failure=False,
        )
        client.connect_timeout = 4.0
        client.username_pw_set(username, password)
        client.tls_set(
            ca_certs=value('MQTT_CA_CERTS') or None,
            certfile=value('MQTT_CLIENT_CERT') or None,
            keyfile=value('MQTT_CLIENT_KEY') or None,
        )
        connected = threading.Event()
        result = {'reason': None}

        def on_connect(_client, _userdata, _flags, reason_code, _properties):
            result['reason'] = reason_code
            connected.set()

        client.on_connect = on_connect
        loop_started = False
        try:
            rc = client.connect(broker, port, keepalive=10)
            if rc != mqtt.MQTT_ERR_SUCCESS:
                raise ConnectionError(f'MQTT transport error: {mqtt.error_string(rc)}')
            client.loop_start()
            loop_started = True
            if not connected.wait(4.0):
                raise TimeoutError('MQTT broker did not acknowledge the connection.')
            if result['reason'] != 0:
                raise ConnectionError(f'MQTT broker refused the connection: {result["reason"]}')
        finally:
            if loop_started:
                client.loop_stop()
            client.disconnect()
        return 'MQTT broker accepted the connection. No message was published.'

    raise ValueError('Unsupported destination.')


@app.route('/api/test_destination', methods=['POST'])
@app.route('/api/test_db', methods=['POST'])
def test_destination():
    settings = request.get_json(silent=True)
    if not isinstance(settings, dict):
        return jsonify({'success': False, 'message': 'Request must be a JSON object.'}), 400
    saved = {}
    try:
        saved = read_config()
        settings = auth.merge_preserved_secrets(settings, saved)
        message = _test_destination(settings)
        return jsonify({'success': True, 'message': message})
    except (ValueError, TypeError) as exc:
        return jsonify({'success': False, 'message': auth.redact_error_message(str(exc), saved, settings)}), 400
    except Exception as exc:
        return jsonify({'success': False, 'message': auth.redact_error_message(f'Connection failed: {exc}', saved, settings)}), 502

@app.route('/api/status', methods=['GET'])
def get_status():
    pid, mode = get_running_process()
    is_running = pid is not None
    config = read_config()
    try:
        recorded_mode = Path(MODE_PATH).read_text(encoding='utf-8').strip()
    except OSError:
        recorded_mode = None
    mode_val = mode or recorded_mode or config.get('AUTO_START_MODE', 'production')
    dest = config.get('DESTINATION', 'postgresql')
    runtime = read_runtime_status(config) if mode_val == 'production' else {}
    stale_pid = os.path.exists(PID_PATH) and not is_running
    fault = runtime.get('last_fault') or ('acquisition_process_exited' if stale_pid else None)
    writer_error = runtime.get('last_writer_error') if is_running else None
    expected = mode_val == 'production' and (
        bool(config.get('AUTO_START_ON_STARTUP', False)) or os.path.exists(PID_PATH))
    fresh = time.time_ns() - runtime.get('checked_at_ns', 0) < 10_000_000_000
    status = {
        'service_name': 'DAQ USB-4716',
        'port': 8081,
        'is_running': is_running,
        'status': ('faulted' if fault else
                   'buffering' if writer_error else
                   'running' if is_running else 'stopped'),
        'mode': mode_val,
        'run_mode': mode_val,
        'pid': pid,
        'destination': dest,
        'expected_running': expected,
        'healthy': (not fault and not writer_error and
                    (not expected or (is_running and fresh and runtime.get('state') == 'running'))),
        'fault': fault,
        'pending_batches': runtime.get('pending_batches', 0),
        'pending_bytes': runtime.get('pending_bytes', 0),
        'spool_bytes': runtime.get('spool_bytes', 0),
        'last_sample_ns': runtime.get('last_sample_ns'),
        'writer_error': writer_error,
        'gaps': read_recent_gaps(config),
        'destination_cutovers': read_recent_cutovers(config),
        'retention_days': (config.get('DB_RETENTION_DAYS', 30) if dest not in ('influxdb', 'mqtt') else None),
        'retention': ('managed by InfluxDB bucket' if dest == 'influxdb'
                      else 'managed by MQTT consumer / downstream broker' if dest == 'mqtt'
                      else 'TimescaleDB policy'),
    }
    if dest == 'mqtt' and mode_val == 'production':
        runtime_matches_destination = runtime.get('destination') == dest
        last_delivery_ns = runtime.get('last_delivery_ns') if runtime_matches_destination else None
        delivery_state = ('stopped' if not is_running else
                          'unavailable' if not fresh or not runtime_matches_destination else
                          'error' if writer_error else
                          'completed' if last_delivery_ns else
                          'pending' if runtime.get('pending_batches', 0) else
                          'idle')
        status['mqtt_delivery'] = {
            'state': delivery_state,
            'last_completed_ns': last_delivery_ns,
            'qos': config.get('MQTT_PRODUCTION_QOS', 1),
        }
    return jsonify(status)


def read_runtime_status(config):
    path = Path(config.get('SPOOL_DIR', '/var/lib/daq_navi/spool')) / 'status.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def read_recent_gaps(config):
    import sqlite3
    path = Path(config.get('SPOOL_DIR', '/var/lib/daq_navi/spool')) / 'production-spool.sqlite3'
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=1) as conn:
            rows = conn.execute('SELECT start_ns,end_ns,cause FROM gaps ORDER BY start_ns DESC LIMIT 20').fetchall()
        return [{'start_ns': start, 'end_ns': end, 'cause': cause} for start, end, cause in rows]
    except (OSError, sqlite3.Error):
        return []


def read_recent_cutovers(config):
    import sqlite3
    path = Path(config.get('SPOOL_DIR', '/var/lib/daq_navi/spool')) / 'production-spool.sqlite3'
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f'file:{path}?mode=ro', uri=True, timeout=1) as conn:
            rows = conn.execute('SELECT time_ns, old_destination, new_destination, pending_records FROM cutovers ORDER BY time_ns DESC, id DESC LIMIT 5').fetchall()
        return [{'time_ns': r[0], 'from': r[1], 'to': r[2], 'pending_records': r[3]} for r in rows]
    except (OSError, sqlite3.Error):
        return []


@app.route('/api/health', methods=['GET'])
def get_health():
    status = get_status().get_json()
    if get_current_session() is not None:
        return jsonify(status), 200 if status.get('healthy') else 503
    minimal = {
        'status': status.get('status', 'unknown'),
        'healthy': status.get('healthy', False),
        'service': 'daq_navi'
    }
    if 'fault' in status and status['fault']:
        minimal['fault'] = status['fault']
    return jsonify(minimal), 200 if status.get('healthy') else 503


@app.route('/api/samples', methods=['GET'])
def get_samples():
    try:
        channel = int(request.args.get('channel', '0'))
        if not 0 <= channel < 16:
            raise ValueError('channel must be 0–15')
    except ValueError as exc:
        return jsonify({'message': str(exc)}), 400
    config = read_config()
    if config.get('DESTINATION') == 'mqtt':
        return jsonify({
            'destination': 'mqtt',
            'channel': channel,
            'points': [],
            'gaps': read_recent_gaps(config),
            'message': 'Sample history is managed by the external MQTT consumer; no local database history is queried.',
        })
    if config.get('DESTINATION') == 'influxdb':
        try:
            def flux_quote(value):
                value = str(value)
                if '\n' in value or '\r' in value:
                    raise ValueError('InfluxDB query values cannot contain newline characters')
                return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

            org = config.get('INFLUX_ORG', '')
            bucket = flux_quote(config.get('INFLUX_BUCKET', ''))
            measurement = flux_quote(config.get('INFLUX_MEASUREMENT', 'daq_telemetry'))
            device = flux_quote(config.get('DEVICE_ID', ''))
            flux = (f'from(bucket: {bucket}) |> range(start: -2m) '
                    f'|> filter(fn: (r) => r._measurement == {measurement} and r.device_id == {device} and r.channel == "{channel}" and r.provenance == "physical_daq")'
                    ' |> filter(fn: (r) => r._field == "raw_voltage" or r._field == "calibrated_value") '
                    '|> aggregateWindow(every: 1s, fn: mean, createEmpty: false, timeSrc: "_start") '
                    '|> pivot(rowKey: ["_time", "device_id", "channel", "session_id", "unit"], columnKey: ["_field"], valueColumn: "_value")')
            query = urllib.request.Request(
                f"{str(config['INFLUX_URL']).rstrip('/')}/api/v2/query?{urllib.parse.urlencode({'org': org})}",
                data=flux.encode(), method='POST', headers={
                    'Authorization': f"Token {config['INFLUX_TOKEN']}",
                    'Content-Type': 'application/vnd.flux', 'Accept': 'application/csv'})
            with urllib.request.urlopen(query, timeout=5) as response:
                records = list(csv.reader(io.StringIO(response.read().decode('utf-8-sig'))))
            header = None
            points = []
            for record in records:
                if not record or record[0].startswith('#'):
                    continue
                if '_time' in record and 'result' in record:
                    header = record
                    continue
                if header is None or len(record) < len(header):
                    continue
                row = dict(zip(header, record))
                if not row.get('_time'):
                    continue
                instant = datetime.fromisoformat(row['_time'].replace('Z', '+00:00')).replace(microsecond=0).isoformat()
                points.append({'time': instant,
                               'raw_voltage': float(row['raw_voltage']) if row.get('raw_voltage') else None,
                               'calibrated_value': float(row['calibrated_value']) if row.get('calibrated_value') else None,
                               'unit': row.get('unit', ''),
                               'session_id': row.get('session_id', '')})
        except Exception as exc:
            return jsonify({'message': f'Production samples unavailable: {exc}'}), 503
        return jsonify({'channel': channel, 'points': points, 'gaps': read_recent_gaps(config)})
    from psycopg2 import sql
    table = config.get('DB_PRODUCTION_TABLE', 'daq_production_samples')
    try:
        import psycopg2
        with psycopg2.connect(config['DB_DSN'], connect_timeout=3) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL('''SELECT time_bucket('1 second', time),
                    avg(raw_voltage), avg(calibrated_value), unit
                    FROM {} WHERE channel=%s AND time > now() - INTERVAL '2 minutes'
                    AND provenance='physical_daq'
                    GROUP BY 1,4 ORDER BY 1''').format(sql.Identifier(table)), (channel,))
                points = [{'time': row[0].isoformat() if hasattr(row[0], 'isoformat') else str(row[0]),
                           'raw_voltage': row[1],
                           'calibrated_value': row[2], 'unit': row[3]} for row in cursor.fetchall()]
    except Exception as exc:
        return jsonify({'message': f'Production samples unavailable: {exc}'}), 503
    return jsonify({'channel': channel, 'points': points,
                    'gaps': read_recent_gaps(config)})

@app.route('/api/scan_usb', methods=['GET'])
def api_scan_usb():
    """Return installed DAQ hardware and serial ports on the host PC."""
    devices, warnings = scan_host_usb_devices()
    return jsonify({
        'status': 'error' if warnings and not devices else 'success',
        'count': len(devices),
        'devices': devices,
        'warnings': warnings,
    }), 503 if warnings and not devices else 200


@socketio.on('connect')
def handle_connect():
    """Fires when browser client opens or refreshes the page."""
    if has_request_context():
        origin = request.headers.get('Origin')
        if origin and not auth.is_allowed_origin(origin, ALLOWED_ORIGINS):
            return False
        if session_store is not None:
            cookie_val = request.cookies.get(COOKIE_NAME)
            if not session_store.validate_cookie(cookie_val):
                return False

    pid, mode = get_running_process()
    is_active = pid is not None
    config = read_config()
    dest = config.get('DESTINATION', 'database')
    try:
        recorded_mode = Path(MODE_PATH).read_text(encoding='utf-8').strip()
    except OSError:
        recorded_mode = None
    mode_val = mode or recorded_mode or config.get('AUTO_START_MODE', 'production')
    
    # 1. Update client running status immediately
    emit('status_change', {'is_running': is_active, 'mode': mode_val, 'destination': dest})
    
    # 2. Feed last stats if process is active
    if is_active and last_stats:
        emit('stats_update', last_stats)
        
    # 3. Stream historical logs so terminal console is populated
    logs = get_last_logs(50)
    for log_line in logs:
        emit('log_update', {'log': log_line})
        
    # Start tailing if a process is already running
    if is_active:
        start_tailing()

@socketio.on('start_daq')
def handle_start(data):
    if has_request_context() and session_store is not None:
        cookie_val = request.cookies.get(COOKIE_NAME)
        if not session_store.validate_cookie(cookie_val):
            if hasattr(request, 'namespace'):
                emit('control_result', {'started': False, 'message': 'Unauthorized'})
            return
    req_mode = (data or {}).get('mode')
    result = start_acquisition(req_mode)
    if hasattr(request, 'namespace'):
        emit('control_result', result)


def start_acquisition(mode=None):
    if mode is None:
        mode = read_config().get('AUTO_START_MODE', 'production')
    if mode == 'real':
        mode = 'production'
    if mode not in ('production', 'mockup'):
        return {'started': False, 'message': 'mode must be production or mockup'}
    pid, _ = get_running_process()
    if pid is not None:
        return {'started': False, 'message': 'Acquisition is already running'}
    config = read_config()
    clear_job = read_clear_job(Path(config.get('SPOOL_DIR', '/var/lib/daq_navi/spool')))
    if clear_job.get('state') in ('starting', 'running'):
        return {'started': False, 'message': 'Wait for pending data clearing to finish'}
    if mode == 'production':
        try:
            validate_production_config(DaqNaviConfig(config, allow_env_overrides=False))
        except (ValueError, TypeError, KeyError) as exc:
            return {'started': False, 'message': str(exc)}
    script = 'buffered_daq_to_timescaledb.py' if mode == 'production' else 'mockup_stream_to_db.py'
    args = [sys.executable, os.path.join(CORE_DIR, script)]
    if mode == 'production':
        args += ['--config', CONFIG_PATH]
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as output:
            process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT,
                                       env={**os.environ, 'PYTHONUNBUFFERED': '1',
                                            'DAQ_CONFIG_PATH': CONFIG_PATH},
                                       close_fds=sys.platform != 'win32')
        Path(PID_PATH).write_text(str(process.pid), encoding='utf-8')
        Path(MODE_PATH).write_text(mode, encoding='utf-8')
        start_tailing()
        socketio.emit('status_change', {'is_running': True, 'mode': mode,
                                        'destination': config.get('DESTINATION')})
        return {'started': True, 'pid': process.pid, 'mode': mode}
    except (OSError, ValueError) as exc:
        return {'started': False, 'message': str(exc)}


@app.route('/api/start', methods=['POST'])
def api_start():
    req_data = request.get_json(silent=True) or {}
    mode = req_data.get('mode')
    with CONFIG_TRANSITION_LOCK:
        result = start_acquisition(mode)
    return jsonify(result), 200 if result['started'] else 400

@socketio.on('stop_daq')
def handle_stop():
    if has_request_context() and session_store is not None:
        cookie_val = request.cookies.get(COOKIE_NAME)
        if not session_store.validate_cookie(cookie_val):
            if hasattr(request, 'namespace'):
                emit('control_result', {'stopped': False, 'message': 'Unauthorized'})
            return
    with CONFIG_TRANSITION_LOCK:
        res = stop_acquisition(manual=True)
    if hasattr(request, 'namespace'):
        emit('control_result', res)


def reconcile_stopped_spool(config):
    """Clear a stale active marker after the acquisition process has exited."""
    if config.get('AUTO_START_MODE', 'production') != 'production':
        return True
    try:
        cfg = DaqNaviConfig(config, allow_env_overrides=False)
        spool = DurableSpool(Path(cfg.SPOOL_DIR), cfg.SPOOL_MAX_BYTES)
        try:
            if spool.state_value('acquisition_active') == '1':
                spool.set_state('acquisition_active', '0')
            pending_batches = spool.pending_batches
            pending_bytes = spool.pending_bytes
            spool_bytes = spool.usage_bytes
        finally:
            spool.close()
        target = Path(cfg.SPOOL_DIR) / 'status.json'
        try:
            runtime = json.loads(target.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            runtime = {}
        runtime.update(state='stopped', checked_at_ns=time.time_ns(),
                       pending_batches=pending_batches, pending_bytes=pending_bytes,
                       spool_bytes=spool_bytes)
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(runtime), encoding='utf-8')
        os.replace(temporary, target)
        return True
    except Exception as exc:
        print(f'Could not reconcile stopped spool state: {auth.redact_error_message(str(exc), config)}')
        return False


def stop_acquisition(manual=True):
    pid, mode = get_running_process()
    if pid is None:
        stop_tail_event.set()
        for path in (PID_PATH, MODE_PATH):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        config = read_config()
        reconciled = reconcile_stopped_spool(config)
        runtime = read_runtime_status(config)
        pending = runtime.get('pending_batches', 0)
        return {'stopped': True, 'spool_state_reconciled': reconciled, 'pending_batches': pending, 'pending_replay': pending > 0,
                'drained': pending == 0, 'writer_error': runtime.get('last_writer_error')}
    stopped = terminate_pid(pid)
    if not stopped:
        return {'stopped': False, 'message': 'Drain timeout; acquisition is still stopping'}
    stop_tail_event.set()
    for path in (PID_PATH, MODE_PATH):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    config = read_config()
    reconciled = reconcile_stopped_spool(config)
    runtime = read_runtime_status(config)
    socketio.emit('status_change', {'is_running': False, 'mode': mode or config.get('AUTO_START_MODE', 'production')})
    pending = runtime.get('pending_batches', 0)
    return {'stopped': True, 'spool_state_reconciled': reconciled, 'pending_batches': pending,
            'pending_replay': pending > 0,
            'drained': pending == 0,
            'writer_error': runtime.get('last_writer_error')}


@app.route('/api/stop', methods=['POST'])
def api_stop():
    with CONFIG_TRANSITION_LOCK:
        result = stop_acquisition(manual=True)
    return jsonify(result), 200 if result['stopped'] else 503


def read_clear_job(directory):
    for job_id, process in list(_clear_processes.items()):
        if process.poll() is not None:
            _clear_processes.pop(job_id, None)
    try:
        job = json.loads((directory / 'buffer-clear-job.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'state': 'idle'}
    if job.get('state') in ('starting', 'running'):
        pid = job.get('pid')
        if pid:
            try:
                os.kill(pid, 0)
            except (OSError, ValueError):
                job = {**job, 'state': 'failed', 'message': 'Buffer clear process exited'}
        elif time.time_ns() - job.get('checked_at_ns', 0) > 10_000_000_000:
            job = {**job, 'state': 'failed', 'message': 'Buffer clear process did not start'}
    return job


@app.route('/api/buffer/clear', methods=['GET', 'POST'])
def api_clear_buffer():
    with CONFIG_TRANSITION_LOCK:
        return _api_clear_buffer_locked()

def _api_clear_buffer_locked():
    settings = read_config()
    directory = Path(settings.get('SPOOL_DIR', '/var/lib/daq_navi/spool'))
    if request.method == 'GET':
        return jsonify(read_clear_job(directory))
    if (request.get_json(silent=True) or {}).get('confirm') != 'CLEAR BUFFER':
        return jsonify({'message': 'Confirm with CLEAR BUFFER to discard pending data'}), 400
    if get_running_process()[0] is not None:
        return jsonify({'message': 'Stop acquisition before clearing the buffer'}), 409
    if not directory.exists():
        return jsonify({'cleared_batches': 0, 'cleared_samples': 0,
                        'cleared_bytes': 0, 'recorded_gaps': 0,
                        'pending_batches': 0, 'spool_bytes': 0})
    if read_clear_job(directory).get('state') in ('starting', 'running'):
        return jsonify({'message': 'Buffer clearing is already in progress'}), 409
    max_bytes = int(settings.get('SPOOL_MAX_BYTES', 128 * 1024**3))
    try:
        spool = DurableSpool(directory, max_bytes)
        spool.close()
    except AcquisitionFault:
        return jsonify({'message': 'Acquisition is using the buffer; stop it first'}), 409
    except Exception:
        app.logger.exception('Could not open DAQ buffer')
        return jsonify({'message': 'Could not open DAQ buffer'}), 500

    job_id = str(uuid.uuid4())
    job_path = directory / 'buffer-clear-job.json'
    try:
        job_path.write_text(json.dumps({'job_id': job_id, 'state': 'starting',
                                        'checked_at_ns': time.time_ns()}), encoding='utf-8')
        with open(LOG_PATH, 'a', encoding='utf-8') as output:
            process = subprocess.Popen(
                [sys.executable, os.path.join(CORE_DIR, 'clear_spool.py'),
                 str(directory), str(max_bytes), job_id],
                stdout=output, stderr=subprocess.STDOUT,
                close_fds=sys.platform != 'win32',
            )
            _clear_processes[job_id] = process
    except OSError:
        app.logger.exception('Could not start DAQ buffer clear process')
        job_path.write_text(json.dumps({'job_id': job_id, 'state': 'failed',
                                        'message': 'Could not start buffer clear process'}), encoding='utf-8')
        return jsonify({'message': 'Could not start buffer clear process'}), 500
    return jsonify({'job_id': job_id, 'state': 'starting'}), 202

def init_application():
    """Initial recovery check and fail-closed auth secret validation on Web GUI startup."""
    try:
        users, session_key = auth.load_auth_secrets()
        configure_auth(users=users, session_key=session_key)
    except Exception as exc:
        print(f"[FATAL] Authentication setup failed: {exc}", file=sys.stderr)
        sys.exit(1)

    pid, mode = get_running_process()
    if pid is not None:
        print(f"[SYSTEM] Detected active background process running (PID: {pid}). Re-attaching...")
        start_tailing()
    else:
        cfg = read_config()
        if cfg.get('AUTO_START_ON_STARTUP', False):
            target_mode = cfg.get('AUTO_START_MODE', 'production')
            print(f"[SYSTEM] Auto-starting saved acquisition mode={target_mode}...")
            result = start_acquisition(target_mode)
            if not result['started']:
                print(f"[SYSTEM] Auto-start failed: {result['message']}")
        else:
            print("[SYSTEM] Auto-start is disabled. Awaiting manual start.")

def handle_shutdown(sig, frame):
    print("[SYSTEM] Gracefully shutting down Web GUI and sub-pipeline...")
    pid, _ = get_running_process()
    if pid is not None:
        terminate_pid(pid)
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)

if __name__ == '__main__':
    init_application()
    # Enable Werkzeug auto-reload in dev mode when FLASK_DEBUG=1 is set (docker-compose.override.yml)
    _debug = os.getenv("FLASK_DEBUG", "0").strip() == "1"
    # Served on Port 8081
    socketio.run(app, host='0.0.0.0', port=8081, debug=_debug, use_reloader=_debug, reloader_type='stat')
