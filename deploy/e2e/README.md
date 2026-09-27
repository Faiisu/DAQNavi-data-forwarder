# E2E qualification stack

This Compose project provides the isolated services expected by `tests/test_e2e_full_system.py`: DAQNavi, a disposable TimescaleDB database, InfluxDB, and a TLS MQTT broker. The DAQNavi configuration and spool use this project's own bind mount and named volume.

On first start, DAQNavi copies the repository's `config.json` template to `deploy/e2e/config/config.json`. The E2E test changes this local file; it is ignored by Git.

The E2E test drives physical DAQ hardware, writes and drops unique tables in `daq_navi_test_local`, changes the Config Center configuration, and starts and stops acquisition. Use this stack only on a qualification host with a supported DAQ and do not connect it to production destinations.

## Start and run

Run these commands from `deploy/e2e`. The standalone deployment also publishes port 8081, so stop it first while preserving its spool. The `-f` option points at the standalone Compose file; the remaining commands use this directory's E2E Compose file.

```bash
docker compose -f ../../compose.yml down
docker compose up -d --build daq-navi-e2e pg influx mqtt
docker compose exec -T daq-navi-e2e sh /run-e2e.sh
```

The runner script installs the pinned, hash-verified dependencies referenced by `requirements-test.txt`, prepares the default TimescaleDB production table used by the configuration switch check, executes the full-system E2E script in the isolated DAQNavi container, and copies its report to `reports/e2e_report.json`, including when test checks fail.

The E2E script exits nonzero if any check fails. The Compose stack remains running after the script exits; inspect it with `docker compose ps` and `docker compose logs daq-navi-e2e`.

## Stop and reset

Stop the qualification services while preserving their test data:

```bash
docker compose down
```

To reset only this disposable stack's database, InfluxDB data, MQTT state, and spool, remove its named volumes:

```bash
docker compose down -v
```

Do not run that reset command for a deployment containing data that must be kept. The certificates and credentials in this folder are test-only values.
