"""Process transitions and runtime state for the physical acquisition child."""

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class LifecycleOperations:
    config_path: str
    core_dir: str
    log_path: str
    pid_path: str
    mode_path: str
    read_config: Callable
    get_running_process: Callable
    read_clear_job: Callable
    validate_config: Callable
    start_tailing: Callable
    stop_tailing: Callable
    emit_status: Callable
    terminate_pid: Callable
    reconcile_stopped_spool: Callable
    read_runtime_status: Callable


def is_pid_running(pid, platform):
    if platform == 'win32':
        try:
            output = subprocess.check_output(f'tasklist /fi "PID eq {pid}"', shell=True)
            return str(pid) in str(output)
        except Exception:
            return False
    try:
        stat_path = f'/proc/{pid}/stat'
        if os.path.exists(stat_path) and Path(stat_path).read_text().split(') ', 1)[1][0] == 'Z':
            return False
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def acquisition_mode_for_pid(pid, is_running, proc_root, core_dir, config_path, platform):
    """Identify a child from its exact command arguments."""
    if not is_running(pid) or platform == 'win32':
        return None
    try:
        arguments = (proc_root / str(pid) / 'cmdline').read_bytes().split(b'\0')
    except OSError:
        return None
    if len(arguments) < 2:
        return None
    script = arguments[1].decode(errors='replace')
    if script == os.path.join(core_dir, 'mockup_stream_to_db.py'):
        return 'mockup'
    if script in (os.path.join(core_dir, 'buffered_daq_to_timescaledb.py'),
                  os.path.join(core_dir, 'stream_to_db.py')):
        for index, argument in enumerate(arguments[:-1]):
            if argument == b'--config' and arguments[index + 1] == os.fsencode(config_path):
                return 'production'
    return None


def find_acquisition_child(proc_root, mode_for_pid, platform):
    """Recover a live acquisition child when its PID file was overwritten."""
    if platform == 'win32':
        return None, None
    try:
        tasks = (proc_root / 'self' / 'task').iterdir()
        children = set()
        for task in tasks:
            children.update(int(pid) for pid in (task / 'children').read_text().split())
    except (OSError, ValueError):
        return None, None
    for pid in sorted(children):
        mode = mode_for_pid(pid)
        if mode:
            return pid, mode
    return None, None


def get_running_process(pid_path, mode_path, is_running, mode_for_pid, find_child, platform):
    if os.path.exists(pid_path) and os.path.exists(mode_path):
        try:
            pid = int(Path(pid_path).read_text(encoding='utf-8').strip())
            mode = Path(mode_path).read_text(encoding='utf-8').strip()
            if (platform == 'win32' and is_running(pid)) or mode_for_pid(pid) == mode:
                return pid, mode
        except Exception as exc:
            print(f'Error checking active PID file: {exc}')
    pid, mode = find_child()
    if pid is not None:
        Path(pid_path).write_text(str(pid), encoding='utf-8')
        Path(mode_path).write_text(mode, encoding='utf-8')
        return pid, mode
    return None, None


def terminate_pid(pid, is_running, platform):
    if platform == 'win32':
        try:
            subprocess.run(['taskkill', '/pid', str(pid), '/t', '/f'], check=False)
            return not is_running(pid)
        except Exception as exc:
            print(f'Error terminating Windows PID {pid}: {exc}')
            return False
    try:
        os.kill(pid, 15)
        for _ in range(250):
            if not is_running(pid):
                return True
            time.sleep(0.1)
        return False
    except ProcessLookupError:
        return True
    except OSError as exc:
        print(f'Error terminating Unix PID {pid}: {exc}')
        return False


def start_acquisition(mode, ops):
    if mode is None:
        mode = ops.read_config().get('AUTO_START_MODE', 'production')
    if mode == 'real':
        mode = 'production'
    if mode != 'production':
        return {'started': False, 'message': 'Only physical production acquisition is supported.'}
    pid, _ = ops.get_running_process()
    if pid is not None:
        return {'started': False, 'message': 'Acquisition is already running'}
    config = ops.read_config()
    clear_job = ops.read_clear_job(Path(config.get('SPOOL_DIR', '/var/lib/daq_navi/spool')))
    if clear_job.get('state') in ('starting', 'running'):
        return {'started': False, 'message': 'Wait for pending data clearing to finish'}
    try:
        ops.validate_config(config)
    except (ValueError, TypeError, KeyError) as exc:
        return {'started': False, 'message': str(exc)}
    args = [sys.executable, os.path.join(ops.core_dir, 'buffered_daq_to_timescaledb.py'),
            '--config', ops.config_path]
    process = None
    try:
        with open(ops.log_path, 'a', encoding='utf-8') as output:
            process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT,
                                       env={**os.environ, 'PYTHONUNBUFFERED': '1',
                                            'DAQ_CONFIG_PATH': ops.config_path},
                                       close_fds=sys.platform != 'win32')
        Path(ops.pid_path).write_text(str(process.pid), encoding='utf-8')
        Path(ops.mode_path).write_text(mode, encoding='utf-8')
        ops.start_tailing()
        ops.emit_status({'is_running': True, 'status': 'starting', 'mode': mode,
                         'destination': config.get('DESTINATION')})
        return {'started': True, 'pid': process.pid, 'mode': mode}
    except (OSError, ValueError) as exc:
        if process is not None:
            stopped = ops.terminate_pid(process.pid)
            if stopped:
                for path in (ops.pid_path, ops.mode_path):
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
            else:
                return {'started': False, 'process_may_be_running': True,
                        'message': f'{exc}; acquisition process may still be running'}
        return {'started': False, 'message': str(exc)}


