"""Authenticated MQTT production writer with acknowledgement tracking."""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse
import uuid

class MQTTProductionDestination:
    """Production MQTT destination delivering physical samples and gaps over authenticated TLS."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.broker = str(cfg.MQTT_BROKER)
        self.port = int(cfg.MQTT_PORT)
        self.username = str(cfg.MQTT_USERNAME)
        self.password = str(cfg.MQTT_PASSWORD)
        self.tls_enabled = bool(cfg.MQTT_TLS_ENABLED)
        self.ca_certs = getattr(cfg, "MQTT_CA_CERTS", "") or None
        self.client_cert = getattr(cfg, "MQTT_CLIENT_CERT", "") or None
        self.client_key = getattr(cfg, "MQTT_CLIENT_KEY", "") or None
        self.qos = int(getattr(cfg, "MQTT_PRODUCTION_QOS", 1))
        self.topic_prefix = str(getattr(cfg, "MQTT_PRODUCTION_TOPIC_PREFIX", "daq/production/v1")).rstrip("/")
        self.max_payload_bytes = int(getattr(cfg, "MQTT_PRODUCTION_MAX_PAYLOAD_BYTES", 256 * 1024))

        self._lock = threading.Lock()
        self._pending_mids = {}
        self._acked_mids = set()
        self._client_id = f"daq_prod_{uuid.uuid4().hex[:8]}"

        import paho.mqtt.client as mqtt
        self.client = mqtt.Client(client_id=self._client_id)
        if self.username or self.password:
            self.client.username_pw_set(self.username, self.password)
        if self.tls_enabled:
            tls_kwargs = {}
            if self.ca_certs:
                tls_kwargs["ca_certs"] = self.ca_certs
            if self.client_cert:
                tls_kwargs["certfile"] = self.client_cert
            if self.client_key:
                tls_kwargs["keyfile"] = self.client_key
            self.client.tls_set(**tls_kwargs)

        self.client.on_connect = lambda *args, **kwargs: None
        self.client.on_publish = self._on_publish
        self.client.connect(self.broker, self.port)
        self.client.loop_start()

    def _on_publish(self, client, userdata, mid, *args, **kwargs):
        with self._lock:
            self._acked_mids.add(mid)
            event = self._pending_mids.get(mid)
            if event is not None:
                event.set()

    def ensure_schema(self):
        pass

    def _publish_tracked(self, topic, payload, events_to_wait):
        encoded = json.dumps(payload, separators=(',', ':'), allow_nan=False)
        result = self.client.publish(topic, encoded, qos=self.qos, retain=False)
        if getattr(result, "rc", 0) != 0:
            raise RuntimeError(f"MQTT publish rejected with code {result.rc}")
        mid = result.mid
        event = threading.Event()
        with self._lock:
            if mid in self._acked_mids:
                event.set()
            else:
                self._pending_mids[mid] = event
        events_to_wait.append((mid, event))

    def write(self, rows: list[dict], gaps: list[dict]):
        if not rows and not gaps:
            return

        events_to_wait = []
        safe_device = urllib.parse.quote(str(self.cfg.DEVICE_ID), safe="")
        sample_topic = f"{self.topic_prefix}/{safe_device}/samples"
        count_topic = f"{self.topic_prefix}/{safe_device}/cycle_counts"
        gap_topic = f"{self.topic_prefix}/{safe_device}/gaps"
        sample_rows = [row for row in rows if row.get("record_type") != "cycle_count"]
        count_rows = [dict(row, interval_seconds=row.get("interval_seconds", 1))
                      for row in rows if row.get("record_type") == "cycle_count"]
        prepared = []

        for records, topic, field in ((sample_rows, sample_topic, "samples"),
                                      (count_rows, count_topic, "cycle_counts")):
            if not records:
                continue
            batch_id = hashlib.sha256(
                f"{records[0].get('sample_id')}:{records[-1].get('sample_id')}:{records[0].get('time_ns')}:{len(records)}".encode()
            ).hexdigest()[:16]

            chunks = []
            curr_chunk = []
            base_envelope = {
                "schema_version": 1,
                "device_id": self.cfg.DEVICE_ID,
                "batch_id": batch_id,
            }

            for row in records:
                if curr_chunk:
                    test_chunk = curr_chunk + [row]
                    test_chunk_id = f"{batch_id}:c{len(chunks)}"
                    envelope = dict(base_envelope, chunk_id=test_chunk_id, **{field: test_chunk})
                    payload_len = len(json.dumps(
                        envelope, separators=(',', ':'), allow_nan=False).encode("utf-8"))
                    if payload_len <= self.max_payload_bytes:
                        curr_chunk.append(row)
                        continue
                    chunks.append(curr_chunk)
                    curr_chunk = []

                chunk_id = f"{batch_id}:c{len(chunks)}"
                envelope = dict(base_envelope, chunk_id=chunk_id, **{field: [row]})
                payload_len = len(json.dumps(
                    envelope, separators=(',', ':'), allow_nan=False).encode("utf-8"))
                if payload_len > self.max_payload_bytes:
                    raise ValueError(
                        "MQTT record envelope exceeds maximum payload size "
                        f"(sample_id={row.get('sample_id')}, actual_bytes={payload_len}, "
                        f"max_bytes={self.max_payload_bytes})")
                curr_chunk.append(row)
            if curr_chunk:
                chunks.append(curr_chunk)

            for idx, chunk in enumerate(chunks):
                chunk_id = f"{batch_id}:c{idx}"
                envelope = dict(base_envelope, chunk_id=chunk_id, **{field: chunk})
                prepared.append((topic, envelope))

        for topic, envelope in prepared:
            self._publish_tracked(topic, envelope, events_to_wait)

        # 2. Publish gaps if any
        for gap in gaps:
            payload = {
                "schema_version": 1,
                "device_id": self.cfg.DEVICE_ID,
                "gap_id": gap["gap_id"],
                "revision": gap.get("revision", 1),
                "start_ns": gap["start_ns"],
                "end_ns": gap.get("end_ns"),
                "cause": gap["cause"],
            }
            self._publish_tracked(gap_topic, payload, events_to_wait)

        # 3. Wait for all messages to complete
        deadline = time.monotonic() + 10.0
        for mid, event in events_to_wait:
            remaining = max(0.01, deadline - time.monotonic())
            if not event.wait(remaining):
                raise TimeoutError(f"MQTT publish timed out waiting for mid {mid}")

        with self._lock:
            for mid, _ in events_to_wait:
                self._pending_mids.pop(mid, None)
                self._acked_mids.discard(mid)

    def close(self):
        try:
            self.client.loop_stop()
        except Exception:
            pass
        try:
            self.client.disconnect()
        except Exception:
            pass
