# Current configuration routes pending production records

Status: accepted

Every production sample and acquisition gap still pending in the local spool is sent using the latest saved destination configuration, including its MQTT QoS when MQTT is selected. Saving a new destination can redirect pending records across PostgreSQL/TimescaleDB, InfluxDB, and MQTT without draining or consulting the previous destination. Records already acknowledged remain where they were delivered; the system records the configuration change and time so operators can find the history split. An old destination may have accepted a record whose acknowledgment was lost, so the same stable ID can appear at both targets, and an old consumer can retain an open gap. This trade-off favors immediate recovery through the currently configured target over preserving destination ownership for queued records.
