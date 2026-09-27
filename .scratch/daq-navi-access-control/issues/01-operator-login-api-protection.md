# 01: Protect DAQ Config Center with operator login

**What to build:** DAQ Navi has its own login page and operator session. DAQ operators sign in before they can read sensitive configuration, change DAQ settings, or start and stop acquisition. The browser can retain configured credentials without the server returning their values, and cross-site requests cannot use an operator session. The public Portal links to DAQ's login page; DAQ login does not authenticate the operator to either MUSASHI service.

**Blocked by:** None (can start immediately).

**Status:** resolved

- [x] The application has no hardcoded fallback account and fails closed when operator credentials or the session key are absent. The example Compose deployment seeds an initial operator credential for first login; deployment documentation requires rotation before wider exposure.
- [x] DAQ serves its own login/logout UI, redirects unauthenticated browser visits to login, returns unauthorized API responses to unauthenticated callers, expires sessions, and invalidates a session on logout.
- [x] Protect configuration, samples, runtime status, retention, device scan, destination testing, acquisition start/stop, buffer controls, and Socket.IO connection and control events with the DAQ session.
- [x] Protect state-changing HTTP and Socket.IO actions from cross-site requests; use a DAQ-specific cookie name, secure cookie attributes, and explicitly limited browser origins.
- [x] Omit saved database passwords, password-bearing DSNs, Influx tokens, MQTT passwords, and other secrets from all API responses and errors. The Config Center can preserve an unchanged secret or explicitly replace it without reading its old value.
- [x] Opening DAQ from the public Portal reaches DAQ's own login page when signed out and the Config Center when signed in. Portal polling uses only an explicitly allowed minimal health response and shows availability without sensitive state.
- [x] The container health check remains usable without a DAQ session. Document HTTPS deployment, credential provisioning and rotation, and recovery when credentials are lost.
- [x] Verify login/logout, expiry, secret redaction, successful operator workflow, rejected anonymous HTTP and Socket.IO access, rejected cross-site mutations, and Portal link/health behavior.

## Test Plan & Tracking

The comprehensive test matrix and tracking document is available at [`../test-matrix.md`](../test-matrix.md).

## Comments

- 2026-09-26: Test matrix and automated verification suite implemented and executed following TDD.
  - Implemented `services/daq_navi/web/auth.py` for PBKDF2 salted password hashing, signed HMAC session store with expiry, secret redaction/masking, and origin validation.
  - Implemented `services/daq_navi/web/templates/login.html` and integrated operator login/logout routes and Socket.IO/API authentication guards in `services/daq_navi/web/app.py`.
  - Added dedicated test suite `services/daq_navi/tests/test_access_control.py` covering all 31 scenarios across Groups A through I (26 unit/integration test methods). All 26 tests passed.
  - Regression verified: all 54 existing tests in `services/daq_navi/tests/test_production_web.py` passed cleanly without breaking spool, replay, retention, or graphs.
  - Full production suite run (`test_production_timescale.py`, `test_production_acquisition.py`, `test_production_web.py`, `test_access_control.py`): 118 tests passed.
