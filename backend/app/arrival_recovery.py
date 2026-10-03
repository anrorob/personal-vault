"""Owner-authorized Arrival recovery using the existing publication evidence.

No canonical file is ever written or removed here. Physical inspection uses the
commissioned manifest and the existing read-only resolver roots.
"""
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

from app.arrival_publication_coordination import VERSION, cancel, cancellation, publication_lock
from app import arrival_managed_publisher as publisher
from app.storage_placement import MANIFEST_SCHEMA, configured_slot_roots, validate_relative_path
from app.vault_master import MemoryVaultMasterStore, safely_remove_rejected_arrival_item, sha256_file

STATES = frozenset({"inventoried", "needs_review", "approved", "rejected", "moved",
    "move_failed", "move_queued", "moving", "theatre_promotion_pending",
    "duplicate_kept", "duplicate_removed", "duplicate_remove_failed", "arrival_removed"})
TERMINAL = frozenset({"moved", "duplicate_removed", "arrival_removed"})


def _message(error):
    return "Publication or staged-file evidence is unavailable. Recheck later." if isinstance(error, OSError) else str(error)


def recovery_candidate(item, store, incoming):
    # The album import card owns all grouped progress, errors and safe recovery.
    # Showing failed members here as well fragments one import into two UIs.
    from app.music_groups import item_album
    return item_album(item) is None


def _owned(store, item_id, owner):
    item = store.get_item(item_id)
    if item is None or item.source_kind != "incoming" or not owner or item.owner_user_id != owner:
        raise LookupError("Arrival item not found")
    return item