def stop_acquisition(ops):
    pid, mode = ops.get_running_process()
    if pid is not None and not ops.terminate_pid(pid):
        return {'stopped': False, 'message': 'Drain timeout; acquisition is still stopping'}
    ops.stop_tailing()
    for path in (ops.pid_path, ops.mode_path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    config = ops.read_config()
    reconciled = ops.reconcile_stopped_spool(config)
    runtime = ops.read_runtime_status(config)
    if pid is not None:
        ops.emit_status({'is_running': False,
                         'mode': mode or config.get('AUTO_START_MODE', 'production')})
    pending = runtime.get('pending_batches', 0)
    return {'stopped': True, 'spool_state_reconciled': reconciled,
            'pending_batches': pending, 'pending_replay': pending > 0,
            'drained': pending == 0, 'writer_error': runtime.get('last_writer_error')}


def reconcile_stopped_spool(config, config_type, spool_type, redact_error):
    """Clear the active marker after the acquisition process has exited."""
    if config.get('AUTO_START_MODE', 'production') != 'production':
        return True
    try:
        cfg = config_type(config, allow_env_overrides=False)
        spool = spool_type(Path(cfg.SPOOL_DIR), cfg.SPOOL_MAX_BYTES)
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
        print(f'Could not reconcile stopped spool state: {redact_error(str(exc), config)}')
        return False


def describe_status(pid, mode, config, runtime, recorded_mode, pid_exists,
                    launched_at_ns, gaps, cutovers, now_ns):
    """Build HTTP status from the live child and its matching runtime snapshot."""
    is_running = pid is not None
    mode_val = mode or recorded_mode or config.get('AUTO_START_MODE', 'production')
    dest = config.get('DESTINATION', 'postgresql')
    if mode_val != 'production':
        runtime = {}
    elif is_running:
        if 'pid' in runtime:
            if runtime['pid'] != pid:
                runtime = {}
        elif launched_at_ns is not None and runtime.get('checked_at_ns', 0) < launched_at_ns:
            runtime = {}
    stale_pid = pid_exists and not is_running
    fresh = 0 <= now_ns - runtime.get('checked_at_ns', 0) < 10_000_000_000
    fault = (runtime.get('last_fault') if is_running or stale_pid else None) or (
        'acquisition_process_exited' if stale_pid else None)
    if is_running and not fresh and (runtime or
                                    launched_at_ns is not None and now_ns - launched_at_ns >= 10_000_000_000):
        fault = fault or 'acquisition_status_stale'
    writer_error = runtime.get('last_writer_error') if is_running and fresh else None
    expected = mode_val == 'production' and (
        bool(config.get('AUTO_START_ON_STARTUP', False)) or pid_exists)
    ready = is_running and fresh and runtime.get('state') == 'running'
    status = {
        'service_name': 'DAQ USB-4716', 'port': 8081,
        'is_running': is_running,
        'status': ('faulted' if fault else 'buffering' if writer_error else
                   'running' if ready else 'starting' if is_running else 'stopped'),
        'mode': mode_val, 'run_mode': mode_val, 'pid': pid,
        'destination': dest, 'expected_running': expected,
        'healthy': (not fault and not writer_error and
                    (ready if is_running else not expected)),
        'fault': fault,
        'pending_batches': runtime.get('pending_batches', 0),
        'pending_bytes': runtime.get('pending_bytes', 0),
        'spool_bytes': runtime.get('spool_bytes', 0),
        'last_sample_ns': runtime.get('last_sample_ns'),
        'writer_error': writer_error,
        'gaps': gaps, 'destination_cutovers': cutovers,
        'retention_days': (config.get('DB_RETENTION_DAYS', 30)
                           if dest not in ('influxdb', 'mqtt') else None),
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
                          'pending' if runtime.get('pending_batches', 0) else 'idle')
        status['mqtt_delivery'] = {
            'state': delivery_state, 'last_completed_ns': last_delivery_ns,
            'qos': config.get('MQTT_PRODUCTION_QOS', 1),
        }
    return status
