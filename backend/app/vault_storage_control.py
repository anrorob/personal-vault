"""Stage 2 Vault Control storage read model and bounded request contract.

The web process never receives a block-device mount or shell authority.  A
root-owned host worker publishes the inventory snapshot and consumes signed,
fixed-schema requests from the queue mounted below metadata storage.
"""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Literal
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb
from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.config import get_database_conninfo, get_metadata_storage_root


Operation = Literal["verify", "commission", "commission_add", "retire_slot", "start_replacement", "erase", "cancel"]
TERMINAL = {"completed", "failed", "cancelled"}


class StorageOperation(BaseModel):
    operation: Operation
    target_hardware_id: str | None = None
    source_disk_id: str | None = None
    operation_id: str | None = None
    vault_area: str | None = Field(default=None, min_length=1, max_length=128)
    confirmation: str = Field(min_length=1, max_length=128)


def _swap_candidate(inventory: dict[str, object], hardware_id: str) -> dict[str, object] | None:
    """Return one host-produced, currently safe replacement candidate.

    This deliberately does not reuse the Add Drive capability flag: a Swap is
    a replacement of an existing slot, not the commissioning of a new slot.
    The root worker repeats every safety check immediately before formatting.
    """
    commissioned, paths = _commissioned_hardware(inventory)
    for value in inventory.get("unassigned_devices", []):
        if (
            _is_selectable_add_drive_candidate(value)
            and value.get("hardware_id") == hardware_id
            and hardware_id not in commissioned
            and value.get("device_path") not in paths
        ):
            return value
    return None


def _swap_snapshot(slot_id: str, disk: dict[str, object], candidate: dict[str, object]) -> dict[str, object]:
    """Freeze the catalogue placement set that the root worker may copy.

    The signed request contains only stable IDs, logical paths, safe
    slot-relative paths and checksums.  It never contains a caller supplied
    physical source or destination path.
    """
    try:
        with psycopg.connect(get_database_conninfo()) as connection:
            rows = connection.execute(
                """
                SELECT placements.file_id, files.asset_id, assets.owner_user_id,
                       files.vault_path, placements.relative_path,
                       files.sha256, files.size_bytes
                FROM vault_file_storage_placements AS placements
                JOIN vault_files AS files ON files.id = placements.file_id
                JOIN vault_assets AS assets ON assets.id = files.asset_id
                WHERE placements.slot_id = %s
                ORDER BY placements.relative_path
                """,
                (slot_id,),
            ).fetchall()
            slot_row = connection.execute("SELECT hardware FROM vault_storage_slots WHERE slot_id = %s", (slot_id,)).fetchone()
    except psycopg.Error:
        raise HTTPException(status_code=503, detail="Authoritative storage placement is unavailable") from None
    files = [
        {
            "file_id": str(row[0]), "asset_id": str(row[1]), "owner_user_id": str(row[2]),
            "logical_path": row[3], "relative_path": row[4], "sha256": row[5], "size_bytes": int(row[6]),
        }
        for row in rows
    ]
    if any(not item["owner_user_id"] or not item["sha256"] for item in files):
        raise HTTPException(status_code=409, detail="The slot has incomplete canonical ownership or checksum data")
    total = sum(int(item["size_bytes"]) for item in files)
    capacity = candidate.get("capacity_bytes")
    if not isinstance(capacity, int) or capacity <= total:
        raise HTTPException(status_code=409, detail="The replacement drive has insufficient capacity for the canonical placement set")
    filesystem_uuid = disk.get("filesystem_uuid")
    if slot_row is None or not isinstance(slot_row[0], dict):
        raise HTTPException(status_code=409, detail="The persistent slot hardware record is incomplete")
    canonical_hardware = slot_row[0]
    hardware_id = canonical_hardware.get("hardware_id")
    canonical_filesystem_uuid = canonical_hardware.get("filesystem_uuid")
    if not isinstance(hardware_id, str) or not isinstance(canonical_filesystem_uuid, str) or not isinstance(filesystem_uuid, str):
        raise HTTPException(status_code=409, detail="The current slot hardware identity is incomplete")
    if canonical_filesystem_uuid != filesystem_uuid:
        raise HTTPException(status_code=409, detail="The current slot filesystem identity does not match the canonical slot record")
    return {
        "slot_id": slot_id, "source_hardware_id": hardware_id,
        "source_filesystem_uuid": filesystem_uuid, "target": candidate,
        "old_hardware": canonical_hardware, "files": files, "canonical_bytes": total,
    }


