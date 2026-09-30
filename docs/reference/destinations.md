# Production destination reference

The selected `DESTINATION` in `config/config.json` controls where committed production records are sent. Its implementation and validation live in [core/production_acquisition.py](../../core/production_acquisition.py). The main Compose stack includes `timescaledb`, `influxdb`, and `mqtt`; only the selected destination receives production records.

When cycle counting is enabled for a channel, each completed `interval_seconds` acquisition window produces a count record, including windows with zero crossings. The interval defaults to one second and can be set independently for each channel. Windows align to Unix time boundaries. An `up` crossing moves from below the calibrated threshold to at or above it; `down` moves from above to at or below it. The first value only arms the counter when it is on the starting side. `cycle_count` is the count in that interval and `total_count` is the count since the current acquisition session began. A count record is committed with the sample batch that first enters the next interval, then delivered through the same durable spool. The final incomplete interval is not published when acquisition stops.

| Destination | Required connection information | History owner |
| --- | --- | --- |
| PostgreSQL/TimescaleDB | Host/port/database/user/password or DSN; a TimescaleDB capable target is required for the production schema and policies | Database |
| InfluxDB 2.x | HTTP(S) URL, organization, bucket, write token, measurement | InfluxDB bucket |
| MQTT | Broker host/port, username/password, verified TLS, topic prefix and QoS | External consumer |

## PostgreSQL/TimescaleDB

Count records are stored in `<DB_PRODUCTION_TABLE>_cycle_counts` with the production retention period.

## InfluxDB

Production writes use nanosecond precision. `sample_id` is a field; `device_id`, `channel`, `session_id`, `unit`, and `provenance` are tags. Keeping `sample_id` out of tags avoids one series per sample. Acquisition gaps use the `daq_acquisition_gaps` measurement and a stable `gap_id` tag. Bucket retention is configured in InfluxDB.

Count records use `<INFLUX_MEASUREMENT>_cycle_counts` with `direction` as an additional tag and `threshold`, `interval_seconds`, `cycle_count`, and `total_count` as fields.

## MQTT

MQTT uses separate sample, cycle count, and gap topics. The broker must authenticate the publisher over verified TLS. QoS 1 waits for broker acknowledgment before the local spool record is acknowledged; QoS 0 completes when the client reports publication, which is a weaker boundary. Consumer processing and historical storage are outside DAQNavi. The [MQTT JSON v1 contract](../production-mqtt-contract-v1.md) defines topic names, payloads, replay identity, and consumer duties.

The connection check does not publish a sample. Confirm delivery at the destination after starting acquisition. When changing destinations, pending records follow the latest saved configuration; the [cutover ADR](../adr/0014-current-configuration-routes-pending-samples.md) explains the reason and duplicate risk.
