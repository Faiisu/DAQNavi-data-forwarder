# 02: Route pending spool records by current configuration and version gaps

Status: resolved
Blocked by: 01

## Outcome

Pending samples and gaps use the latest saved destination and QoS without losing a gap revision during concurrent capture and delivery.

## Work

1. Remove spool-owner restrictions from startup and save paths for pending production records. Preserve stable sample and gap IDs while replaying under the latest saved configuration, including across TimescaleDB, InfluxDB, and MQTT.
2. Add durable gap revision tracking. Acknowledge only the revision actually written; if capture closes an open gap during a write, its new revision remains pending. Keep open and close events available for the MQTT writer.
3. Record configuration cutovers with timestamp and old/new target descriptions for operator history. The cutover record is informational and does not control routing. Do not copy already acknowledged history.
4. Cover crash points between destination success and spool acknowledgment, direct startup after configuration changes, and old target availability failures.

## Acceptance

- Pending records always follow current saved settings after restart; old spool metadata cannot block or redirect them.
- An in-flight open gap followed by closure cannot cause the unsent closed revision to be acknowledged.
- Existing database destination replay remains idempotent; cutover history distinguishes delivered history from pending replay.

## Tests required to close

- Pass `test_gap_close_during_open_delivery_remains_pending` in `test_production_mqtt_contract.py` and the current-destination replay tests in `test_production_acquisition.py` (`test_startup_replays_pending_spool_to_current_destination`, `test_startup_replays_pending_spool_after_target_change`).
- Pass the existing spool/replay regression tests in `test_production_acquisition.py`. Record commands and results as required by the [spec test gate](../spec.md#ticket-closure-test-gate).

## Answer

### 1. Removal of Spool Owner Restrictions
- In `services/daq_navi/core/production_acquisition.py`, removed hard failure exceptions in `run_production` when destination or target identity changes with pending records in the spool.
- In `services/daq_navi/web/app.py`, updated `drain_spool_for_destination_switch` so destination switches do not attempt to drain pending records to the old destination. Instead, pending records are preserved for the new destination, open gaps are cleanly closed, and a cutover record is written.

### 2. Durable Gap Revision Tracking
- Enhanced the `gaps` SQLite table schema in `DurableSpool` with `revision INTEGER NOT NULL DEFAULT 1` and `delivered_revision INTEGER NOT NULL DEFAULT 0`.
- In `open_gap` and `record_gap`, initial events are recorded with `revision: 1`.
- When an open gap is closed (either in `close_open_gaps_for_switch` or when samples arrive in `append`), the record is updated with `revision = revision + 1` and `end_ns`.
- `pending_gaps()` filters by `delivered_revision < revision` and includes `revision` in returned dictionaries.
- `acknowledge_gaps(gap_items)` accepts dictionaries and updates `delivered_revision = MAX(delivered_revision, rev)`. If an open gap was delivered while capture closed it concurrently (incrementing `revision` to 2), acknowledging revision 1 leaves revision 2 pending.
- `ProductionPipeline.flush_batches` passes the actual `gaps` list with revisions into `acknowledge_gaps`.

### 3. Configuration Cutover Ledger
- Added `cutovers` table in SQLite (`id`, `time_ns`, `old_destination`, `new_destination`, `pending_records`).
- Added `record_cutover` and `recent_cutovers` methods to `DurableSpool`.
- Recorded cutovers whenever the target changes, storing `from`, `to`, and count of pending records at cutover.

## Comments

### Test Gate Execution
- Gap revision test:
  ```bash
  .venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttSpoolTests.test_gap_close_during_open_delivery_remains_pending -v
  ```
  Result: 1 passed, 0 failed.

- Spool replay & acquisition regression suite:
  ```bash
  .venv/bin/python -m unittest services.daq_navi.tests.test_production_acquisition -v
  ```
  Result: 42 passed, 0 failed.
  - `test_startup_replays_pending_spool_to_current_destination`: passed
  - `test_startup_replays_pending_spool_after_target_change`: passed
  - All 40 existing regression tests: passed

- Web destination switch and config reliability tests:
  ```bash
  .venv/bin/python -m unittest services.daq_navi.tests.test_production_web services.daq_navi.tests.test_config_reliability -v
  ```
  Result: 74 passed, 0 failed.

