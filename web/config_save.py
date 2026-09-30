"""Coordinate a config save and recovery independently of Flask request handling."""

from dataclasses import dataclass
from typing import Callable, ContextManager

from . import auth


@dataclass(frozen=True)
class SaveResult:
    body: dict
    reason: str = "ok"


@dataclass(frozen=True)
class SaveOperations:
    """Effects required by the save transaction, bound by the application at call time."""

    transition_lock: ContextManager
    read_config: Callable[[], dict]
    config_revision: Callable[[dict], str]
    merge_config: Callable[[dict, dict], dict]
    get_running_process: Callable[[], tuple]
    destination_identity: Callable[[dict], object]
    preflight_destination: Callable[[dict], object]
    stop_acquisition: Callable[..., dict]
    drain_spool_for_destination_switch: Callable[[dict, dict], None]
    apply_retention_policy: Callable[[dict], None]
    effective_timescale_retention: Callable[[dict], str]
    update_spool_owner: Callable[[dict, str, object], None]
    write_config: Callable[[dict], bool]
    start_acquisition: Callable[[str], dict]
    redact_response_values: Callable[..., object]
    read_runtime_status: Callable[[dict], dict]


def outcome(body, reason="ok"):
    return SaveResult(body, reason)


def resume_after_failed_save(ops, stop_result, mode, current):
    if not stop_result or not stop_result.get('stopped'):
        return None
    try:
        return ops.start_acquisition(mode or current.get('AUTO_START_MODE', 'production'))
    except Exception as exc:
        return {'started': False, 'message': auth.redact_error_message(str(exc), current)}


def disk_state(ops, current, updated):
    """Read the actual saved revision after an uncertain atomic-write outcome."""
    try:
        saved = ops.read_config()
        if ops.config_revision(saved) == ops.config_revision(current):
            return 'old'
        if ops.config_revision(saved) == ops.config_revision(updated):
            return 'new'
    except Exception:
        pass
    return 'unknown'


def runtime_after_recovery(stop_result, recovered):
    if recovered and recovered.get('started'):
        return 'old configuration resumed'
    return 'stopped' if stop_result else 'unchanged'


def restore_previous_retention(ops, config, days):
    """Compensate a policy update and verify its effective value."""
    ops.apply_retention_policy(config)
    effective = ops.effective_timescale_retention(config)
    if effective != f"{days} days":
        raise RuntimeError(f'readback after compensation was {effective}')

def save_configuration(payload, ops):
    with ops.transition_lock:
        return _save_config_locked(payload, ops)

