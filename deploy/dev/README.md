# Local development stack

This Compose project runs DAQNavi with TimescaleDB, InfluxDB, and a TLS MQTT broker on one Docker network. DAQNavi reaches them by the service names `pg`, `influx`, and `mqtt`. The credentials and certificate in this directory are for local development only. InfluxDB's initial account is `admin` / `adminpassword123`; its API token for DAQNavi and Grafana is `local-dev-token`. These account settings apply when InfluxDB initializes an empty volume; an existing volume keeps its current account.

From the repository root, start the stack with:

```bash
docker compose -f deploy/dev/compose.yml up -d --build
docker compose -f deploy/dev/compose.yml ps
```

Open `http://localhost:18082` (or set `DAQ_DEV_PORT` before starting). Sign in with `admin` / `00000000` and change the password for any shared development host. Physical acquisition still requires the supported DAQ hardware and host driver; the web UI can start without them.

Grafana is available at `http://localhost:3000` (or set `GRAFANA_DEV_PORT`). Sign in with `admin` / `admin`. It provisions the `DAQNavi` dashboard and TimescaleDB as the default data source, with InfluxDB available as a second source. The dashboard reads production samples and acquisition gaps; DAQNavi creates those tables when it first delivers production data, so panels have no data until production acquisition runs. Change the Grafana password before sharing a development host.

The first start may take several minutes while TimescaleDB and InfluxDB initialize their volumes. Compose waits for both health checks before starting DAQNavi.

On first start, the entrypoint copies `config.template.json` to the ignored `config/config.json`. The template selects InfluxDB by default with the local API token already configured. Config Center can switch to TimescaleDB or MQTT using the already configured service names and local credentials. Saved settings and the SQLite spool persist across `docker compose down`.

Stop the stack without deleting its data:

```bash
docker compose -f deploy/dev/compose.yml down
```

This stack publishes the web UI and InfluxDB on host ports `18082` and `18086` by default. Other database and broker ports are available to containers on its Docker network.
