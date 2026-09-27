# 01: Implement InfluxDB 2.x as a production DAQ destination

Status: resolved
Blocked by: None

## Outcome

Physical DAQ production acquisition can use either PostgreSQL/TimescaleDB or InfluxDB 2.x as its saved destination. InfluxDB writes preserve nanosecond sample timestamps and durable spool/replay behavior, and the Config Center and sample API reflect the selected destination.

## Evidence

- `services/daq_navi/core/production_acquisition.py` and its destination modules implement InfluxDB writes behind the production writer path, including sample and acquisition-gap records.
- `services/daq_navi/web/app.py` and the Config Center support InfluxDB destination validation, status, retention presentation, and sample queries.
- `services/daq_navi/web/README.md` documents the InfluxDB record layout, spool behavior, destination switching, and safe qualification procedure.
- On 2026-09-26, the focused acquisition, destination connection, and production web suites passed (99 tests), and `git diff --check` passed. The implementation review reported no remaining blockers.
- The user confirmed InfluxDB 2.x worked during operator qualification with the physical DAQ.

## Work

- [x] Add the production InfluxDB destination using nanosecond timestamps, stable sample identity, and acquisition-gap representation.
- [x] Preserve spool acknowledgement ordering, retries, replay behavior, and destination-switch safeguards.
- [x] Support InfluxDB configuration, connection testing, runtime status, bucket-managed retention, and production sample queries in the Config Center and API.
- [x] Document configuration, limitations, and safe operator qualification.
- [x] Verify focused automated suites and implementation review; complete physical DAQ operator qualification.

## Acceptance

- [x] Config Center supports both PostgreSQL/TimescaleDB and InfluxDB production destinations.
- [x] InfluxDB writes use nanosecond precision; retries preserve logical sample and gap identity, and failed writes remain pending for replay.
- [x] Production sample queries follow the selected destination, and InfluxDB retention is identified as bucket-managed.
- [x] Existing PostgreSQL behavior and focused automated coverage pass; qualification succeeds on the physical DAQ.
- [x] No live deployment, production bucket fault injection, or data deletion was performed as part of implementation.

## Comments

- 2026-09-26: Ticket opened at the user's request. GPT-6-Sol (medium) planned and reviewed; GPT-6-Luna implemented. The existing uncommitted `services/daq_navi/config.json` change was preserved.
- 2026-09-26: Implementation completed across the production writer, destination selection, spool ownership and switch guards, Config Center, sample API, documentation, and focused tests. Review found no remaining blockers. No live DAQ run, InfluxDB write, deployment, or config change was performed during implementation; physical qualification remained for the operator.
- 2026-09-26: User confirmed InfluxDB 2.x worked in operator qualification with the physical DAQ. Ticket resolved.
