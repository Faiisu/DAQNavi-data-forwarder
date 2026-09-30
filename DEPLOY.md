# Main deployment and operations

The main Docker Compose project deploys DAQNavi with TimescaleDB, InfluxDB, and a TLS MQTT broker on one network. Each service has a persistent volume. Linux is the supported host for physical DAQ acquisition. For the isolated full-system E2E deployment, see [deploy/e2e](deploy/e2e/README.md).

---

## 1. Prerequisites

<a id="prepare-the-linux-host"></a>

Before starting, ensure the host meets the following requirements:

1. **Docker Engine & Compose:**
   - Install Docker Engine and the [Docker Compose plugin](https://docs.docker.com/compose/install/linux/).
   - Ensure the daemon is running:
     ```bash
     docker --version
     docker compose version
     ```
2. **Python 3 and OpenSSL:** Required on the host to create service secrets and the MQTT TLS certificate.
3. **Physical DAQ Card:**
   - Connect and power the Advantech DAQ card on the Linux host.
   - Install the vendor driver & SDK ([Advantech DAQNavi Driver for Linux](https://www.advantech.com/en-sg/support/details/driver?id=1-LXHFQJ)) matching your kernel (`uname -r`) and architecture (`uname -m`).

---

## 2. Deployment Steps

Follow these steps from the repository root:

### Step 1: Configure Environment (.env)

Create and secure your local `.env` configuration:

```bash
cp .env.example .env
sed -i "s/^DAQ_SESSION_KEY=.*/DAQ_SESSION_KEY=$(openssl rand -hex 32)/" .env
chmod 600 .env
python3 scripts/init-main-destinations.py
```

The script fills blank destination secrets in `.env` and creates `config/mqtt/server.crt` and `server.key`. Keep these files for later restarts and backups. Existing nonempty secrets and certificates are preserved.

The closed deployment uses `ALLOWED_ORIGINS=*`, so operators can open DAQNavi through any host address on that network. Authentication still applies. This setting accepts every browser Origin header; if the service later becomes reachable from a less trusted network, set a comma-separated list of the exact browser origins instead.

### Step 2: Build and Start the Stack

The Docker image includes Python 3.12 and installs the pinned, hash-verified packages from `requirements.lock` during the build.

Start DAQNavi and the three destination services in the background:

```bash
docker compose up -d --build
docker compose ps
```

Verify service health:
```bash
curl http://localhost:8081/api/health
```
*(Should return JSON response containing `"service":"daq_navi"`)*.

### Step 3: First-time Login and Configuration

1. Open your browser and navigate to:
   ```
   http://<host-ip>:8081
   ```
2. Sign in with the starter credentials:
   - **Username:** `admin`
   - **Password:** `00000000`
   > [!IMPORTANT]
   > Change the password immediately in the web interface upon first login.

3. In **Config Center**:
   - Configure the physical device, channel span, signal mode, and input range.
   - Select one bundled destination. A new private config already contains the connection values and generated secrets listed below. Existing saved configs keep their current settings.
   - Test destination connectivity, save any changes, and start acquisition.
   *(Settings will be saved to `config/config.json` automatically)*.

| Destination | Config Center connection |
| --- | --- |
| PostgreSQL/TimescaleDB | Host `timescaledb`, port `5432`, database/user from `DAQ_DB_NAME`/`DAQ_DB_USER`, password from `DAQ_DB_PASSWORD` |
| InfluxDB | URL `http://influxdb:8086`, organization/bucket from `DAQ_INFLUX_ORG`/`DAQ_INFLUX_BUCKET`, token from `DAQ_INFLUX_TOKEN` |
| MQTT | Broker `mqtt`, port `8883`, username/password from `DAQ_MQTT_USER`/`DAQ_MQTT_PASSWORD`, TLS enabled, CA certificate `/main-mqtt/server.crt` |

InfluxDB's host UI is available on host port `8086` by default, including from other machines that can reach the host. The MQTT listener uses host port `8883` and binds to localhost by default; DAQNavi uses the internal service names above.

---

## 3. Operations and Maintenance

### View Logs
```bash
docker compose logs -f daq-navi
```

### Stop / Restart Service
```bash
# Stop the stack (preserves all named volumes)
docker compose down

# Start service
docker compose up -d
```

### Updates
To pull updates and rebuild without data loss:
```bash
git pull
docker compose up -d --build
```
> [!NOTE]
> Do NOT use `docker compose down -v` unless you intend to wipe the persistent SQLite spool buffer.

### Backups
Back up the following:
- Configuration: `config/config.json`
- Environment and MQTT certificate: `.env`, `config/mqtt/`
- Data volumes: `daq-navi_daq_spool`, `daq-navi_pg_data`, `daq-navi_influx_data`, `daq-navi_influx_config`, and `daq-navi_mqtt_data`

---

## 4. Debugging and Troubleshooting

This section contains diagnostic steps and technical details for verifying hardware, drivers, and container bindings.

### Host DAQ Driver Verification
If using a physical DAQ card, verify that the Linux host kernel recognized the card and loaded the BioDAQ library:

```bash
# Check device node
ls -l /dev/daq*

# Check vendor shared libraries
find /usr/lib /opt/advantech -name 'libbiodaq.so*' -print 2>/dev/null
```
If `/dev/daq*` is missing, ensure the kernel module is loaded and the board is firmly connected.

### Container Driver Pass-Through Verification
`compose.yml` runs the container with `privileged: true` and automatically bind-mounts `/dev`, `/usr/lib`, `/opt/advantech`, `/etc/biobdaq`, and `/var/lib/daq` into the container, with `LD_LIBRARY_PATH` configured. You do not need to configure mounts manually.

To verify that the container can see the host's DAQ driver and device nodes:

```bash
docker compose exec daq-navi sh -lc "find /dev -maxdepth 1 -name 'daq*' -print; find /usr/lib /opt/advantech -name 'libbiodaq.so*' -print 2>/dev/null"
```
- **Expected:** Both the `/dev/daq*` node and `libbiodaq.so` path are listed.
- **Troubleshooting:** If either is missing inside the container, verify that they exist on the host first, and ensure the host paths match the volumes in `compose.yml`.

### Destination Connectivity
- If your database or broker is hosted directly on the Docker host machine (outside Docker), use `host.docker.internal` or the host's LAN IP as the destination host rather than `localhost`.
- For PostgreSQL/TimescaleDB, ensure `pg_hba.conf` allows connections from the Docker subnet (or host gateway).

### Recovery and Spool Diagnostics
- DAQNavi writes acquired samples to a persistent local SQLite spool (`daq_spool` volume) before attempting delivery to the destination.
- If the destination is unreachable, acquisition continues spooled locally. Once connectivity recovers, pending batches are delivered in order.
