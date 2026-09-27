"""Physical web Start, database outage, saved-config restart, and spool replay."""

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
    database = psycopg2.extensions.parse_dsn(dsn).get('dbname', '')
    if not database.startswith('daq_navi_test_'):
        raise SystemExit('DAQ_TEST_DB_DSN must name an isolated daq_navi_test_* database')
    web = importlib.import_module('web.app')
    run_id = uuid.uuid4().hex[:10]
    table = f'web_replay_{run_id}'
    report = {'table': table, 'database': database}
    source = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    with tempfile.TemporaryDirectory(prefix='daq_web_replay_') as directory:
        root = Path(directory)
        source.update(DB_DSN=dsn.rsplit('/', 1)[0] + '/daq_navi_test_unavailable',
                      DB_PRODUCTION_TABLE=table, SPOOL_DIR=str(root / 'spool'),
                      AUTO_START_ON_STARTUP=False, AUTO_START_MODE='production')
        config_path = root / 'config.json'
        config_path.write_text(json.dumps(source), encoding='utf-8')
        web.CONFIG_PATH = str(config_path)
        web.PID_PATH = str(root / 'acquisition.pid')
        web.MODE_PATH = str(root / 'acquisition.mode')
        web.LOG_PATH = str(root / 'acquisition.log')
        client = web.app.test_client()
        try:
            report['start'] = client.post('/api/start', json={'mode': 'production'}).get_json()
            if not report['start']['started']:
                raise RuntimeError(report['start'])
            time.sleep(4)
            report['outage_status'] = client.get('/api/status').get_json()
            report['outage_health_code'] = client.get('/api/health').status_code
            changed = client.post('/api/config', json={'DB_DSN': dsn})
            report['save_and_restart_code'] = changed.status_code
            if changed.status_code != 200:
                report['save_error'] = changed.get_json()
                raise RuntimeError(report['save_error'])
            time.sleep(5)
        finally:
            report['stop'] = client.post('/api/stop').get_json()
            report['final_status'] = client.get('/api/status').get_json()
            report['log_tail'] = (root / 'acquisition.log').read_text(encoding='utf-8')[-1800:] if (root / 'acquisition.log').exists() else ''
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql.SQL('SELECT channel,count(*),count(distinct sample_id) FROM {} GROUP BY channel ORDER BY channel').format(sql.Identifier(table)))
                report['rows_by_channel'] = [list(row) for row in cursor.fetchall()]
                cursor.execute(sql.SQL('DROP TABLE {} CASCADE').format(sql.Identifier(table)))
        target = ROOT / '.scratch/daq-navi-production/qualification' / f'web-replay-{run_id}.json'
        target.write_text(json.dumps(report, indent=2, default=str), encoding='utf-8')
        print(target)
        print('outage:', report['outage_status']['status'], report['outage_health_code'])
        print('rows:', report['rows_by_channel'])
        print('pending:', report['stop'].get('pending_batches'))
        if (report['outage_status']['status'] != 'buffering' or
                report['outage_health_code'] != 503 or
                report['stop'].get('pending_batches') != 0 or
                not report['rows_by_channel'] or
                any(count != unique for _, count, unique in report['rows_by_channel'])):
            raise SystemExit(1)


if __name__ == '__main__':
    main()
