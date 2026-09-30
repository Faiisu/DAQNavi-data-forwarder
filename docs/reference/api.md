# HTTP API reference

The Flask routes in [web/app.py](../../web/app.py) are the source of truth. DAQNavi listens on container port `8081`; Compose publishes it on `DAQ_PORT` (default `8081`). Except for login, logout, static assets, and `/api/health`, requests require an operator session. Unauthenticated API requests receive HTTP `401`.

| Method and path | Purpose |
| --- | --- |
| `GET /api/health` | Minimal public health; returns HTTP `200` when healthy or `503` otherwise. |
| `GET /api/config` | Read saved config with secrets redacted and a revision token. |
| `POST /api/config` | Validate and save a revision-aware config change. A stale revision returns `409`. |
| `POST /api/test_destination` | Check selected destination connectivity without writing a sample. `/api/test_db` is an alias. |
| `GET /api/status` | Acquisition, spool, gap, cutover, and delivery state. |
| `GET /api/preview?channel=all&limit=200` | Recent committed acquisition samples before destination delivery. `channel` accepts `all` or 0–15; `limit` accepts 1–500. Add `range=1m` or `range=5m` for one-second min/max/average history bins. Available with PostgreSQL, InfluxDB, and MQTT. |
| `GET /api/samples?channel=N` | Recent database history for a channel; MQTT history belongs to the external consumer. |
| `GET /api/retention` | Retention information for the selected destination. |
| `GET /api/scan_usb` | Discover supported devices. |
| `POST /api/start` | Start the requested acquisition mode. |
| `POST /api/stop` | Stop acquisition. |
| `GET /api/buffer/clear` | Read the state of a pending clear job. |
| `POST /api/buffer/clear` | After acquisition stops, clear pending spool records using JSON `{"confirm":"CLEAR BUFFER"}`. |
| `POST /api/auth/change-password` | Change the signed-in operator's password. |

`GET/POST /login` creates the operator session and `GET/POST /logout` ends it. Config Center uses the same API and Socket.IO control events. This is a route map, not a generated request/response schema; inspect the linked source for exact payload fields and error responses.