def _control_root() -> Path:
    return get_metadata_storage_root() / "storage-control"


def _snapshot_path() -> Path:
    return _control_root() / "inventory.json"


def _operations_root() -> Path:
    return _control_root() / "operations"


def _queue_path() -> Path:
    return get_metadata_storage_root() / "storage-control-requests"


def _swap_queue_path() -> Path:
    return get_metadata_storage_root() / "storage-swap-requests"


def _swap_generation(inventory: dict[str, object]) -> str | None:
    """Return the root-published generation for an executable Swap request.

    This is an operational epoch, not caller authority: the backend can only
    use it after the root inventory publisher has explicitly confirmed the
    root executor's allowlist and current generation.
    """
    capability = inventory.get("swap_drive")
    if not isinstance(capability, dict) or capability.get("enabled") is not True:
        return None
    generation = capability.get("generation")
    return generation.strip() if isinstance(generation, str) and generation.strip() else None


def _has_live_swap_request() -> bool:
    """A request queue entry is live until the root executor records a suffix."""
    try:
        return any(_swap_queue_path().glob("*.json"))
    except OSError:
        raise HTTPException(status_code=503, detail="Storage operation queue is unavailable") from None


def _swap_ack_path(operation_id: str) -> Path:
    return get_metadata_storage_root() / "storage-swap-acks" / f"{operation_id}.json"


def _swap_integration_error_path(operation_id: str) -> Path:
    return get_metadata_storage_root() / "storage-swap-acks" / f"{operation_id}.error.json"


def _key_path() -> Path:
    return Path(os.getenv("PV_STORAGE_CONTROL_SIGNING_KEY_FILE", "/run/secrets/storage-control.key"))


def require_administrator(username: object) -> None:
    if getattr(username, "role", None) != "administrator" or not getattr(username, "active", False):
        raise HTTPException(status_code=403, detail="Vault Control administrator access is required")


def _requester_label(value: object) -> str:
    """Keep a display/audit label separate from immutable actor identity."""
    if isinstance(value, str):
        return value
    username = getattr(value, "username", None)
    return username if isinstance(username, str) else "administrator"


def get_inventory() -> dict[str, object]:
    """Return only host-produced facts; a missing snapshot is explicitly unavailable."""
    try:
        document = json.loads(_snapshot_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unavailable", "generated_at": None, "summary": None, "disks": [], "unassigned_devices": [], "verification": None}
    if not isinstance(document, dict) or document.get("schema") != "personal-vault.storage-inventory.v1":
        return {"status": "unavailable", "generated_at": None, "summary": None, "disks": [], "unassigned_devices": [], "verification": None}
    return document


def get_operations() -> list[dict[str, object]]:
    """Read only structured root-executor states; never parse terminal output."""
    operations: list[dict[str, object]] = []
    try:
        paths = sorted(_operations_root().glob("*.json"), reverse=True)
    except OSError:
        return operations
    for path in paths:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict) and value.get("schema") == "personal-vault.storage-operation.v1":
            operations.append(value)
    return sorted(operations, key=lambda item: str(item.get("updated_at", "")), reverse=True)


def _is_selectable_add_drive_candidate(value: object) -> bool:
    """Accept only a complete host-approved candidate for the destructive UI.

    The full inventory deliberately retains blocked devices for diagnostics, but
    a known-ineligible device must never reach the Add Drive selector.  The
    root executor independently repeats hardware validation before any action.
    """
    return (
        isinstance(value, dict)
        and value.get("safety") == "ready"
        and isinstance(value.get("hardware_id"), str)
        and bool(value["hardware_id"].strip())
        and isinstance(value.get("device_path"), str)
        and bool(value["device_path"].strip())
    )


def _commissioned_hardware(inventory: dict[str, object]) -> tuple[set[str], set[str]]:
    """Return hardware identities and paths already assigned to PV-DISK slots."""
    identities: set[str] = set()
    paths: set[str] = set()
    disks = inventory.get("disks", [])
    if not isinstance(disks, list):
        return identities, paths
    for disk in disks:
        if not isinstance(disk, dict):
            continue
        device_path = disk.get("device_path")
        if isinstance(device_path, str) and device_path:
            paths.add(device_path)
        wwn = disk.get("wwn")
        serial = disk.get("serial")
        if isinstance(wwn, str) and wwn.strip():
            identities.add(f"wwn:{wwn.strip()}")
        if isinstance(serial, str) and serial.strip():
            identities.add(f"serial:{serial.strip()}")
    return identities, paths


