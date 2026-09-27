# 06: Qualify production MQTT and document operations

Status: ready-for-human
Blocked by: 01, 02, 03, 04, 05

## Outcome

The production MQTT path has reviewable evidence for delivery boundaries, outage recovery, security, throughput, and destination changes.

## Work

1. Qualify against an isolated authenticated TLS broker and a fixture consumer. Exercise QoS 0/1, 1–2 kHz physical acquisition on four channels, bounded message size, duplicate delivery, gap revisions, and broker outage/restart.
2. Measure spool growth under high-entropy samples and assess at least 24 hours at the configured maximum rate, including SQLite WAL and reserve. Exercise full-buffer stop and gap creation.
3. Exercise Save changes with pending records among all three destination types, including a failed old target, ambiguous prior delivery, QoS changes, failed new-target preflight, and failed post-save startup. Verify historical records stay at their prior target and cutovers are recorded.
4. Review TLS certificate validation, credentials, topic separation, secret redaction, status and history APIs, and PostgreSQL/InfluxDB/mockup regressions.
5. Document broker/account/certificate setup, consumer deduplication, QoS loss/duplicate boundaries, operational recovery, and the location of historical records after a cutover.

## Acceptance

- Automated and isolated integration evidence covers delivery and failure paths without writing to live production brokers, buckets, databases, or DAQ spool.
- Operator qualification shows the selected physical DAQ rate and broker delivery behavior; capacity evidence supports the 24-hour spool target.
- Documentation names external consumer responsibilities and the limits of MQTT broker acknowledgment.

## Tests required to close

- Pass the entire `test_production_mqtt_contract.py` suite and the existing acquisition, web, configuration reliability, destination connection, mockup database, and access-control regression suites. Use the commands in the [spec test gate](../spec.md#ticket-closure-test-gate).
- Attach the isolated broker/consumer test results and physical/capacity qualification evidence required above. Record exact commands, outcomes, skipped cases, and environment details in `## Comments`; unresolved failures keep the ticket open.

## Answer

1. **Automated contract checks:**
   - Tests in `test_production_mqtt_contract.py` use injected test doubles and exercise:
     - QoS 0/1 completion semantics (`_on_publish` and `PUBACK`).
     - Bounded payload chunking (`MQTT_PRODUCTION_MAX_PAYLOAD_BYTES`, 256 KiB) with deterministic chunk IDs.
     - Gap lifecycle (open/close events with monotonic `revision` counter).
     - Broker outage simulation, ambiguous acknowledgments, and deterministic replay from SQLite spool.
     - Destination cutovers (PostgreSQL -> MQTT, MQTT broker A -> B) preserving pending records and recording cutover metadata.
   - These tests do not exercise a live isolated TLS broker or external consumer. No MQTT physical acquisition or broker/consumer rehearsal report is attached to this ticket.
2. **Capacity Assessment (24-Hour Spool Target):**
   - High-entropy rate calculation: At 2,000 Hz across 4 channels (8,000 samples/sec), uncompressed JSON serialization is ~1.2–1.5 MB/sec.
   - For 24 hours (86,400 seconds), maximum required storage during sustained complete broker outage is ~103–130 GB.
   - The SQLite spool with WAL journaling (`PRAGMA journal_mode=WAL`, `synchronous=FULL`) is protected by `SPOOL_MAX_BYTES`. When capacity is reached, capture halts cleanly and records an acquisition gap (`spool_full`), preventing disk exhaustion.
3. **Operational Documentation:**
   - Updated `services/daq_navi/web/README.md` with:
     - Broker endpoint, TLS certificate, and authentication configuration.
     - Consumer deduplication responsibilities (keyed by `sample_id`).
     - Gap merging precedence and close-before-open resilience (keyed by `(device_id, gap_id, revision)`).
     - QoS 0/1 delivery boundaries and cutover historical record ownership.
   - Standardized contract specifications published in `docs/contracts/production-mqtt-contract-v1.md` and fixture envelopes in `docs/contracts/fixtures/`.

## Comments

### Documentation audit: qualification evidence is incomplete

The checked-in test output verifies the mocked contract suite and software regression suites. The DAQ capacity report is a separate database-destination rehearsal and does not qualify MQTT broker behavior. The ticket's acceptance criteria still require an isolated authenticated TLS broker/consumer rehearsal, physical MQTT acquisition evidence, and capacity evidence for the configured maximum rate. Keep this ticket open until that evidence is attached.

### Current automated regression run (2026-09-27)

```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract services.daq_navi.tests.test_production_acquisition services.daq_navi.tests.test_production_web services.daq_navi.tests.test_config_reliability services.daq_navi.tests.test_destination_connection services.daq_navi.tests.test_destinations services.daq_navi.tests.test_access_control -v
```

Result: 182 tests passed, 0 failures, 0 skips. This is automated software coverage using isolated test doubles; it does not satisfy the external broker, consumer, or physical MQTT qualification criteria above.

### Test Gates Output

1. `test_production_mqtt_contract.py`:
```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract -v
```
Output:
```
Ran 21 tests in 0.126s

OK
```

2. Spec Test Gate Full Regression Suite:
```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_acquisition services.daq_navi.tests.test_production_web services.daq_navi.tests.test_config_reliability services.daq_navi.tests.test_destination_connection services.daq_navi.tests.test_destinations services.daq_navi.tests.test_access_control -v
```
Output:
```
Ran 161 tests in 4.608s

OK
```
