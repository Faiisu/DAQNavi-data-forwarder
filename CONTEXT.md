# Industrial data acquisition

This context defines the terms used for physical sensor acquisition and the resulting telemetry.

## Language

**Production acquisition**: A run that measures physical sensor values through a DAQ card for operational records. _Avoid_: real mode.

**Mockup acquisition**: A run that generates synthetic measurements for demonstration or testing. _Avoid_: fallback data.

**DAQ sample**: One measurement from one physical input channel at a particular sampling instant.

**Raw voltage**: The electrical value measured at a DAQ input before sensor calibration.

**Calibrated measurement**: A DAQ sample converted from raw voltage into the engineering unit assigned to that channel.

**Acquisition gap**: An interval during which production acquisition could not record physical samples. A gap represents missing data, not synthetic values.

**Production destination**: The one receiver selected for a production run: PostgreSQL/TimescaleDB, InfluxDB, or an authenticated external MQTT broker.

**Spool record**: A committed sample batch or acquisition-gap event waiting for delivery to the currently selected production destination.

**Acknowledged record**: A record the active destination has completed according to its delivery contract. Acknowledged records remain at the destination that accepted them.

**External consumer**: A service that subscribes to production MQTT topics and owns downstream processing, historical storage, retention, and its own health reporting.
