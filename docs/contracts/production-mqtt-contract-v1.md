# Production MQTT Record Contract (Version 1)

Status: Accepted  
Scope: DAQNavi Production Telemetry MQTT Destination

---

## 1. Overview and Boundaries

When an operator selects `mqtt` as the production destination, DAQNavi delivers physical acquisition samples and recorded acquisition gaps to an authenticated external MQTT broker.

### Responsibilities
- **DAQNavi (Publisher):**
  - Guarantees local durable capture into an on-disk SQLite spool prior to broker publication.
  - Formats physical samples and gap events according to this JSON v1 contract.
  - Enforces authenticated TLS transport and operator-configured delivery QoS (0 or 1).
  - Handles spool replay after broker outages, network disconnects, or process restarts.
  - Reports acquisition, local spool status, and broker delivery health via `/api/status`.
- **External Consumer (Subscriber):**
  - Subscribes to production topics.
  - Deduplicates sample chunks and individual points by `sample_id`.
  - Reconstructs acquisition timelines and merges gap states using `gap_id` and `revision`.
  - Manages long-term historical storage, retention policies, and historical queries (DAQNavi `/api/samples` does not query TimescaleDB for MQTT runs).

---

## 2. Topic Hierarchy & Device ID Encoding

### Topic Structure
Production topics are strictly versioned and prefixed to guarantee physical data provenance and prevent collision with legacy or mockup telemetry:

- **Samples:** `<MQTT_PRODUCTION_TOPIC_PREFIX>/<safe_device_id>/samples`
- **Gaps:** `<MQTT_PRODUCTION_TOPIC_PREFIX>/<safe_device_id>/gaps`

The default prefix is `daq/production/v1`.

### Safe Device ID Encoding
MQTT topic syntax uses the forward slash (`/`) as a hierarchical topic level separator, plus `+` and `#` as wildcards. Physical hardware device identifiers frequently contain slashes or special characters (e.g., `rack/one`, `pci-1716/0`).

- The device identifier segment in the topic string **must be percent-encoded** (RFC 3986, e.g. `urllib.parse.quote(device_id, safe='')`).
- Example: `device_id = "rack/one"` encodes to `rack%2Fone`.
- Formatted topics:
  - Samples: `daq/production/v1/rack%2Fone/samples`
  - Gaps: `daq/production/v1/rack%2Fone/gaps`
- The payload itself retains the original, unencoded `device_id` string (`"rack/one"`).

---

## 3. Sample Wire Record Contract

### Envelope Schema (`daq/production/v1/<safe_device_id>/samples`)
Sample publications are batched into deterministic, byte-bounded envelopes:

```json
{
  "schema_version": 1,
  "device_id": "rack/one",
  "batch_id": "batch-1700000000000000000-0001",
  "chunk_id": "batch-1700000000000000000-0001:c0",
  "samples": [
    {
      "time_ns": 1700000000000000000,
      "sample_id": "run:0:0",
      "session_id": "run",
      "device_id": "rack/one",
      "channel": 0,
      "sensor_name": "Sensor 0",
      "raw_voltage": 1.25,
      "calibrated_value": 25.0,
      "unit": "kPa",
      "provenance": "physical_daq"
    }
  ]
}
```

### Field Definitions
| Field | Type | Description |
| :--- | :--- | :--- |
| `schema_version` | integer | Fixed integer `1` for this contract version. |
| `device_id` | string | The unencoded device identifier string. |
| `batch_id` | string | Unique identifier of the source spool batch. |
| `chunk_id` | string | Deterministic chunk identifier within the batch (stable across replays). |
| `samples` | array | Array of individual physical sample objects. |

#### Sample Point Fields
| Field | Type | Description |
| :--- | :--- | :--- |
| `time_ns` | integer | Nanoseconds since Unix epoch (UTC) based on host system clock authority. |
| `sample_id` | string | Globally unique sample identifier (`<session_id>:<channel>:<seq>`). |
| `session_id` | string | Unique identifier of the acquisition session run. |
| `device_id` | string | Unencoded hardware device identifier. |
| `channel` | integer | Zero-indexed physical analog input channel. |
| `sensor_name` | string | Operator-assigned channel label. |
| `raw_voltage` | float | Direct measured voltage from DAQ card. |
| `calibrated_value`| float | Scaled engineering unit value. |
| `unit` | string | Engineering unit of measure (e.g. `kPa`, `V`, `psi`). |
| `provenance` | string | Always `"physical_daq"` for production records. |

*(Note: Per ADR 0011, `calibration_revision` is retired and omitted.)*

