"""Signed, fixed-schema contract for one managed Theatre movie rename."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
from uuid import UUID, uuid4

from app.arrival_managed_publisher import _payload

SCHEMA = "personal-vault.theatre-movie-rename.v1"


def _movie_path(value: str) -> str:
    path = PurePosixPath(value)
    root = PurePosixPath("/vault/Theatre/Movies")
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise ValueError("Theatre movie rename path is outside Movies") from error
    if not path.is_absolute() or len(relative.parts) < 1 or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise ValueError("Theatre movie rename path is invalid")
    return path.as_posix()


def _canonical_destination(title: str, year: int, suffix: str) -> str:
    if not isinstance(title, str) or not title.strip() or year < 1000 or year > 9999:
        raise ValueError("The reviewed movie identity is invalid")
    safe_title = re.sub(r"[\\/:]+", " - ", title.strip())
    safe_title = re.sub(r"\s+", " ", safe_title).strip(" .-")
    canonical = f"{safe_title} ({year})"
    return str(
        PurePosixPath("/vault/Theatre/Movies")
        / canonical
        / f"{canonical}{suffix.casefold()}"
    )


@dataclass(frozen=True)
class TheatreMovieRenameRequest:
    schema: str
    request_id: UUID
    asset_id: UUID
    file_id: UUID
    owner_user_id: UUID
    slot_id: str
    source_logical_path: str
    destination_logical_path: str
    source_relative_path: str
    destination_relative_path: str
    title: str
    release_year: int
    expected_sha256: str
    expected_size_bytes: int
    created_at: str

    @classmethod
    def create(
        cls, snapshot: dict[str, object], destination: str, title: str, year: int
    ) -> "TheatreMovieRenameRequest":
        source = _movie_path(str(snapshot["vault_path"]))
        destination = _movie_path(destination)
        if destination != _canonical_destination(
            title, year, Path(source).suffix
        ):
            raise ValueError("The Theatre destination does not match reviewed identity")
        source_relative = PurePosixPath(source).relative_to("/vault").as_posix()
        destination_relative = (
            PurePosixPath(destination).relative_to("/vault").as_posix()
        )
        if source == destination:
            raise ValueError("The movie already has the requested canonical path")
        if snapshot.get("relative_path") != source_relative:
            raise ValueError("The authoritative movie placement is inconsistent")
        return cls(
            SCHEMA,
            uuid4(),
            UUID(str(snapshot["asset_id"])),
            UUID(str(snapshot["file_id"])),
            UUID(str(snapshot["owner_user_id"])),
            str(snapshot["slot_id"]),
            source,
            destination,
            source_relative,
            destination_relative,
            title,
            year,
            str(snapshot["sha256"]),
            int(snapshot["size_bytes"]),
            datetime.now(UTC).isoformat(),
        )


def queue_request(
    request: TheatreMovieRenameRequest, *, queue_root: Path, key: bytes
) -> Path:
    queue_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if queue_root.is_symlink() or not queue_root.is_dir():
        raise ValueError("Unsafe Theatre rename request queue")
    for candidate in queue_root.glob("*.json"):
        if candidate.is_file():
            raise ValueError("A Theatre movie rename request is already pending")
    body = asdict(request)
    document = {
        "request": body,
        "signature": hmac.new(key, _payload(body), hashlib.sha256).hexdigest(),
    }
    target = queue_root / f"{request.request_id}.json"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(_payload(document))
        stream.flush()
        os.fsync(stream.fileno())
    return target


def verify_receipt(document: object, key: bytes) -> dict[str, object] | None:
    if not isinstance(document, dict) or not isinstance(document.get("receipt"), dict):
        return None
    receipt = document["receipt"]
    signature = document.get("signature")
    required = set(TheatreMovieRenameRequest.__dataclass_fields__) | {"completed_at"}
    if set(receipt) != required or not isinstance(signature, str):
        return None
    expected = hmac.new(key, _payload(receipt), hashlib.sha256).hexdigest()
    return receipt if hmac.compare_digest(expected, signature) else None


def queue_movie_rename(
    snapshot: dict[str, object], destination: str, title: str, year: int
) -> TheatreMovieRenameRequest:
    request = TheatreMovieRenameRequest.create(snapshot, destination, title, year)
    queue_request(request, queue_root=_queue_root(), key=_key())
    return request


def reconcile_next_receipt(store: object) -> UUID | None:
    root = _receipt_root()
    if root.is_symlink() or not root.is_dir():
        return None
    try:
        key = _key()
    except OSError:
        return None
    for path in sorted(root.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            receipt = verify_receipt(json.loads(path.read_text(encoding="utf-8")), key)
            if receipt is None:
                continue
            updated = store.complete_theatre_movie_rename(receipt)
            if updated is not None:
                return updated.id
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return None


def _queue_root() -> Path:
    return Path(os.getenv("PV_THEATRE_RENAME_QUEUE", "/var/lib/personal-vault/theatre-rename-requests"))


def _receipt_root() -> Path:
    return Path(os.getenv("PV_THEATRE_RENAME_RECEIPTS", "/var/lib/personal-vault/theatre-rename-receipts"))


def _key() -> bytes:
    return Path(os.getenv("PV_ARRIVAL_MANAGED_PUBLISHER_KEY_PATH", "/run/secrets/arrival-managed-publisher.key")).read_bytes()
