# System Context

```mermaid
flowchart LR
    Operator[Operator] --> Portal[Portal :8080]
    Operator -->|login and configuration| Web[DAQNavi :8081]
    Portal -->|links and public health| Web
    Web -->|control| Production[Physical acquisition process]
    Web -->|control| Mockup[Mockup acquisition process]
    Hardware[Advantech DAQNavi / BioDAQ] --> Production
    Production --> Spool[(SQLite production spool)]
    Spool --> Writer[Production destination writer]
    Writer --> PG[(PostgreSQL / TimescaleDB)]
    Writer --> Influx[(InfluxDB 2.x)]
    Writer --> Broker[External authenticated TLS MQTT broker]
    Broker --> Consumer[External consumer and history]
    Mockup -. mockup writes .-> PG
    Mockup -. mockup writes .-> Influx
```

DAQNavi acquires physical measurements or synthetic mockup measurements. Production samples and acquisition gaps are committed to a local spool before the production writer sends them to the single configured destination. PostgreSQL/TimescaleDB and InfluxDB provide queryable history; with MQTT, history and downstream health belong to the external consumer. Mockup acquisition writes only to database destinations. The repository does not include a local MQTT broker or an MQTT-to-database subscriber.

The [data flow](data-flow.md), [storage records](erd.md), and [acquisition sequence](sequences/streaming_pipeline.md) describe these boundaries in more detail.
