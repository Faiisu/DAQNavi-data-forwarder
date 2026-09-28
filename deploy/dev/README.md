# Local development stack

This Compose project runs DAQNavi with TimescaleDB, InfluxDB, and a TLS MQTT broker on one Docker network. DAQNavi reaches them by the service names `pg`, `influx`, and `mqtt`. The credentials and certificate in this directory are for local development only.

From the repository root, start the stack with:

```bash
docker compose -f deploy/dev/compose.yml up -d --build
docker compose -f deploy/dev/compose.yml ps
```

Open `http://localhost:18082` (or set `DAQ_DEV_PORT` before starting). Sign in with `admin` / `00000000` and change the password for any shared development host. Physical acquisition still requires the supported DAQ hardware and host driver; the web UI can start without them.

The first start may take several minutes while TimescaleDB and InfluxDB initialize their volumes. Compose waits for both health checks before starting DAQNavi.

On first start, the entrypoint copies `config.template.json` to the ignored `config/config.json`. The template selects TimescaleDB by default. Config Center can switch to InfluxDB or MQTT using the already configured service names and local credentials. Saved settings and the SQLite spool persist across `docker compose down`.

Stop the stack without deleting its data:

```bash
docker compose -f deploy/dev/compose.yml down
```

This stack publishes only the web UI port. Database and broker ports are available to containers on its Docker network.
