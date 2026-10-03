"""Explicit, expiring incident grants. Never consulted by normal intake workers."""
from datetime import datetime, timezone
from uuid import UUID
from psycopg.types.json import Jsonb

from app.arrival_publication_coordination import serialized_publication
from app.arrival_photo_approval import _queue_authorized_gallery_photo
from app.vault_master import MemoryVaultMasterStore, verify_camera_photo
from app.vault_master_autopilot import camera_photo_safety_eligible, _preflight


def initialize_recovery_authorizations(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS vault_intake_recovery_authorizations (
        id UUID PRIMARY KEY, owner_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
        requested_by TEXT NOT NULL, issued_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL, items JSONB NOT NULL, consumed JSONB NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','completed')),
        CHECK(expires_at>issued_at))""")


def _discovered_at(store, item_id):
    if isinstance(store, MemoryVaultMasterStore):
        return min((event.created_at for event in store.activity
                    if event.item_id == item_id and event.action == 'file_analysed'), default=None)
    with store._connect() as connection:
        row = connection.execute('SELECT discovered_at FROM vault_master_items WHERE id=%s',(item_id,)).fetchone()
    return row['discovered_at'] if row else None


def _validated_manifest(store, manifest, now):
    if manifest.get('schema') != 'personal-vault.intake-recovery-authorization.v1':
        raise ValueError('Explicit recovery authorization manifest required')
    owner = UUID(manifest['owner_user_id'])
    issued = datetime.fromisoformat(manifest['issued_at'])
    expires = datetime.fromisoformat(manifest['expires_at'])
    if issued.tzinfo is None or expires.tzinfo is None or not issued <= now < expires:
        raise ValueError('Recovery authorization is expired or not yet valid')
    expected = manifest['items']
    if not expected or len({entry['item_id'] for entry in expected}) != len(expected):
        raise ValueError('Recovery requires unique explicit historical intake identities')
    for entry in expected:
        item = store.get_item(UUID(entry['item_id']))
        discovered = _discovered_at(store, UUID(entry['item_id']))
        if (item is None or discovered is None or item.owner_user_id != owner or item.source_kind != 'incoming'
                or discovered > issued or item.sha256 != entry['sha256']
                or item.size_bytes != entry['size_bytes']):
            raise ValueError('Recovery manifest does not match historical owner/source evidence')
    requested = manifest['requested_by']
    if not isinstance(requested, str) or not requested.strip():
        raise ValueError('Explicit recovery approval audit identity required')
    return dict(id=UUID(manifest['id']),owner_user_id=owner,requested_by=requested,
                issued_at=issued,expires_at=expires,items=[dict(entry) for entry in expected],consumed=[],status='active')


@serialized_publication
def register_recovery_authorization(store, manifest, *, now=None):
    """Operator-only registration; creates no policy and authorizes no other items."""
    grant = _validated_manifest(store, manifest, now or datetime.now(timezone.utc))
    if isinstance(store, MemoryVaultMasterStore):
        grants = getattr(store, '_recovery_authorizations', {})
        existing = grants.get(grant['id'])
        if existing and any(existing[k] != v for k,v in grant.items() if k not in {'consumed','status'}):
            raise ValueError('Recovery authorization is immutable')
        grants.setdefault(grant['id'], grant)
        store._recovery_authorizations = grants
    else:
        with store._connect() as connection:
            connection.execute("""INSERT INTO vault_intake_recovery_authorizations
                (id,owner_user_id,requested_by,issued_at,expires_at,items)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING""",
                (grant['id'],grant['owner_user_id'],grant['requested_by'],grant['issued_at'],grant['expires_at'],Jsonb(grant['items'])))
            existing = connection.execute('SELECT * FROM vault_intake_recovery_authorizations WHERE id=%s FOR UPDATE',(grant['id'],)).fetchone()
            if any(existing[k] != v for k,v in grant.items() if k not in {'consumed','status'}):
                raise ValueError('Recovery authorization is immutable')
    return grant['id']


def _queue_grant_item(store, grant, item, incoming_root, destinations, ai_store, now, cursor=None):
    if (not grant or grant['status'] != 'active' or not grant['issued_at'] <= now < grant['expires_at']
            or item is None or item.owner_user_id != grant['owner_user_id']
            or str(item.id) in grant['consumed']):
        return None
    expected = next((row for row in grant['items'] if row['item_id'] == str(item.id)), None)
    if (expected is None or item.sha256 != expected['sha256'] or item.size_bytes != expected['size_bytes']):
        return None
    evidence = ai_store.list_evidence(item.id, item.owner_user_id)
    latest = max(evidence,key=lambda e:e.created_at,default=None)
    if not camera_photo_safety_eligible(item,latest,set()) or _preflight(item,incoming_root,destinations):
        return None
    verify_camera_photo(incoming_root.joinpath(*item.relative_path.split('/')))
    audit = dict(version='incident-camera-photo-v1',recovery_id=str(grant['id']),
                 owner_user_id=str(grant['owner_user_id']),threshold=80,verified_at=now.isoformat())
    queued = _queue_authorized_gallery_photo(store,item,audit,grant['requested_by'],cursor=cursor)
    if queued is not None:
        consumed = [*grant['consumed'],str(item.id)]
        status = 'completed' if len(consumed) == len(grant['items']) else 'active'
        if cursor is None:
            grant.update(consumed=consumed,status=status)
        else:
            cursor.execute('UPDATE vault_intake_recovery_authorizations SET consumed=%s,status=%s WHERE id=%s',
                           (Jsonb(consumed),status,grant['id']))
    return queued


@serialized_publication
def queue_recovery_photo(store, authorization_id, item_id, incoming_root, destinations, ai_store, *, now=None):
    now = now or datetime.now(timezone.utc)
    item = store.get_item(item_id)
    if isinstance(store, MemoryVaultMasterStore):
        grant = getattr(store,'_recovery_authorizations',{}).get(authorization_id)
        return _queue_grant_item(store,grant,item,incoming_root,destinations,ai_store,now)
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute('SELECT * FROM vault_intake_recovery_authorizations WHERE id=%s FOR UPDATE',(authorization_id,))
        return _queue_grant_item(store,cursor.fetchone(),item,incoming_root,destinations,ai_store,now,cursor)


def main():
    import argparse, json
    from pathlib import Path
    from app.vault_master import get_vault_master_store
    from app.vault_master_ingestion_ai import get_ingestion_ai_store
    from app.incoming import get_arrival_hall_path
    from app.vault_master_api import get_destination_paths
    parser=argparse.ArgumentParser(description='Explicit historical photo recovery; never enables a policy')
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--register',action='store_true')
    parser.add_argument('--recover-once',action='store_true')
    parser.add_argument('--limit',type=int,default=50)
    args=parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error('limit must be between 1 and 100')
    manifest=json.loads(args.manifest.read_text(encoding='utf-8'))
    store=get_vault_master_store()
    grant=_validated_manifest(store,manifest,datetime.now(timezone.utc))
    if args.register:
        register_recovery_authorization(store,manifest)
    queued=0
    if args.recover_once:
        for row in grant['items']:
            if queued >= args.limit:
                break
            if queue_recovery_photo(store,grant['id'],UUID(row['item_id']),get_arrival_hall_path(),
                                    get_destination_paths(),get_ingestion_ai_store()):
                queued+=1
    print(json.dumps(dict(selected=len(grant['items']),queued=queued)))


if __name__=='__main__':
    main()
