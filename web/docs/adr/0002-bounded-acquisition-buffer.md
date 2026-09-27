# Bound the production spool during destination outages

Status: superseded

This earlier decision targeted a persistent buffer holding at least 24 hours of production data and replaying it to PostgreSQL/TimescaleDB. The standalone service now uses a byte-bounded persistent SQLite spool and supports PostgreSQL/TimescaleDB, InfluxDB, and MQTT destinations. Capacity depends on record volume, compression, WAL growth, and available storage, so the 24-hour minimum is not a current guarantee. See [production data flow](../data-flow.md) for the current recovery boundary.

The original decision required prompt local commits, replay after restart, stopping acquisition rather than silently dropping data when storage filled, and reporting detectable gaps. Those recovery principles remain in effect, subject to the current spool capacity and destination behavior.
