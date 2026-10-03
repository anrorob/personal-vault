"""Album review coordinates existing intake rows and managed publication."""
from dataclasses import replace
from uuid import uuid4
from psycopg.types.json import Jsonb

from app.arrival_publication_coordination import serialized_publication
from app.music_groups import item_album


def album_items(items, owner, album_id):
    return [item for item in items if item.source_kind == "incoming"
            and item.owner_user_id == owner and (album := item_album(item)) is not None
            and album.id == album_id]


def validate_selection(items, expected_ids):
    if not items or len(expected_ids) != len(set(expected_ids)) or set(expected_ids) != {item.id for item in items}:
        raise ValueError("The received album tracks changed. Refresh and review the complete import.")
    progressed = {"move_queued", "moving", "theatre_promotion_pending", "moved"}
    if all(item.state in progressed for item in items):
        return False  # Repeated approval cannot duplicate publication.
    if any(item.state != "needs_review" or item.duplicate_of_id for item in items):
        raise ValueError("The album needs attention before it can be published together.")
    destinations = [item.proposed_destination for item in items]
    if not all(destinations) or len(set(destinations)) != len(destinations):
        raise ValueError("Album destinations conflict; no tracks were queued.")
    for item in items:
        album = item_album(item)
        if not item.proposed_destination.startswith(f"/vault/Music/Albums/{album.id}/"):
            raise ValueError("Album destination does not match its canonical group.")
    return True


@serialized_publication
def approve_import(store, owner, album_id, expected_ids):
    """One owner decision, atomic queue transition, unchanged per-file executor."""
    from app.vault_master import MemoryVaultMasterStore
    if not owner:
        raise ValueError("An immutable owner is required")
    album = store.get_music_album(album_id)
    if album is None or album.owner_user_id != owner:
        raise LookupError("Music import not found")
    if isinstance(store, MemoryVaultMasterStore):
        items = album_items(store.list_items(), owner, album_id)
        if not validate_selection(items, expected_ids):
            return items
        for item in items:
            store.items[item.source_path] = replace(item, state="move_queued")
            store._record_activity("music_import_approved", item=item, username=str(owner), detail=str(album_id))
        return [store.get_item(item.id) for item in items]
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT id FROM vault_music_albums WHERE id=%s AND owner_user_id=%s FOR UPDATE", (album_id, owner))
        if cursor.fetchone() is None:
            raise LookupError("Music import not found")
        cursor.execute("SELECT * FROM vault_master_items WHERE source_kind='incoming' AND owner_user_id=%s FOR UPDATE", (owner,))
        items = album_items([store._to_item(row) for row in cursor.fetchall()], owner, album_id)
        if not validate_selection(items, expected_ids):
            return items
        for item in items:
            cursor.execute("UPDATE vault_master_items SET state='move_queued',updated_at=CURRENT_TIMESTAMP WHERE id=%s", (item.id,))
            cursor.execute("INSERT INTO vault_master_decisions(id,item_id,decision,username) VALUES(%s,%s,'approved',%s)", (uuid4(), item.id, str(owner)))
            cursor.execute("INSERT INTO vault_master_activity(id,batch_id,item_id,action,username,detail) VALUES(%s,%s,%s,'music_import_approved',%s,%s)", (uuid4(), item.batch_id, item.id, str(owner), str(album_id)))
        cursor.execute("INSERT INTO vault_music_album_history(album_id,actor_user_id,action,details) VALUES(%s,%s,'import_approved',%s)", (album_id, owner, Jsonb({"item_ids": [str(item.id) for item in items]})))
    return [store.get_item(item.id) for item in items]


