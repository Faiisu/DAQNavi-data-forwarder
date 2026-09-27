# Configuration reference

## Where settings come from

`compose.yml` reads `.env` for the web port, operator identity, session key, and origin policy. It sets `DAQ_CONFIG_PATH=/app/config/config.json`; on first start, `entrypoint.sh` copies the tracked `config.json` template into the writable `config/` directory. Config Center then saves changes to that private file. It also persists a changed operator password in the spool, which takes precedence over `DAQ_OPERATOR_HASH`.

The exact defaults for acquisition and destinations are in [config.json](../../config.json). The typed loader and environment override behavior are in [core/config_loader.py](../../core/config_loader.py); Config Center validation and saves are in [web/app.py](../../web/app.py). This page points to those sources rather than copying every default into a second list.

## Initial operator login

On a fresh installation, the default username is `admin` and the default password is `00000000`. Change the password after first login. Config Center saves a changed password hash in the persistent spool; that hash takes precedence over `DAQ_OPERATOR_HASH`, so an existing installation may use a different password.

## Setting groups

| Group | Important keys | Why it matters |
| --- | --- | --- |
| Device and sampling | `DEVICE_DESCRIPTION`, `DEVICE_ID`, `START_CHANNEL`, `CHANNEL_COUNT`, `CLOCK_RATE`, `SECTION_LENGTH` | Selects the physical input span and capture rate. Match these to the device and wiring. |
| Channel interpretation | `CHANNELS.*.enabled`, `label`, `unit`, `signal_type`, `value_range`, `scale` | Converts voltage to engineering values; production requires calibration on enabled channels. |
| Startup | `AUTO_START_ON_STARTUP` | Controls whether physical acquisition begins after service startup. An older saved mockup mode disables auto-start until the operator saves the updated configuration. |
| Delivery | `DESTINATION`, `DB_*`, `INFLUX_*`, `MQTT_*` | Selects one reachable external target. The Compose project does not create it. |
| Local durability | `SPOOL_DIR`, `SPOOL_MAX_BYTES` | Bounds local pending data. Config Center does not support changing `SPOOL_DIR` after data exists. |

## Database connection mode

Config Center supports `fields` and `dsn` modes for PostgreSQL. In `fields` mode it builds `DB_DSN` from host, port, name, username, and password when saving. In `dsn` mode the saved connection string is authoritative. Older config files without `DB_CONNECTION_MODE` are classified by `core/config_loader.py` according to whether the saved DSN matches the individual fields.

For a database running on the Docker host, `host.docker.internal` is mapped by Compose. For a database in another Docker project, use a hostname or address that the DAQNavi container can actually resolve and reach. `localhost` inside the container refers to that container.

## Private values

Keep `.env` and `config/config.json` out of Git. The tracked template contains example endpoint and credential values; replace them before using a destination. Store MQTT CA/client certificate files at paths that are readable inside the container if those options are used.
