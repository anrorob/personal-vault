"""Durable admission to the existing KEN queue, never a second inference pipeline."""
import hashlib
import json
from psycopg.types.json import Jsonb
from app import ken_config

VERSION_KEYS = ("model_id", "model_revision", "runtime", "input_mode", "task_version",
                "result_binding_version", "prompt_version", "prompt_sha256", "parameters",
                "temporal_policy", "correction_version", "metadata_destination")


def version_config(config):
    return {**{key: config[key] for key in VERSION_KEYS if key in config},
            "grounded_prompt_version": config.get("grounded_prompt_version", ken_config.GROUNDED_VERSION)}


def version_key(config):
    return hashlib.sha256(json.dumps(version_config(config), sort_keys=True).encode()).hexdigest()


def initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS vault_home_video_backfill (
        asset_id UUID NOT NULL REFERENCES vault_assets(id), owner_user_id UUID NOT NULL,
        version TEXT NOT NULL, source_sha256 TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('pending','dispatched','skipped','failed')),
        run_id UUID REFERENCES vault_ken_runs(id), created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(asset_id,owner_user_id,version))""")


def current_run(conn, asset_id, owner_id, checksum, config):
    # Read version/binding state only; descriptions are not admission authority.
    return conn.execute("""SELECT id FROM vault_ken_runs WHERE asset_id=%s AND owner_user_id=%s
        AND status='completed' AND configuration @> %s
        AND COALESCE(configuration #>> '{source,verified_sha256}',
                     configuration #>> '{source,catalogue_sha256}')=%s
        ORDER BY created_at DESC,id DESC LIMIT 1""",
        (asset_id, owner_id, Jsonb(version_config(config)), checksum)).fetchone()


def enqueue(store, assets, owner_id, config):
    version = version_key(config)
    outcomes = dict(queued_new=0, already_current=0, skipped_active=0,
                    failed_existing_requires_attention=0, not_eligible=0)
    with store.connect() as conn:
        # Share admission serialization with manual runs and worker dispatch.
        conn.execute('SELECT pg_advisory_xact_lock(761003)')
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 761010))", (str(owner_id),))
        for asset in assets:
            if asset.owner_user_id != owner_id or asset.asset_type != 'Home Videos' or asset.lifecycle_state != 'active':
                outcomes['not_eligible'] += 1
                continue
            current_result = current_run(conn, asset.id, owner_id, asset.sha256, config)
            if current_result:
                record_admission(conn, asset, version, 'dispatched', current_result['id'])
                outcomes['already_current'] += 1
                continue
            run = blocking_run(conn, asset.id, owner_id, config)
            if run:
                same_version = version_config(run['configuration']) == version_config(config)
                record_admission(conn, asset, version, 'dispatched' if same_version else 'pending', run['id'])
                outcomes['failed_existing_requires_attention' if run['status'] == 'failed' else 'skipped_active'] += 1
                continue
            admission = conn.execute("""SELECT status FROM vault_home_video_backfill
                WHERE asset_id=%s AND owner_user_id=%s AND version=%s AND source_sha256=%s""",
                (asset.id,owner_id,version,asset.sha256)).fetchone()
            if admission and admission['status'] == 'failed':
                outcomes['failed_existing_requires_attention'] += 1
                continue
            inserted = conn.execute("""INSERT INTO vault_home_video_backfill(asset_id,owner_user_id,version,source_sha256,status)
                VALUES(%s,%s,%s,%s,'pending') ON CONFLICT(asset_id,owner_user_id,version) DO UPDATE
                SET status='pending', run_id=NULL, source_sha256=EXCLUDED.source_sha256
                WHERE vault_home_video_backfill.status='skipped'
                   OR vault_home_video_backfill.source_sha256<>EXCLUDED.source_sha256
                RETURNING asset_id""", (asset.id, owner_id, version, asset.sha256)).fetchone()
            outcomes['queued_new' if inserted else 'skipped_active'] += 1
    return {**progress(store, owner_id, config), **outcomes}


def blocking_run(conn, asset_id, owner_id, config):
    # Any active version serializes the asset. Failed current work needs explicit recovery.
    return conn.execute("""SELECT id,status,configuration,error,completed_at FROM vault_ken_runs
        WHERE asset_id=%s AND owner_user_id=%s AND
        (status IN ('queued','preparing_input','analysing','saving_result') OR
         (status='failed' AND configuration @> %s))
        ORDER BY CASE WHEN status='failed' THEN 1 ELSE 0 END,created_at DESC,id DESC LIMIT 1""",
        (asset_id, owner_id, Jsonb(version_config(config)))).fetchone()


def record_admission(conn, asset, version, status, run_id):
    conn.execute("""INSERT INTO vault_home_video_backfill
        (asset_id,owner_user_id,version,source_sha256,status,run_id) VALUES(%s,%s,%s,%s,%s,%s)
        ON CONFLICT(asset_id,owner_user_id,version) DO UPDATE SET
        status=EXCLUDED.status,run_id=EXCLUDED.run_id,source_sha256=EXCLUDED.source_sha256""",
        (asset.id, asset.owner_user_id, version, asset.sha256, status, run_id))


def progress(store, owner_id, config):
    with store.connect() as conn:
        rows = conn.execute("""WITH states AS (
            SELECT DISTINCT ON (asset_id) asset_id,status FROM vault_ken_runs
            WHERE owner_user_id=%s AND configuration @> %s
            ORDER BY asset_id,CASE WHEN status IN ('queued','preparing_input','analysing','saving_result') THEN 0
                WHEN status='completed' THEN 1 ELSE 2 END,created_at DESC,id DESC
        ), combined AS (
            SELECT status FROM states UNION ALL
            SELECT b.status FROM vault_home_video_backfill b
            WHERE b.owner_user_id=%s AND b.version=%s
            AND NOT EXISTS(SELECT 1 FROM states s WHERE s.asset_id=b.asset_id)
        ) SELECT status,count(*) AS count FROM combined GROUP BY status""",
            (owner_id, Jsonb(version_config(config)), owner_id, version_key(config))).fetchall()
    result = dict(pending=0, queued=0, processing=0, completed=0, failed=0, skipped=0)
    for row in rows:
        state = row['status']
        if state == 'dispatched':
            state = 'pending'  # Waiting for an older active version to finish.
        key = state if state in result else 'processing'
        result[key] += row['count']
    return result


def admit_next(store, vault, config):
    """Called under the existing KEN worker lock; at most one admission per turn."""
    with store.connect() as conn:
        row = conn.execute("""SELECT b.* FROM vault_home_video_backfill b
            WHERE b.status='pending' AND b.version=%s
            AND (SELECT count(*) FROM vault_ken_runs r WHERE r.owner_user_id=b.owner_user_id AND r.status='queued')<32
            ORDER BY b.created_at,b.asset_id LIMIT 1""", (version_key(config),)).fetchone()
    if row is None:
        return
    asset = vault.get_catalogued_asset_by_id(row['asset_id'])
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(761003)')
        # A bulk click or controlled retry may have changed this row while resolving the asset.
        row = conn.execute("""SELECT * FROM vault_home_video_backfill WHERE asset_id=%s
            AND owner_user_id=%s AND version=%s AND status='pending' FOR UPDATE""",
            (row['asset_id'],row['owner_user_id'],row['version'])).fetchone()
        if row is None:
            return
        status, run_id = 'skipped', None
        if (asset is not None and asset.owner_user_id == row['owner_user_id']
            and asset.asset_type == 'Home Videos' and asset.lifecycle_state == 'active'
            and asset.vault_path.startswith('/vault/Home Videos/') and asset.sha256 == row['source_sha256']):
            current = current_run(conn, asset.id, asset.owner_user_id, asset.sha256, config)
            run = current or blocking_run(conn, asset.id, asset.owner_user_id, config)
            if run and run.get('status') != 'failed' and not current and version_config(run['configuration']) != version_config(config):
                return
            if run:
                status, run_id = 'dispatched', run['id']
            else:
                try:
                    run = store.queue_ken(asset.id, asset.owner_user_id, config, connection=conn)
                    status, run_id = 'dispatched', run['id']
                except ValueError as error:
                    if str(error) == 'Too many queued analyses':
                        return
                    status = 'failed'
        conn.execute("""UPDATE vault_home_video_backfill SET status=%s,run_id=%s
            WHERE asset_id=%s AND owner_user_id=%s AND version=%s AND source_sha256=%s""",
            (status, run_id, row['asset_id'], row['owner_user_id'], row['version'], row['source_sha256']))


def retry_failed(store, asset, run_id, config):
    """One explicit retry, atomically linked to immutable failure evidence."""
    from app.ken_failure import failure_info
    with store.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(761003)')
        failed = conn.execute("""SELECT id,status,configuration,error,completed_at FROM vault_ken_runs
            WHERE id=%s AND asset_id=%s AND owner_user_id=%s""",
            (run_id,asset.id,asset.owner_user_id)).fetchone()
        if not failed or failed['status'] != 'failed' or not failure_info(failed)['retry_allowed']:
            raise ValueError('This analysis requires technical review; retry is not available')
        if version_config(failed['configuration']) != version_config(config):
            raise ValueError('Analysis version changed; use Analyse existing videos')
        source = failed['configuration'].get('source', {})
        if source.get('verified_sha256', source.get('catalogue_sha256', asset.sha256)) != asset.sha256:
            raise ValueError('Video source changed; technical review is required')
        if current_run(conn,asset.id,asset.owner_user_id,asset.sha256,config):
            raise ValueError('This video already has current analysis')
        child = conn.execute("""SELECT * FROM vault_ken_runs WHERE asset_id=%s AND owner_user_id=%s
            AND configuration->>'recovery_retry_of'=%s ORDER BY created_at LIMIT 1""",
            (asset.id,asset.owner_user_id,str(run_id))).fetchone()
        if child:
            return child  # Includes a failed retry: never create another attempt.
        active = blocking_run(conn,asset.id,asset.owner_user_id,config)
        if active and active['status'] != 'failed':
            raise ValueError('Wait for the active analysis to finish')
        retry = store.queue_ken(asset.id,asset.owner_user_id,
            {**config,'recovery_retry_of':str(run_id),'recovery_attempt':1},connection=conn)
        record_admission(conn,asset,version_key(config),'dispatched',retry['id'])
        return retry
