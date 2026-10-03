"""Operational bulk recovery through existing owner policies and publication states.

The caller supplies reviewed intake UUIDs from a private incident manifest.
No real IDs, filenames, credentials or receipt bodies belong in source control.
"""
from collections import Counter
from uuid import UUID

from app.vault_master_autopilot import process_autopilot_batch


def preview_bulk_recovery(vault_store, item_ids):
    selected = set(item_ids)
    if not selected or any(not isinstance(item_id, UUID) for item_id in selected):
        raise ValueError("Explicit intake UUIDs are required")
    counts = Counter()
    for item_id in selected:
        item = vault_store.get_item(item_id)
        if item is None:
            counts["missing"] += 1
        elif item.source_kind != "incoming" or item.owner_user_id is None:
            counts["identity_unavailable"] += 1
        else:
            counts[item.state] += 1
    return dict(counts)


def recover_bulk_once(policy_store, ai_store, vault_store, incoming_root, destinations, item_ids):
    """Queue at most one policy-sized batch, rechecking eligibility and source bytes.

    Repeated calls skip already queued/published/failed items. This neither
    authorizes unresolved items nor creates replacement uploads or identities.
    Files are moved only by the normal signed publisher after queue processing.
    """
    item_ids = set(item_ids)
    preview_bulk_recovery(vault_store, item_ids)
    selected = {item_id for item_id in item_ids
                if (item := vault_store.get_item(item_id)) is not None
                and item.proposed_category == "Gallery"}
    if not selected:
        return None
    return process_autopilot_batch(policy_store, ai_store, vault_store, incoming_root,
                                  destinations, allowed_item_ids=selected)


def incident_metrics(connection, item_ids):
    """Aggregate-only read-only operator telemetry for an explicit incident."""
    selected = list(set(item_ids))
    if not selected or any(not isinstance(item_id, UUID) for item_id in selected):
        raise ValueError('Explicit intake UUIDs are required')
    with connection.cursor() as cursor:
        cursor.execute("SELECT state,count(*) AS count FROM vault_master_items WHERE id=ANY(%s) GROUP BY state",(selected,))
        states = {row['state']: row['count'] for row in cursor.fetchall()}
        cursor.execute('''SELECT count(*) AS receipts,
            count(*) FILTER(WHERE published_at >= CURRENT_TIMESTAMP-INTERVAL '10 minutes') AS completed_last_ten_minutes
            FROM vault_arrival_managed_publications WHERE item_id=ANY(%s)''',(selected,))
        publications = dict(cursor.fetchone())
        cursor.execute('''SELECT EXTRACT(EPOCH FROM CURRENT_TIMESTAMP-min(discovered_at)) AS oldest_seconds,
            EXTRACT(EPOCH FROM CURRENT_TIMESTAMP-max(discovered_at)) AS newest_seconds
            FROM vault_master_items WHERE id=ANY(%s) AND state IN ('needs_review','approved','move_queued','theatre_promotion_pending')''',(selected,))
        age={key:float(value) if value is not None else None for key,value in cursor.fetchone().items()}
        cursor.execute('''SELECT count(*) AS scan_retries_scheduled FROM vault_arrival_scan_checkpoints checkpoint
            JOIN vault_master_items item ON item.source_path=checkpoint.source_path
            WHERE item.id=ANY(%s) AND NOT checkpoint.succeeded AND checkpoint.next_attempt_at>CURRENT_TIMESTAMP''',(selected,))
        retries=dict(cursor.fetchone())
    return dict(selected=len(selected),missing=len(selected)-sum(states.values()),states=states,
                **publications,**age,**retries)


def main():
    import argparse,json
    from pathlib import Path
    import psycopg
    from psycopg.rows import dict_row
    from app.config import get_database_conninfo
    parser=argparse.ArgumentParser(description='Preview or queue one bounded, owner-policy-checked incident batch')
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--recover-once',action='store_true')
    args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
    if manifest.get('schema')!='personal-vault.intake-recovery.v1':
        raise ValueError('Unsupported incident manifest')
    expected={UUID(row['item_id']):row for row in manifest['items']}
    if len(expected)!=len(manifest['items']) or not expected:
        raise ValueError('Incident IDs must be nonempty and unique')
    with psycopg.connect(get_database_conninfo(),row_factory=dict_row,
                         options='-c default_transaction_read_only=on -c statement_timeout=20000') as connection:
        rows=connection.execute('SELECT id,owner_user_id,sha256,size_bytes,source_kind FROM vault_master_items WHERE id=ANY(%s)',(list(expected),)).fetchall()
        if len(rows)!=len(expected):
            raise ValueError('An incident item is missing')
        for row in rows:
            original=expected[row['id']]
            if (str(row['owner_user_id'])!=original['owner_user_id'] or row['sha256']!=original['sha256']
                    or row['size_bytes']!=original['size_bytes'] or row['source_kind']!='incoming'):
                raise ValueError('Incident identity or source evidence changed; no recovery queued')
        print(json.dumps(incident_metrics(connection, expected),sort_keys=True))
    if args.recover_once:
        from app.vault_master import get_vault_master_store
        from app.vault_master_autopilot import get_autopilot_store
        from app.vault_master_ingestion_ai import get_ingestion_ai_store
        from app.incoming import get_arrival_hall_path
        from app.vault_master_api import get_destination_paths
        run=recover_bulk_once(get_autopilot_store(),get_ingestion_ai_store(),get_vault_master_store(),
                              get_arrival_hall_path(),get_destination_paths(),expected)
        print(json.dumps({'batch_queued':run is not None}))


if __name__=='__main__':
    main()
