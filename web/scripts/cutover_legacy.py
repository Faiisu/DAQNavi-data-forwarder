"""Replace the legacy DAQ hypertable with a physical-production-only view.

Run only after the full-system qualification gate. The production hypertable is
never dropped; the old legacy hypertable and its chunks are deleted together.
"""

import argparse
import json
import re
from pathlib import Path

import psycopg2
from psycopg2 import sql


IDENTIFIER = re.compile(r'^[a-z][a-z0-9_]*$')


def cutover(connection, legacy_table, production_table):
    if (legacy_table == production_table or not IDENTIFIER.fullmatch(legacy_table)
            or not IDENTIFIER.fullmatch(production_table)):
        raise ValueError('Distinct simple table names are required')
    with connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT to_regclass(%s)', (production_table,))
            if cursor.fetchone()[0] is None:
                raise ValueError('Production hypertable does not exist')
            cursor.execute(sql.SQL("SELECT COUNT(*) FROM {} WHERE provenance='physical_daq'").format(
                sql.Identifier(production_table)))
            before = cursor.fetchone()[0]
            if before < 1:
                raise ValueError('No physical production samples exist; cutover gate has not passed')
            cursor.execute('SELECT relkind FROM pg_class WHERE oid=to_regclass(%s)', (legacy_table,))
            row = cursor.fetchone()
            if row and row[0] in ('r', 'p'):
                cursor.execute(sql.SQL('DROP TABLE {} CASCADE').format(sql.Identifier(legacy_table)))
            elif row and row[0] != 'v':
                raise ValueError(f'Unexpected legacy relation kind: {row[0]}')
            cursor.execute(sql.SQL('''CREATE OR REPLACE VIEW {} AS SELECT
                time,sample_id,session_id,device_id,channel,sensor_name,
                raw_voltage,calibrated_value,unit,provenance
                FROM {} WHERE provenance='physical_daq' ''').format(
                    sql.Identifier(legacy_table), sql.Identifier(production_table)))
            cursor.execute(sql.SQL('SELECT COUNT(*) FROM {}').format(sql.Identifier(legacy_table)))
            after = cursor.fetchone()[0]
            if after < before:
                raise RuntimeError('Production row count decreased during cutover')
            return {'production_rows_before': before, 'production_rows_after': after,
                    'legacy_relation': legacy_table, 'production_table': production_table}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(__file__).resolve().parents[1] / 'config.json')
    parser.add_argument('--execute', action='store_true', help='Delete the old table and create the production view')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding='utf-8'))
    if not args.execute:
        print(json.dumps({'action': 'dry-run', 'legacy_table': config['DB_TABLE'],
                          'production_table': config['DB_PRODUCTION_TABLE']}))
        return
    connection = psycopg2.connect(config['DB_DSN'], connect_timeout=3)
    try:
        print(json.dumps(cutover(connection, config['DB_TABLE'], config['DB_PRODUCTION_TABLE'])))
    finally:
        connection.close()


if __name__ == '__main__':
    main()
