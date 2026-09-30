# DAQNavi

DAQNavi lets an operator configure an Advantech DAQ device, collect analog measurements, and deliver them to PostgreSQL/TimescaleDB, InfluxDB 2.x, or MQTT. The main Compose stack includes all three destinations. A persistent SQLite spool holds committed production records until delivery completes.

![Example Config Center screen showing device and sensor settings](image.png)

## Prerequisites

- Linux host with Docker Engine, the Docker Compose plugin, and OpenSSL for generating a session key. The Docker image includes Python 3.12. See [host preparation and installation](DEPLOY.md#prepare-the-linux-host) for install instructions.
- Python 3 on the host to initialize private destination secrets and the MQTT certificate.
- For physical capture: a supported Advantech DAQ card and the DAQNavi/BioDAQ Linux driver and SDK installed on the host. The container receives the host device nodes and driver libraries through Compose; Docker does not install the vendor driver.

## Quickstart

Run from the repository root after cloning this repository. Compose starts DAQNavi and all three destination services with separate persistent volumes.

```bash
cp .env.example .env
sed -i "s/^DAQ_SESSION_KEY=.*/DAQ_SESSION_KEY=$(openssl rand -hex 32)/" .env
chmod 600 .env
python3 scripts/init-main-destinations.py
docker compose up -d --build
curl http://localhost:8081/api/health
```

The health request should return JSON with `"service":"daq_navi"`. Open `http://<host>:8081` and sign in with the default username `admin` and default password `00000000`; change the password immediately. The first start creates private `config/config.json` with the bundled destination addresses and generated credentials. Test the selected destination in Config Center before starting acquisition.

See [deployment and operations](DEPLOY.md) for the physical device checklist, bundled destination addresses, updates, and backups. The [destination guide](docs/guides/configure-destination.md) explains how to select a destination.

## Environment variables

The tracked [.env.example](.env.example) is the source for the values below. Compose also sets `DAQ_CONFIG_PATH` and a persistent operator hash path inside the container.

| Variable | Purpose | Example value |
| --- | --- | --- |
| `DAQ_PORT` | Published host port for the web UI | `8081` |
| `DAQ_OPERATOR_USER` | Initial operator username | `admin` |
| `DAQ_OPERATOR_HASH` | Initial password hash; a hash saved in the spool takes precedence | starter hash |
| `DAQ_SESSION_KEY` | Signs operator sessions; replace before startup | generated secret |
| `ALLOWED_ORIGINS` | Browser origins accepted by the closed deployment | `*` |
| `DAQ_DB_*` | Bundled TimescaleDB name, user, and generated password | `daq_db`, `admin` |
| `DAQ_INFLUX_*` | Bundled InfluxDB initialization, token, and host UI port | `mddp`, `daq_telemetry`, `8086` |
| `DAQ_MQTT_*` | Bundled TLS MQTT account and host port | `daqnavi`, `8883` |

Device, channel, and destination settings are saved in `config/config.json` by Config Center. The tracked [config.json](config.json) is a first-run template; Compose fills its private copy with secrets only when the file is first created. Existing saved settings are preserved on restart. The bundled services use the internal names `timescaledb`, `influxdb`, and `mqtt`. See [configuration reference](docs/reference/configuration.md).

## Testing and linting

Install the project and test dependencies with `uv`, then run the suite as documented in the [test and qualification guide](tests/README.md). For local development with bundled destinations, see [deploy/dev](deploy/dev/README.md). For the isolated full-system E2E deployment, see [deploy/e2e](deploy/e2e/README.md). This checkout has no configured lint command.

## Architecture

```mermaid
flowchart LR
    DAQ[DAQ device] --> Capture[Production acquisition]
    Capture --> Spool[(SQLite spool)]
    Spool --> Writer[Destination writer]
    Writer --> Destination[(Selected database or MQTT broker)]
    Operator[Operator browser] --> Web[Config Center]
    Web --> Capture
```

The spool commits a batch before the writer tries delivery so an outage can leave records pending for replay. See [architecture and tradeoffs](docs/architecture.md), the [production data flow](docs/data-flow.md), and the [documentation index](docs/README.md).
