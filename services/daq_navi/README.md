# DAQNavi service

DAQNavi provides operator login, hardware and channel configuration, acquisition control, a durable production spool, and writers for PostgreSQL/TimescaleDB, InfluxDB 2.x, and production MQTT.

## Deployment

Use the independent deployment project at [`deploy/daq-navi/`](../../deploy/daq-navi/) and follow the [Linux deployment guide](../../DEPLOY_LINUX.md). The service mounts the host DAQNavi/BioDAQ libraries and the private configuration directory. Physical capture requires the supported vendor driver and DAQ device on the host.

See the [Linux deployment guide](../../DEPLOY_LINUX.md#first-operator-login) for initial login and password rotation.

## Production delivery

Production acquisition writes physical samples and acquisition gaps to a persistent SQLite spool before sending them to the selected destination. Pending records follow the latest saved configuration. Database destinations provide queryable history; MQTT history belongs to an external consumer. Mockup acquisition writes to database destinations and does not use MQTT. See the [production data flow](../../docs/architecture/data-flow.md) and [MQTT v1 contract](../../docs/contracts/production-mqtt-contract-v1.md) for the authoritative delivery details.

See the [DAQNavi Config Center guide](web/README.md), [test and qualification guide](tests/README.md), and [MQTT v1 contract](../../docs/contracts/production-mqtt-contract-v1.md).
