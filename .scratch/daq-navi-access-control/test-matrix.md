# Test Matrix & Execution Plan: DAQ Navi Operator Access Control

- **Feature / Effort:** `daq-navi-access-control`
- **Target Issue:** [`01-operator-login-api-protection.md`](issues/01-operator-login-api-protection.md)
- **Specification:** [`spec.md`](spec.md)
- **Primary Modules Under Test:**
  - Web Server & Routes: [`services/daq_navi/web/app.py`](../../services/daq_navi/web/app.py)
  - Config Loader & Validation: [`services/daq_navi/core/config_loader.py`](../../services/daq_navi/core/config_loader.py)
  - Automated Web Suite: [`services/daq_navi/tests/test_access_control.py`](../../services/daq_navi/tests/test_access_control.py) (to be added)
  - Regression Suite: [`services/daq_navi/tests/test_production_web.py`](../../services/daq_navi/tests/test_production_web.py)
- **Status:** Complete (All Passed)

---

## 1. Traceability & Requirements Mapping

| Acceptance Criteria in Issue 01 | Covered Test IDs |
| :--- | :--- |
| Provisioning via private secrets (Salted Hash + Session Key), Fail-closed on missing | `SEC-01`, `SEC-02`, `SEC-03`, `SEC-04` |
| DAQ login/logout UI, redirect unauthenticated, return 401 for API, session expiry | `AUTH-01`, `AUTH-02`, `AUTH-03`, `AUTH-04`, `AUTH-05`, `AUTH-06`, `AUTH-07` |
| DAQ cookie isolation (specific name, HttpOnly, SameSite, Secure), cross-service isolation | `ISO-01`, `ISO-02` |
| Protect config, samples, runtime status, retention, scan_usb, start/stop, buffer/clear | `API-01`, `API-02`, `API-03`, `API-04` |
| Protect Socket.IO connection and control events (`connect`, `start_daq`, `stop_daq`) | `WS-01`, `WS-02`, `WS-03` |
| Protect state-changing actions from cross-site requests, restrict browser origins | `CSRF-01`, `CSRF-02` |
| Redact secrets in API readback, preserve unchanged secrets on edit, explicit replace | `SEC-05`, `SEC-06`, `SEC-07`, `SEC-08` |
| Public minimal health endpoint (`/api/health`) for Portal and Docker healthcheck | `HLTH-01`, `HLTH-02`, `HLTH-03` |
| Regression compatibility: All existing 54 web tests continue passing | `REG-01` |

---

## 2. Test Execution Matrix & Progress Tracker

Status Legend:
- `[ ] PENDING`: Not yet executed
- `[x] PASS`: Executed and verified passed
- `[-] FAIL`: Test failed, bug logged
- `[>] IN-PROGRESS`: Currently under implementation/testing

### Group A: Provisioning & Fail-Closed Startup

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **SEC-01** | Normal startup with valid secrets | Salted hash and session key provided in secure files/env | App starts cleanly, auth middleware active | Unit / E2E | `[x] PASS` |
| **SEC-02** | Fail-closed: missing password hash | Password secret missing or empty | Process exits with non-zero code, logs error without starting server | Unit | `[x] PASS` |
| **SEC-03** | Fail-closed: missing session key | Session secret missing or empty | Process exits with non-zero code | Unit | `[x] PASS` |
| **SEC-04** | No hardcoded authentication fallback | Start the application without an operator hash or session key, outside Compose defaults | Refuses to start. The example Compose deployment supplies an initial operator credential; rotate it after first login as documented in `DEPLOY_LINUX.md` | Security | `[x] PASS` |