def get_add_drive_context() -> dict[str, object]:
    """Expose only host-approved candidates and configured logical areas.

    Selection is deliberately repeated by the root executor.  This response is
    a UI/preflight aid, never a destructive authority.  Blocked inventory
    entries remain in the host snapshot for diagnostics and safety evidence.
    """
    inventory = get_inventory()
    commissioned_ids, commissioned_paths = _commissioned_hardware(inventory)
    return {
        "status": inventory.get("status", "unavailable"),
        "operations_enabled": inventory.get("operations_enabled") is True,
        "eligible_areas": inventory.get("eligible_areas", []),
        "candidates": [
            item
            for item in inventory.get("unassigned_devices", [])
            if _is_selectable_add_drive_candidate(item)
            and item["hardware_id"] not in commissioned_ids
            and item["device_path"] not in commissioned_paths
        ],
        "active_operation": next(
            (item for item in get_operations() if item.get("state") not in {"completed", "failed", "cancelled"}),
            None,
        ),
    }


def get_swap_context(slot_id: str) -> dict[str, object]:
    """Expose replacement candidates for one Active lifecycle-eligible slot."""
    inventory = get_inventory()
    disk = next((item for item in inventory.get("disks", []) if isinstance(item, dict) and item.get("id") == slot_id), None)
    if not isinstance(disk, dict) or disk.get("state") != "Active" or disk.get("lifecycle_eligible") is not True:
        raise HTTPException(status_code=409, detail="This commissioned slot is not eligible for Swap Drive")
    commissioned, paths = _commissioned_hardware(inventory)
    candidates = [
        item for item in inventory.get("unassigned_devices", [])
        if _is_selectable_add_drive_candidate(item)
        and item["hardware_id"] not in commissioned and item["device_path"] not in paths
    ]
    return {
        "slot_id": slot_id,
        "swap_enabled": _swap_generation(inventory) is not None,
        "candidates": candidates,
        "active_operation": next((item for item in get_operations() if item.get("state") not in TERMINAL), None),
    }


def get_retire_preflight(slot_id: str) -> dict[str, object]:
    """Return authoritative placement evidence before a retirement confirmation.

    A filesystem directory is deliberately not the authority: active durable
    placement rows block retirement even when a directory happens to be empty.
    """
    inventory = get_inventory()
    disk = next((item for item in inventory.get("disks", []) if isinstance(item, dict) and item.get("id") == slot_id), None)
    if not isinstance(disk, dict) or disk.get("state") != "Active":
        raise HTTPException(status_code=404, detail="The active commissioned slot is unavailable")
    if disk.get("lifecycle_eligible") is not True:
        raise HTTPException(
            status_code=409,
            detail="This legacy direct slot is operational but is not eligible for managed lifecycle operations. Migrate it explicitly first.",
        )
    if disk.get("production_lifecycle_eligible") is False:
        raise HTTPException(
            status_code=409,
            detail="This slot is reconciled for managed inventory, but its production retirement executor is not implemented.",
        )
    if next((item for item in get_operations() if item.get("state") not in {"completed", "failed", "cancelled"}), None):
        raise HTTPException(status_code=409, detail="A storage operation is already active")
    try:
        with psycopg.connect(get_database_conninfo()) as connection:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(vault_files.size_bytes), 0) FROM vault_file_storage_placements JOIN vault_files ON vault_files.id = vault_file_storage_placements.file_id WHERE slot_id = %s",
                (slot_id,),
            ).fetchone()
    except psycopg.Error:
        raise HTTPException(status_code=503, detail="Authoritative storage placement is unavailable") from None
    assert row is not None
    count, total = int(row[0]), int(row[1])
    return {"slot_id": slot_id, "state": "eligible" if count == 0 else "blocked", "areas": disk.get("areas", []), "hardware_id": f"wwn:{disk['wwn']}" if disk.get("wwn") else f"serial:{disk['serial']}" if disk.get("serial") else None, "filesystem_uuid": disk.get("filesystem_uuid"), "canonical_file_count": count, "canonical_bytes": total, "mounted": disk.get("mounted") is True}


