#!/bin/sh

uv pip install --system --no-cache --require-hashes -r requirements-test.txt
if [ "$?" -ne 0 ]; then
    exit 1
fi
python /app/deploy/e2e/prepare-e2e-db.py
if [ "$?" -ne 0 ]; then
    exit 1
fi
python tests/test_e2e_full_system.py
test_status=$?
if [ -f /tmp/e2e_report.json ]; then
    cp /tmp/e2e_report.json /reports/e2e_report.json
fi
exit "$test_status"
