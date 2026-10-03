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
from app.arrival_publication_coordination import cancellation, publication_lock, serialized_publication

REQUEST_MAX_AGE = timedelta(minutes=15)


def _payload(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def _logical_destination(value: str) -> str:
    path = PurePosixPath(value)
    if path.parts[:3] == ("/", "vault", "Ledger"):
        raise ValueError("Ledger is not a canonical publication destination")
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
    routing_authorization: dict[str, object] | None = None

    @classmethod
    def create(cls, *, item: ImportItem) -> "ArrivalManagedPublicationRequest":
        from app.tv_publication_authority import validate_request_authority

        validate_request_authority(item)
        from app.music_video_identity import validate_publication
        validate_publication(item)
        if (
            item.owner_user_id is None
            or not item.relative_path
            or item.size_bytes < 0
            or len(item.sha256) != 64
            or not item.proposed_destination
        ):
            raise ValueError("invalid Arrival Hall managed publication request")
        routing = item.metadata.get("arrival_publication_rule")
        authorization = None
        if routing is not None:
            if not isinstance(routing, dict):
                raise ValueError("Malformed automatic routing authorization")
            if routing.get("version") == "incident-camera-photo-v1":
                # Explicit camera-recovery grants have their own authority and
                # do not masquerade as scored auto-pilot authorizations.
                routing = None
            elif routing.get("version") not in {"vm-routing-score-v1", "vm-routing-score-v2"}:
                raise ValueError("Unsupported automatic routing authorization version")
        if isinstance(routing, dict):
            if (routing.get("owner_user_id") != str(item.owner_user_id)
                    or routing.get("source_sha256") != item.sha256
                    or routing.get("destination") != item.proposed_category
                    or not isinstance(routing.get("score"), (int, float))
                    or not isinstance(routing.get("threshold"), int)
                    or routing["score"] < routing["threshold"]):
                raise ValueError("Automatic routing authorization disagrees with intake")
            for field in ("decision_id", "authorization_id", "policy_id"):
                UUID(str(routing[field]))
            authorization = {field: routing[field] for field in (
                "version", "decision_id", "authorization_id", "policy_id",
                "owner_user_id", "semantic_class", "destination", "score", "threshold")}
        return cls(
            uuid4(), item.id, item.owner_user_id, item.relative_path,
            _logical_destination(item.proposed_destination), item.sha256,
            item.size_bytes, datetime.now(UTC).isoformat(), authorization,
        )


def queue_request(request: ArrivalManagedPublicationRequest, *, queue_root: Path, key: bytes) -> Path:
    queue_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with publication_lock(queue_root):
        if cancellation(queue_root, request.item_id, key) is not None:
            raise ValueError("This intake was cancelled and cannot be published")
        return _queue_request_locked(request, queue_root=queue_root, key=key)


def _queue_request_locked(request: ArrivalManagedPublicationRequest, *, queue_root: Path, key: bytes) -> Path:
    queue_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if queue_root.is_symlink() or not queue_root.is_dir():
        raise ValueError("unsafe Arrival Hall managed request queue")
    target = queue_root / f"{request.request_id}.json"
    payload = {key: value for key, value in asdict(request).items() if value is not None}
    document = {"request": payload, "signature": hmac.new(key, _payload(payload), hashlib.sha256).hexdigest()}
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_payload(document)); stream.flush(); os.fsync(stream.fileno())
    return target


def verify_request(document: object, key: bytes) -> ArrivalManagedPublicationRequest | None:
    if not isinstance(document, dict) or not isinstance(document.get("request"), dict) or not isinstance(document.get("signature"), str):
        return None
    request = document["request"]
    required = {"request_id", "item_id", "owner_user_id", "source_relative_path", "logical_destination", "expected_sha256", "expected_size_bytes", "created_at"}
    if set(request) not in (required, required | {"routing_authorization"}):
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
            routing_authorization=request.get("routing_authorization"),
        )
        created_at = datetime.fromisoformat(parsed.created_at)
    except (TypeError, ValueError):
        return None
    if created_at.tzinfo is None or parsed.expected_size_bytes < 0 or len(parsed.expected_sha256) != 64:
        return None
    if parsed.routing_authorization is not None:
        auth = parsed.routing_authorization
        if not isinstance(auth, dict) or set(auth) != {
            "version", "decision_id", "authorization_id", "policy_id",
            "owner_user_id", "semantic_class", "destination", "score", "threshold"
        }:
            return None
        try:
            for field in ("decision_id", "authorization_id", "policy_id"):
                UUID(str(auth[field]))
            if (auth["version"] not in {"vm-routing-score-v1", "vm-routing-score-v2"}
                    or auth["owner_user_id"] != str(parsed.owner_user_id)
                    or auth["destination"] != parsed.logical_destination.split("/")[2]
                    or not isinstance(auth["score"], (int, float))
                    or not isinstance(auth["threshold"], int)
                    or auth["score"] < auth["threshold"]):
                return None
        except (TypeError, ValueError, KeyError):
            return None
    return parsed


