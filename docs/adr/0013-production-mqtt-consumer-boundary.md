# External consumer owns production MQTT history

Status: accepted

Production MQTT uses a versioned JSON contract on production-only topics split by device and by samples versus gaps. Sample messages contain byte-bounded batches with stable chunk identity; gap messages report opening and closing events with the same gap ID and a revision. Arrival order across topics is not guaranteed; consumers reconstruct state from stable IDs, revisions, and sample time. DAQNavi reports acquisition, spool, and broker delivery health. An external consumer owns processing, deduplication, long-term storage, historical queries, and its own health. MQTT is a selectable production destination; Config Center identifies the external history owner and does not query TimescaleDB for an MQTT run.
