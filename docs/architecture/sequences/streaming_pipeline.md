# Production Acquisition Sequence

```mermaid
sequenceDiagram
    actor Operator
    participant Web as DAQNavi Config Center / API
    participant Capture as Acquisition process
    participant DAQ as Advantech device
    participant Spool as Persistent SQLite spool
    participant Writer as Destination writer
    participant Dest as PostgreSQL / InfluxDB / MQTT broker
    participant Consumer as External MQTT consumer

    Operator->>Web: Save configuration and start production
    Web->>Capture: Launch with saved configuration
    Capture->>DAQ: Prepare and start waveform input
    loop Each acquired section
        Capture->>DAQ: Read configured channel span
        DAQ-->>Capture: Raw voltage values
        Capture->>Capture: Timestamp, identify, and calibrate samples
        Capture->>Spool: Commit sample batch and pending gaps
    end
    loop Pending records
        Writer->>Spool: Read oldest pending records
        Spool-->>Writer: Samples and gap revisions
        Writer->>Dest: Write rows or publish JSON v1 messages
        alt Destination confirms completion
            Dest-->>Writer: Write success or MQTT QoS completion
            Writer->>Spool: Acknowledge completed records
        else Destination unavailable or timeout
            Dest-->>Writer: Error or no completion
            Note over Writer,Spool: Keep records pending for retry under the latest saved configuration
        end
    end
    opt MQTT destination
        Dest-->>Consumer: Per-device samples and gap topics
        Note over Consumer: Consumer owns deduplication, history, retention, and downstream health
    end
    Web->>Spool: Read acquisition, spool, gaps, and delivery state
    Web->>Dest: Query recent samples or retention when database destination is selected
```

MQTT QoS completion is defined in the [JSON v1 contract](../../contracts/production-mqtt-contract-v1.md). Pending-record routing and cutover behavior are defined in the [production data flow](../data-flow.md). `/api/samples` does not query local database history while MQTT is selected.
