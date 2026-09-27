# Linux deployment and operations

Linux is the supported deployment host. Physical DAQ use requires a supported Advantech DAQNavi/BioDAQ driver, SDK libraries, and card installed on the host. Docker does not provide the vendor driver.

## Prepare

Install Docker Engine and the Compose plugin, then create private environment and DAQ configuration files:

```bash
cp .env.example .env
cp deploy/daq-navi/.env.example deploy/daq-navi/.env
cp deploy/portal/.env.example deploy/portal/.env
mkdir -p deploy/daq-navi/config
cp services/daq_navi/config.json deploy/daq-navi/config/config.json
```

Replace example infrastructure credentials in the root `.env`. Review `deploy/daq-navi/config/config.json` for the physical device, channel span, signal type, input range, calibration, destination, and startup settings. For PostgreSQL/TimescaleDB use host `timescaledb` on the Compose network and a DSN matching the root `.env`. For InfluxDB use host `influxdb` and its configured organization, bucket, and token. MQTT connects to an external broker and requires a host, port, username, password, verified TLS, and a topic prefix; the root Compose project does not run a broker. Keep all private settings out of Git.

Replace the example `DAQ_SESSION_KEY` in `deploy/daq-navi/.env` with a private random value and restrict the file's permissions. The shipped operator hash is a starter credential documented below; rotate the password after first login.

The DAQNavi config directory is bind-mounted at `/app/config`. Atomic config saves replace `config.json` in that directory. The production SQLite spool is a separate persistent Docker volume named `iiot-data-ingest_daq_spool`.

## First operator login

On a fresh deployment using `deploy/daq-navi/.env.example`, sign in with username `admin` and password `00000000`. Change the password from the operator menu immediately. The new password must contain at least 8 characters. Keep the service on a trusted network while the default is active. A hash saved in the spool takes precedence over environment settings, so an existing installation can have a different password.

## Start the stack

Start infrastructure, DAQNavi, and Portal separately:

```bash
docker compose -f docker-compose.yml up -d
docker volume create iiot-data-ingest_daq_spool
docker compose --env-file deploy/daq-navi/.env -f deploy/daq-navi/compose.yml up -d --build
docker compose --env-file deploy/portal/.env -f deploy/portal/compose.yml up -d
docker compose -f docker-compose.yml ps
docker compose --env-file deploy/daq-navi/.env -f deploy/daq-navi/compose.yml ps
docker compose --env-file deploy/portal/.env -f deploy/portal/compose.yml ps
curl http://localhost:8081/api/health
```

Open the Portal at `http://<host>:8080` and DAQNavi at `http://<host>:8081`. DAQNavi `/api/health` is public and minimal; the Config Center and other DAQNavi APIs require an operator session. Read detailed runtime state in the Config Center after login. `/api/status` and `/api/samples` return `401` without a session.

Before starting physical acquisition, scan for the DAQ card, check wiring and channel settings, test the selected destination, save configuration, and explicitly start production acquisition unless the saved auto-start mode is intended. A successful destination test checks connectivity; it does not verify data delivery. Confirm current samples in the selected database or external MQTT consumer.

To follow service logs or inspect lifecycle state:

```bash
docker compose --env-file deploy/daq-navi/.env -f deploy/daq-navi/compose.yml logs -f daq-navi
docker compose --env-file deploy/daq-navi/.env -f deploy/daq-navi/compose.yml ps
```

DAQNavi and Portal have independent Compose projects. Stopping either does not stop the infrastructure project. Do not use `docker compose down -v` if database or spool data must be retained.

## Destination behavior

The [production data flow](docs/architecture/data-flow.md) is authoritative for spool delivery and destination changes. See the [MQTT JSON v1 contract](docs/contracts/production-mqtt-contract-v1.md) for MQTT-specific configuration and consumer behavior.

## Existing installations

For an existing combined-stack installation, stop acquisition through the Config Center, inspect pending batches, preserve the spool volume, and migrate the private config before starting the independent DAQNavi project. On hosts where the old config was a file mount:

```bash
mkdir -p deploy/daq-navi/config
cp -a deploy/daq-navi/config.local.json deploy/daq-navi/config/config.json
chmod 600 deploy/daq-navi/config/config.json
cmp deploy/daq-navi/config.local.json deploy/daq-navi/config/config.json
docker volume inspect iiot-data-ingest_daq_spool
```

Review the current Compose files and config before cutover. Recreate DAQNavi and Portal from their own Compose projects. Do not remove or recreate the spool volume during migration. Check the DAQNavi health endpoint, sign in, inspect acquisition and pending-spool state, and verify samples at the configured destination before retiring previous config copies.

If the former bundled MQTT broker still exists, remove its container after confirming that no remaining service depends on it:

```bash
docker rm -f daq_mosquitto
```

This removes the legacy broker container only. It does not delete Docker volumes.

## Network and authentication

Default ports are Portal `8080`, DAQNavi `8081`, PostgreSQL/TimescaleDB `5432`, and InfluxDB `8086`. Database ports are published by default; restrict them with host firewall or network rules when not needed externally. There is no local MQTT broker port.

DAQNavi protects operator pages, configuration, and control APIs with operator sessions. `/api/health` is intentionally public and minimal. Portal and MUSASHI II/IV interfaces do not include built-in authentication. Keep them on trusted networks or add access control before wider exposure. Use HTTPS through a trusted reverse proxy when credentials or operator sessions cross an untrusted network.

## Hardware reference

The DAQNavi container mounts host device files and vendor library directories. Consult [PCI-1716 project notes](docs/hardware/PCI-1716.md) and the linked official manual before wiring or selecting signal modes. Obtain current SDK, driver, and library packages from [Advantech Support](https://www.advantech.com/emt/support/details/driver?id=1-LXHFQJ); see the [provenance inventory](docs/third-party-provenance.md).
