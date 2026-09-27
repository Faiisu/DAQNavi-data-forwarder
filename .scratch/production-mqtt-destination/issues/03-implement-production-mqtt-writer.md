# 03: Implement the production MQTT writer

Status: resolved
Blocked by: 01, 02

## Outcome

The physical acquisition pipeline publishes full production records to an external authenticated TLS broker while retaining the current durable replay boundary.

## Work

1. Implement a destination behind the existing `write(rows, gaps)` seam. Split large batches into deterministic byte-bounded messages. Keep publish calls and callbacks safe across reconnects and process shutdown.
2. For QoS 0 wait for each message's `on_publish`; for QoS 1 wait for each PUBACK. Return success only after all sample chunks and gap events in the write complete. On timeout, disconnect, rejection, or uncertain completion, leave the source spool entries pending for replay.
3. Validate broker host/port, username and password, TLS CA and server certificate, optional client certificate, production topic prefix, and QoS. Require TLS/authentication for production. Keep credentials out of logs, errors, and API responses.
4. Use stable record IDs across retries, never use retained messages for telemetry, and bound in-flight messages and memory. Keep mockup database destinations unchanged.

## Acceptance

- QoS 0 and 1 completion occurs at their agreed boundaries; retries may repeat bytes but preserve logical IDs.
- Broker outage and reconnect do not acknowledge unsent records; bounded chunks sustain the configured sample rate without unbounded memory use.
- TLS validation failure, bad credentials, and publish timeout are visible as delivery faults without leaking secrets.

## Tests required to close

- Pass the configuration, writer, and spool cases in `test_production_mqtt_contract.py`: secure QoS 0/1 validation, independent QoS default, invalid settings, sample and gap wire records, bounded stable chunks, all-message `on_publish` completion, publish rejection, and ambiguous completion replay.
- Pass the existing mockup database and destination preflight regression tests in `test_destinations.py` and `test_destination_connection.py`. Record commands and results as required by the [spec test gate](../spec.md#ticket-closure-test-gate).

## Answer

### 1. Implementation of MQTTProductionDestination
Implemented `MQTTProductionDestination` in `services/daq_navi/core/production_acquisition.py`:
- Initializes an authenticated TLS Paho MQTT client using configured host, port, username, password, and certificates.
- Separate topic routing for samples (`<prefix>/<safe_device_id>/samples`) and gaps (`<prefix>/<safe_device_id>/gaps`).
- Connects and runs background client network loop safely.
- Implements `write(rows, gaps)`, `ensure_schema()`, and `close()`.

### 2. Deterministic Bounded Chunking & Replay
- Partitions rows into deterministic chunks whose serialized sample envelopes stay at or below `MQTT_PRODUCTION_MAX_PAYLOAD_BYTES` (default 256 KiB, tested at 600 bytes). Rejects an indivisible sample whose one-record envelope exceeds the maximum before publishing any message from that source batch; the durable spool retains the source batch for retry.
- Generates deterministic `batch_id` and `chunk_id` (`{batch_id}:c{idx}`) ensuring identical chunk identity across replays.
- Publishes gap records with schema version 1 and exact revision.

### 3. Completion Tracking for QoS 0 and QoS 1
- `_on_publish` callback sets thread-safe completion events (`_pending_mids`).
- `write()` blocks until all published sample chunks and gap messages in the write call receive `on_publish` confirmation (client flush for QoS 0, broker PUBACK for QoS 1).
- Publish rejection (`rc != 0`) or timeout immediately raises an exception to the caller, leaving source batches in the durable spool for replay.

### 4. Configuration Validation
- In `validate_production_config`, added validation for production MQTT:
  - Enforces `MQTT_TLS_ENABLED=true`
  - Requires non-empty `MQTT_USERNAME` and `MQTT_PASSWORD`
  - Restricts `MQTT_PRODUCTION_QOS` to 0 or 1
  - Forbids wildcards (`+`, `#`) in `MQTT_PRODUCTION_TOPIC_PREFIX`
- Loaded `MQTT_PRODUCTION_QOS` (default 1) and topic prefix in `DaqNaviConfig`.
- Integrated `MQTTProductionDestination` into `run_production`.

## Comments

### Resolution: byte ceiling edge case

An oversized indivisible sample now raises a clear `ValueError` with its stable sample ID and measured/maximum byte counts. The writer performs chunk sizing before publishing, so no earlier chunk from the same source batch is emitted before the error. `ProductionPipeline.flush_once()` leaves the source batch pending in the durable spool. Automated tests verify both the writer boundary and spool behavior.

### Test Gate Execution (2026-09-27)

```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract services.daq_navi.tests.test_production_acquisition services.daq_navi.tests.test_destinations services.daq_navi.tests.test_destination_connection services.daq_navi.tests.test_production_web services.daq_navi.tests.test_config_reliability services.daq_navi.tests.test_access_control -q
```

Result: 186 tests passed, 0 failed, 0 skipped. The test suite uses a simulated MQTT broker; no live broker qualification is implied, and that evidence remains tracked by Ticket 06.
