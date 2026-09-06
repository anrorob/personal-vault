"""Explicit operator-only recovery through existing managed publication receipts.

No endpoint, scheduler, raw file operation or arbitrary SQL repair entrypoint.
The normal backend stays stopped until the reviewed recovery is complete.
"""
from dataclasses import asdict, replace
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.arrival_managed_publisher import ArrivalManagedPublicationRequest, ResolverRelocationRequest
from app.tv_publication_authority import approved_projection

RELOCATION_SCHEMA = "personal-vault.tv-resolver-relocation.v1"


def restore_approved_batch_projection(store, batch_id, owner_user_id, expected_states):
    """Restore approved review projections only after an exact inventory review.

    expected_states maps every episode UUID to its audited state. Extras are
    never updated. Durable approval, checksum and ownership are revalidated.
    """
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM vault_tv_resolver_batches WHERE id=%s AND owner_user_id=%s FOR UPDATE", (batch_id, owner_user_id))
        batch = cursor.fetchone()
        if batch is None or batch["status"] not in {"publishing", "failed"}:
            raise ValueError("Recovery requires an approved unfinished batch")
        cursor.execute("""SELECT item.* FROM vault_master_items item JOIN vault_tv_resolver_tracks track
            ON track.arrival_item_id=item.id WHERE track.batch_id=%s AND track.classification='likely_episode'
            ORDER BY item.id FOR UPDATE OF item""", (batch_id,))
        items = [store._to_item(row) for row in cursor.fetchall()]
        if {item.id: item.state for item in items} != expected_states:
            raise ValueError("Recovery inventory differs from reviewed episode states")
        projected = []
        for item in items:
            if item.state not in {"moved", "theatre_promotion_pending", "move_queued"}:
                raise ValueError("Recovery episode state is unsupported")
            projection = approved_projection(cursor, item)
            if projection is None or projection["metadata"]["tv_resolver_batch_id"] != str(batch_id):
                raise ValueError("Recovery approval is missing")
            metadata = projection["metadata"]
            metadata.setdefault("tv_resolver_recovery_original", {
                "state": item.state, "category": item.proposed_category,
                "destination": item.proposed_destination,
            })
            projected.append((item, projection))
        for item, projection in projected:
            cursor.execute("""UPDATE vault_master_items SET proposed_category='TV Shows', proposed_destination=%s,
                publication_audience=%s, metadata=%s, updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                (projection["proposed_destination"], projection["publication_audience"], Jsonb(projection["metadata"]), item.id))
            cursor.execute("""INSERT INTO vault_master_activity(id,batch_id,item_id,action,username,detail,succeeded)
                SELECT %s,%s,%s,'tv_resolver_authority_restored','TV resolver recovery',%s,TRUE
                WHERE NOT EXISTS (SELECT 1 FROM vault_master_activity WHERE item_id=%s AND action='tv_resolver_authority_restored')""",
                (uuid4(), item.batch_id, item.id, f"Restored approved mapping from resolver batch {batch_id}", item.id))


def relocation_request(store, item_id):
    """Create a signed-request value from catalogue identity and durable approval.

    Calling this does not enqueue it. The operator must inspect requests before
    submitting them to the existing privileged managed-storage executor.
    """
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM vault_master_items WHERE id=%s", (item_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Recovery item is missing")
        item = store._to_item(row)
        projection = approved_projection(cursor, item)
        original = item.metadata.get("tv_resolver_recovery_original", {})
        source = original.get("destination", "")
        if (projection is None or item.state != "moved" or original.get("category") != "Home Videos"
            or not source.startswith("/vault/Home Videos/")):
            raise ValueError("Recovery requires a reviewed wrong Home Videos publication")
        cursor.execute("""SELECT file.id,file.asset_id,file.sha256,file.size_bytes,asset.owner_user_id,
            asset.lifecycle_state,asset.asset_type FROM vault_files file JOIN vault_assets asset ON asset.id=file.asset_id
            WHERE file.vault_path=%s AND file.file_role='primary'""", (source,))
        file = cursor.fetchone()
        if (file is None or file["owner_user_id"] != item.owner_user_id or file["sha256"] != item.sha256
            or file["size_bytes"] != item.size_bytes or file["asset_type"] != "Home Videos" or file["lifecycle_state"] != "active"):
            raise ValueError("Recovery source catalogue evidence disagrees")
        cursor.execute("SELECT id,sha256 FROM vault_files WHERE vault_path=%s", (projection["proposed_destination"],))
        if cursor.fetchone() is not None:
            raise ValueError("Recovery destination already has canonical identity; review duplicate")
        request = ArrivalManagedPublicationRequest.create(item=replace(item, **projection))
        recovery = {"schema": RELOCATION_SCHEMA, "source_logical_path": source,
            "asset_id": str(file["asset_id"]), "file_id": str(file["id"]),
            "batch_id": projection["metadata"]["tv_resolver_batch_id"],
            "track_id": projection["metadata"]["tv_resolver_publication"]["track_id"]}
        return ResolverRelocationRequest(**{**asdict(request), "source_relative_path": source.removeprefix("/vault/")}, recovery=recovery)


def reconcile_relocation(store, receipt):
    """Consume an authenticated root receipt, preserving original asset/file IDs."""
    recovery = receipt.get("recovery")
    if not isinstance(recovery, dict) or set(recovery) != {"schema", "source_logical_path", "asset_id", "file_id", "batch_id", "track_id"} or recovery["schema"] != RELOCATION_SCHEMA:
        raise ValueError("Invalid resolver relocation receipt")
    item_id, asset_id, file_id = UUID(receipt["item_id"]), UUID(recovery["asset_id"]), UUID(recovery["file_id"])
    source, destination = recovery["source_logical_path"], receipt["logical_destination"]
    if not source.startswith("/vault/Home Videos/") or not destination.startswith("/vault/Theatre/TV Shows/"):
        raise ValueError("Invalid resolver relocation areas")
    changed = False
    with store._connect() as connection, connection.cursor() as cursor:
        cursor.execute("SELECT * FROM vault_master_items WHERE id=%s FOR UPDATE", (item_id,))
        row = cursor.fetchone()
        if row is None:
            raise ValueError("Recovery item is missing")
        item = store._to_item(row)
        projection = approved_projection(cursor, item)
        if projection is None:
            raise ValueError("Recovery approval is missing")
        marker = projection["metadata"]["tv_publication_set"]
        authority = projection["metadata"]["tv_resolver_publication"]
        if (projection["proposed_destination"] != destination
            or projection["metadata"]["tv_resolver_batch_id"] != recovery["batch_id"]
            or authority["track_id"] != recovery["track_id"]
            or item.metadata.get("tv_resolver_recovery_original", {}).get("destination") != source
            or receipt.get("owner_user_id") != str(item.owner_user_id)
            or receipt.get("expected_sha256") != item.sha256 or receipt.get("expected_size_bytes") != item.size_bytes
            or receipt.get("logical_area") != "Theatre / TV Shows"
            or receipt.get("relative_path") != destination.removeprefix("/vault/")
            or not isinstance(receipt.get("slot_id"), str)):
            raise ValueError("Recovery receipt does not match approved evidence")
        cursor.execute("""SELECT file.vault_path,file.sha256,file.size_bytes,asset.owner_user_id,
            asset.lifecycle_state,asset.asset_type FROM vault_files file JOIN vault_assets asset ON asset.id=file.asset_id
            WHERE file.id=%s AND file.asset_id=%s FOR UPDATE OF file,asset""", (file_id, asset_id))
        file = cursor.fetchone()
        if (file is None or file["sha256"] != item.sha256 or file["size_bytes"] != item.size_bytes
            or file["owner_user_id"] != item.owner_user_id or file["lifecycle_state"] != "active"):
            raise ValueError("Recovery canonical identity disagrees")
        cursor.execute("SELECT * FROM vault_arrival_managed_publications WHERE item_id=%s", (item_id,))
        published = cursor.fetchone()
        if file["vault_path"] == destination:
            if (published is None or str(published["request_id"]) != receipt["request_id"]
                or published["asset_id"] != asset_id or published["file_id"] != file_id):
                raise ValueError("Recovery destination has conflicting publication identity")
        else:
            if file["vault_path"] != source or file["asset_type"] != "Home Videos" or published is not None:
                raise ValueError("Recovery source changed")
            cursor.execute("SELECT 1 FROM vault_files WHERE vault_path=%s", (destination,))
            if cursor.fetchone() is not None:
                raise ValueError("Recovery destination collision")
            cursor.execute("SELECT 1 FROM vault_file_storage_placements WHERE file_id=%s", (file_id,))
            if cursor.fetchone() is not None:
                raise ValueError("Recovery source already has managed placement; review is required")
            placement = {"slot_id": receipt["slot_id"], "relative_path": receipt["relative_path"]}
            cursor.execute("INSERT INTO vault_storage_slots(slot_id,state,assigned_areas) VALUES (%s,'active',%s) ON CONFLICT DO NOTHING", (receipt["slot_id"], Jsonb(["Theatre / TV Shows"])))
            cursor.execute("UPDATE vault_files SET vault_path=%s,filename=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (destination, PurePosixPath(destination).name, file_id))
            cursor.execute("""INSERT INTO vault_file_storage_placements(file_id,slot_id,relative_path,assigned_by,placement_reason)
                VALUES (%s,%s,%s,'TV resolver managed recovery','root-verified relocation receipt')""", (file_id, receipt["slot_id"], receipt["relative_path"]))
            cursor.execute("""UPDATE vault_assets SET asset_type='TV Shows',visibility=%s,
                metadata=metadata || %s, effective_metadata=effective_metadata || %s,
                metadata_provenance=metadata_provenance || %s,updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                (projection["publication_audience"], Jsonb({"storage_placement": placement}), Jsonb({"storage_placement": placement}), Jsonb({"storage_placement": "root_verified_receipt"}), asset_id))
            cursor.execute("""INSERT INTO vault_arrival_managed_publications(item_id,request_id,owner_user_id,logical_destination,logical_area,asset_id,file_id,slot_id,relative_path)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (item_id, receipt["request_id"], item.owner_user_id, destination, receipt["logical_area"], asset_id, file_id, receipt["slot_id"], receipt["relative_path"]))
            cursor.execute("""INSERT INTO vault_asset_history(id,asset_id,action,username,previous_values,current_values)
                VALUES (%s,%s,'tv_resolver_relocated','TV resolver recovery',%s,%s)""",
                (uuid4(), asset_id, Jsonb({"vault_path": source, "asset_type": "Home Videos", "sha256": item.sha256}), Jsonb({"vault_path": destination, "asset_type": "TV Shows", "sha256": item.sha256, "request_id": receipt["request_id"], "resolver_batch_id": recovery["batch_id"]})))
            cursor.execute("""UPDATE vault_master_items SET state='moved',proposed_category='TV Shows',proposed_destination=%s,
                metadata=%s,publication_audience=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s""",
                (destination, Jsonb(projection["metadata"]), projection["publication_audience"], item_id))
            changed = True
    asset = store.get_catalogued_asset_by_id(asset_id)
    if asset is not None:
        store._export_sidecar(asset)
    # Also repair a crash between receipt consumption and grouped publication.
    store._publish_completed_tv_set(marker, item.owner_user_id)
    return asset if changed else None