### Bounded Payloads & Replay Invariants
1. **Size Bounding:** Sample batches are partitioned into deterministic chunks packed against `MQTT_PRODUCTION_MAX_PAYLOAD_BYTES` (default 256 KiB). The limit is a hard maximum for each sample envelope. A sample record is indivisible; if its one-record envelope exceeds the configured maximum, DAQNavi rejects the write before publishing any message from that source batch. The source batch stays pending in the durable spool for replay after the payload or limit is corrected. Gap events are published individually.
2. **Deterministic Chunking:** Chunk identifiers are computed deterministically (e.g., `<batch_id>:c<index>`). Replay of an unacknowledged batch produces identical `chunk_id`s and byte contents.
3. **Deduplication:** Subscribers must use `sample_id` as the primary key for deduplication. Ingest pipelines may also reject duplicate chunks using `chunk_id`.

---

## 4. Gap Wire Record Contract

### Envelope Schema (`daq/production/v1/<safe_device_id>/gaps`)
Acquisition interruptions (buffer overflow, hardware timeout, network backpressure, or clean shutdown) publish gap event records.

#### Gap Opened Event
```json
{
  "schema_version": 1,
  "device_id": "rack/one",
  "gap_id": "gap-1700000000100-001",
  "revision": 1,
  "start_ns": 1700000000100000000,
  "end_ns": null,
  "cause": "broker_outage"
}
```

#### Gap Closed Event
```json
{
  "schema_version": 1,
  "device_id": "rack/one",
  "gap_id": "gap-1700000000100-001",
  "revision": 2,
  "start_ns": 1700000000100000000,
  "end_ns": 1700000000200000000,
  "cause": "broker_outage"
}
```

### Field Definitions
| Field | Type | Description |
| :--- | :--- | :--- |
| `schema_version` | integer | Fixed integer `1`. |
| `device_id` | string | Unencoded hardware device identifier. |
| `gap_id` | string | Stable unique identifier for the gap incident. |
| `revision` | integer | Monotonically increasing revision counter (starts at 1). |
| `start_ns` | integer | Nanoseconds since Unix epoch when data acquisition ceased. |
| `end_ns` | integer or null | Nanoseconds since Unix epoch when acquisition resumed, or `null` if open. |
| `cause` | string | Cause code: `broker_outage`, `spool_full`, `hardware_fault`, `stop_reconfigure`, etc. |

### Gap Merge & Out-of-Order Rules
- **No Cross-Topic Ordering:** MQTT does not order messages across separate topics. Samples and gaps arrive asynchronously.
- **Idempotent State Merge:** Consumers maintain gap state keyed by `(device_id, gap_id)`.
- **Revision Precedence:** Updates are accepted only if `incoming.revision > current.revision`. Older or equal revisions are discarded.
- **Close-Before-Open Resilience:** A closed gap event (`revision: 2`) contains all start, end, and cause data. If `revision: 2` arrives prior to `revision: 1`, the consumer creates the closed gap record immediately; when `revision: 1` arrives later, it is discarded since `1 < 2`.
- **Cutover Survivability:** If the destination is changed while a gap is open, the open event may exist at the prior destination while the close event is delivered to the new destination. Because the close event is self-contained with `start_ns` and `end_ns`, the new destination consumer accurately captures the entire outage span.

---

## 5. QoS Boundaries and Delivery Guarantees

Operators can configure QoS 0 or QoS 1 (`MQTT_PRODUCTION_QOS`, defaults to 1):

### QoS 0 (At Most Once)
- **Publisher Completion Boundary:** The writer returns success after the Paho MQTT client's `on_publish` callback is received for all chunks and gap events in the write call (confirming data was flushed to the local socket).
- **Spool Acknowledgment:** The source spool batch is acknowledged immediately upon local client flush.
- **Loss Boundary:** Messages may be dropped over the network or broker without publisher retransmission. Duplicates are rare but possible on client reconnect.

### QoS 1 (At Least Once - Default)
- **Publisher Completion Boundary:** The writer returns success only after receiving a broker `PUBACK` (`on_publish` with QoS 1 confirmation) for every chunk and gap event in the write call.
- **Spool Acknowledgment:** The source spool batch is acknowledged only after all `PUBACK` confirmations arrive.
- **Duplicate Boundary:** If the process restarts, crashes, or the network drops before spool acknowledgment completes, the unacknowledged batch is replayed. The external consumer must deduplicate by `sample_id`.

---

## 6. Security and Preflight Rules

- **TLS Mandatory:** Production MQTT requires `MQTT_TLS_ENABLED = true`. Unencrypted plaintext transmission is rejected during configuration validation.
- **Credentials Required:** `MQTT_USERNAME` and `MQTT_PASSWORD` must be non-empty.
- **Credential Privacy:** MQTT passwords are redacted in API responses (`/api/config`) and must never appear in logs or error messages.
- **Preflight Check:** `POST /api/test_destination` establishes a TLS socket, verifies broker handshake and credentials, and immediately disconnects without publishing any test messages to production topics.
