# Record each production sample at most once

Each DAQ sample must keep a stable identity through local buffering and retry so that an uncertain database response or application replay cannot create duplicate production records. Production writes to TimescaleDB must be idempotent. This adds identity and index costs but makes recovery safe and keeps sample counts meaningful.
