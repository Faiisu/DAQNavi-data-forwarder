# Qualify a destination before production use

Use disposable destination resources and a private DAQ config. The goal is to observe real delivery and replay behavior, beyond the Config Center connection check.

## InfluxDB

1. Create a disposable organization/bucket and a token limited to that bucket. Set a short bucket retention period.
2. Configure one enabled, calibrated channel and start at 1 kHz, then 2 kHz. Check distinct timestamps and point counts at the destination.
3. While acquisition continues, interrupt the HTTP destination and record the spool backlog. Restore the destination without stopping acquisition. Check that pending bytes decrease over a sustained observation window while new DAQ samples still arrive, or that the spool stays near empty. An empty spool after acquisition stops only proves that stored data can be replayed; it does not prove the writer can catch up with live acquisition. Compare sample IDs and point counts after the spool empties. Stable IDs should overwrite the same logical points on ambiguous retry.
4. Record a gap and inspect `gap_id`, start/end, cause, and open state. Compare Config Center status with the bucket contents before moving to a production bucket.

## MQTT

1. Provision a disposable authenticated TLS broker account and a consumer subscribed to the production topic prefix.
2. Test the connection in Config Center, then run a short physical capture and check the received sample and gap payloads against the [MQTT v1 contract](../production-mqtt-contract-v1.md).
3. Interrupt the broker and restore it. Verify spool pending counts, replay, consumer deduplication by `sample_id`, and gap merging by `gap_id` and revision.
4. Confirm the consumer stores history and applies its own retention. DAQNavi's MQTT delivery status does not prove consumer processing.

For physical DAQ timing and outage replay against TimescaleDB, use the isolated scripts in the [test and qualification guide](../../tests/README.md).