def _reconcile_operation(operation_id: str) -> dict[str, object]:
    operation = next((item for item in get_operations() if item.get("operation_id") == operation_id), None)
    if not isinstance(operation, dict) or operation.get("state") not in {"integrating", "completed"}:
        raise HTTPException(status_code=409, detail="The storage operation is not ready for integration")
    receipt = operation.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("slot_id"), str):
        raise HTTPException(status_code=409, detail="The completed operation has no integration receipt")
    pending = receipt.get("integration_pending")
    if pending not in {"activate", "retire", "swap"}:
        return {"status": "already_reconciled"}
    from app.storage_slot_integrations import SlotIntegrationError, reconcile_movies_slot
    from app.vault_master_jellyfin import get_jellyfin_metadata_client
    try:
        result = reconcile_movies_slot(get_jellyfin_metadata_client(), receipt["slot_id"], activate=pending != "retire")
    except SlotIntegrationError as error:
        _persist_slot_integration_error(str(operation_id), error)
        raise HTTPException(status_code=503, detail=f"Slot integration reconciliation failed: {error}") from None
    except Exception as error:
        raise HTTPException(status_code=503, detail=f"Slot integration reconciliation failed: {error}") from None
    if pending == "swap":
        receipt_sha256 = _record_swap_hardware(receipt, operation.get("receipt_signature"))
        acknowledgement = _signed_backend_ack(str(operation_id), receipt_sha256)
        path = _swap_ack_path(str(operation_id)); path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp"); temporary.write_text(json.dumps(acknowledgement, sort_keys=True), encoding="utf-8"); os.replace(temporary, path)
        return {"status": "swap_acknowledged", **result}
    else:
        operation["state"] = "completed"
    receipt.pop("integration_pending", None); operation["receipt"] = receipt; operation["updated_at"] = datetime.now(UTC).isoformat()
    lifecycle = operation.get("lifecycle")
    if isinstance(lifecycle, dict):
        lifecycle.update({"Updating Jellyfin": "complete", "Updating backup": "complete", "Releasing drive": "complete", "Verifying": "complete", "Complete": "complete"})
    path = _operations_root() / f"{operation_id}.json"
    temporary = path.with_suffix(".tmp"); temporary.write_text(json.dumps(operation, sort_keys=True), encoding="utf-8"); os.replace(temporary, path)
    return {"status": "reconciled", **result}


def reconcile_completed_operation(operation_id: str, username: object) -> dict[str, object]:
    require_administrator(username)
    return _reconcile_operation(operation_id)


def reconcile_pending_slot_integrations() -> int:
    """Complete only root-created integration-pending slot operations.

    Block-device and mount decisions remain root-owned.  This worker owns only
    the credential-bearing Jellyfin API call and leaves a durable receipt that
    the root inventory publisher must observe before declaring a slot Active.
    """
    reconciled = 0
    for operation in get_operations():
        receipt = operation.get("receipt") if isinstance(operation, dict) else None
        operation_id = operation.get("operation_id") if isinstance(operation, dict) else None
        if operation.get("state") == "integrating" and isinstance(receipt, dict) and receipt.get("integration_pending") in {"activate", "retire", "swap"} and isinstance(operation_id, str):
            _reconcile_operation(operation_id)
            reconciled += 1
    return reconciled