### Group B: Authentication Flow & Session Lifecycle

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **AUTH-01** | Successful operator login | `POST /login` with correct username + password | Returns 200/302, `Set-Cookie` with DAQ session, redirect to `next` | Integration | `[x] PASS` |
| **AUTH-02** | Failed operator login (bad password) | `POST /login` with wrong password | Returns 401 Unauthorized, generic error message, no session cookie | Integration | `[x] PASS` |
| **AUTH-03** | Anonymous access to Config Center | `GET /` without cookie | Redirects (302) to `/login?next=%2F` | Integration | `[x] PASS` |
| **AUTH-04** | Authenticated access to login page | `GET /login` with valid session cookie | Redirects (302) to `/` | Integration | `[x] PASS` |
| **AUTH-05** | Operator logout | `POST /logout` or `GET /logout` with session | Session invalidated on server, cookie cleared (`Max-Age=0`), redirect to `/login` | Integration | `[x] PASS` |
| **AUTH-06** | Expired session rejection | Request with timestamp exceeding TTL | Treated as unauthenticated (API 401 / UI redirect), session purged | Integration | `[x] PASS` |
| **AUTH-07** | Tampered session cookie rejection | Altered payload or mismatched HMAC signature | Rejected with 401 or redirect; session rejected immediately | Security | `[x] PASS` |

### Group C: Cookie Security & Cross-Service Isolation

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **ISO-01** | Cookie security flags | Inspect `Set-Cookie` response header | Cookie name is `daq_session_id`, flags include `HttpOnly`, `SameSite=Lax`, `Path=/`, and `Secure` (when HTTPS) | Security | `[x] PASS` |
| **ISO-02** | Isolation from MUSASHI sessions | Present MUSASHI session cookie or foreign cookie to DAQ | DAQ rejects session, treats as anonymous | Security | `[x] PASS` |

### Group D: HTTP API Boundary Protection

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **API-01** | Anonymous `GET /api/config` | Request without cookie | Returns JSON `401 Unauthorized` (`{"error": "Unauthorized"}`), NOT HTML | API | `[x] PASS` |
| **API-02** | Anonymous mutating API calls | `POST /api/config`, `/api/start`, `/api/stop`, `/api/buffer/clear` | Returns JSON `401 Unauthorized`, zero mutations occur | API | `[x] PASS` |
| **API-03** | Anonymous telemetry & state inspection | `GET /api/status`, `/api/samples`, `/api/retention`, `/api/scan_usb` | Returns JSON `401 Unauthorized`, no telemetry or hardware data leaked | API | `[x] PASS` |
| **API-04** | Authenticated API operations | Valid session cookie attached to all above endpoints | Returns HTTP 200 with standard payload and full normal functionality | API | `[x] PASS` |

### Group E: WebSocket / Socket.IO Boundary Protection

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **WS-01** | Anonymous Socket.IO handshake | Connect without session cookie | Connection rejected (`connect` handler returns `False` / disconnect) | WebSocket | `[x] PASS` |
| **WS-02** | Authenticated Socket.IO connection | Connect with valid session cookie | Handshake succeeds, client joins room and receives status broadcasts | WebSocket | `[x] PASS` |
| **WS-03** | Anonymous acquisition control over WS | Emit `start_daq` or `stop_daq` without session | Command ignored or rejected, no acquisition process spawned/stopped | WebSocket | `[x] PASS` |

### Group F: Cross-Site Request & Origin Validation

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **CSRF-01**| Disallowed cross-site POST | POST with `Origin: http://evil.com` | Rejected with 403 Forbidden or SameSite protection prevents cookie attachment | Security | `[x] PASS` |
| **CSRF-02**| Disallowed Socket.IO origin | Handshake with `Origin: http://evil.com` | Connection rejected | Security | `[x] PASS` |

### Group G: Secret Redaction & Safe Config Mutation

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **SEC-05** | Redact secrets in `GET /api/config` | Call `GET /api/config` with valid session | Passwords and tokens (`POSTGRES_PASSWORD`, `INFLUX_TOKEN`, `MQTT_PASSWORD`, DSN credentials) are masked | Security | `[x] PASS` |
| **SEC-06** | Preserve secrets on partial save | POST config with empty/placeholder password | Stored password in `config.json` remains untouched, other settings update | Integration | `[x] PASS` |
| **SEC-07** | Explicit password replacement | POST config with new non-empty password | New password saved to `config.json` | Integration | `[x] PASS` |
| **SEC-08** | Error message redaction | Trigger DB/Influx connection failure | Error response and UI alert do NOT expose plaintext passwords or DSN tokens | Security | `[x] PASS` |

