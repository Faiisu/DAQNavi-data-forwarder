# DAQ Config Center

The DAQ Navi Flask service serves the Config Center at port `8081`. Its REST API is available under `/api/` on the same port. The page template is `templates/index.html`; the JavaScript and CSS are in `static/config_center/`.

## Operator login

See [First-time Login and Configuration in the deployment guide](../DEPLOY.md#step-3-first-time-login-and-configuration) for the initial credentials and password rotation instructions.

Production startup requires operator credentials and a session signing key supplied by the deployment. `/api/health` is intentionally public and returns a minimal health response. The Config Center and other API routes, including `/api/status`, require a valid operator session.

## Configuration workflow

1. Set the start channel and number of AI channels to read. The sensor table follows that span. The current production service accepts AI0–AI15. Enabled inputs outside a changed span remain visible so they can be disabled.
2. Edit Enabled, sensor label, engineering unit, signal type, and input range directly in the sensor table. On PCI-1716, differential pairs start on an even channel and reserve the following odd channel.
3. Turn calibration on or off in each sensor row. Select **Edit** on a row to set its linear conversion. Production acquisition requires calibration on every enabled sensor.
4. Configure and test the destination, then save. PostgreSQL can use explicit host/credentials fields mode or custom connection string mode. MQTT uses production-only topic and QoS settings and requires broker authentication and verified TLS. See the [MQTT v1 contract](../docs/production-mqtt-contract-v1.md) for wire and delivery semantics. Saved credentials remain masked; leave a secret input blank to preserve its saved value. Saving while acquisition runs may stop and restart that run after confirmation. The browser submits the loaded config revision, and an outdated tab must reload after a 409 response.

If a destination preflight fails, the running acquisition is left alone. Pending spool behavior and destination cutovers are defined in the [production data flow](../docs/data-flow.md) and [ADR 0014](../docs/adr/0014-current-configuration-routes-pending-samples.md). A post-commit start failure means the new settings are persisted but acquisition is stopped; the error panel and runtime status show that state. A TimescaleDB retention failure tries to restore the prior policy; if restoration also fails, the response names the table, saved retention, and operator correction. Save a PostgreSQL destination change and retention-day change separately; the new target must already have a policy matching the saved days before switching. Influx bucket retention stays managed in InfluxDB. MQTT history and downstream health are managed by the external consumer.

Configuration errors appear in the header of the section that contains them, with details directly below that header. The page scrolls to the first affected section after an unsuccessful save.

The **Pending data** card shows the current pending batch count and observed increase, decrease, and net rates in batches per minute. The page samples `/api/status` every five seconds and calculates these rates over the most recent 60 seconds (or the time observed so far). The separate increase and decrease rates expose fluctuations that a net rate alone would hide. Rates restart after a status request fails or the page reloads; changes between polls cannot be measured.

The **Clear pending data** button becomes available after acquisition stops and when the production spool contains undelivered batches. It permanently discards those batches, records their sample intervals as `operator_cleared_buffer` gaps, and reclaims local spool space. It does not delete samples already delivered to TimescaleDB. The matching API is `POST /api/buffer/clear` with JSON `{"confirm":"CLEAR BUFFER"}`; it rejects a running acquisition or locked spool. Clearing runs in a separate process so the web API stays responsive. Poll `GET /api/buffer/clear` for completion or failure.

In development mode, Compose mounts the service directory into the DAQ container. Static asset edits become visible immediately. Restart `daq-navi` after changing the page template if its Flask process has cached the template.

## API behavior

| Endpoint | Access | Behavior |
| --- | --- | --- |
| `GET /api/health` | Public | Minimal service health for container checks and Portal polling. |
| `GET /api/status` | Operator session | Acquisition, destination, pending spool, recent gaps, cutovers, and delivery status. |
| `GET /api/config`, `POST /api/config` | Operator session | Read secret-redacted settings or validate and save settings. |
| `POST /api/test_destination` | Operator session | Check the selected backend connection without writing production data. |
| `POST /api/start`, `POST /api/stop` | Operator session | Start or stop the selected acquisition mode. |
| `GET /api/samples?channel=N` | Operator session | Recent database history for PostgreSQL/TimescaleDB or InfluxDB; MQTT returns no historical samples and identifies the external consumer as owner. |
| `GET /api/retention` | Operator session | TimescaleDB policy, Influx bucket-managed retention, or MQTT consumer-managed retention. |

The clear-buffer operation is `POST /api/buffer/clear` with `{"confirm":"CLEAR BUFFER"}`. It is available only while acquisition is stopped and clears pending local spool records; it does not delete records already delivered to a destination.

## Production destinations

Production acquisition supports PostgreSQL/TimescaleDB, InfluxDB 2.x, and MQTT (v1 JSON wire contract).
Config Center allows selecting MQTT as the production destination with an external authenticated TLS broker. The [MQTT JSON v1 contract](../docs/production-mqtt-contract-v1.md) is authoritative for topics, payloads, QoS, and consumer responsibilities.

InfluxDB requires an HTTP(S) URL, organization, bucket, and token with write access. Production writes use the configured measurement at `precision=ns`; `sample_id` is a field, while `device_id`, `channel`, `session_id`, `unit`, and `provenance` are tags. Do not add `sample_id` as a tag: that creates a high-cardinality series per sample.

Influx acquisition gaps are written to the `daq_acquisition_gaps` measurement. `gap_id` is the stable tag; `start_ns`, optional `end_ns`, `open`, and `cause` are fields. Open intervals are updated in place when their boundary becomes known. Influx retention is configured on the bucket in InfluxDB and is displayed as bucket-managed in Config Center. The production samples API reads the selected Influx bucket; it does not query TimescaleDB for an Influx run.

For MQTT, `/api/status` exposes `mqtt_delivery` with the last completed spool transfer in the current run and an error, pending, idle, stopped, or unavailable state. The runtime card uses this delivery state. Completion means the configured QoS boundary was reached: QoS 1 waits for broker acknowledgment; QoS 0 completes after the client sends. It does not assert an ongoing broker connection or consumer processing.

The `/api/status` response supplies recent `destination_cutovers` entries (`time_ns`, `from`, `to`, and `pending_records`), which the operator UI displays with the pending local spool. The [production data flow](../docs/data-flow.md) defines retry and destination-change behavior. Config Center does not allow changing `SPOOL_DIR`; moving a spool requires an explicit migration.

## Safe Influx qualification

Use a disposable Influx organization and bucket with a short bucket retention period and a token scoped to that bucket. Start with one enabled channel at 1 kHz, then 2 kHz, and confirm distinct per-channel timestamps at the expected 1 ms and 500 μs intervals. Stop acquisition and compare sample IDs and point counts after an induced HTTP outage/replay; ambiguous retries should overwrite the same points. Exercise a recorded gap and verify its stable `gap_id`, start/end, cause, and open state. Confirm Config Center status, retention text, and the samples graph/API, then return to the production bucket only after qualification. Do not use a production bucket for fault injection.

## Safe MQTT qualification & operations

### 1. Broker, Account & Certificate Setup
- Production MQTT requires an authenticated broker endpoint and verified TLS encryption (`MQTT_TLS_ENABLED: true`).
- Configure `MQTT_BROKER`, `MQTT_PORT` (default 8883), `MQTT_USERNAME`, and `MQTT_PASSWORD`.
- If using custom or internal CA certificates, provide `MQTT_CA_CERTS`, `MQTT_CLIENT_CERT`, and `MQTT_CLIENT_KEY`.
- Test destination connectivity via `POST /api/test_destination` (or "Test Connection" button). The preflight check validates TLS negotiation and credential authentication without publishing dummy sample data.

### 2. Consumer Deduplication & Gap Merging
- Topic, payload, size-target, deduplication, and gap-merging rules are defined by the [MQTT JSON v1 contract](../docs/production-mqtt-contract-v1.md).

### 3. QoS Boundaries & Delivery Guarantees
- MQTT QoS completion, replay, and deduplication rules are defined in the [MQTT v1 contract](../docs/production-mqtt-contract-v1.md).
- During a broker outage, committed records remain in the persistent spool. The contract defines delivery completion; the [production data flow](../docs/data-flow.md) describes the spool lifecycle.

### 4. Destination Cutovers & Historical Records
- Pending-record routing and acknowledged history are described in the [production data flow](../docs/data-flow.md). Cutovers are exposed by authenticated `/api/status` under `destination_cutovers`.
