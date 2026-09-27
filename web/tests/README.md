# Tests and qualification

## Automated suite

Install the project and test dependencies, then run the suite from the repository root:

```bash
python3 -m pip install -r requirements-test.txt
python3 -m unittest discover -s tests -v
```

The suite exercises the standalone DAQNavi app, production spool and writer, web API, Compose deployment contract, and MQTT wire contract. It does not require a physical DAQ or a live destination for the normal unit run. Database-backed TimescaleDB tests are skipped unless `DAQ_TEST_DB_DSN` names an isolated database beginning with `daq_navi_test_`. Never point tests at a production database or spool. No linter is configured in this repository.

If running the suite inside an existing DAQNavi container after changing tests or documentation, rebuild the image first. The Dockerfile copies the repository into the image, including `docs/contracts/fixtures/`; a container built before those files were added cannot run the MQTT fixture checks.

Legacy checks for the removed setup wizard, monorepo SQL bootstrap, and shared Compose stack remain in the test tree as skipped tests, so their history is preserved without implying that those components ship with this standalone service.

Tests that require a real DAQ or TimescaleDB are qualification procedures below, not part of unittest discovery.

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
