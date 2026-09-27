# DAQNavi documentation

Choose the page by what you need to do:

| Purpose | Start here |
| --- | --- |
| Learn and run the service | [README quickstart](../README.md#quickstart) |
| Perform an operation | [Deploy and operate](../DEPLOY.md), [configure an external destination](guides/configure-destination.md), [use Config Center](../web/README.md), [qualify a destination](guides/qualify-destination.md) |
| Look up a contract or setting | [Configuration reference](reference/configuration.md), [destination reference](reference/destinations.md), [HTTP API routes](reference/api.md), [MQTT v1 contract](production-mqtt-contract-v1.md) |
| Understand design decisions | [Architecture](architecture.md), [production data flow](data-flow.md), [decision records](#decision-records) |

## Source of truth

Runtime settings come from `compose.yml`, `.env.example`, `entrypoint.sh`, `core/config_loader.py`, and `web/app.py`. HTTP routes live in `web/app.py`. The production delivery boundary lives in `core/production_acquisition.py`. Read these files when a reference detail needs to be exact. Update the linked docs in the same change as a behavior, interface, or configuration change.

## Decision records

These records describe DAQNavi product behavior and operational trade-offs. They were reviewed for this standalone checkout; none describes repository-wide monorepo tooling or another service. ADR 0002 is retained as project history and marked superseded because its 24-hour PostgreSQL-only buffer target is not the current capacity guarantee.

| ADR | Decision |
| --- | --- |
| [0001](adr/0001-production-acquisition-control.md) | Production acquisition control and no mockup fallback |
| [0002](adr/0002-bounded-acquisition-buffer.md) | Earlier buffer target; superseded by the byte-bounded SQLite spool |
| [0003](adr/0003-web-managed-acquisition-configuration.md) | Manage supported acquisition settings through Config Center |
| [0004](adr/0004-raw-telemetry-retention.md) | 30-day default production retention, configurable by operators |
| [0005](adr/0005-idempotent-sample-recording.md) | Stable sample identity for retries |
| [0006](adr/0006-startup-follows-saved-configuration.md) | Startup behavior follows saved configuration |
| [0007](adr/0007-local-host-time-authority.md) | DAQNavi host clock as timestamp authority |
| [0008](adr/0008-preserve-raw-and-calibrated-measurements.md) | Preserve raw and calibrated measurements |
| [0009](adr/0009-record-acquisition-gaps.md) | Record acquisition gaps explicitly |
| [0010](adr/0010-replace-legacy-telemetry-at-cutover.md) | Replace ambiguous legacy telemetry at production cutover |
| [0011](adr/0011-retire-calibration-revision.md) | Retire calibration revision metadata |
| [0012](adr/0012-production-mqtt-delivery-boundary.md) | Define MQTT QoS and spool acknowledgment behavior |
| [0013](adr/0013-production-mqtt-consumer-boundary.md) | Assign MQTT history and downstream processing to consumers |
| [0014](adr/0014-current-configuration-routes-pending-samples.md) | Route pending records using current destination configuration |