def _queue_root() -> Path:
    return Path(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_QUEUE", "/var/lib/personal-vault/arrival-managed-requests"))


def _receipt_root() -> Path:
    return Path(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_RECEIPTS", "/var/lib/personal-vault/arrival-managed-receipts"))


def _key() -> bytes:
    return Path(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH", "/run/secrets/arrival-managed-publisher.key")).read_bytes()


@serialized_publication
def reissue_request(request: ArrivalManagedPublicationRequest, *, queue_root: Path, key: bytes, now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("reissue time must be timezone-aware")
    queue_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if queue_root.is_symlink() or not queue_root.is_dir():
        raise ValueError("unsafe Arrival Hall managed request queue")
    if cancellation(queue_root, request.item_id, key) is not None:
        raise ValueError("This intake was cancelled and cannot be retried")
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
        if (existing.owner_user_id, existing.source_relative_path, existing.logical_destination,
                existing.expected_sha256, existing.expected_size_bytes,
                existing.routing_authorization) != (
                request.owner_user_id, request.source_relative_path, request.logical_destination,
                request.expected_sha256, request.expected_size_bytes,
                request.routing_authorization):
            raise ValueError("Existing publication request disagrees with the approved intake")
        if datetime.fromisoformat(existing.created_at) > now - REQUEST_MAX_AGE:
            return candidate
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
    return receipt if set(receipt) in (required, required | {"routing_authorization"}) and hmac.compare_digest(signature, document["signature"]) else None


def queue_item(item: ImportItem) -> UUID:
    request = ArrivalManagedPublicationRequest.create(item=item)
    queue_request(request, queue_root=_queue_root(), key=_key())
    return request.request_id


@serialized_publication
def reissue_item(item: ImportItem, incoming_root: Path) -> Path:
    if item.state != "theatre_promotion_pending" or item.proposed_category not in {"Movies", "TV Shows", "Music", "Music Videos", "Gallery", "Documents", "Archives"}:
        raise ValueError("only a pending managed Arrival Hall publication can be reissued")
    if cancellation(_queue_root(), item.id, _key()) is not None:
        raise ValueError("This intake was cancelled and cannot be retried")
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


@serialized_publication
def reconcile_next_receipt(store: object) -> UUID | None:
    root = _receipt_root()
    if root.is_symlink() or not root.is_dir():
        return None
    try:
        key = _key()
    except OSError:
        return None
    completed = getattr(store, "completed_arrival_request_ids", lambda: set())()
    for receipt_path in sorted(root.glob("*.json")):
        # Completion is durable in the atomic catalogue transaction. Avoid a
        # database transaction for every historical receipt on every worker pass.
        if receipt_path.stem in completed:
            continue
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


@serialized_publication
def reconcile_rejected_request(store: object) -> UUID | None:
    """Turn one verified executor rejection into a retryable move failure.

    A receipt always wins, so a post-publication source-cleanup failure cannot
    regress successful canonical work.
    """
    try:
        key = _key()
    except OSError:
        return None
    for path in sorted(_queue_root().glob("*.rejected.request")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            request = verify_request(json.loads(path.read_text(encoding="utf-8")), key)
            if request is None:
                continue
            item = store.get_item(request.item_id)
            if (
                item is None
                or item.state != "theatre_promotion_pending"
                or item.proposed_category not in {"Movies", "TV Shows", "Music", "Music Videos", "Gallery", "Documents", "Archives"}
                or item.metadata.get("managed_request_id") != str(request.request_id)
                or item.owner_user_id != request.owner_user_id
                or item.sha256 != request.expected_sha256
                or item.proposed_destination != request.logical_destination
                or (_receipt_root() / f"{request.request_id}.json").exists()
            ):
                continue
            store.record_move_result(
                item.id,
                "move_failed",
                "Arrival Hall managed publisher",
                "Managed publisher rejected this attempt; source, checksum, destination or capacity verification failed",
            )
            return item.id
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return None