def _canonical(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _persist_slot_integration_error(operation_id: str, error: object) -> None:
    """Persist signed predicate evidence without changing operation authority."""
    evidence = getattr(error, "evidence", None)
    if not isinstance(evidence, dict):
        evidence = {"stage": "slot_integration", "predicate": "unclassified"}
    try:
        key = _key_path().read_bytes()
        payload: dict[str, object] = {
            "schema": "personal-vault.storage-slot-integration-error.v1",
            "operation_id": operation_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "error": str(error),
            "evidence": evidence,
        }
        document = {**payload, "signature": hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()}
        path = _swap_integration_error_path(operation_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        raise HTTPException(status_code=503, detail=f"Slot integration reconciliation failed: {error}; diagnostic evidence could not be persisted") from None


def _signed_backend_ack(operation_id: str, receipt_sha256: str) -> dict[str, str]:
    """Create the only backend-to-root acknowledgement accepted by Swap.

    The shared root-owned key already protects the request queue.  The
    acknowledgement contains no device/path authority; it only allows the
    root executor to mark its own verified receipt complete.
    """
    try:
        key = _key_path().read_bytes()
    except OSError:
        raise HTTPException(status_code=503, detail="Storage operation service is unavailable") from None
    payload = {"operation_id": operation_id, "receipt_sha256": receipt_sha256, "acknowledged_at": datetime.now(UTC).isoformat()}
    return {**payload, "signature": hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()}


def _validate_swap_placement_snapshot(cursor: psycopg.Cursor[object], slot_id: str, files: object) -> None:
    """Require the root-signed canonical placement set to remain unchanged."""
    if not isinstance(files, list):
        raise HTTPException(status_code=409, detail="The replacement receipt placement snapshot is invalid")
    expected: set[tuple[str, str]] = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("file_id"), str) or not isinstance(item.get("relative_path"), str):
            raise HTTPException(status_code=409, detail="The replacement receipt placement snapshot is invalid")
        pair = (item["file_id"], item["relative_path"])
        if pair in expected:
            raise HTTPException(status_code=409, detail="The replacement receipt placement snapshot is invalid")
        expected.add(pair)
    current = {
        (str(file_id), relative_path)
        for file_id, relative_path in cursor.execute(
            "SELECT file_id, relative_path FROM vault_file_storage_placements WHERE slot_id = %s FOR UPDATE",
            (slot_id,),
        ).fetchall()
    }
    if current != expected:
        raise HTTPException(status_code=409, detail="Canonical storage placement changed before replacement receipt integration")


def _record_swap_hardware(receipt: dict[str, object], signature: object) -> str:
    """Atomically preserve slot identity and append verified hardware history."""
    required = {"operation_id", "slot_id", "old_hardware", "new_hardware", "files", "reboot_verified", "safe_to_disconnect"}
    if not required <= set(receipt) or receipt.get("reboot_verified") is not True or receipt.get("safe_to_disconnect") is not False:
        raise HTTPException(status_code=409, detail="The replacement receipt is not ready for final integration")
    try:
        key = _key_path().read_bytes()
    except OSError:
        raise HTTPException(status_code=503, detail="Storage operation service is unavailable") from None
    if not isinstance(signature, str) or not hmac.compare_digest(hmac.new(key, _canonical(receipt), hashlib.sha256).hexdigest(), signature):
        raise HTTPException(status_code=409, detail="The replacement receipt signature is invalid")
    operation_id, slot_id = receipt["operation_id"], receipt["slot_id"]
    if not isinstance(operation_id, str) or not isinstance(slot_id, str):
        raise HTTPException(status_code=409, detail="The replacement receipt identity is invalid")
    receipt_sha256 = hashlib.sha256(_canonical(receipt)).hexdigest()
    try:
        with psycopg.connect(get_database_conninfo()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT hardware FROM vault_storage_slots WHERE slot_id = %s FOR UPDATE", (slot_id,))
            row = cursor.fetchone()
            if row is None:
                raise HTTPException(status_code=409, detail="The persistent storage slot is absent")
            _validate_swap_placement_snapshot(cursor, slot_id, receipt["files"])
            cursor.execute("SELECT receipt_sha256 FROM vault_storage_slot_hardware_history WHERE operation_id = %s", (operation_id,))
            prior = cursor.fetchone()
            if prior is not None:
                if prior[0] != receipt_sha256:
                    raise HTTPException(status_code=409, detail="The replacement operation already has different receipt evidence")
                return receipt_sha256
            if not isinstance(row[0], dict) or not isinstance(receipt["old_hardware"], dict) or any(row[0].get(key) != receipt["old_hardware"].get(key) for key in row[0]):
                raise HTTPException(status_code=409, detail="The slot hardware changed before replacement receipt integration")
            cursor.execute("UPDATE vault_storage_slots SET hardware = %s WHERE slot_id = %s", (Jsonb(receipt["new_hardware"]), slot_id))
            cursor.execute(
                "INSERT INTO vault_storage_slot_hardware_history (id, slot_id, operation_id, previous_hardware, replacement_hardware, receipt_sha256) VALUES (%s, %s, %s, %s, %s, %s)",
                (str(uuid4()), slot_id, operation_id, Jsonb(receipt["old_hardware"]), Jsonb(receipt["new_hardware"]), receipt_sha256),
            )
    except psycopg.Error:
        raise HTTPException(status_code=503, detail="Storage hardware history is unavailable") from None
    return receipt_sha256


def queue_operation(operation: StorageOperation, username: str) -> dict[str, str]:
    """Atomically queue a signed allowlisted request; never execute a command."""
    require_administrator(username)
    expected_confirmation = {
        "commission": "COMMISSION",
        "commission_add": "PREPARE DRIVE",
        "retire_slot": "RETIRE DRIVE",
        "start_replacement": operation.source_disk_id,
        "erase": operation.source_disk_id,
    }.get(operation.operation)
    if expected_confirmation and operation.confirmation != expected_confirmation:
        raise HTTPException(
            status_code=422,
            detail=f"Type {expected_confirmation} to confirm this storage operation",
        )
    if operation.operation in {"commission", "commission_add", "start_replacement", "erase"} and not operation.target_hardware_id:
        raise HTTPException(status_code=422, detail="A stable physical hardware identity is required")
    candidate: dict[str, object] | None = None
    retirement: dict[str, object] | None = None
    if operation.operation == "commission_add":
        if get_inventory().get("operations_enabled") is not True:
            raise HTTPException(status_code=503, detail="Add Drive commissioning is not enabled on this Vault")
        if not operation.vault_area:
            raise HTTPException(status_code=422, detail="Choose the logical Vault area this slot will serve")
        context = get_add_drive_context()
        if operation.vault_area not in context["eligible_areas"]:
            raise HTTPException(status_code=422, detail="The selected Vault area is not eligible")
        candidate = next(
            (item for item in context["candidates"] if isinstance(item, dict) and item.get("hardware_id") == operation.target_hardware_id),
            None,
        )
        if not candidate or candidate.get("safety") != "ready":
            raise HTTPException(status_code=422, detail="The selected drive is not ready for commissioning")
    if operation.operation == "retire_slot":
        if not operation.source_disk_id:
            raise HTTPException(status_code=422, detail="An active commissioned slot is required")
        retirement = get_retire_preflight(operation.source_disk_id)
        if retirement["state"] != "eligible":
            raise HTTPException(status_code=409, detail="This slot contains canonical Vault content and cannot be retired")
    if operation.operation == "start_replacement" and not operation.source_disk_id:
        raise HTTPException(status_code=422, detail="A Vault disk is required")
    swap: dict[str, object] | None = None
    swap_generation: str | None = None
    if operation.operation == "start_replacement":
        inventory = get_inventory()
        disk = next(
            (item for item in inventory.get("disks", []) if isinstance(item, dict) and item.get("id") == operation.source_disk_id),
            None,
        )
        if not isinstance(disk, dict) or disk.get("lifecycle_eligible") is not True:
            raise HTTPException(
                status_code=409,
                detail="This slot is not eligible for managed replacement. Legacy direct topology requires a separately approved migration.",
        )
        if disk.get("state") != "Active":
            raise HTTPException(status_code=409, detail="The source slot is not Active")
        swap_generation = _swap_generation(inventory)
        if swap_generation is None:
            raise HTTPException(status_code=503, detail="Swap Drive is not operationally enabled on this Vault")
        if _has_live_swap_request():
            raise HTTPException(status_code=409, detail="A Swap Drive request is already awaiting root execution")
        candidate = _swap_candidate(inventory, operation.target_hardware_id or "")
        if candidate is None:
            raise HTTPException(status_code=422, detail="The selected replacement drive is no longer safe and available")
        swap = _swap_snapshot(operation.source_disk_id, disk, candidate)
    if operation.operation == "cancel" and not operation.operation_id:
        raise HTTPException(status_code=422, detail="A storage operation is required")
    try:
        key = _key_path().read_bytes()
    except OSError:
        raise HTTPException(status_code=503, detail="Storage operation service is unavailable") from None
    request_id = str(uuid4())
    payload: dict[str, object] = {
        "schema": "personal-vault.storage-request.v1", "request_id": request_id,
        "operation": operation.operation, "target_hardware_id": operation.target_hardware_id,
        "source_disk_id": operation.source_disk_id, "operation_id": operation.operation_id, "requested_by": _requester_label(username),
        "created_at": datetime.now(UTC).isoformat(),
    }
    if operation.operation == "commission_add":
        payload["vault_area"] = operation.vault_area
        payload["candidate"] = candidate
    if operation.operation == "retire_slot":
        payload["retirement"] = retirement
        payload["requested_by_user_id"] = str(getattr(username, "user_id", ""))
    if operation.operation == "start_replacement":
        assert swap is not None
        payload["swap"] = swap
    if operation.operation == "start_replacement":
        assert swap_generation is not None
        payload["swap_drive_generation"] = swap_generation
    document = {"request": payload, "signature": hmac.new(key, _canonical(payload), hashlib.sha256).hexdigest()}
    queue = _swap_queue_path() if operation.operation == "start_replacement" else _queue_path()
    try:
        queue.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = queue / f"{request_id}.json"
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(document))
            stream.flush(); os.fsync(stream.fileno())
    except OSError:
        raise HTTPException(status_code=503, detail="Storage operation queue is unavailable") from None
    return {"request_id": request_id, "status": "queued"}
