# 04: Apply the newly saved destination to all pending records

Status: resolved
Blocked by: 02, 03

## Outcome

Saving a destination or QoS change safely resumes production with the new settings, and every pending spool record uses them.

## Work

1. Update Config Center save flow to stop capture after local durable commit without requiring old-destination drain. Preflight the new target, atomically persist settings, and restart using the newly saved destination.
2. Apply the new MQTT QoS to pending records as well as new records. Allow changes across MQTT broker/topic and across PostgreSQL/TimescaleDB, InfluxDB, and MQTT. Preserve IDs; leave already acknowledged history at its prior destination.
3. Keep failed preflight from changing saved settings. If startup fails after save, keep pending records and report the saved configuration and stopped state accurately. Make cutover history visible and informational.
4. Preserve secret-safe config responses and existing rollback behavior where possible; replace the old spool-owner/drain checks that conflict with current-configuration routing.

## Acceptance

- A Save during an old-target outage succeeds when the new target passes preflight; pending records replay only to the latest saved target.
- QoS 1-to-0 and 0-to-1 saves apply the new QoS to pending MQTT records.
- Failed preflight, interrupted save, and post-save restart failure retain committed spool data and report the effective saved state.

## Tests required to close

- Pass `test_save_new_broker_and_qos_keeps_pending_records_for_latest_config` and `test_save_database_to_mqtt_routes_pending_records_by_new_selection` in `test_production_mqtt_contract.py`.
- Pass the destination-switch and same-backend target-change tests in `test_production_web.py`, the current-destination replay tests in `test_production_acquisition.py`, and the save-failure tests in `test_config_reliability.py`. Record commands and results as required by the [spec test gate](../spec.md#ticket-closure-test-gate).

## Answer

1. Updated `services/daq_navi/web/app.py`:
   - Added `'mqtt'` to `supported_sinks` in `_save_config_locked()`.
   - Constrained TimescaleDB retention verification to relational destinations (`postgresql`, `timescaledb`, `database`) so switching to/from MQTT or InfluxDB does not query TimescaleDB retention policies.
   - Preserved `drain_spool_for_destination_switch` behavior (closing open gaps and recording cutover metadata in spool cutovers table without draining pending records to old sinks), allowing pending records to replay directly to the new destination.
   - Updated `_test_destination` to handle MQTT preflight verification using secure TLS, credentials, and connection validation without message publication.
   - Updated save response retention description for MQTT (`managed by MQTT consumer / downstream broker`).
2. Retained committed spool data across preflight failures, interrupted saves, or post-save restart failures, with accurate reporting of saved configuration and process state.

## Comments

### Test Gates Output

1. `services.daq_navi.tests.test_production_mqtt_contract`:
```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_save_new_broker_and_qos_keeps_pending_records_for_latest_config services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_save_database_to_mqtt_routes_pending_records_by_new_selection -v
```
Output:
```
test_save_new_broker_and_qos_keeps_pending_records_for_latest_config (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_save_new_broker_and_qos_keeps_pending_records_for_latest_config) ... ok
test_save_database_to_mqtt_routes_pending_records_by_new_selection (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_save_database_to_mqtt_routes_pending_records_by_new_selection) ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.042s

OK
```

2. `services.daq_navi.tests.test_production_web`:
```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_web.ProductionWebTests.test_destination_switch_keeps_pending_spool_for_new_destination services.daq_navi.tests.test_production_web.ProductionWebTests.test_destination_switch_saves_then_restarts_running_acquisition services.daq_navi.tests.test_production_web.ProductionWebTests.test_same_backend_target_change_saves_with_pending_records -v
```
Output:
```
test_destination_switch_keeps_pending_spool_for_new_destination (services.daq_navi.tests.test_production_web.ProductionWebTests.test_destination_switch_keeps_pending_spool_for_new_destination) ... ok
test_destination_switch_saves_then_restarts_running_acquisition (services.daq_navi.tests.test_production_web.ProductionWebTests.test_destination_switch_saves_then_restarts_running_acquisition) ... ok
test_same_backend_target_change_saves_with_pending_records (services.daq_navi.tests.test_production_web.ProductionWebTests.test_same_backend_target_change_saves_with_pending_records) ... ok

----------------------------------------------------------------------
Ran 3 tests in 0.058s

OK
```

3. `services.daq_navi.tests.test_production_acquisition`:
```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_acquisition.ProductionAcquisitionTests.test_startup_replays_pending_spool_to_current_destination services.daq_navi.tests.test_production_acquisition.ProductionAcquisitionTests.test_startup_replays_pending_spool_after_target_change -v
```
Output:
```
test_startup_replays_pending_spool_to_current_destination (services.daq_navi.tests.test_production_acquisition.ProductionAcquisitionTests.test_startup_replays_pending_spool_to_current_destination) ... ok
test_startup_replays_pending_spool_after_target_change (services.daq_navi.tests.test_production_acquisition.ProductionAcquisitionTests.test_startup_replays_pending_spool_after_target_change) ... ok

----------------------------------------------------------------------
Ran 2 tests in 0.007s

OK
```

4. `services.daq_navi.tests.test_config_reliability`:
```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_config_reliability -v
```
Output:
```
Ran 21 tests in 0.339s

OK
```
