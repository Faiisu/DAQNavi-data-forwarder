# IIoT-data-ingestion

IIoT-data-ingestion is the umbrella name for the industrial data ingestion services in this repository. The infrastructure, DAQ Navi, and Portal have separate Compose projects. MUSASHI II and MUSASHI IV are separate services and are not started by these Compose files.

## Deploy on Linux

### 1. Prepare the host and configuration

Use a Linux host with Docker Engine and the Docker Compose plugin. Physical DAQ acquisition also needs a supported Advantech DAQNavi/BioDAQ driver, SDK, and device installed on that host. The DAQ container mounts host device files and vendor libraries; Windows deployment is unsupported.

```bash
cp .env.example .env
cp deploy/daq-navi/.env.example deploy/daq-navi/.env
cp deploy/portal/.env.example deploy/portal/.env
mkdir -p deploy/daq-navi/config
cp services/daq_navi/config.json deploy/daq-navi/config/config.json
# Edit .env for infrastructure credentials, and the two service files for host ports.
```

Review the private `deploy/daq-navi/config/config.json` or open the Config Center before collecting data. [The checked-in DAQ JSON](services/daq_navi/config.json) is a starting example; its device, channel, calibration, and destination values are machine-specific. Keep destination credentials in the private config consistent with those in `.env`. The service may begin acquisition when it starts if `AUTO_START_ON_STARTUP` is enabled.

If the TimescaleDB volume already exists, changing `POSTGRES_PASSWORD` in `.env` alone does not change the password of the existing database user. Update that user in PostgreSQL and the DAQ destination configuration together.

### 2. Start the services

