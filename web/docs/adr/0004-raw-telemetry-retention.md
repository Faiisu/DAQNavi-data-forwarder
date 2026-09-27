# Retain raw production telemetry for 30 days by default

Raw production telemetry has a rolling 30-day retention period by default, and operators can change that period through the web UI. The period must be applied to the active TimescaleDB retention policy, not just saved in application configuration. This sets a clear storage target for four channels at 1,000–2,000 samples per second per channel; actual disk use must be measured before deployment.
