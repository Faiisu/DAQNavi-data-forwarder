# Retire calibration revision metadata from production records

Calibration revision metadata is no longer required in DAQ production sample records. Newly captured samples, TimescaleDB hypertable records, InfluxDB line-protocol points, and `/api/samples` query responses omit calibration revision. Existing schema is migrated via a versioned, repeatable schema migration (`0001_retire_calibration_revision`) recorded in `daq_schema_migrations` to ensure service startup does not repeat destructive DDL.
