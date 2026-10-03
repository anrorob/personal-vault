"""Signed request/receipt contract for root-managed Supplier remediation.

The backend can queue an exact same-slot rename but never receives writable
storage.  The Development root worker consumes this contract using the existing
Arrival Hall publisher key and operation-root mount.
"""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
from uuid import UUID

from app.arrival_managed_publisher import _payload
from app.storage_placement import validate_relative_path

SCHEMA = "personal-vault.supplier-filename-remediation-storage.v1"


def _root(name: str, default: str) -> Path:
    return Path(os.getenv(name, default))


def queue_root() -> Path:
    return _root("PV_SUPPLIER_REMEDIATION_QUEUE", "/var/lib/personal-vault/supplier-remediation-requests")


def receipt_root() -> Path:
    return _root("PV_SUPPLIER_REMEDIATION_RECEIPTS", "/var/lib/personal-vault/supplier-remediation-receipts")


def key() -> bytes:
    return Path(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH", "/run/secrets/arrival-managed-publisher.key")).read_bytes()


def request_from_snapshot(action_id: UUID, snapshot: dict[str, object]) -> dict[str, object]:
    required = {"owner_user_id", "file_id", "asset_id", "slot_id", "old_relative_path", "new_relative_path", "sha256", "size_bytes", "transfer_id"}
    if not required <= set(snapshot):
        raise ValueError("remediation snapshot is incomplete")
    old, new = validate_relative_path(str(snapshot["old_relative_path"])), validate_relative_path(str(snapshot["new_relative_path"]))
    if old.parent != new.parent or old == new:
        raise ValueError("remediation must be an exact same-directory rename")
    return {
        "schema": SCHEMA, "action_id": str(action_id), "version": "pv-vs-filename-remediation-v1",
        "owner_user_id": str(UUID(str(snapshot["owner_user_id"]))), "file_id": str(UUID(str(snapshot["file_id"]))),
        "asset_id": str(UUID(str(snapshot["asset_id"]))), "slot_id": str(snapshot["slot_id"]),
        "old_relative_path": old.as_posix(), "new_relative_path": new.as_posix(),
        "expected_sha256": str(snapshot["sha256"]), "expected_size_bytes": int(snapshot["size_bytes"]),
        "transfer_id": str(UUID(str(snapshot["transfer_id"]))), "created_at": datetime.now(UTC).isoformat(),
    }


def queue_request(request: dict[str, object], *, root: Path | None = None, signing_key: bytes | None = None) -> Path:
    root, signing_key = root or queue_root(), signing_key or key()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir(): raise ValueError("unsafe remediation request queue")
    target = root / f"{request['action_id']}.json"
    document = {"request": request, "signature": hmac.new(signing_key, _payload(request), hashlib.sha256).hexdigest()}
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return target
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_payload(document)); stream.flush(); os.fsync(stream.fileno())
    return target


def verify_request(document: object, signing_key: bytes) -> dict[str, object] | None:
    if not isinstance(document, dict) or not isinstance(document.get("request"), dict) or not isinstance(document.get("signature"), str):
        return None
    request = document["request"]
    required = {"schema", "action_id", "version", "owner_user_id", "file_id", "asset_id", "slot_id", "old_relative_path", "new_relative_path", "expected_sha256", "expected_size_bytes", "transfer_id", "created_at"}
    if set(request) != required or request.get("schema") != SCHEMA or request.get("version") != "pv-vs-filename-remediation-v1":
        return None
    if not hmac.compare_digest(hmac.new(signing_key, _payload(request), hashlib.sha256).hexdigest(), document["signature"]):
        return None
    try:
        for field in ("action_id", "owner_user_id", "file_id", "asset_id", "transfer_id"): UUID(str(request[field]))
        old, new = validate_relative_path(str(request["old_relative_path"])), validate_relative_path(str(request["new_relative_path"]))
        if old.parent != new.parent or old == new or int(request["expected_size_bytes"]) < 0: return None
    except (TypeError, ValueError): return None
    return request


def verify_receipt(document: object, signing_key: bytes) -> dict[str, object] | None:
    if not isinstance(document, dict) or not isinstance(document.get("receipt"), dict) or not isinstance(document.get("signature"), str): return None
    receipt = document["receipt"]
    required = {"schema", "action_id", "slot_id", "old_relative_path", "new_relative_path", "expected_sha256", "expected_size_bytes", "post_sha256", "post_size_bytes", "status", "started_at", "completed_at"}
    if set(receipt) != required or receipt.get("schema") != SCHEMA or receipt.get("status") != "completed": return None
    if not hmac.compare_digest(hmac.new(signing_key, _payload(receipt), hashlib.sha256).hexdigest(), document["signature"]): return None
    try:
        UUID(str(receipt["action_id"])); validate_relative_path(str(receipt["old_relative_path"])); validate_relative_path(str(receipt["new_relative_path"])); int(receipt["expected_size_bytes"]); int(receipt["post_size_bytes"])
    except (TypeError, ValueError): return None
    return receipt