def _configured(store):
    return not isinstance(store, MemoryVaultMasterStore) or bool(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_QUEUE"))


def _evidence(item, store):
    """Fail closed even on unrelated malformed queue evidence: never guess IDs."""
    requests, receipts = [], []
    if not _configured(store):
        return requests, receipts, None
    queue, root, key = publisher._queue_root(), publisher._receipt_root(), publisher._key()
    version = queue / ".coordination-version"
    if version.is_symlink() or version.read_text().strip() != VERSION:
        raise ValueError("Recovery awaits the coordinated publisher update. Recheck after activation.")
    if queue.is_symlink() or root.is_symlink() or not queue.is_dir() or not root.is_dir():
        raise ValueError("Publication evidence is unavailable. Recheck later.")
    cancelled = cancellation(queue, item.id, key)
    if cancelled and (cancelled.get("owner_user_id") != str(item.owner_user_id) or cancelled.get("sha256") != item.sha256 or cancelled.get("size_bytes") != item.size_bytes):
        raise ValueError("Cancellation and intake identity disagree. Needs recovery.")
    for path in sorted(queue.iterdir()):
        if path.suffix not in {".json", ".request", ".processed"}:
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError("Unsafe publication request evidence")
        request = publisher.verify_request(json.loads(path.read_text()), key)
        if request is None:
            raise ValueError("Publication request evidence cannot be verified")
        if request.item_id == item.id:
            if (request.owner_user_id != item.owner_user_id or request.expected_sha256 != item.sha256
                    or request.expected_size_bytes != item.size_bytes or request.source_relative_path != item.relative_path):
                raise ValueError("Publication request and intake identity disagree")
            requests.append((path, request))
    for path in sorted(root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ValueError("Unsafe publication receipt evidence")
        receipt = publisher.verify_receipt(json.loads(path.read_text()), key)
        if receipt is None:
            raise ValueError("Publication receipt evidence cannot be verified")
        if receipt["item_id"] == str(item.id):
            receipts.append(receipt)
    return requests, receipts, cancelled


def _catalogued(store, item, destinations):
    if isinstance(store, MemoryVaultMasterStore):
        return item.id in store.arrival_managed_publications or any(store.get_catalogued_asset(p) is not None for p in destinations)
    with store._connect() as conn:
        return conn.execute("""SELECT EXISTS(SELECT 1 FROM vault_arrival_managed_publications WHERE item_id=%s)
            OR EXISTS(SELECT 1 FROM vault_files f JOIN vault_assets a ON a.id=f.asset_id
                WHERE f.vault_path=ANY(%s)
                OR (f.sha256=%s AND f.size_bytes=%s AND a.owner_user_id=%s)) AS published""",
            (item.id, list(destinations), item.sha256, item.size_bytes, item.owner_user_id)).fetchone()["published"]


def _physical_absence(destinations, store):
    if not _configured(store):
        return
    roots = configured_slot_roots()
    manifest = Path(os.getenv("PV_SECTION_MOVE_MANIFEST", os.getenv("PV_STORAGE_SLOT_MANIFEST_FILE", "/var/lib/personal-vault/metadata/storage-control/active-slots.json")))
    document = json.loads(manifest.read_text())
    slots = document.get("slots")
    if document.get("schema") != MANIFEST_SCHEMA or not isinstance(slots, dict) or not slots:
        raise ValueError("Commissioned storage cannot be verified")
    # Inspect every manifest slot, including unavailable/retired entries. An
    # absent mount is not proof that a formerly published file does not exist.
    if set(slots) != set(roots):
        raise ValueError("All commissioned storage must be available to prove non-publication")
    for destination in destinations:
        if not destination.startswith("/vault/"):
            raise ValueError("Canonical destination cannot be verified")
        relative = validate_relative_path(destination.removeprefix("/vault/"))
        for root in roots.values():
            if root.is_symlink() or not root.is_dir():
                raise ValueError("Commissioned storage is unavailable")
            candidate = root
            for part in relative.parts:
                candidate = candidate / part
                if candidate.is_symlink():
                    raise ValueError("Canonical destination is ambiguous")
            # stat failures other than absence must fail closed, not look absent.
            try:
                candidate.stat()
            except FileNotFoundError:
                pass
            else:
                raise ValueError("A canonical destination exists. Reconcile instead of removing.")
        # Retained legacy logical mount is evidence too; never a write target.
        try:
            Path(destination).lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError("A canonical destination exists. Reconcile instead of removing.")


def _source(item, incoming, *, verify_hash=True):
    root = incoming.resolve(strict=True)
    relative = validate_relative_path(item.relative_path)
    candidate = incoming.joinpath(*relative.parts)
    if Path(item.source_path) != candidate:
        raise ValueError("Staged path no longer matches the intake record")
    current = incoming
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Staged symbolic links cannot be removed")
    if not candidate.resolve(strict=False).is_relative_to(root):
        raise ValueError("Staged path is outside Arrival Hall")
    try:
        stat = candidate.stat()
    except FileNotFoundError:
        return False
    if not candidate.is_file() or stat.st_size != item.size_bytes or (verify_hash and sha256_file(candidate) != item.sha256):
        raise ValueError("Staged file identity has changed")
    return True


def inspect(item, store, incoming):
    result = {"item_id": str(item.id), "filename": item.filename, "state": item.state,
              "status": "needs_recovery", "can_remove": False, "can_retry": False,
              "message": "PV cannot safely determine whether publication completed. Recheck."}
    if item.state in TERMINAL:
        return {**result, "status": "complete", "message": "Intake is complete."}
    try:
        requests, receipts, cancelled = _evidence(item, store)
        if receipts:
            return {**result, "can_retry": True, "message": "Publication evidence is available. Reconcile this intake."}
        destinations = {r.logical_destination for _, r in requests}
        if item.proposed_destination:
            destinations.add(item.proposed_destination)
        if not destinations and item.state not in {"inventoried", "needs_review", "rejected"}:
            raise ValueError("The prior publication destination cannot be verified. Needs recovery.")
        if _catalogued(store, item, destinations):
            raise ValueError("Canonical catalogue evidence exists. Recheck publication; removal is blocked.")
        _physical_absence(destinations, store)
        # A GET must not hash an entire movie collection. Mutation repeats the
        # proof and verifies bytes before cancellation and staged unlink.
        exists = _source(item, incoming, verify_hash=False)
        return {**result, "status": "unpublished", "can_remove": True,
                "can_retry": bool(exists and not cancelled and item.state in {"approved", "move_failed", "theatre_promotion_pending"}),
                "message": "Unpublished staging can be removed safely." if not cancelled else "Publication cancelled. Finish removing this intake."}
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {**result, "message": _message(error)}


def _audit(store, item, owner, action, final, detail, succeeded=True):
    text = json.dumps({"actor_user_id": str(owner), "prior_state": item.state, "final_state": final, **detail}, sort_keys=True)
    if isinstance(store, MemoryVaultMasterStore):
        store._record_activity(action, item=item, username=str(owner), detail=text, succeeded=succeeded)
    else:
        with store._connect() as conn:
            conn.execute("INSERT INTO vault_master_activity(id,batch_id,item_id,action,username,detail,succeeded) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (uuid4(), item.batch_id, item.id, action, str(owner), text, succeeded))


def remove(store, item_id, owner, incoming):
    with publication_lock(publisher._queue_root() if _configured(store) else None):
        item = _owned(store, item_id, owner)
        if item.state == "arrival_removed":
            return item
        assessment = inspect(item, store, incoming)
        if not assessment["can_remove"]:
            _audit(store, item, owner, "arrival_remove_refused", item.state,
                   {"reason": assessment["message"], "staged_removed": False}, False)
            raise ValueError(assessment["message"])
        requests, receipts, _ = _evidence(item, store)
        if receipts:
            raise ValueError("Publication completed; reconcile instead")
        existed = _source(item, incoming)
        if _configured(store):
            queue, key = publisher._queue_root(), publisher._key()
            cancel(queue, {"version": VERSION, "item_id": str(item.id), "owner_user_id": str(owner),
                "sha256": item.sha256, "size_bytes": item.size_bytes,
                "created_at": datetime.now(UTC).isoformat(), "prior_state": item.state,
                "request_ids": [str(r.request_id) for _, r in requests], "reason": "Owner abandoned unpublished intake"}, key)
            for path, _ in requests:
                if path.suffix == ".json":
                    os.replace(path, path.with_suffix(".cancelled.request"))
        # Keep the existing strict staged-path/hash removal primitive. The
        # authoritative proof above, not an arbitrary old state allowlist,
        # authorizes later states. Never supply a canonical path to it.
        safely_remove_rejected_arrival_item(replace(item, state="rejected"), incoming)
        if isinstance(store, MemoryVaultMasterStore):
            store.items[item.source_path] = replace(item, state="arrival_removed")
        else:
            with store._connect() as conn:
                updated = conn.execute("UPDATE vault_master_items SET state='arrival_removed',updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s AND state=%s RETURNING id", (item.id, owner, item.state)).fetchone()
                if updated is None:
                    raise ValueError("Intake changed; cancellation is retained. Recheck to finish removal.")
                conn.execute("INSERT INTO vault_master_decisions(id,item_id,decision,username,detail) VALUES (%s,%s,'arrival_removed',%s,%s)",
                    (uuid4(), item.id, str(owner), json.dumps({"actor_user_id": str(owner), "prior_state": item.state,
                        "final_state": "arrival_removed", "staged_removed": existed, "canonical_publication": False,
                        "reason": "Owner abandoned verified unpublished staging; signed cancellation retained"})))
        _audit(store, item, owner, "arrival_removed", "arrival_removed", {"staged_removed": existed, "canonical_publication": False, "reason": "Owner abandoned unpublished intake"})
        return store.get_item(item.id)


def recheck(store, item_id, owner, incoming, retry=False):
    with publication_lock(publisher._queue_root() if _configured(store) else None):
        item = _owned(store, item_id, owner)
        if item.state in TERMINAL:
            return inspect(item, store, incoming)
        try:
            _, receipts, cancelled = _evidence(item, store)
            for receipt in receipts:
                store.publish_arrival_managed_receipt(item.id, receipt)
            refreshed = store.get_item(item.id)
            if refreshed.state == "moved":
                _audit(store, item, owner, "arrival_reconciled", "moved", {"canonical_publication": True, "staged_removed": False})
                return inspect(refreshed, store, incoming)
            assessment = inspect(refreshed, store, incoming)
            if retry and assessment["can_retry"] and not receipts and not cancelled:
                if item.state == "theatre_promotion_pending":
                    path = publisher.reissue_item(item, incoming)
                    request_id = str(UUID(path.stem))
                    if isinstance(store, MemoryVaultMasterStore):
                        store.items[item.source_path] = replace(item, metadata={**item.metadata, "managed_request_id": request_id})
                    else:
                        with store._connect() as conn:
                            conn.execute("UPDATE vault_master_items SET metadata=jsonb_set(metadata,'{managed_request_id}',to_jsonb(%s::text)),updated_at=CURRENT_TIMESTAMP WHERE id=%s AND state='theatre_promotion_pending'", (request_id, item.id))
                else:
                    if store.queue_move(item.id, str(owner)) is None:
                        raise ValueError("This intake is not currently eligible for safe move. Recheck its review state.")
                _audit(store, item, owner, "arrival_retry", store.get_item(item.id).state,
                       {"reason": "Owner retried managed publication", "staged_removed": False})
            elif retry:
                raise ValueError("Retry is not currently safe. Recheck publication evidence or remove verified unpublished staging.")
            else:
                _audit(store, item, owner, "arrival_recheck", refreshed.state,
                       {"reason": assessment["message"], "staged_removed": False})
            return inspect(store.get_item(item.id), store, incoming)
        except (OSError, ValueError, TypeError, KeyError) as error:
            _audit(store, item, owner, "arrival_recheck", item.state, {"reason": str(error), "staged_removed": False}, False)
            return {**inspect(item, store, incoming), "status": "needs_recovery", "message": _message(error)}
