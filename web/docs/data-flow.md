# Production Data Flow

1. An operator saves the physical device, channel span, signal settings, calibration, and one production destination in DAQNavi.
2. The acquisition process opens the configured Advantech device and reads the selected analog input span.
3. It timestamps each enabled channel measurement, retains raw voltage, and calculates the calibrated value and unit.
4. The acquisition loop commits each production sample batch and acquisition-gap event to the persistent SQLite spool. This local commit is synchronous; the record is eligible for delivery only after the commit succeeds.
5. The writer sends pending records to the latest saved destination. PostgreSQL/TimescaleDB and InfluxDB acknowledge after successful writes. MQTT uses separate sample and gap topics; its QoS completion rules are defined in the [MQTT v1 contract](production-mqtt-contract-v1.md).
6. A successful destination completion removes the corresponding pending record from the spool. Failures leave it pending for retry. Every pending record follows the latest saved destination and MQTT QoS; already acknowledged records remain at the destination that accepted them.
7. A destination may have accepted a record even when its acknowledgment is lost. Replays and destination changes can therefore produce duplicates. Sample IDs, gap IDs, and gap revisions provide stable identities for deduplication.
8. PostgreSQL/TimescaleDB stores production samples and gaps in relational tables. InfluxDB stores them in measurements. With MQTT, an external consumer owns processing, historical storage, retention, and downstream health.

The spool limit is measured in bytes. Outage coverage depends on sample volume, compression, WAL growth, and available capacity. If capture reads data that cannot be committed because the spool is full, acquisition stops and records a gap when possible. A process failure before local commit can lose the in-flight hardware read.

Destination writes and retries run in a separate writer thread, so network delays do not make the DAQ read loop wait for the database or broker. Local SQLite commits still run in the acquisition process before its next read. This reduces coupling to destination availability; it is not a hard real-time guarantee against local disk latency, CPU contention, or DAQ/driver delays. See the [threading model](architecture.md#production-threading-model).

The legacy `daq_telemetry` schema is separate from production records; its presence does not prove production delivery.