def _save_config_locked(payload, ops):
    try:
        current = ops.read_config()
    except Exception as exc:
        return outcome({'status': 'error', 'message': f'Cannot save: existing configuration file is invalid or missing ({exc})'}, 'failed')
    try:
        payload = dict(payload or {})
        submitted_revision = payload.pop('_REV', None)
        if submitted_revision is not None and submitted_revision != ops.config_revision(current):
            return outcome({'status': 'error', 'message': 'Configuration was modified since it was loaded. Reload before saving.'}, 'conflict')
        payload = auth.merge_preserved_secrets(payload, current)
        updated = ops.merge_config(current, payload)
    except (ValueError, TypeError, KeyError) as exc:
        return outcome({'status': 'error', 'message': auth.redact_error_message(str(exc), current, payload)}, 'invalid')
    if current.get('SPOOL_DIR', '/var/lib/daq_navi/spool') != updated.get('SPOOL_DIR', '/var/lib/daq_navi/spool'):
        return outcome({'status': 'error', 'message': 'SPOOL_DIR changes are unsupported; pending records and gap history must remain in the existing spool.'}, 'invalid')
    previous_retention = current.get('DB_RETENTION_DAYS', 30)
    pid, mode = ops.get_running_process()
    was_running = pid is not None
    stop_result = None
    supported_sinks = {'postgresql', 'timescaledb', 'database', 'influxdb', 'mqtt'}
    needs_identity = (
        current.get('AUTO_START_MODE', 'production') == 'production' or
        updated.get('AUTO_START_MODE', 'production') == 'production'
    ) and current.get('DESTINATION', 'postgresql') in supported_sinks and updated.get('DESTINATION', 'postgresql') in supported_sinks
    current_identity = updated_identity = None
    destination_changed = False
    if needs_identity:
        try:
            current_identity = ops.destination_identity(current)
            updated_identity = ops.destination_identity(updated)
        except (ValueError, TypeError) as exc:
            return outcome({'status': 'error', 'message': auth.redact_error_message(str(exc), current, updated)}, 'invalid')
        destination_changed = current_identity != updated_identity
    retention_changed = ('DB_RETENTION_DAYS' in updated and
                         ('DB_RETENTION_DAYS' not in current or updated['DB_RETENTION_DAYS'] != previous_retention))
    if destination_changed and updated.get('DESTINATION', 'postgresql') in ('postgresql', 'timescaledb', 'database'):
        if retention_changed:
            return outcome({'status': 'error', 'persisted': False,
                            'message': 'Save the destination and retention days in separate operations; no changes were applied.'}, 'invalid')
    if destination_changed:
        try:
            ops.preflight_destination(updated)
        except Exception as exc:
            return outcome({'status': 'error', 'message': f'New destination preflight failed: {auth.redact_error_message(str(exc), current, updated)}'}, 'dependency_failed')
    if was_running:
        try:
            stop_result = ops.stop_acquisition(manual=False)
        except Exception as exc:
            return outcome({'status': 'error', 'persisted': False, 'runtime': 'unknown',
                            'message': f'Could not stop acquisition before saving configuration: {auth.redact_error_message(str(exc), current, updated)}'}, 'unavailable')
        if not stop_result.get('stopped'):
            return outcome({'status': 'error', 'message': 'Could not stop acquisition before saving configuration.', 'persisted': False, 'runtime': 'prior acquisition may still be stopping'}, 'unavailable')
    if destination_changed:
        try:
            ops.drain_spool_for_destination_switch(current, updated)
        except Exception as exc:
            recovered = resume_after_failed_save(ops, stop_result, mode, current)
            recovery_note = ('; prior acquisition resumed' if recovered and recovered.get('started') else
                       '; recovery failed; acquisition remains stopped' if stop_result else '')
            runtime = 'old configuration resumed' if recovered and recovered.get('started') else 'stopped' if stop_result else 'unchanged'
            return outcome({'status': 'error', 'message': f'Could not record destination cutover: {auth.redact_error_message(str(exc), current, updated)}{recovery_note}.', 'persisted': False, 'runtime': runtime, 'resumed': bool(recovered and recovered.get('started')), 'recovery': ops.redact_response_values(recovered, current, updated)}, 'unavailable')
    retention_applied = False
    if retention_changed and updated.get('DESTINATION', 'postgresql') in ('postgresql', 'timescaledb', 'database'):
        try:
            ops.apply_retention_policy(updated)
            effective = ops.effective_timescale_retention(updated)
            if effective != f"{updated['DB_RETENTION_DAYS']} days":
                raise RuntimeError(f'Policy readback is {effective}; expected {updated["DB_RETENTION_DAYS"]} days')
            retention_applied = True
        except Exception as exc:
            # ensure_schema may have changed the policy before raising, so always
            # try to restore and verify the previous setting.
            restore_error = None
            try:
                restore_previous_retention(ops, current, previous_retention)
            except Exception as restore_exc:
                restore_error = auth.redact_error_message(str(restore_exc), current, updated)
            recovered = resume_after_failed_save(ops, stop_result, mode, current) if restore_error is None else None
            resumed = bool(recovered and recovered.get('started'))
            recovery = (' Previous acquisition resumed.' if resumed else
                        ' Previous acquisition remains stopped.' if stop_result else '')
            if restore_error:
                message = (f'Retention apply failed ({auth.redact_error_message(str(exc), current, updated)}); compensation failed ({restore_error}). '
                           f'Saved config remains {previous_retention} days; effective policy may differ. '
                           f'Operator action: set retention on {current.get("DB_PRODUCTION_TABLE")} to {previous_retention} days and verify it.{recovery}')
            else:
                message = f'Could not apply retention policy; previous {previous_retention}-day policy was restored: {auth.redact_error_message(str(exc), current, updated)}.{recovery}'
            return outcome({'status': 'error', 'persisted': False, 'message': message,
                            'runtime': 'old configuration resumed' if resumed else 'stopped' if stop_result else 'unchanged',
                            'resumed': resumed, 'recovery': ops.redact_response_values(recovered, current, updated),
                            'retention_compensated': restore_error is None}, 'unavailable')
    if destination_changed:
        try:
            ops.update_spool_owner(updated, updated.get('DESTINATION', 'postgresql'), updated_identity)
        except Exception as exc:
            restored = True
            try:
                ops.update_spool_owner(current, current.get('DESTINATION', 'postgresql'), current_identity)
            except Exception:
                restored = False
            retention_restore_error = None
            if retention_applied:
                try:
                    restore_previous_retention(ops, current, previous_retention)
                except Exception as restore_exc:
                    retention_restore_error = auth.redact_error_message(str(restore_exc), current, updated)
            recovered = None
            if restored and retention_restore_error is None:
                recovered = resume_after_failed_save(ops, stop_result, mode, current)
            return outcome({'status': 'error', 'persisted': False,
                            'runtime': 'old configuration resumed' if recovered and recovered.get('started') else 'stopped' if stop_result else 'unchanged',
                            'resumed': bool(recovered and recovered.get('started')),
                            'spool_owner_restored': restored,
                            'retention_compensated': retention_restore_error is None,
                            'message': (f'Could not update spool destination ownership: {auth.redact_error_message(str(exc), current, updated)}; '
                                        + ('old owner restored.' if restored else 'spool ownership restoration failed; inspect spool destination metadata before restarting acquisition.')
                                        + (f' Retention compensation failed ({retention_restore_error}); verify the effective policy before retrying.' if retention_restore_error else ''))}, 'unavailable')
    try:
        written = ops.write_config(updated)
    except Exception:
        written = False
    if not written:
        saved_state = disk_state(ops, current, updated)
        if saved_state != 'old':
            return outcome({'status': 'error', 'persisted': True if saved_state == 'new' else None,
                            'runtime': 'stopped' if stop_result else 'unchanged', 'resumed': False,
                            **({'config': auth.redact_config_secrets({**updated, '_REV': ops.config_revision(updated)})}
                               if saved_state == 'new' else {}),
                            'message': 'Configuration write failed; disk contains the new configuration.' if saved_state == 'new'
                                       else 'Configuration write failed; saved configuration could not be verified. Inspect the disk before restarting acquisition.'}, 'failed')
        spool_restored = True
        if destination_changed:
            try:
                ops.update_spool_owner(current, current.get('DESTINATION', 'postgresql'), current_identity)
            except Exception:
                spool_restored = False
        compensation = 'not required'
        retention_restored = True
        if retention_applied:
            try:
                restore_previous_retention(ops, current, previous_retention)
                compensation = f'previous {previous_retention}-day policy restored'
            except Exception as exc:
                retention_restored = False
                compensation = f'FAILED ({auth.redact_error_message(str(exc), current, updated)}); set {current.get("DB_PRODUCTION_TABLE")} retention to {previous_retention} days and verify'
        recovered = None
        if spool_restored and retention_restored:
            recovered = resume_after_failed_save(ops, stop_result, mode, current)
        recovery_text = ('; prior acquisition resumed' if recovered and recovered.get('started') else
                         '; prior acquisition remains stopped' if stop_result else '')
        if not spool_restored:
            recovery_text += '; spool ownership restoration failed; inspect spool metadata before restarting acquisition'
        return outcome({'status': 'error', 'persisted': False, 'runtime': runtime_after_recovery(stop_result, recovered), 'resumed': bool(recovered and recovered.get('started')), 'recovery': ops.redact_response_values(recovered, current, updated), 'spool_owner_restored': spool_restored, 'retention_compensated': retention_restored,
                        'message': f'Failed to save configuration; {compensation}{recovery_text}.'}, 'failed')
    safe_config = auth.redact_config_secrets({**updated, '_REV': ops.config_revision(updated)})
    if stop_result:
        target_mode = updated.get('AUTO_START_MODE') if ('AUTO_START_MODE' in (payload or {})) else (mode or updated.get('AUTO_START_MODE', 'production'))
        try:
            start_result = ops.start_acquisition(target_mode)
        except Exception as exc:
            start_result = {'started': False, 'message': auth.redact_error_message(str(exc), current, updated)}
        if not start_result['started']:
            runtime = 'unknown' if start_result.get('process_may_be_running') else 'stopped'
            return outcome({'status': 'error', 'persisted': True, 'runtime': runtime, 'message': 'Configuration is saved, but acquisition did not restart.', 'config': safe_config, **ops.redact_response_values(start_result, current, updated)}, 'unavailable')
        pending_replay = stop_result.get('pending_replay', False)
        pending_batches = stop_result.get('pending_batches', 0)
        drained = stop_result.get('drained', not pending_replay)
        return outcome({'status': 'success', 'config': safe_config,
                        'retention': ('managed by InfluxDB bucket' if updated.get('DESTINATION') == 'influxdb'
                                      else 'managed by MQTT consumer / downstream broker' if updated.get('DESTINATION') == 'mqtt'
                                      else 'TimescaleDB policy'),
                        'previous_session': {'stopped': True, 'drained': drained,
                                             'pending_replay': pending_replay, 'pending_batches': pending_batches},
                        'drained': drained, 'pending_replay': pending_replay, 'pending_batches': pending_batches,
                        'retention_days': updated['DB_RETENTION_DAYS']})
    runtime = ops.read_runtime_status(updated)
    pending = runtime.get('pending_batches', 0)
    return outcome({
        'status': 'success',
        'config': safe_config,
        'retention_days': updated['DB_RETENTION_DAYS'],
        'drained': pending == 0,
        'pending_replay': pending > 0,
        'pending_batches': pending,
    })
