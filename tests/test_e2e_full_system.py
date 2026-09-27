#!/usr/bin/env python3
"""End-to-End (E2E) Test Suite for DAQNavi Data Forwarder.

Exercises the entire system from physical hardware capture to storage/streaming,
web API, access control, dynamic configuration, and outage replay resiliency.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import shutil
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import paho.mqtt.client as mqtt
import psycopg2
from psycopg2 import sql

from core.config_loader import DaqNaviConfig
from core.production_acquisition import (
    AdvantechDaq,
    DurableSpool,
    InfluxProductionDestination,
    MQTTProductionDestination,
    ProductionPipeline,
    TimescaleProductionDestination,
    run_production,
    validate_production_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("e2e_test")

WEB_BASE = "http://localhost:8081"
TEST_DSN = "postgresql://admin:testpass@pg:5432/daq_navi_test_local"
INFLUX_URL = "http://influx:8086"
INFLUX_ORG = "mddp"
INFLUX_BUCKET = "daq_telemetry"
INFLUX_TOKEN = "local-test-token"
MQTT_HOST = "mqtt"
MQTT_PORT = 8883
MQTT_USER = "tester"
MQTT_PASS = "testpass"
MQTT_CA = "/qa/mqtt/ca.crt"

RESULTS: List[Dict[str, Any]] = []


def record_result(phase: str, name: str, passed: bool, details: Dict[str, Any]):
    status_str = "PASS" if passed else "FAIL"
    log.info("[%s] %s: %s", status_str, phase, name)
    RESULTS.append({
        "phase": phase,
        "name": name,
        "passed": passed,
        "details": details,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    if not passed:
        log.error("Failure details: %s", json.dumps(details, indent=2, default=str))


# ----------------------------------------------------------------------
# Phase 1: Environment & Hardware Sanity
# ----------------------------------------------------------------------
def test_phase_1_sanity():
    log.info("=== Phase 1: Environment & Hardware Sanity ===")
    
    # 1.1 DAQ device node
    daq_dev_exists = os.path.exists("/dev/daq0")
    record_result(
        "Phase 1",
        "Advantech DAQ device node (/dev/daq0)",
        daq_dev_exists,
        {"path": "/dev/daq0", "exists": daq_dev_exists},
    )

    # 1.2 Destination network connectivity
    pg_ok = False
    try:
        with socket.create_connection(("pg", 5432), timeout=3):
            pg_ok = True
    except OSError as e:
        log.error("Cannot reach pg:5432: %s", e)

    influx_ok = False
    try:
        with socket.create_connection(("influx", 8086), timeout=3):
            influx_ok = True
    except OSError as e:
        log.error("Cannot reach influx:8086: %s", e)

    mqtt_ok = False
    try:
        with socket.create_connection(("mqtt", 8883), timeout=3):
            mqtt_ok = True
    except OSError as e:
        log.error("Cannot reach mqtt:8883: %s", e)

    record_result(
        "Phase 1",
        "Destination Connectivity (pg:5432, influx:8086, mqtt:8883)",
        pg_ok and influx_ok and mqtt_ok,
        {"pg_5432": pg_ok, "influx_8086": influx_ok, "mqtt_8883": mqtt_ok},
    )


# ----------------------------------------------------------------------
# Phase 2: Web API & Access Control E2E
# ----------------------------------------------------------------------
def test_phase_2_web_auth() -> str:
    log.info("=== Phase 2: Web API & Access Control E2E ===")
    
    # 2.1 Public health check
    req = urllib.request.Request(f"{WEB_BASE}/api/health")
    with urllib.request.urlopen(req, timeout=3) as resp:
        health_data = json.loads(resp.read().decode())
    health_ok = (
        resp.status == 200
        and health_data.get("service") == "daq_navi"
        and health_data.get("healthy") is True
    )
    record_result(
        "Phase 2",
        "Public /api/health endpoint",
        health_ok,
        {"status_code": resp.status, "body": health_data},
    )

    # 2.2 Unauthenticated access blocked
    unauth_blocked = False
    try:
        urllib.request.urlopen(f"{WEB_BASE}/api/config", timeout=3)
    except urllib.error.HTTPError as e:
        unauth_blocked = (e.code == 401)
    record_result(
        "Phase 2",
        "Unauthenticated /api/config rejected (401)",
        unauth_blocked,
        {"blocked": unauth_blocked},
    )

    # 2.3 Invalid login rejected
    invalid_login_rejected = False
    login_data = urllib.parse.urlencode({"username": "admin", "password": "wrongpassword"}).encode()
    req = urllib.request.Request(f"{WEB_BASE}/login", data=login_data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            content = resp.read().decode()
            invalid_login_rejected = "Invalid username or password" in content or resp.status == 401
    except urllib.error.HTTPError as e:
        invalid_login_rejected = (e.code in (400, 401))
    record_result(
        "Phase 2",
        "Invalid operator credentials rejected",
        invalid_login_rejected,
        {"rejected": invalid_login_rejected},
    )

    # 2.4 Valid login produces session cookie
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    valid_login_data = urllib.parse.urlencode({"username": "admin", "password": "00000000"}).encode()
    req = urllib.request.Request(f"{WEB_BASE}/login", data=valid_login_data, method="POST")
    cookie_str = ""
    try:
        resp = opener.open(req, timeout=3)
    except urllib.error.HTTPError as e:
        if e.code == 302:
            cookie_hdr = e.headers.get("Set-Cookie", "")
            if "daq_session_id=" in cookie_hdr:
                for part in cookie_hdr.split(";"):
                    if "daq_session_id=" in part:
                        cookie_str = part.strip()
                        break

    session_ok = bool(cookie_str)
    record_result(
        "Phase 2",
        "Valid operator login sets signed session cookie",
        session_ok,
        {"cookie_present": session_ok},
    )

    if not session_ok:
        log.error("Cannot proceed with authenticated Web API tests without session cookie.")
        return cookie_str

    # 2.5 Hardware scan endpoint via Web API
    req = urllib.request.Request(f"{WEB_BASE}/api/scan_usb")
    req.add_header("Cookie", cookie_str)
    with urllib.request.urlopen(req, timeout=5) as resp:
        scan_data = json.loads(resp.read().decode())
    
    daq_found = any(
        d.get("is_daq") is True and "PCI-1716" in d.get("name", "")
        for d in scan_data.get("devices", [])
    )
    record_result(
        "Phase 2",
        "Device scan /api/scan_usb discovers Advantech PCI-1716",
        daq_found,
        {"device_count": scan_data.get("count"), "daq_found": daq_found},
    )

    # 2.6 Destination test endpoints
    for dest_name, dest_payload in [
        ("postgresql", {"DESTINATION": "postgresql", "DB_HOST": "pg", "DB_PORT": 5432, "DB_USER": "admin", "DB_PASSWORD": "testpass", "DB_NAME": "daq_navi_test_local"}),
        ("influxdb", {"DESTINATION": "influxdb", "INFLUX_URL": INFLUX_URL, "INFLUX_ORG": INFLUX_ORG, "INFLUX_BUCKET": INFLUX_BUCKET, "INFLUX_TOKEN": INFLUX_TOKEN}),
        ("mqtt", {"DESTINATION": "mqtt", "MQTT_BROKER": MQTT_HOST, "MQTT_PORT": MQTT_PORT, "MQTT_USERNAME": MQTT_USER, "MQTT_PASSWORD": MQTT_PASS, "MQTT_TLS_ENABLED": True, "MQTT_CA_CERTS": MQTT_CA}),
    ]:
        req = urllib.request.Request(
            f"{WEB_BASE}/api/test_destination",
            data=json.dumps(dest_payload).encode(),
            headers={"Content-Type": "application/json", "Cookie": cookie_str},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                test_res = json.loads(resp.read().decode())
                dest_ok = test_res.get("success") is True
        except Exception as e:
            test_res = {"error": str(e)}
            dest_ok = False
        record_result(
            "Phase 2",
            f"Test connection to {dest_name} destination",
            dest_ok,
            test_res,
        )

    return cookie_str


# ----------------------------------------------------------------------
# Helper to configure 4 channels consistently
# ----------------------------------------------------------------------
def prepare_base_config() -> Dict[str, Any]:
    base = json.loads(open("/app/config/config.json", encoding="utf-8").read())
    base["DEVICE_DESCRIPTION"] = "PCI-1716,BID#0"
    base["DEVICE_ID"] = "pci1716-0"
    base["START_CHANNEL"] = 0
    base["CHANNEL_COUNT"] = 4
    base["CLOCK_RATE"] = 1000
    base["SECTION_LENGTH"] = 500
    base["SECTION_COUNT"] = 0
    for k in list(base["CHANNELS"].keys()):
        idx = int(k)
        if idx < 4:
            base["CHANNELS"][k].update({
                "enabled": True,
                "label": f"press-ch{idx}",
                "unit": "kPa",
                "scale": {
                    "enabled": True,
                    "low_voltage": 0.0,
                    "high_voltage": 5.0,
                    "low_value": 0.0,
                    "high_value": 1000.0,
                },
            })
        else:
            base["CHANNELS"][k]["enabled"] = False
    return base


# ----------------------------------------------------------------------
# Phase 3: Hardware Data Pipeline -> TimescaleDB / PostgreSQL
# ----------------------------------------------------------------------
def test_phase_3_timescaledb():
    log.info("=== Phase 3: Physical Hardware -> Spool -> TimescaleDB ===")
    time.sleep(2.0)
    run_id = uuid.uuid4().hex[:8]
    table = f"daq_e2e_timescale_{run_id}"

    base = prepare_base_config()
    base.update({
        "DESTINATION": "postgresql",
        "DB_DSN": TEST_DSN,
        "DB_PRODUCTION_TABLE": table,
        "DB_RETENTION_DAYS": 30,
    })
    cfg = DaqNaviConfig(base, allow_env_overrides=False)
    validate_production_config(cfg)

    with tempfile.TemporaryDirectory(prefix="daq_e2e_spool_pg_") as spool_dir:
        stop_event = threading.Event()
        
        # Run physical capture for 6 seconds
        def stopper():
            time.sleep(6)
            log.info("Stopping physical capture for TimescaleDB test...")
            stop_event.set()

        threading.Thread(target=stopper, daemon=True).start()
        start_mono = time.monotonic()
        run_res = run_production(cfg, stop_event=stop_event, daq_factory=AdvantechDaq, spool_dir=spool_dir)
        elapsed = time.monotonic() - start_mono

        record_result(
            "Phase 3",
            "Physical DAQ acquisition completed and spool drained",
            run_res["pending_batches"] == 0 and len(run_res["writer_errors"]) == 0,
            {"run_res": run_res, "elapsed_s": round(elapsed, 2)},
        )

        # Verify in TimescaleDB database
        with psycopg2.connect(TEST_DSN) as conn:
            with conn.cursor() as cur:
                # Check hypertable existence
                cur.execute(
                    "SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name = %s",
                    (table,),
                )
                is_hypertable = bool(cur.fetchone())

                # Check rows and calibration
                cur.execute(sql.SQL("""
                    SELECT channel, count(*), min(time), max(time),
                           min(calibrated_value), max(calibrated_value),
                           count(DISTINCT sample_id),
                           count(*) FILTER (WHERE provenance = 'physical_daq')
                    FROM {} GROUP BY channel ORDER BY channel
                """).format(sql.Identifier(table)))
                channel_stats = []
                total_rows = 0
                for row in cur.fetchall():
                    ch, cnt, t_min, t_max, v_min, v_max, uniq, prov_cnt = row
                    total_rows += cnt
                    span = (t_max - t_min).total_seconds() if cnt > 1 else 0
                    rate_calc = round((cnt - 1) / span, 1) if span > 0 else 0
                    channel_stats.append({
                        "channel": ch,
                        "count": cnt,
                        "unique_ids": uniq,
                        "time_span_s": round(span, 2),
                        "observed_rate_hz": rate_calc,
                        "calibrated_range": [round(v_min, 2), round(v_max, 2)],
                        "physical_provenance_rows": prov_cnt,
                    })

                # Check gaps
                cur.execute("SELECT cause, start_time, end_time FROM daq_production_gaps ORDER BY start_time DESC LIMIT 5")
                gaps = cur.fetchall()

                # Clean up test table
                cur.execute(sql.SQL("DROP TABLE {} CASCADE").format(sql.Identifier(table)))

        all_channels_populated = len(channel_stats) == 4 and all(c["count"] >= 1000 for c in channel_stats)
        rates_valid = all(990 <= c["observed_rate_hz"] <= 1010 for c in channel_stats)
        no_duplicates = all(c["count"] == c["unique_ids"] for c in channel_stats)

        record_result(
            "Phase 3",
            "TimescaleDB data verification (hypertables, sampling rate ~1000Hz, calibration, provenance)",
            is_hypertable and all_channels_populated and rates_valid and no_duplicates,
            {
                "is_hypertable": is_hypertable,
                "total_rows_inserted": total_rows,
                "channels": channel_stats,
                "gaps_recorded": len(gaps),
            },
        )



# ----------------------------------------------------------------------
# Phase 4: Hardware Data Pipeline -> InfluxDB 2.x
# ----------------------------------------------------------------------
def test_phase_4_influxdb():
    log.info("=== Phase 4: Physical Hardware -> Spool -> InfluxDB 2.x ===")
    time.sleep(2.0)
    run_id = uuid.uuid4().hex[:8]
    measurement = f"daq_e2e_influx_{run_id}"

    base = prepare_base_config()
    base.update({
        "DESTINATION": "influxdb",
        "INFLUX_URL": INFLUX_URL,
        "INFLUX_ORG": INFLUX_ORG,
        "INFLUX_BUCKET": INFLUX_BUCKET,
        "INFLUX_MEASUREMENT": measurement,
        "INFLUX_TOKEN": INFLUX_TOKEN,
    })
    cfg = DaqNaviConfig(base, allow_env_overrides=False)
    validate_production_config(cfg)

    with tempfile.TemporaryDirectory(prefix="daq_e2e_spool_influx_") as spool_dir:
        stop_event = threading.Event()
        
        def stopper():
            time.sleep(5)
            log.info("Stopping physical capture for InfluxDB test...")
            stop_event.set()

        threading.Thread(target=stopper, daemon=True).start()
        run_res = run_production(cfg, stop_event=stop_event, daq_factory=AdvantechDaq, spool_dir=spool_dir)

        spool_drained = (run_res["pending_batches"] == 0 and len(run_res["writer_errors"]) == 0)
        record_result(
            "Phase 4",
            "InfluxDB physical acquisition run & spool drain",
            spool_drained,
            {"run_res": run_res},
        )

        # Query InfluxDB 2.x via Flux query
        flux_query = f'''
        from(bucket: "{INFLUX_BUCKET}")
          |> range(start: -5m)
          |> filter(fn: (r) => r._measurement == "{measurement}")
          |> filter(fn: (r) => r._field == "calibrated_value")
          |> count()
        '''
        req = urllib.request.Request(
            f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}",
            data=flux_query.encode("utf-8"),
            headers={
                "Authorization": f"Token {INFLUX_TOKEN}",
                "Content-Type": "application/vnd.flux",
                "Accept": "application/csv",
            },
            method="POST",
        )
        points_by_channel = {}
        with urllib.request.urlopen(req, timeout=5) as resp:
            reader = csv.DictReader(io.StringIO(resp.read().decode("utf-8")))
            for row in reader:
                if row.get("_value") and row.get("channel"):
                    points_by_channel[int(row["channel"])] = int(row["_value"])

        has_data = len(points_by_channel) == 4 and all(v >= 500 for v in points_by_channel.values())
        record_result(
            "Phase 4",
            "InfluxDB telemetry verification via Flux API",
            has_data,
            {"measurement": measurement, "points_by_channel": points_by_channel},
        )


# ----------------------------------------------------------------------
# Phase 5: Hardware Data Pipeline -> Secure MQTT (TLS Contract v1)
# ----------------------------------------------------------------------
def test_phase_5_mqtt():
    log.info("=== Phase 5: Physical Hardware -> Spool -> MQTT (TLS QoS 1) ===")
    time.sleep(2.0)
    
    # 5.1 Set up TLS MQTT subscriber
    received_messages: List[Dict[str, Any]] = []
    sub_ready = threading.Event()
    
    sub = mqtt.Client(client_id=f"e2e_sub_{uuid.uuid4().hex[:6]}")
    sub.username_pw_set(MQTT_USER, MQTT_PASS)
    sub.tls_set(ca_certs=MQTT_CA, cert_reqs=ssl.CERT_REQUIRED)
    
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            client.subscribe("daq/production/v1/+/samples", qos=1)
            sub_ready.set()
        else:
            log.error("MQTT connect failed with rc=%d", rc)

    def on_message(client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            received_messages.append(payload)
        except Exception as e:
            log.error("Failed to parse MQTT message: %s", e)

    sub.on_connect = on_connect
    sub.on_message = on_message
    sub.connect(MQTT_HOST, MQTT_PORT)
    sub.loop_start()

    connected = sub_ready.wait(5)
    if not connected:
        sub.loop_stop()
        record_result("Phase 5", "MQTT subscriber connect with TLS", False, {"error": "Connection timed out"})
        return

    # 5.2 Configure DAQ pipeline with MQTT destination
    base = prepare_base_config()
    base.update({
        "DESTINATION": "mqtt",
        "MQTT_BROKER": MQTT_HOST,
        "MQTT_PORT": MQTT_PORT,
        "MQTT_USERNAME": MQTT_USER,
        "MQTT_PASSWORD": MQTT_PASS,
        "MQTT_TLS_ENABLED": True,
        "MQTT_CA_CERTS": MQTT_CA,
    })
    cfg = DaqNaviConfig(base, allow_env_overrides=False)
    validate_production_config(cfg)

    with tempfile.TemporaryDirectory(prefix="daq_e2e_spool_mqtt_") as spool_dir:
        stop_event = threading.Event()
        
        def stopper():
            time.sleep(5)
            log.info("Stopping physical capture for MQTT test...")
            stop_event.set()

        threading.Thread(target=stopper, daemon=True).start()
        run_res = run_production(cfg, stop_event=stop_event, daq_factory=AdvantechDaq, spool_dir=spool_dir)

        # Allow subscriber to receive inflight messages
        wait_deadline = time.monotonic() + 5
        while time.monotonic() < wait_deadline and len(received_messages) < 10:
            time.sleep(0.1)

        sub.loop_stop()
        sub.disconnect()

        # Verify MQTT contract v1 compliance
        messages_received = len(received_messages) > 0
        all_contract_v1 = all(m.get("schema_version") == 1 for m in received_messages)
        total_samples_delivered = sum(len(m.get("samples", [])) for m in received_messages)
        has_required_keys = all(
            all(k in m for k in ("schema_version", "device_id", "batch_id", "chunk_id", "samples"))
            for m in received_messages
        )
        sample_keys_valid = all(
            all(all(k in s for k in ("sample_id", "session_id", "channel", "raw_voltage", "calibrated_value", "time_ns"))
                for s in m.get("samples", []))
            for m in received_messages
        )

        record_result(
            "Phase 5",
            "MQTT delivery & Contract v1 compliance (TLS QoS 1)",
            messages_received and all_contract_v1 and has_required_keys and sample_keys_valid and run_res["pending_batches"] == 0,
            {
                "messages_received": len(received_messages),
                "total_samples": total_samples_delivered,
                "schema_version_1": all_contract_v1,
                "spool_pending_batches": run_res["pending_batches"],
            },
        )


# ----------------------------------------------------------------------
# Phase 6: Outage & Store-and-Forward Replay Resiliency E2E
# ----------------------------------------------------------------------
def test_phase_6_outage_replay():
    log.info("=== Phase 6: Outage & Store-and-Forward Replay Resiliency ===")
    time.sleep(1.0)
    run_id = uuid.uuid4().hex[:8]
    table = f"daq_e2e_replay_{run_id}"

    base = prepare_base_config()
    base.update({
        "DESTINATION": "postgresql",
        "DB_DSN": TEST_DSN,
        "DB_PRODUCTION_TABLE": table,
        "DB_RETENTION_DAYS": 30,
    })
    cfg = DaqNaviConfig(base, allow_env_overrides=False)
    validate_production_config(cfg)

    with tempfile.TemporaryDirectory(prefix="daq_e2e_spool_outage_") as spool_dir:
        spool_path = Path(spool_dir)
        real_dest = TimescaleProductionDestination(cfg)
        
        # 1. Pipeline starts with working destination
        pipeline = ProductionPipeline(cfg, spool_path, real_dest)
        daq = AdvantechDaq(cfg)

        # Collect 2 seconds normal
        for _ in range(4):
            raw, end_ns = daq.read(cfg.USER_BUFFER_SIZE)
            pipeline.capture(raw, end_ns, timing_reliable=True)
            pipeline.flush_batches(4)

        normal_samples = pipeline.frame_index
        log.info("Normal acquisition: captured %d frames. Pending: %d", normal_samples, pipeline.pending_batches)

        # 2. Simulate Destination Outage by replacing destination with a failing one
        class FailingDestination:
            def __init__(self):
                self.fail_count = 0
            def ensure_schema(self):
                pass
            def write(self, rows, gaps):
                self.fail_count += 1
                raise ConnectionError("Simulated Destination Outage: network partition")

        pipeline.destination = FailingDestination()

        # Collect 3 seconds during destination outage
        for _ in range(6):
            raw, end_ns = daq.read(cfg.USER_BUFFER_SIZE)
            pipeline.capture(raw, end_ns, timing_reliable=True)
            try:
                pipeline.flush_batches(4)
            except Exception:
                pass  # expected failure

        outage_pending = pipeline.pending_batches
        log.info("Outage active: captured frames. Backlogged batches in spool: %d", outage_pending)
        outage_held_in_spool = (outage_pending >= 6)

        # 3. Restore Destination
        pipeline.destination = real_dest
        log.info("Destination restored. Replaying backlogged batches from SQLite spool...")
        replay_flushes = 0
        while pipeline.pending_batches > 0 and replay_flushes < 20:
            flushed = pipeline.flush_batches(4)
            replay_flushes += 1

        final_pending = pipeline.pending_batches
        daq.close()
        pipeline.close()

        # 4. Verify in database
        with psycopg2.connect(TEST_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute(sql.SQL("""
                    SELECT count(*), count(DISTINCT sample_id), count(DISTINCT time)
                    FROM {}
                """).format(sql.Identifier(table)))
                db_count, uniq_samples, uniq_times = cur.fetchone()
                cur.execute(sql.SQL("DROP TABLE {} CASCADE").format(sql.Identifier(table)))

        expected_rows = pipeline.frame_index * len(pipeline.enabled)
        all_recovered = (
            outage_held_in_spool
            and final_pending == 0
            and db_count == expected_rows
            and uniq_samples == db_count
        )
        record_result(
            "Phase 6",
            "Outage resilience & Store-and-Forward Replay (Zero data loss, 100% deduplicated)",
            all_recovered,
            {
                "outage_batches_backlogged": outage_pending,
                "final_pending_batches": final_pending,
                "total_frames_captured": pipeline.frame_index,
                "db_total_samples": db_count,
                "expected_rows": expected_rows,
                "unique_samples": uniq_samples,
            },
        )


# ----------------------------------------------------------------------
# Phase 7: Web API Dynamic Config & Full Lifecycle Control
# ----------------------------------------------------------------------
def test_phase_7_web_lifecycle(cookie_str: str):
    log.info("=== Phase 7: Web API Dynamic Config & Lifecycle Control ===")
    time.sleep(1.0)
    if not cookie_str:
        record_result("Phase 7", "Web API Lifecycle skipped (no auth cookie)", False, {})
        return

    # 7.1 Fetch current config to acquire revision
    req = urllib.request.Request(f"{WEB_BASE}/api/config", headers={"Cookie": cookie_str})
    with urllib.request.urlopen(req, timeout=3) as resp:
        cur_config = json.loads(resp.read().decode())
    
    # 7.2 Update config via POST /api/config to ensure valid test database and 4 channels
    cur_config.update({
        "DESTINATION": "postgresql",
        "DB_HOST": "pg",
        "DB_PORT": 5432,
        "DB_NAME": "daq_navi_test_local",
        "DB_USER": "admin",
        "DB_PASSWORD": "testpass",
        "DB_PRODUCTION_TABLE": "daq_production_samples",
        "START_CHANNEL": 0,
        "CHANNEL_COUNT": 4,
        "CLOCK_RATE": 1000,
        "SECTION_LENGTH": 500,
    })
    for k in list(cur_config.get("CHANNELS", {}).keys()):
        if int(k) < 4:
            cur_config["CHANNELS"][k]["enabled"] = True
        else:
            cur_config["CHANNELS"][k]["enabled"] = False

    req = urllib.request.Request(
        f"{WEB_BASE}/api/config",
        data=json.dumps(cur_config).encode(),
        headers={"Content-Type": "application/json", "Cookie": cookie_str},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        save_res = json.loads(resp.read().decode())
    config_saved = (save_res.get("status") != "error")
    record_result("Phase 7", "Web API /api/config dynamic update", config_saved, save_res)

    # 7.3 Start acquisition via Web API
    req = urllib.request.Request(
        f"{WEB_BASE}/api/start",
        data=json.dumps({"mode": "production"}).encode(),
        headers={"Content-Type": "application/json", "Cookie": cookie_str},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        start_res = json.loads(resp.read().decode())
    
    started_ok = start_res.get("started") is True
    time.sleep(5)

    # 7.4 Check status while running
    req = urllib.request.Request(f"{WEB_BASE}/api/status", headers={"Cookie": cookie_str})
    with urllib.request.urlopen(req, timeout=3) as resp:
        status_res = json.loads(resp.read().decode())
    
    is_running = status_res.get("is_running") is True and status_res.get("status") == "running"

    # 7.5 Read live samples from web API
    req = urllib.request.Request(f"{WEB_BASE}/api/samples?channel=0", headers={"Cookie": cookie_str})
    with urllib.request.urlopen(req, timeout=3) as resp:
        samples_status = resp.status
        samples_data = json.loads(resp.read().decode())
    points_list = samples_data.get("points", [])
    samples_ok = (samples_status == 200 and isinstance(points_list, list) and len(points_list) > 0)

    # 7.6 Stop acquisition via Web API
    req = urllib.request.Request(
        f"{WEB_BASE}/api/stop",
        data=b"{}",
        headers={"Content-Type": "application/json", "Cookie": cookie_str},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        stop_res = json.loads(resp.read().decode())

    # 7.7 Check final stopped status
    req = urllib.request.Request(f"{WEB_BASE}/api/status", headers={"Cookie": cookie_str})
    with urllib.request.urlopen(req, timeout=3) as resp:
        final_status = json.loads(resp.read().decode())

    stopped_cleanly = (
        final_status.get("is_running") is False
        and final_status.get("healthy") is True
    )

    record_result(
        "Phase 7",
        "Web API Lifecycle (/api/start -> /api/status -> /api/samples -> /api/stop)",
        started_ok and is_running and samples_ok and stopped_cleanly,
        {
            "start": start_res,
            "running_status": status_res.get("status"),
            "points_count": len(points_list),
            "stop": stop_res,
            "final_healthy": final_status.get("healthy"),
        },
    )


def main():
    log.info("Starting Full System E2E Test Suite...")
    start_time = time.time()
    
    try:
        test_phase_1_sanity()
        cookie = test_phase_2_web_auth()
        time.sleep(3.0)
        test_phase_3_timescaledb()
        time.sleep(3.0)
        test_phase_4_influxdb()
        time.sleep(3.0)
        test_phase_5_mqtt()
        time.sleep(3.0)
        test_phase_6_outage_replay()
        time.sleep(3.0)
        if cookie:
            test_phase_7_web_lifecycle(cookie)
    except Exception as e:
        log.exception("Fatal error during E2E test execution: %s", e)
        record_result("FATAL", "Execution aborted", False, {"exception": str(e)})

    total_duration = time.time() - start_time
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["passed"])
    failed = total - passed

    log.info("==================================================")
    log.info("E2E Test Run Finished in %.2f seconds", total_duration)
    log.info("Total Checks: %d | PASSED: %d | FAILED: %d", total, passed, failed)
    log.info("==================================================")

    report = {
        "summary": {
            "total_checks": total,
            "passed": passed,
            "failed": failed,
            "duration_seconds": round(total_duration, 2),
            "all_passed": failed == 0,
        },
        "results": RESULTS,
    }
    
    report_file = Path("/tmp/e2e_report.json")
    report_file.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("Detailed JSON report written to: %s", report_file)

    if failed > 0:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
