# 12: Control acquisition and apply saved settings from the web UI

**What to build:** Operators start and stop production or explicit mockup acquisition through the web UI. Saving changed settings applies them at a clear session boundary through controlled stop, local drain, and restart.

**Blocked by:** 11: Edit supported acquisition settings through the web UI.

**Status:** resolved

- [x] Web Stop and configuration changes preserve committed buffered data and report whether the previous session drained or remains pending replay.
- [x] The latest saved configuration controls automatic start after reboot; when auto-start is enabled, reboot starts acquisition even after an earlier manual Stop.
- [x] A failed acquisition child is restarted according to saved configuration or reported as stopped with a fault; no implicit mockup fallback occurs.
- [x] Web/API tests cover start, Stop, save while running, process failure, and reboot behavior.

## Answer

All acceptance criteria for Ticket 12 have been implemented and verified:

1. **Web Stop and Configuration Changes Preserving Buffer and Reporting Drain Status**:
   - `stop_acquisition()` in [`services/daq_navi/web/app.py`](../../../services/daq_navi/web/app.py) initiates graceful termination via SIGTERM, unlinks PID/mode tracking files, and inspects the spool runtime status. It returns `drained: bool`, `pending_replay: bool`, and `pending_batches: int`. All committed SQLite spool batches remain preserved on disk.
   - `save_config()` checks if an acquisition process is active, executes `stop_acquisition(manual=False)` to drain, restarts acquisition with the saved mode, and returns `drained`, `pending_replay`, and `pending_batches` in the response payload. If acquisition was idle, it similarly reads the existing spool runtime state to report drain status.

2. **Reboot Auto-Start Controlled by Saved Configuration**:
   - `init_application()` checks `config.json` for `AUTO_START_ON_STARTUP` and `AUTO_START_MODE`.
   - Manual `stop_acquisition(manual=True)` terminates the active process and removes `.daq_process.pid` and `.daq_process.mode`, but leaves `config.json` intact.
   - Upon reboot, `init_application()` detects that no process is running and launches the configured mode if `AUTO_START_ON_STARTUP` is true.

3. **Fault Handling and No Implicit Mockup Fallback**:
   - If an acquisition child terminates unexpectedly while `.daq_process.pid` exists, `/api/status` detects the stale PID and marks the system as `'faulted'` with `fault: 'acquisition_process_exited'` (or specific runtime fault), with `healthy: false` (/api/health returns 503).
   - In `handle_connect()` and log tailing, client WebSocket emissions report the actual configured/recorded mode (`mode_val`), eliminating any implicit fallback to `'mockup'`.
   - The child process can be restarted explicitly or via configured reboot without falling back to mockup.

4. **Testing and Verification**:
   - Strengthened and added comprehensive unit tests in [`services/daq_navi/tests/test_production_web.py`](../../../services/daq_navi/tests/test_production_web.py) covering:
     - `test_start_already_running_rejected`
     - `test_start_invalid_mode_rejected`
     - `test_start_explicit_mockup_launches_mockup_script`
     - `test_start_default_mode_uses_saved_configuration`
     - `test_stop_when_not_running`
     - `test_stop_drain_timeout_reports_503`
     - `test_save_running_session_drains_then_restarts_saved_mode`
     - `test_save_running_session_with_pending_replay_reports_not_drained`
     - `test_save_running_session_stop_timeout_aborts_without_restart`
     - `test_stale_pid_process_exit_reports_faulted_without_mockup_fallback`
     - `test_handle_connect_in_faulted_state_preserves_configured_mode`
     - `test_restart_after_process_failure_recovers_saved_mode`
     - `test_reboot_auto_start_after_manual_stop_lifecycle`
     - `test_reboot_auto_start_disabled_does_not_start`
     - `test_stop_reports_drained_and_pending_replay`
   - All 26 tests pass via `docker exec daq_navi python3 -m unittest tests.test_production_web -v`.

