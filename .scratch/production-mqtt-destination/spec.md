# Production acquisition with MQTT as a destination

Status: accepted

## Outcome

An operator can select MQTT as the single production destination alongside PostgreSQL/TimescaleDB and InfluxDB 2.x. Physical samples and acquisition gaps leave the durable local spool for an authenticated TLS MQTT broker. An external consumer owns downstream processing, history, and its own health.

## Delivery contract

- The latest saved configuration controls delivery of **all records still pending in the spool**. A Save can change broker, topic, QoS, or destination type; pending records then use the new values. Delivery does not depend on the previous destination. Already acknowledged records stay at their prior destination. Record each configuration cutover and its time for operators, without using that record to gate delivery.
- MQTT production QoS is selectable as 0 or 1, defaulting to 1. A source batch is acknowledged only when every message made from it completes: `on_publish` for QoS 0 (sent from the client, without broker acknowledgment), PUBACK for QoS 1. A process failure before spool acknowledgment may cause replay. QoS 0 can lose a message after it leaves the client; QoS 1 can deliver duplicates. Stable IDs let the consumer deduplicate.
- Keep the existing spool capacity target of at least 24 hours at the configured production rate during broker outage. A stopped or unreachable broker leaves data pending; a full spool stops acquisition and records a gap.
- Connect to an external broker using verified TLS and authentication. Secrets remain private in configuration readback. Destination preflight checks connection and credentials; actual publication errors leave records pending.

## MQTT record contract

- Define a production-only versioned JSON v1 contract. Topics separate samples and gaps by device. Encode the device topic segment safely and include the original `device_id` in each payload.
- Each sample retains `time_ns`, `sample_id`, `session_id`, `device_id`, `channel`, `sensor_name`, `raw_voltage`, `calibrated_value`, `unit`, and `provenance`. Sample messages contain deterministic, byte-bounded chunks with stable batch/chunk IDs. The consumer deduplicates by `sample_id`.
- Publish gap-open and gap-close events using the same `gap_id` and increasing revision. Each event includes its current start, end, and cause, so a close event can stand alone after a destination change. Acknowledge a gap revision only if that exact revision was sent.
- No arrival order across sample and gap topics is promised. Consumers reconstruct state using IDs, revisions, and timestamps. MQTT messages are not retained snapshots.

## Operator and API behavior

- Config Center validates MQTT production settings, shows the selected QoS and delivery boundary, and controls start/stop with the saved settings. Saving a new destination stops capture after durable local commit, preflights the new destination, atomically saves configuration, and restarts against the new destination. It does not drain pending records to the old destination.
- `/api/status` and health report acquisition, local spool, and broker delivery state. The existing local spool can show recent gaps. Consumer storage and health are external.
- `/api/samples` gives an explicit response that historical MQTT samples are owned by the external consumer; it never queries TimescaleDB for an MQTT run. Retention is displayed as consumer-managed and does not modify a TimescaleDB policy while MQTT is selected.

## Delivery order

1. Define the JSON v1 contract and production-only configuration fields.
2. Make spool gap acknowledgments revision-safe and route pending records from current saved configuration.
3. Implement the production MQTT writer with bounded messages and QoS-specific completion.
4. Update Config Center save/restart behavior for current-configuration delivery across all three destinations.
5. Update production validation, status, samples, retention, and operator controls.
6. Qualify isolated broker and consumer behavior, throughput, outages, recovery, and configuration cutovers; document operator setup.

## Acceptance

- Four physical channels sustain 1–2 kHz each; isolated load checks cover the maximum supported 16 channels at 2 kHz. A measured capacity assessment establishes at least 24 hours of spool space for the configured rate.
- Broker outages, publish timeouts, ambiguous acknowledgments, process restarts, and spool exhaustion preserve the stated QoS boundaries without silently dropping committed records.
- Saving any supported destination type while records are pending routes those records to the newly saved destination and QoS. Already acknowledged records remain at their prior destination; cutover history is visible.
- Gap-open and gap-close delivery survives replay and a concurrent gap transition without acknowledging an unsent revision.
- MQTT history and retention responses identify the external consumer; PostgreSQL/TimescaleDB, InfluxDB, and mockup behavior remain valid.

## Ticket closure test gate

- Before changing an implementation ticket to `resolved`, run the existing automated tests named in that ticket's **Tests required to close** section. Every applicable test must pass; do not skip, mark expected failure, delete, or weaken a test to close a ticket. If a test expectation is genuinely wrong, update the contract and test together and record the reason in the ticket.
- Run the relevant existing regression suites for the files changed by that ticket. Record the exact command, pass/fail count, skipped tests and reasons, and any environment prerequisite in the ticket's `## Comments` section. A failed required test keeps the ticket open.
- Ticket 01 defines the record contract before its writer exists: its closure requires the documented examples to match the existing test fixtures and the previously passing regression tests to remain passing. The production writer tests become mandatory when Ticket 03 closes.
- Ticket 06 closes only after the full production MQTT contract suite, the affected acquisition/web/configuration suites, and the existing mockup database regression tests pass, in addition to its isolated integration and physical qualification evidence.

Local automated test commands:

```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract -v
.venv/bin/python -m unittest services.daq_navi.tests.test_production_acquisition services.daq_navi.tests.test_production_web services.daq_navi.tests.test_config_reliability services.daq_navi.tests.test_destination_connection services.daq_navi.tests.test_destinations services.daq_navi.tests.test_access_control -v
```
