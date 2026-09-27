# Configure an external destination

Use this guide after [starting DAQNavi](../../README.md#quickstart). The service must be able to reach the chosen destination from inside its container.

1. Sign in to Config Center at `http://<host>:8081`.
2. Select PostgreSQL/TimescaleDB, InfluxDB 2.x, or MQTT in the destination section.
3. Enter the destination's reachable address and credentials. For PostgreSQL choose fields or DSN mode. For InfluxDB enter its URL, organization, bucket, and token. For MQTT enter broker host, port, username, password, verified TLS settings, and topic prefix. Use `host.docker.internal` for a destination on the Docker host; `localhost` would point back to the DAQNavi container.
4. Select **Test Connection**. This checks reachability and credentials; it does not prove that samples were written or consumed.
5. Save the configuration. Check the device and channel settings, then start physical acquisition in Config Center.
6. Confirm new samples in the destination database or external MQTT consumer. For MQTT, the consumer owns history and must deduplicate replayed IDs according to the [MQTT v1 contract](../production-mqtt-contract-v1.md).

Production acquisition commits to the local spool before delivery. If the target is unavailable, pending records remain for retry. Changing the destination sends pending records to the new target and leaves acknowledged history at the old target; review the [cutover decision](../adr/0014-current-configuration-routes-pending-samples.md) before switching a live installation.

MQTT requires an authenticated broker with verified TLS. The [Config Center guide](../../web/README.md) has details for calibration, gaps, and safe qualification.