Start the infrastructure first. DAQ Navi joins its Docker network and reuses the existing `iiot-data-ingest_daq_spool` volume. On a new host, create that volume once before starting DAQ. Existing combined-stack installations must follow the [cutover steps](DEPLOY_LINUX.md#cutover-from-the-former-combined-stack) before starting independent DAQ and Portal containers. The saved DAQ configuration must use `timescaledb` as its database host when writing to this infrastructure project.

```bash
docker compose -f docker-compose.yml up -d
docker volume create iiot-data-ingest_daq_spool
docker compose --env-file deploy/daq-navi/.env -f deploy/daq-navi/compose.yml up -d --build
docker compose --env-file deploy/portal/.env -f deploy/portal/compose.yml up -d
docker compose --env-file deploy/daq-navi/.env -f deploy/daq-navi/compose.yml ps
curl http://localhost:8081/api/health
```

For local DAQ source mounts and Flask debugging, add the DAQ development override:

```bash
docker compose --env-file deploy/daq-navi/.env -f deploy/daq-navi/compose.yml -f deploy/daq-navi/compose.dev.yml up -d --build
```

Open the Portal at `http://<linux-host>:8080` and the DAQ Config Center at `http://<linux-host>:8081`. The DAQ REST API uses port 8081 as well.

| Compose project / service | Default host port | Role |
|:---|---:|:---|
| `deploy/portal/compose.yml` / `portal` | 8080 | Service links and browser-side status polling. |
| `deploy/daq-navi/compose.yml` / `daq-navi` | 8081 | DAQ Config Center, API, and acquisition control. |
| `docker-compose.yml` / `timescaledb` | 5432 | PostgreSQL/TimescaleDB destination. |
| `docker-compose.yml` / `mqtt-broker` | 1883 | Mosquitto for configured MQTT paths. |
| `docker-compose.yml` / `influxdb` | 8086 | InfluxDB for configured InfluxDB paths. |

The Portal also links to MUSASHI II on port 8082 and MUSASHI IV on port 8083. Those services need separate deployment. Set `PORTAL_PORT` in `deploy/portal/.env`, `DAQ_PORT` in `deploy/daq-navi/.env`, and infrastructure ports in the root `.env`. Portal link and polling ports are owned by `services/portal/config.json`; update its `daq` entry if the DAQ host port changes.

### 3. Start acquisition and check delivery

For physical acquisition, confirm the DAQ card is visible on the Linux host. In the Config Center, scan for the device, check wiring and channel settings, test the destination connection, save, then start **production** acquisition. If saved auto-start is enabled, check the running mode before making changes. A successful connection test checks connectivity only.

```bash
curl http://localhost:8081/api/status
curl 'http://localhost:8081/api/samples?channel=0'
docker compose --env-file deploy/daq-navi/.env -f deploy/daq-navi/compose.yml logs -f daq-navi
```

Use an enabled channel number in the sample URL. In the status response, inspect `status`, `healthy`, `writer_error`, `pending_batches`, and `last_sample_ns`. A queue can grow briefly while the writer catches up; a persistent increase needs investigation. Confirm that recent samples reach the intended table. An API health response alone does not establish end-to-end delivery.

The DAQ Config Center and its control APIs provide operator authentication when configured. The Portal, MUSASHI II, and MUSASHI IV web interfaces have no built-in authentication. Limit access to a trusted network or add access control before wider exposure. [Linux deployment](DEPLOY_LINUX.md) describes host mounts, port exposure, lifecycle commands, and cutover from the former combined stack. Avoid `docker compose down -v` when preserving database or spool data.

## Architecture

### Service boundaries

```mermaid
flowchart LR
    Browser[Operator browser] --> Portal[Portal :8080]
    Portal -->|links and status| Web[DAQ Config Center and API :8081]
    Web -->|start, stop, configure| Production[Production acquisition process]
    Web -->|start, stop, configure| Mockup[Mockup acquisition process]
    Production --> SDK[Advantech BioDAQ on Linux host]
    SDK --> Production
    Production --> Spool[(Persistent SQLite spool)]
    Spool --> Writer[Retrying writer]
    Writer -->|configured destination| PG[(TimescaleDB / PostgreSQL)]
    Writer -->|configured destination| Influx[InfluxDB]
    Mockup -. synthetic samples to configured destination .-> PG
    Mockup -. synthetic samples to configured destination .-> MQTT[Mosquitto]
    Mockup -. synthetic samples to configured destination .-> Influx
    MusashiII[MUSASHI II service] -. configured destination .-> Storage[(PostgreSQL, InfluxDB, or SQLite)]
    MusashiIV[MUSASHI IV service] -. configured destination .-> Storage
```

| Component | Responsibility | Location |
|:---|:---|:---|
| Portal | Static service directory. It links to consoles and polls their status; it does not ingest telemetry. | `services/portal/`, Nginx container |
| DAQ Config Center and API | Save and validate configuration, scan devices, test destinations, control acquisition, and report status. | `services/daq_navi/web/`, `daq-navi` container |
| DAQ acquisition | Physical capture or synthetic mockup generation, launched as a child process by the DAQ web service. | `services/daq_navi/core/` |
| TimescaleDB | Production DAQ sample and gap storage (when selected), plus legacy/bootstrap tables. | `timescaledb` container, `tsdb_data` volume |
| InfluxDB | Production DAQ destination (when selected) or mockup destination. | `influxdb` container |
| Mosquitto | Message broker for configured mockup paths; production acquisition does not publish to MQTT. | `mqtt-broker` container |
| MUSASHI II | Polls a dispenser over serial/RS-232C and writes via its configured PostgreSQL, InfluxDB, or SQLite handler. | `services/musashi_ii/`; separate runtime |
| MUSASHI IV | Polls a dispenser HTTP API and writes via its configured PostgreSQL, InfluxDB, or SQLite destination; supports mock API data. | `services/musashi_iv/`; separate runtime |

The MUSASHI services have their own configuration and process controls. Their Portal links do not start them or connect them to the DAQ pipeline.

### DAQ control and configuration

The DAQ web service reads and writes the private `deploy/daq-navi/config/config.json`, mounted through `/app/config` in the container so saves use atomic replacement. Start it from [the checked-in DAQ example](services/daq_navi/config.json). The Config Center exposes the hardware span, sensor channel table, per-channel calibration, destination, startup behavior, and runtime status. It shows saved secrets as unchanged placeholders; blank keeps them and an explicit clear action removes optional values. PostgreSQL connection mode is explicit: fields mode follows host/user controls, while custom DSN mode uses the connection string. Saves carry a revision to reject stale browser tabs. A failed destination transition attempts to resume the previous run; a committed config whose run cannot start is reported as saved with acquisition stopped. Timescale retention changes are checked against policy readback and compensated on save failure. Saving during acquisition can stop and restart the run after confirmation in the UI.

`START_CHANNEL` and `CHANNEL_COUNT` select a contiguous hardware input span. `CHANNELS` contains each input's enabled state, label, unit, signal type, voltage range, and linear calibration. For PCI-1716 differential inputs, an even channel starts a pair and reserves the following odd channel. The physical wiring, input mode, and range must agree.

| Setting | Meaning |
|:---|:---|
| `DEVICE_DESCRIPTION`, `DEVICE_ID`, `PROFILE_PATH` | Driver device selection, stored device identifier, and optional DAQNavi profile. |
| `START_CHANNEL`, `CHANNEL_COUNT`, `CHANNELS` | Hardware span and sensor settings. |
| `CLOCK_RATE`, `SECTION_LENGTH`, `SECTION_COUNT` | Samples per second per channel, samples per channel per read section, and section count. Continuous production capture requires `SECTION_COUNT=0`. |
| `DESTINATION`, `DB_*`, `INFLUX_*`, `MQTT_*` | Destination and connection settings. Production mode supports PostgreSQL/TimescaleDB or InfluxDB 2.x. |
| `SPOOL_DIR`, `SPOOL_MAX_BYTES` | Persistent production queue and its configured byte limit. |
| `AUTO_START_ON_STARTUP`, `AUTO_START_MODE` | Whether service startup begins acquisition and which mode to use. |

The root Compose `.env` configures infrastructure credentials and ports. `deploy/daq-navi/.env` and `deploy/portal/.env` configure their host ports. The private DAQ JSON remains authoritative for acquisition settings and destination credentials. Keep it out of Git. See the [Config Center workflow](services/daq_navi/web/README.md) for channel editing and calibration.

### Production data path

1. The acquisition process opens the configured Advantech device through BioDAQ and reads waveform sections from the selected input span.
2. It timestamps enabled channel samples, retains raw voltage, and applies each channel's calibration to produce an engineering-unit value.
3. It compresses each batch and commits it to a persistent SQLite spool in the `daq_spool` volume. A committed batch is the local recovery boundary.
4. A writer sends pending batches to the configured destination (PostgreSQL/TimescaleDB or InfluxDB 2.x), acknowledges them after successful writes, and retries unacknowledged batches. Stable sample IDs make replay idempotent.
5. Interruptions are recorded in `daq_production_gaps` (or the `daq_acquisition_gaps` measurement in InfluxDB). For PostgreSQL/TimescaleDB, the production table, normally `daq_production_samples`, stores raw and calibrated values, channel and sensor metadata, session ID, and provenance, with an active retention policy. For InfluxDB, samples are written via line protocol at nanosecond precision with bucket-managed retention.

The spool limit bounds bytes, not outage duration. A process crash can lose a hardware section that has not yet been committed to the spool. The production writer does not publish to MQTT. See [production records](docs/architecture/erd.md), [data flow](docs/architecture/data-flow.md), and [acquisition sequence](docs/architecture/sequences/streaming_pipeline.md).

### Mockup and other storage paths

DAQ mockup acquisition generates synthetic values and uses its configured destination. Its PostgreSQL table is configured separately from the production table through `DB_MOCKUP_TABLE` and `DB_PRODUCTION_TABLE`. The older `daq_telemetry` table created by [`scripts/sql/db_setup.sql`](scripts/sql/db_setup.sql) is separate from production samples; reading it does not verify physical ingestion. MUSASHI II and IV use their own configuration files and storage schemas.

### Repository map

| Path | Contents |
|:---|:---|
| `docker-compose.yml`, `deploy/daq-navi/`, `deploy/portal/` | Infrastructure and independent DAQ/Portal Compose projects. |
| `services/portal/` | Static Portal frontend and its service-link configuration. |
| `docs/hardware/` | Project hardware notes that link to Advantech's official manuals. Obtain DAQNavi software and libraries from [Advantech Support](https://www.advantech.com/emt/support/details/driver?id=1-LXHFQJ); see the [third-party provenance inventory](docs/third-party-provenance.md). |
| `services/daq_navi/web/` | Config Center, REST API, and process control. |
| `services/daq_navi/core/` | DAQ configuration loading, physical capture, spool, writer, and mockup acquisition. |
| `services/musashi_ii/`, `services/musashi_iv/` | Independent dispenser ingestion services. |
| `config/` | Mosquitto configuration. |
| `scripts/sql/` | Legacy/bootstrap database schema. |
| `deploy/linux/`, `scripts/setup_wizard.sh` | Optional Linux host setup helpers. |
| `CONTEXT.md`, `docs/adr/`, `docs/architecture/` | Domain language, decisions, and detailed diagrams. |
| `docs/hardware/` | Hardware specifications and wiring references, including [PCI-1716](docs/hardware/PCI-1716.md). |

## DAQ API and further reading

| Endpoint | Method | Use |
|:---|:---:|:---|
| `/api/health`, `/api/status` | GET | Health, mode, fault, queue, and recent gap state. |
| `/api/config` | GET/POST | Read or save validated DAQ configuration. |
| `/api/scan_usb` | GET | Discover DAQ devices visible to the service. |
| `/api/test_destination` | POST | Test destination connectivity; `/api/test_db` is an alias. |
| `/api/start`, `/api/stop` | POST | Control acquisition. |
| `/api/samples?channel=N` | GET | Query recent production samples for a channel. |
| `/api/retention` | GET | Inspect the production retention policy. |

Read [Linux deployment](DEPLOY_LINUX.md) for operations, [DAQ Config Center](services/daq_navi/web/README.md) for configuration, [domain glossary](CONTEXT.md) for acquisition terms, and [DAQ check guidance](services/daq_navi/tests/README.md) before running checks that need hardware or a database.
