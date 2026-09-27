"""Run the real web API to physical DAQ to isolated TimescaleDB path.

Requires DAQ_TEST_DB_DSN naming a daq_navi_test_* database. Never writes to the
production telemetry database. The short run records counts and health states.
"""

import importlib
import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import psycopg2
from psycopg2 import sql


ROOT = Path(__file__).resolve().parents[1]


def main():
    dsn = os.environ.get('DAQ_TEST_DB_DSN', '')
    if not psycopg2.extensions.parse_dsn(dsn).get('dbname', '').startswith('daq_navi_test_'):
        raise SystemExit('DAQ_TEST_DB_DSN must name an isolated daq_navi_test_* database')
    web = importlib.import_module('web.app')
    run_id = uuid.uuid4().hex[:10]
    table = f'web_qualification_{run_id}'
    report = {'table': table, 'database': psycopg2.extensions.parse_dsn(dsn)['dbname']}
    source = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='daq_web_qualification_') as directory:
        root = Path(directory)
        source.update(DB_DSN=dsn, DB_PRODUCTION_TABLE=table,
                      SPOOL_DIR=str(root / 'spool'), AUTO_START_ON_STARTUP=False,
                      AUTO_START_MODE='production')
        config_path = root / 'config.json'
        config_path.write_text(json.dumps(source), encoding='utf-8')
        web.CONFIG_PATH = str(config_path)
        web.PID_PATH = str(root / 'acquisition.pid')
        web.MODE_PATH = str(root / 'acquisition.mode')
        web.LOG_PATH = str(root / 'acquisition.log')
        client = web.app.test_client()
        try:
            started = client.post('/api/start', json={'mode': 'production'})
            report['start'] = started.get_json()
            if started.status_code != 200:
                raise RuntimeError(f'Start failed: {report["start"]}')
            time.sleep(6)
            report['running_status'] = client.get('/api/status').get_json()
            report['graph_status'] = client.get('/api/samples?channel=0').status_code
        finally:
            report['stop'] = client.post('/api/stop').get_json()
            report['stopped_status'] = client.get('/api/status').get_json()
            report['log_tail'] = (root / 'acquisition.log').read_text(encoding='utf-8')[-1800:] if (root / 'acquisition.log').exists() else ''
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL('SELECT channel,count(*) FROM {} GROUP BY channel ORDER BY channel').format(sql.Identifier(table)))
                report['rows_by_channel'] = dict(cursor.fetchall())
                cursor.execute(sql.SQL('DROP TABLE {} CASCADE').format(sql.Identifier(table)))
        target = ROOT / '.scratch/daq-navi-production/qualification' / f'web-{run_id}.json'
        target.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
        print(target)
        print('rows_by_channel:', report['rows_by_channel'])
        print('running_healthy:', report['running_status']['healthy'])
        print('graph_status:', report['graph_status'])
        print('pending_replay:', report['stop'].get('pending_replay'))
        if (not report['running_status']['healthy'] or
                report['running_status']['status'] != 'running' or
                report['graph_status'] != 200 or
                not report['rows_by_channel']):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
