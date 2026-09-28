"""Disk spool and destination effects used by configuration transitions."""

import time
from pathlib import Path

try:
    from daq_navi.core.config_loader import DaqNaviConfig
    from daq_navi.core.production_acquisition import DurableSpool, destination_identity
except ModuleNotFoundError:
    from core.config_loader import DaqNaviConfig
    from core.production_acquisition import DurableSpool, destination_identity


def drain_spool_for_destination_switch(old_config, new_config):
    """Close open gaps and record cutover while acquisition is stopped."""
    old_cfg = DaqNaviConfig(old_config, allow_env_overrides=False)
    new_cfg = DaqNaviConfig(new_config, allow_env_overrides=False)
    if destination_identity(old_cfg) == destination_identity(new_cfg):
        return
    spool = DurableSpool(Path(old_cfg.SPOOL_DIR), old_cfg.SPOOL_MAX_BYTES)
    try:
        spool.close_open_gaps_for_switch(time.time_ns())
        pending = spool.pending_batches + len(spool.pending_gaps())
        spool.record_cutover(old_cfg.DESTINATION, new_cfg.DESTINATION, pending)
    finally:
        spool.close()


def effective_timescale_retention(config):
    """Read the selected production hypertable's active retention policy."""
    import psycopg2

    cfg = DaqNaviConfig(config, allow_env_overrides=False)
    with psycopg2.connect(cfg.DB_DSN, connect_timeout=3) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT config->>'drop_after' FROM timescaledb_information.jobs "
                "WHERE hypertable_name=%s AND proc_name='policy_retention'",
                (cfg.DB_PRODUCTION_TABLE,),
            )
            rows = [row[0] for row in cursor.fetchall()]
    if not rows:
        raise RuntimeError('No active production retention policy')
    if len(set(rows)) != 1:
        raise RuntimeError('Multiple conflicting production retention policies are active')
    return rows[0]


def update_spool_owner(config, destination, identity):
    cfg = DaqNaviConfig(config, allow_env_overrides=False)
    spool = DurableSpool(Path(cfg.SPOOL_DIR), cfg.SPOOL_MAX_BYTES)
    try:
        spool.set_state('destination', destination)
        if identity:
            spool.set_state('destination_identity', identity)
    finally:
        spool.close()
