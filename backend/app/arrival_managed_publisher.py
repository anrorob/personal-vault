"""Signed logical-destination contract for root-managed Arrival Hall publication.

The backend may request publication, but it never chooses a physical slot or
receives a writable permanent-storage mount.  The root executor resolves the
signed logical destination against the final managed-slot manifest.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
from uuid import UUID, uuid4

from app.vault_master import ImportItem, sha256_file

REQUEST_MAX_AGE = timedelta(minutes=15)


def _payload(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _logical_destination(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or path.parts[:1] != ("/",)
        or path.parts[1:2] != ("vault",)
        or len(path.parts) < 3
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("invalid canonical logical Vault destination")
    return path.as_posix()


@dataclass(frozen=True)
class ArrivalManagedPublicationRequest:
    request_id: UUID
    item_id: UUID
    owner_user_id: UUID
    source_relative_path: str
    logical_destination: str
    expected_sha256: str
    expected_size_bytes: int
    created_at: str

    @classmethod
    def create(cls, *, item: ImportItem) -> "ArrivalManagedPublicationRequest":
        if (
            item.owner_user_id is None
            or not item.relative_path
            or item.size_bytes < 0
            or len(item.sha256) != 64
            or not item.proposed_destination
        ):
            raise ValueError("invalid Arrival Hall managed publication request")
        return cls(
            uuid4(), item.id, item.owner_user_id, item.relative_path,
            _logical_destination(item.proposed_destination), item.sha256,
            item.size_bytes, datetime.now(UTC).isoformat(),
        )


def queue_request(request: ArrivalManagedPublicationRequest, *, queue_root: Path, key: bytes) -> Path:
    queue_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if queue_root.is_symlink() or not queue_root.is_dir():
        raise ValueError("unsafe Arrival Hall managed request queue")
    target = queue_root / f"{request.request_id}.json"
    document = {"request": asdict(request), "signature": hmac.new(key, _payload(asdict(request)), hashlib.sha256).hexdigest()}
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_payload(document)); stream.flush(); os.fsync(stream.fileno())
    return target


def verify_request(document: object, key: bytes) -> ArrivalManagedPublicationRequest | None:
    if not isinstance(document, dict) or not isinstance(document.get("request"), dict) or not isinstance(document.get("signature"), str):
        return None
    request = document["request"]
    required = {"request_id", "item_id", "owner_user_id", "source_relative_path", "logical_destination", "expected_sha256", "expected_size_bytes", "created_at"}
    if set(request) != required:
        return None
    signature = hmac.new(key, _payload(request), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, document["signature"]):
        return None
    try:
        parsed = ArrivalManagedPublicationRequest(
            request_id=UUID(str(request["request_id"])), item_id=UUID(str(request["item_id"])),
            owner_user_id=UUID(str(request["owner_user_id"])),
            source_relative_path=str(request["source_relative_path"]),
            logical_destination=_logical_destination(str(request["logical_destination"])),
            expected_sha256=str(request["expected_sha256"]),
            expected_size_bytes=int(request["expected_size_bytes"]), created_at=str(request["created_at"]),
        )
        created_at = datetime.fromisoformat(parsed.created_at)
    except (TypeError, ValueError):
        return None
    if created_at.tzinfo is None or parsed.expected_size_bytes < 0 or len(parsed.expected_sha256) != 64:
        return None
    return parsed


def _queue_root() -> Path:
    return Path(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_QUEUE", "/var/lib/personal-vault/arrival-managed-requests"))


def _receipt_root() -> Path:
    return Path(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_RECEIPTS", "/var/lib/personal-vault/arrival-managed-receipts"))


def _key() -> bytes:
    return Path(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH", "/run/secrets/arrival-managed-publisher.key")).read_bytes()


def reissue_request(request: ArrivalManagedPublicationRequest, *, queue_root: Path, key: bytes, now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("reissue time must be timezone-aware")
    queue_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if queue_root.is_symlink() or not queue_root.is_dir():
        raise ValueError("unsafe Arrival Hall managed request queue")
    for candidate in sorted(queue_root.glob("*.json")):
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("unsafe Arrival Hall managed request entry")
        try:
            existing = verify_request(json.loads(candidate.read_text(encoding="utf-8")), key)
        except (OSError, json.JSONDecodeError):
            raise ValueError("unreadable Arrival Hall managed request entry") from None
        if existing is None:
            raise ValueError("invalid Arrival Hall managed request entry")
        if existing.item_id != request.item_id:
            continue
        if datetime.fromisoformat(existing.created_at) > now - REQUEST_MAX_AGE:
            raise ValueError("a live Arrival Hall managed request already exists")
        historical = candidate.with_suffix(".superseded.request")
        if historical.exists() or historical.is_symlink():
            raise ValueError("Arrival Hall managed request history already exists")
        os.replace(candidate, historical)
    return queue_request(request, queue_root=queue_root, key=key)


def verify_receipt(document: object, key: bytes) -> dict[str, object] | None:
    if not isinstance(document, dict) or not isinstance(document.get("receipt"), dict) or not isinstance(document.get("signature"), str):
        return None
    receipt = document["receipt"]
    required = {"request_id", "item_id", "owner_user_id", "logical_destination", "logical_area", "slot_id", "relative_path", "expected_sha256", "expected_size_bytes", "verified_at"}
    signature = hmac.new(key, _payload(receipt), hashlib.sha256).hexdigest()
    return receipt if set(receipt) == required and hmac.compare_digest(signature, document["signature"]) else None


def queue_item(item: ImportItem) -> None:
    queue_request(ArrivalManagedPublicationRequest.create(item=item), queue_root=_queue_root(), key=_key())


def reissue_item(item: ImportItem, incoming_root: Path) -> Path:
    if item.state != "theatre_promotion_pending" or item.proposed_category not in {"Movies", "TV Shows"}:
        raise ValueError("only a pending managed Arrival Hall publication can be reissued")
    request = ArrivalManagedPublicationRequest.create(item=item)
    candidate = incoming_root / item.relative_path
    if candidate != Path(item.source_path) or candidate.is_symlink():
        raise ValueError("the approved Arrival Hall source no longer matches its recorded path")
    try:
        source, root = candidate.resolve(strict=True), incoming_root.resolve(strict=True)
    except OSError as error:
        raise ValueError("the approved Arrival Hall source is unavailable") from error
    if not source.is_relative_to(root) or not source.is_file() or source.stat().st_size != item.size_bytes or sha256_file(source) != item.sha256:
        raise ValueError("the approved Arrival Hall source no longer matches its size or checksum")
    receipts = _receipt_root()
    if receipts.is_symlink() or not receipts.is_dir():
        raise ValueError("unsafe Arrival Hall managed receipt directory")
    key = _key()
    for receipt_path in receipts.glob("*.json"):
        try:
            receipt = verify_receipt(json.loads(receipt_path.read_text(encoding="utf-8")), key)
        except (OSError, json.JSONDecodeError):
            raise ValueError("unreadable Arrival Hall managed receipt entry") from None
        if receipt is None:
            raise ValueError("invalid Arrival Hall managed receipt entry")
        if receipt.get("item_id") == str(item.id):
            raise ValueError("a root-verified Arrival Hall managed receipt already exists")
    return reissue_request(request, queue_root=_queue_root(), key=key)


def reconcile_next_receipt(store: object) -> UUID | None:
    root = _receipt_root()
    if root.is_symlink() or not root.is_dir():
        return None
    try:
        key = _key()
    except OSError:
        return None
    for receipt_path in sorted(root.glob("*.json")):
        if receipt_path.is_symlink() or not receipt_path.is_file():
            continue
        try:
            receipt = verify_receipt(json.loads(receipt_path.read_text(encoding="utf-8")), key)
            if receipt is None:
                continue
            published = store.publish_arrival_managed_receipt(UUID(str(receipt["item_id"])), receipt)
            if published is not None:
                return published.id
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None