### Group H: Public Health & Portal Integration

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **HLTH-01**| Public Minimal Health Endpoint | Anonymous `GET /api/health` | Returns 200 OK with minimal payload (e.g. `{"status": "ok", "service": "daq_navi"}`); no channels or buffer stats | API | `[x] PASS` |
| **HLTH-02**| Container health check compatibility | Docker Compose health check probe | Passes without requiring authentication credentials | Deployment | `[x] PASS` |
| **HLTH-03**| Portal Origin CORS Allowlist | `GET /api/health` from Portal origin (e.g. `http://host:8080`) | Allowed via CORS headers; Portal badge functions properly | Integration | `[x] PASS` |

### Group I: Regression & Legacy Web Suite Compatibility

| ID | Test Scenario | Pre-conditions & Input | Expected Result | Type | Status |
| :--- | :--- | :--- | :--- | :---: | :---: |
| **REG-01** | Existing 54 web unit tests | Run `test_production_web.py` with authenticated test client | All 54 tests pass without breakage to spool, replay, retention, or graphs | Regression | `[x] PASS` |

---

## 3. Step-by-Step Execution & Verification Guide

### Step 1: Automated Unit & Integration Tests (TDD)

Run the dedicated access control test suite:
```bash
./.venv/bin/python -m unittest services/daq_navi/tests/test_access_control.py -v
```

Run the existing regression suite to confirm zero regressions:
```bash
./.venv/bin/python -m unittest services/daq_navi/tests/test_production_web.py -v
```

### Step 2: Verification of Fail-Closed Startup

Test startup behavior without secrets:
```bash
# 1. Start without secret files -> Must fail closed
env -u DAQ_OPERATOR_HASH -u DAQ_SESSION_KEY ./.venv/bin/python -c "
import importlib
try:
    web = importlib.import_module('services.daq_navi.web.app')
    web.init_application()
    print('FAIL: App started without credentials')
except SystemExit:
    print('PASS: App failed closed as expected')
"
```

### Step 3: Secret Redaction & Safe Edit Verification

1. Verify `GET /api/config` output:
   ```bash
   # Check that no passwords or auth tokens are in the returned JSON
   curl -s -b cookies.txt http://localhost:8081/api/config | jq '.POSTGRES_PASSWORD, .INFLUX_TOKEN'
   ```
2. Verify saving without changing password preserves the existing password in `config.json`.

### Step 4: Docker Container Health & Portal Verification

1. Build and launch container:
   ```bash
   docker compose -f deploy/daq-navi/compose.yml up -d --build
   ```
2. Check container health status:
   ```bash
   docker compose -f deploy/daq-navi/compose.yml ps
   # STATUS must indicate "(healthy)"
   ```
3. Test unauthenticated endpoint from host:
   ```bash
   curl -I http://localhost:8081/api/health # Returns 200 OK
   curl -I http://localhost:8081/api/config # Returns 401 Unauthorized
   curl -I http://localhost:8081/           # Returns 302 Found -> /login?next=/
   ```

---

## 4. Evidence Recording & Sign-off Protocol

When executing the tests:
1. Mark completed items in this file (`[ ]` -> `[x]`).
2. If any test fails, mark `[-]`, log the error details in `## Test Run Logs`, and do not mark the ticket as resolved.
3. Once all P0 and P1 tests pass and `REG-01` passes, update ticket [`.scratch/daq-navi-access-control/issues/01-operator-login-api-protection.md`](issues/01-operator-login-api-protection.md) with test output and mark `Status: resolved`.
