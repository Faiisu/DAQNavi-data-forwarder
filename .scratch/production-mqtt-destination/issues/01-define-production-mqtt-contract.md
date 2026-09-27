# 01: Define the production MQTT record contract

Status: resolved
Blocked by: None

## Outcome

Publisher and external consumer share an explicit JSON v1 contract that preserves production identity, provenance, and gap state.

## Work

1. Document versioned production topic names for each device's samples and gaps. Specify safe device ID encoding and a production topic prefix.
2. Define bounded sample chunk envelopes with deterministic batch/chunk IDs, all production sample fields, exact nanosecond timestamps, and stable replay bytes or equivalent canonical identities. Define consumer deduplication by `sample_id`.
3. Define gap-open and gap-close event envelopes with `gap_id`, increasing revision, current start/end/cause, and consumer merge rules. State that arrival order across topics is not guaranteed.
4. Define the QoS 0 and QoS 1 completion boundaries and external consumer responsibilities. Add example payloads and consumer fixtures under project documentation.

## Acceptance

- A consumer can reconstruct samples and gaps from the contract, including duplicate chunks, close-before-open arrival, and replay after a destination cutover.
- Production topics and payloads have a unique prefix and cannot be confused with database mockup records.

## Tests required to close

- Check the JSON examples and consumer fixtures against the input/output expectations in `services/daq_navi/tests/test_production_mqtt_contract.py`, especially the sample wire record, bounded replay chunks, and open/closed gap cases. Document any schema decision that changes those expectations.
- Run the existing mockup database and destination preflight regression tests in `test_destinations.py` and `test_destination_connection.py`; they must remain passing. Record commands and results as required by the [spec test gate](../spec.md#ticket-closure-test-gate). The production writer tests are required to pass at Ticket 03 closure.

## Answer

### 1. Specification Document
Documented the full JSON v1 contract in [`docs/contracts/production-mqtt-contract-v1.md`](../../../docs/contracts/production-mqtt-contract-v1.md).

### 2. Topic Hierarchy & Safe Encoding
- Topic pattern:
  - Samples: `<MQTT_PRODUCTION_TOPIC_PREFIX>/<safe_device_id>/samples`
  - Gaps: `<MQTT_PRODUCTION_TOPIC_PREFIX>/<safe_device_id>/gaps`
- Default prefix: `daq/production/v1`
- Safe encoding: Device identifiers are percent-encoded (`urllib.parse.quote(device_id, safe='')`) so that IDs such as `rack/one` occupy a single topic level (`rack%2Fone`), while payloads retain the unencoded `device_id` string.

### 3. Wire Record Envelopes & Consumer Fixtures
Created consumer fixtures under [`docs/contracts/fixtures/`](../../../docs/contracts/fixtures/):
- **Sample Envelope** ([`production-sample-envelope.json`](../../../docs/contracts/fixtures/production-sample-envelope.json)): Schema version 1, `device_id`, `batch_id`, deterministic `chunk_id`, and array of physical sample points including `time_ns`, `sample_id`, `session_id`, `device_id`, `channel`, `sensor_name`, `raw_voltage`, `calibrated_value`, `unit`, `provenance: "physical_daq"`. `calibration_revision` is retired.
- **Bounded Replay Chunks** ([`bounded-chunks.json`](../../../docs/contracts/fixtures/bounded-chunks.json)): Demonstrates deterministic partitioning when batches exceed byte bounds, maintaining identical `chunk_id` and payload contents across replayed attempts. Consumers deduplicate by `sample_id`.
- **Gap Open Event** ([`production-gap-open.json`](../../../docs/contracts/fixtures/production-gap-open.json)): Schema version 1, `gap_id`, `revision: 1`, `start_ns`, `end_ns: null`, and `cause`.
- **Gap Closed Event** ([`production-gap-closed.json`](../../../docs/contracts/fixtures/production-gap-closed.json)): Schema version 1, matching `gap_id`, `revision: 2`, `start_ns`, `end_ns: <ts>`, and `cause`. Self-contained fields enable consumers to recover state even if close arrives before open or across destination cutovers.

### 4. QoS Boundaries & Consumer Responsibilities
- **QoS 0**: Spool acknowledges upon MQTT client `on_publish` callback (local socket write).
- **QoS 1 (Default)**: Spool acknowledges upon broker `PUBACK`. Delivery guarantees at least once; consumer deduplicates using `sample_id`.
- **Consumer Ownership**: Downstream processing, long-term storage, deduplication, historical queries, and alerting are owned by the external consumer. DAQNavi displays history and retention as consumer-managed.

## Comments

### Test Gate Execution
- Fixture validation test:
  ```bash
  .venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract.ProductionMqttContractFixtureTests -v
  ```
  Result: 3 passed, 0 failed.
  - `test_sample_wire_record_fixture`: passed
  - `test_gap_fixtures_match_open_and_closed_expectations`: passed
  - `test_bounded_chunks_fixture_and_consumer_deduplication`: passed

- Mockup database and destination preflight regression test:
  ```bash
  .venv/bin/python -m unittest services.daq_navi.tests.test_destinations services.daq_navi.tests.test_destination_connection -v
  ```
  Result: 14 passed, 0 failed.