@serialized_publication
def reconcile_manual_import(store, owner, installation, source_id, expected_ids, incoming):
    """Explicit staged-only backfill. Never infer imports or auto-publish them.

    The caller supplies an audited exact intake UUID set. Database changes are
    atomic; the staging provenance mirror is retryable from transfer records.
    Run with intake quiesced so no other process rewrites that mirror.
    """
    from dataclasses import fields
    from pathlib import Path
    from app.arrival_recovery import _evidence, _source, inspect
    from app.incoming import get_arrival_hall_file_owner, record_arrival_hall_file_source_context
    from app.music_groups import supplier_manual_album, declared_album, ensure_album
    from app.vault_master import ScannedFile, create_deterministic_proposal
    plans = []
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT i.*, t.transfer_id, t.installation_id,
            t.source_context AS transfer_context, t.state AS transfer_state,
            t.expected_sha256 AS transfer_hash, t.total_size AS transfer_size
            FROM vault_master_items i JOIN vault_supplier_transfer_sessions t
              ON t.arrival_hall_filename=i.relative_path AND t.user_id=i.owner_user_id
            WHERE t.user_id=%s AND t.installation_id=%s AND t.source_context->>'source_id'=%s
              AND i.source_kind='incoming' FOR UPDATE OF i,t""", (owner, installation, str(source_id)))
        rows = cursor.fetchall()
        if not rows or len(expected_ids) != len(set(expected_ids)) or {row['id'] for row in rows} != set(expected_ids):
            raise ValueError("The exact audited import membership no longer matches")
        for row in rows:
            item = store._to_item(row)
            if row['transfer_hash'] != item.sha256 or row['transfer_size'] != item.size_bytes or row['transfer_context'].get('source_kind') != 'manual_upload':
                raise ValueError("Supplier and staged file identity disagree")
            if item.state != 'needs_review' or row['transfer_state'] != 'finalized' or item.duplicate_of_id or item.proposal_reason == 'Category selected by the user.':
                raise ValueError("Only unchanged unpublished review items can be reconciled")
            requests, receipts, cancelled = _evidence(item, store)
            if requests or receipts or cancelled or not inspect(item, store, incoming)['can_remove']:
                raise ValueError("Publication evidence prevents automatic grouping reconciliation")
            if not _source(item, incoming) or get_arrival_hall_file_owner(incoming, Path(item.source_path)) != str(owner):
                raise ValueError("Staging identity or ownership cannot be verified")
            context = supplier_manual_album(row['transfer_context'], item.mime_type, installation)
            if 'music_album' not in context:
                raise ValueError("This is not a declared Manual Upload folder")
            album = declared_album(owner, context['music_album'])
            metadata = {**item.metadata, 'source_context': {**context, 'transfer_id': str(row['transfer_id'])}}
            scanned = ScannedFile(**{field.name: metadata if field.name == 'metadata' else getattr(item, field.name)
                                     for field in fields(ScannedFile)})
            proposal = create_deterministic_proposal(scanned)
            plans.append((item, row['transfer_id'], context, album, metadata, proposal))
        if len({plan[3].id for plan in plans}) != 1:
            raise ValueError("The audited files do not share one safe album boundary")
        for item, transfer_id, context, album, metadata, proposal in plans:
            ensure_album(cursor, album)
            cursor.execute("UPDATE vault_supplier_transfer_sessions SET source_context=%s WHERE transfer_id=%s", (Jsonb(context), transfer_id))
            if metadata != item.metadata or proposal[1] != item.proposed_destination:
                cursor.execute("""UPDATE vault_master_items SET metadata=%s,proposed_category=%s,
                    proposed_destination=%s,proposal_reason=%s,proposal_confidence=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                    (Jsonb(metadata), *proposal, item.id))
                cursor.execute("INSERT INTO vault_master_activity(id,batch_id,item_id,action,username,detail) VALUES(%s,%s,%s,'music_import_reconciled',%s,%s)",
                    (uuid4(), item.batch_id, item.id, str(owner), str(album.id)))
    for item, transfer_id, context, album, _, _ in plans:
        record_arrival_hall_file_source_context(incoming, Path(item.source_path), owner, context, transfer_id)
    return {'album_group_id': str(plans[0][3].id), 'item_count': len(plans), 'published': False}
