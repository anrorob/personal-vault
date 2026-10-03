"""Atomic, owner-scoped Gallery autopilot approval with durable rule evidence."""
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
from psycopg.types.json import Jsonb
from app.arrival_publication_coordination import serialized_publication
from app.vault_master import MemoryVaultMasterStore

RESERVED_STATES = ('approved', 'move_queued', 'theatre_promotion_pending')


@serialized_publication
def queue_automatic_gallery_photo(store, item, policy, rule_version, username, *, policy_store=None):
    """Fail closed until an approved deterministic-score authority exists.

    An enabled policy, numeric policy threshold, caller rule label, or model
    score cannot authorize fresh Gallery publication. Manual decisions and
    explicit historical grants use their own approval boundary.
    """
    return None


def _queue_authorized_gallery_photo(store, item, evidence, username, *, cursor=None):
    """Internal mutation shared by policy and explicit recovery authorization.

    Caller holds publication lock and the authorizing database row lock.
    """
    if cursor is None:
        current = store.get_item(item.id)
    else:
        cursor.execute('SELECT * FROM vault_master_items WHERE id=%s FOR UPDATE', (item.id,))
        row = cursor.fetchone()
        current = store._to_item(row) if row else None
    if (current is None or current.state != 'needs_review' or current.owner_user_id != item.owner_user_id
            or current.sha256 != item.sha256 or current.size_bytes != item.size_bytes
            or current.proposed_destination != item.proposed_destination):
        return None
    if isinstance(store, MemoryVaultMasterStore):
        if any(a.owner_user_id == item.owner_user_id and a.sha256 == item.sha256
               for a in store.catalogued_assets.values()):
            return None
        if any(other.id != item.id and other.owner_user_id == item.owner_user_id
               and other.sha256 == item.sha256 and other.state in RESERVED_STATES
               for other in store.list_items()):
            return None
        current = replace(current, metadata={**current.metadata, 'arrival_publication_rule': evidence})
        store.items[current.source_path] = current
        store.record_decision(item.id, 'approved', username)
        return store.queue_move(item.id, username)
    if cursor is None:
        raise ValueError('Atomic authorization transaction is required')
    cursor.execute('SELECT * FROM vault_master_items WHERE id=%s FOR UPDATE', (item.id,))
    row = cursor.fetchone()
    if (row is None or row['state'] != 'needs_review'
            or row['owner_user_id'] != item.owner_user_id or row['sha256'] != item.sha256
            or row['size_bytes'] != item.size_bytes or row['proposed_destination'] != item.proposed_destination):
        return None
    cursor.execute('''SELECT EXISTS(SELECT 1 FROM vault_files f JOIN vault_assets a ON a.id=f.asset_id
        WHERE a.owner_user_id=%s AND f.sha256=%s) OR EXISTS(
        SELECT 1 FROM vault_master_items WHERE id<>%s AND owner_user_id=%s AND sha256=%s AND state=ANY(%s)) AS duplicate''',
        (item.owner_user_id,item.sha256,item.id,item.owner_user_id,item.sha256,list(RESERVED_STATES)))
    if cursor.fetchone()['duplicate']:
        return None
    cursor.execute('''UPDATE vault_master_items SET state='move_queued', metadata=metadata || %s,
        updated_at=CURRENT_TIMESTAMP WHERE id=%s RETURNING *''',
        (Jsonb({'arrival_publication_rule':evidence}),item.id))
    queued=cursor.fetchone()
    cursor.execute("INSERT INTO vault_master_decisions(id,item_id,decision,username) VALUES(%s,%s,'approved',%s)", (uuid4(),item.id,username))
    for action in ('proposal_approved','move_queued'):
        cursor.execute('''INSERT INTO vault_master_activity(id,batch_id,item_id,action,username,detail,succeeded)
            VALUES(%s,%s,%s,%s,%s,%s,TRUE)''',(uuid4(),item.batch_id,item.id,action,username,evidence['version']))
    return store._to_item(queued)
