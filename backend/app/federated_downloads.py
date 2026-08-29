"""Worker-side reconciliation of verified Stage 9 Theatre promotion receipts."""
from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from uuid import UUID

from app.federated_download_executor import verify_receipt
from app.federation import FederationStore
from app.vault_master import CataloguedAsset, VaultMasterStore


def reconcile_next_theatre_download_receipt(store: VaultMasterStore) -> UUID | None:
    root = Path(os.getenv("PV_FEDERATED_DOWNLOAD_EXECUTOR_RECEIPTS", "/var/lib/personal-vault/federated-download-receipts"))
    key_path = Path(os.getenv("PV_FEDERATED_DOWNLOAD_EXECUTOR_KEY_PATH", "/run/secrets/federated-download.key"))
    if root.is_symlink() or not root.is_dir():
        return None
    try:
        key = key_path.read_bytes()
    except OSError:
        return None
    from app.config import get_database_conninfo
    federation = FederationStore(get_database_conninfo())
    for receipt_path in sorted(root.glob("*.json")):
        if receipt_path.is_symlink() or not receipt_path.is_file():
            continue
        try:
            receipt = verify_receipt(json.loads(receipt_path.read_text(encoding="utf-8")), key)
            if receipt is None:
                continue
            context = federation.theatre_download_context(UUID(str(receipt["operation_id"])))
            if context is None:
                continue
            operation, share, recipient_username = context
            if operation.local_asset_id is None or str(operation.local_asset_id) != receipt["local_asset_id"]:
                continue
            metadata = share.origin_metadata or {}
            filename = metadata.get("filename")
            if not isinstance(filename, str) or "/" in filename or not filename:
                continue
            if receipt["expected_sha256"] != operation.expected_sha256 or receipt["expected_size_bytes"] != operation.expected_size_bytes:
                continue
            captured = metadata.get("captured_on")
            copied_metadata = {key: value for key, value in metadata.items() if key not in {"people", "biometric_evidence", "face_embeddings"}}
            asset = CataloguedAsset(
                id=operation.local_asset_id, asset_type=share.asset_type, display_title=share.display_title,
                captured_on=date.fromisoformat(captured) if isinstance(captured, str) and captured else None,
                location=None, vault_path=str(receipt["destination_vault_path"]), filename=filename,
                size_bytes=operation.expected_size_bytes, mime_type=str(metadata.get("media_type") or "application/octet-stream"),
                sha256=operation.expected_sha256, metadata=copied_metadata,
                metadata_provenance={key: "federated-origin-snapshot" for key in copied_metadata},
                imported_metadata=copied_metadata, effective_metadata=copied_metadata,
                owner_username=recipient_username, owner_user_id=operation.recipient_user_id,
            )
            existing = store.get_catalogued_asset_by_id(asset.id)
            if existing is None:
                store.restore_catalogued_asset(asset, recipient_username)
            federation.complete_download(operation.operation_id, operation.recipient_user_id, asset.id)
            return asset.id
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None
