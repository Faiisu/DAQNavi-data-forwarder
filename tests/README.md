# Tests and qualification

## Automated suite

Install `uv` using the [official installation guide](https://docs.astral.sh/uv/getting-started/installation/). The test environment uses Python 3.12, matching the application image. From the repository root, create an isolated environment, install the project and test dependencies, then run the suite:

```bash
uv venv --python 3.12
uv pip install --python .venv/bin/python --require-hashes -r requirements-test.txt
. .venv/bin/activate
python -m unittest discover -s tests -v
```

The application Docker image and tests install the exact versions and SHA-256 verified distributions in `requirements.lock`. `requirements.txt` lists direct runtime dependencies and is the input for the lock file; `requirements-test.txt` uses the same lock because the automated suite needs no additional packages.

When intentionally updating dependencies, use `uv` 0.12.17 and Python 3.12 from the repository root, then review the lock-file diff and run this suite:

```bash
uv pip compile requirements.txt --python-version 3.12 --generate-hashes --no-header --output-file requirements.lock
```

The suite exercises the standalone DAQNavi app, production spool and writer, web API, Compose deployment contract, and MQTT wire contract. It does not require a physical DAQ or a live destination for the normal unit run. Database-backed TimescaleDB tests are skipped unless `DAQ_TEST_DB_DSN` names an isolated database beginning with `daq_navi_test_`. Never point tests at a production database or spool. No linter is configured in this repository.

If running the suite inside an existing DAQNavi container after changing tests or documentation, rebuild the image first. The Dockerfile copies the repository into the image, including `docs/contracts/fixtures/`; a container built before those files were added cannot run the MQTT fixture checks.

Legacy checks for the removed setup wizard, monorepo SQL bootstrap, and shared Compose stack remain in the test tree as skipped tests, so their history is preserved without implying that those components ship with this standalone service.

Tests that require a real DAQ or TimescaleDB are qualification procedures below, not part of unittest discovery.

## Full-system end-to-end test

`tests/test_e2e_full_system.py` targets a dedicated test stack with the web service at `localhost:8081`, test services named `pg`, `influx`, and `mqtt`, an isolated `daq_navi_test_local` database, MQTT test certificates under `/qa/mqtt/`, and a supported physical DAQ. The isolated Compose project and run instructions are in [deploy/e2e](../deploy/e2e/README.md). Run the script inside the DAQNavi service from that directory:

```bash
docker compose exec -T daq-navi-e2e sh /run-e2e.sh
```

The script installs `requirements-test.txt` and copies its summary to `deploy/e2e/reports/e2e_report.json` on the host.

This test writes and drops uniquely named tables in the test database and changes the web service configuration and acquisition lifecycle. Run it only against a disposable qualification stack and test database, never a production service or destination. Inside the container, it first writes the summary to `/tmp/e2e_report.json`.

## Physical DAQ qualification

On a Linux host with the supported card, driver, Python dependencies, and an isolated TimescaleDB test database, run from the repository root. Set `DAQ_TEST_DB_DSN` to the isolated database DSN first. The scripts enforce the `daq_navi_test_` database name prefix.

```bash
mkdir -p .scratch/daq-navi-production/qualification
python3 tests/qualify_standalone.py --rate 1000 --duration 30 --report-dir .scratch/daq-navi-production/qualification
python3 tests/qualify_standalone.py --rate 2000 --duration 30 --report-dir .scratch/daq-navi-production/qualification
python3 tests/qualify_outage_replay.py --outage-seconds 30 --replay-seconds 60 --report-dir .scratch/daq-navi-production/qualification
python3 tests/qualify_outage_replay.py --crash-first --outage-seconds 30 --replay-seconds 60 --report-dir .scratch/daq-navi-production/qualification
```

These scripts write reports under the explicit `--report-dir` path and use unique test table names. Record device, channel span, destination, rates, duration, command output, and skipped checks with the report. The standalone timer begins after the first physical sample appears in the spool status, with a 30-second startup limit. A passing run also requires all committed batches to reach the database before shutdown completes. The physical test verifies the rate observed by the application; compare inter-sample timing and the hardware manual before treating that value as the card's per-channel rate.

The outage runner checks delivery capacity while physical acquisition continues after the database becomes reachable. `--replay-seconds` is the observation period, not a deadline for an empty spool. It passes the live catch-up check when the spool stays near empty or its pending bytes decrease over the final 15 seconds while new samples keep arriving. A positive drain rate gives an estimated time to catch up if that rate continues; it is a projection, not an observed return to real time. If the window is too short or acquisition does not advance, the result is `inconclusive` (exit code 2); a stable or growing backlog fails the delivery-capacity check. An acquisition fault also fails it. The report includes the sampled backlog and acquisition status so the trend can be reviewed.

The runner reports replay integrity separately. `replay_integrity: pending` means committed samples remain in the spool and full identity/value comparison is still outstanding; it does not fail the live catch-up check. `replay_integrity: verified` means the spool emptied and the committed outage samples were compared with the database. Keep the isolated test database and spool until any pending integrity check is completed.

## Recovery boundary

A batch becomes replayable after its local SQLite spool commit. A process failure after a hardware read but before commit can lose the in-flight read. A destination outage leaves committed batches pending. MQTT QoS 1 waits for broker PUBACK before spool acknowledgment; QoS 0 waits for Paho `on_publish` and can lose messages after the client sends them. An isolated broker and consumer rehearsal is required to qualify a real MQTT deployment; the automated suite does not perform that integration.
