# DAQNavi tests and qualification

## Automated tests

Run the isolated DAQNavi unit and web tests with:

```bash
.venv/bin/python -m unittest services.daq_navi.tests.test_production_mqtt_contract services.daq_navi.tests.test_production_acquisition services.daq_navi.tests.test_production_web services.daq_navi.tests.test_config_reliability services.daq_navi.tests.test_destination_connection services.daq_navi.tests.test_destinations services.daq_navi.tests.test_access_control -v
```

The MQTT contract suite checks validation, JSON fixtures, writer behavior, QoS completion, spool replay, and API behavior using isolated test doubles. It does not prove connectivity to a live MQTT broker or qualify physical throughput. The broader suite also does not replace hardware and destination qualification.

Database-dependent tests require `DAQ_TEST_DB_DSN` to point to an isolated database whose name starts with `daq_navi_test_`. The tests skip when that guard is not satisfied. Never point qualification tests at a production database or spool.

## Physical DAQ qualification

On a host with the supported PCI-1716 device and SDK, set `DAQ_TEST_DB_DSN` to an isolated database and run:

```bash
.venv/bin/python services/daq_navi/tests/qualify_standalone.py --rate 1000 --duration 30
.venv/bin/python services/daq_navi/tests/qualify_standalone.py --rate 2000 --duration 30
.venv/bin/python services/daq_navi/tests/qualify_outage_replay.py
.venv/bin/python services/daq_navi/tests/qualify_outage_replay.py --crash-first
```

These commands write reports under `.scratch/daq-navi-production/qualification/` and use uniquely named test tables. Record device, channel span, destination, rates, duration, command output, and any skipped checks with the report. The physical test verifies the rate observed by the application; check the hardware manual and actual inter-sample timing before treating that value as the card's per-channel rate.

## Recovery boundary

A batch becomes replayable after its local SQLite spool commit. A process failure after a hardware read but before commit can lose the in-flight read. A destination outage leaves committed batches pending. MQTT QoS 1 waits for broker PUBACK before spool acknowledgment; QoS 0 waits for Paho `on_publish` and can lose messages after the client sends them. An isolated broker/consumer rehearsal is required to qualify a real broker deployment; the automated MQTT suite does not perform that external integration.
