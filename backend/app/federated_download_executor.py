"""Narrow signed contract for Stage 9 Theatre Download to My Vault promotion.

The backend may stage and verify bytes but never writes /vault/Theatre/Movies.
Only the root-owned companion executor consumes this fixed request shape.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4


THEATRE_ROOT = PurePosixPath("/vault/Theatre/Movies")
STAGING_ROOT = PurePosixPath("/var/lib/personal-vault/federated-download-staging")


@dataclass(frozen=True)
class FederatedDownloadPromotionRequest:
    request_id: UUID
    operation_id: UUID
    local_asset_id: UUID
    owner_user_id: UUID
    origin_vault_id: UUID
    origin_asset_id: UUID
    staging_path: str
    destination_vault_path: str
    expected_sha256: str
    expected_size_bytes: int
    created_at: str

    @classmethod
    def create(cls, *, operation_id: UUID, local_asset_id: UUID, owner_user_id: UUID, origin_vault_id: UUID, origin_asset_id: UUID, staging_name: str, filename: str, expected_sha256: str, expected_size_bytes: int) -> "FederatedDownloadPromotionRequest":
        if not staging_name or "/" in staging_name or not filename or "/" in filename or expected_size_bytes < 0 or len(expected_sha256) != 64:
            raise ValueError("invalid federated download promotion request")
        return cls(uuid4(),operation_id,local_asset_id,owner_user_id,origin_vault_id,origin_asset_id,str(STAGING_ROOT / staging_name),str(THEATRE_ROOT / str(local_asset_id) / filename),expected_sha256,expected_size_bytes,datetime.now(UTC).isoformat())


def canonical_payload(request: FederatedDownloadPromotionRequest) -> bytes:
    return json.dumps(asdict(request),sort_keys=True,separators=(",",":"),default=str).encode()


def sign_request(request: FederatedDownloadPromotionRequest, key: bytes) -> str:
    return hmac.new(key,canonical_payload(request),hashlib.sha256).hexdigest()


def queue_signed_request(request: FederatedDownloadPromotionRequest, *, queue_root: Path, key: bytes) -> Path:
    queue_root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if queue_root.is_symlink() or not queue_root.is_dir(): raise ValueError("unsafe federated download queue")
    target=queue_root/f"{request.request_id}.json"
    payload=json.dumps({"request":asdict(request),"signature":sign_request(request,key)},sort_keys=True,separators=(",",":"),default=str).encode()
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,"wb") as stream:
        stream.write(payload); stream.flush(); os.fsync(stream.fileno())
    return target


def verify_receipt(document: object, key: bytes) -> dict[str, object] | None:
    if not isinstance(document,dict) or not isinstance(document.get("receipt"),dict) or not isinstance(document.get("signature"),str): return None
    receipt=document["receipt"]
    signature=hmac.new(key,json.dumps(receipt,sort_keys=True,separators=(",",":"),default=str).encode(),hashlib.sha256).hexdigest()
    required={"request_id","operation_id","local_asset_id","destination_vault_path","expected_sha256","expected_size_bytes","verified_at"}
    return receipt if hmac.compare_digest(signature,document["signature"]) and set(receipt)==required else None
