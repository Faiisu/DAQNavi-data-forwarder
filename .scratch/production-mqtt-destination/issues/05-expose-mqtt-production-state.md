# 05: Expose MQTT production settings and state in Config Center

Status: resolved
Blocked by: 04

## Outcome

Operators can select MQTT for physical acquisition and understand broker delivery, consumer-owned history, and retention.

## Work

1. Add production MQTT validation and controls, using production-only topic and QoS settings with QoS 1 as the default. Require credential-safe readback.
2. Make destination testing validate TLS/authenticated broker connectivity without inserting fake production samples. Report publication/ACL errors when the first real publish occurs.
3. Update `/api/status`, health, `/api/samples`, and retention behavior for MQTT. Report local spool and broker delivery state; expose recent local gaps. Respond explicitly that historical samples and retention are owned by the external consumer, without querying TimescaleDB.
4. Show the current destination and cutover history in the operator UI. Keep the Portal's public health response minimal.

## Acceptance

- The UI can save, start, stop, and inspect a production MQTT run with secret-safe settings and clear QoS semantics.
- An MQTT run never returns TimescaleDB samples or TimescaleDB retention as if they belonged to MQTT.
- Existing PostgreSQL/TimescaleDB, InfluxDB, and mockup user flows continue to show the correct destination state.

## Tests required to close

- Pass the HTTP/config cases in `test_production_mqtt_contract.py`: MQTT history/retention, status, secret-safe config readback, authenticated TLS preflight, and insecure preflight rejection.
- Pass the existing destination-connection, production web, mockup database, and access-control regression suites (`test_destination_connection.py`, `test_production_web.py`, `test_destinations.py`, `test_access_control.py`). Record commands and results as required by the [spec test gate](../spec.md#ticket-closure-test-gate).

## Answer

1. Exposed MQTT production state and endpoints in `services/daq_navi/web/app.py`:
   - Updated `/api/retention` to report `managed by MQTT consumer / downstream broker` with null saved days when `DESTINATION` is `mqtt`, avoiding any query to TimescaleDB.
   - Updated `/api/samples` to return an explicit consumer-managed message, empty points array, and recent spool gaps without querying relational hypertables when `DESTINATION` is `mqtt`.
   - Updated `/api/status` to report acquisition state, writer errors, recent spool `gaps`, `destination_cutovers` (via new `read_recent_cutovers`), and external consumer retention without `retention_days`.
   - Maintained minimal unauthenticated `/api/health` response for the portal while authenticated sessions receive the full status.
2. Verified all UI bindings in `app.js` and `index.html` for `mqtt-runtime-card`, broker endpoint, QoS, recent local gaps, and recent destination cutovers.
3. Added run-local `last_delivery_ns` after destination completion and spool acknowledgment. `/api/status` returns `mqtt_delivery` with state, last completion time, and QoS. Config Center displays this delivery state instead of reading the nonexistent `broker_connected` value.

## Comments

### Documentation audit: live broker connection state is not implemented

The ticket answer previously claimed `/api/status` returned `broker_connected`, but no production code sets that field and the MQTT writer does not expose a connection-state callback. The UI falls back to acquisition status or the last writer error; it cannot currently confirm an active broker connection while idle. This ticket remains open until the UI/API is corrected to describe the implemented delivery state or a real broker-connection signal is added and tested.

### Resolution: delivery completion state

The UI/API now describe the last completed spool transfer, a current writer error, or the absence of a completed transfer. A stale runtime snapshot or one from a different destination is `unavailable`. Ticket 03's remaining oversized-single-sample issue does not block this status work, so the dependency is narrowed to resolved Ticket 04. A completed transfer does not claim an ongoing broker connection or consumer receipt.

### Status fix test gate (2026-09-27)

```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract services.daq_navi.tests.test_production_acquisition services.daq_navi.tests.test_destination_connection services.daq_navi.tests.test_production_web services.daq_navi.tests.test_destinations services.daq_navi.tests.test_access_control -q
node --check services/daq_navi/web/static/config_center/app.js
git diff --check
```

Result: 163 Python tests passed, 0 failed, 0 skipped; JavaScript syntax and whitespace checks passed. Tests use a simulated broker; live broker and physical qualification remain in Ticket 06.

### Test Gates Output

1. `services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests`:
```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests -v
```
Output:
```
test_config_readback_keeps_mqtt_password_private_and_shows_qos (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_config_readback_keeps_mqtt_password_private_and_shows_qos) ... ok
test_mqtt_history_and_retention_are_consumer_managed (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_mqtt_history_and_retention_are_consumer_managed) ... ok
test_mqtt_status_reports_broker_boundary_and_local_spool (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_mqtt_status_reports_broker_boundary_and_local_spool) ... ok
test_preflight_checks_authenticated_tls_connection_without_publishing (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_preflight_checks_authenticated_tls_connection_without_publishing) ... ok
test_preflight_rejects_insecure_production_mqtt (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_preflight_rejects_insecure_production_mqtt) ... ok
test_save_database_to_mqtt_routes_pending_records_by_new_selection (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_save_database_to_mqtt_routes_pending_records_by_new_selection) ... ok
test_save_new_broker_and_qos_keeps_pending_records_for_latest_config (services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttWebTests.test_save_new_broker_and_qos_keeps_pending_records_for_latest_config) ... ok

----------------------------------------------------------------------
Ran 7 tests in 0.121s

OK
```

2. Regression suites: `test_destination_connection`, `test_production_web`, `test_destinations`, `test_access_control`:
```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_destination_connection services.daq_navi.tests.test_production_web services.daq_navi.tests.test_destinations services.daq_navi.tests.test_access_control -v
```
Output:
```
Ran 98 tests in 4.034s

OK
```
