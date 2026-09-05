"""LAN-only Vault Supplier resumable receiver protocol v1.

The receiver owns only hidden Arrival Hall staging and publishes verified files
through the existing Arrival Hall ownership boundary.  It never selects a
canonical Vault destination.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import os
from pathlib import Path
import re
import threading
from types import SimpleNamespace
from typing import Literal, Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
import psycopg
from psycopg.rows import dict_row

from app.incoming import (
    complete_arrival_hall_publication,
    record_arrival_hall_file_source_context,
    get_arrival_hall_path,
    get_available_name,
    require_incoming_path,
    validate_filename,
)
from app.auth import get_authentication_store
from app.auth_store import AuthenticationStore
from app.config import get_database_conninfo
from app.vault_master_intake import IntakeStore, get_intake_store
from app.vault_supplier import SupplierInstallation, VaultSupplierStore, get_vault_supplier_store


TRANSFER_PROTOCOL_VERSION = 1
MAX_HASH_BATCH = 128
MIN_CHUNK_BYTES = 1
RECOMMENDED_CHUNK_BYTES = 8 * 1024 * 1024
MAX_CHUNK_BYTES = 64 * 1024 * 1024
MAX_SOURCE_CONTEXT_BYTES = 16 * 1024
MAX_TOTAL_SIZE = 10 * 1024 * 1024 * 1024 * 1024
SESSION_LIFETIME = timedelta(days=7)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TERMINAL_STATES = frozenset({"finalized", "failed", "aborted"})
ACTIVE_STATES = frozenset({"created", "receiving", "paused", "verifying"})

router = APIRouter(prefix="/api/vault-supplier", tags=["vault-supplier-transfer"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error(code: str, message: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _staging_root(arrival_hall: Path) -> Path:
    configured = os.getenv("PV_VAULT_SUPPLIER_TRANSFER_STAGING_PATH")
    root = Path(configured) if configured else arrival_hall / ".pv-vault-supplier-transfers"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise _error("receiver_unavailable", "Vault Supplier receiver staging is unavailable.", status.HTTP_503_SERVICE_UNAVAILABLE)
    resolved_root = root.resolve(strict=True)
    resolved_arrival = arrival_hall.resolve(strict=True)
    if not resolved_root.is_relative_to(resolved_arrival):
        raise _error("receiver_unavailable", "Vault Supplier receiver staging is outside Arrival Hall.", status.HTTP_503_SERVICE_UNAVAILABLE)
    return resolved_root


def _part_path(staging_root: Path, transfer_id: UUID) -> Path:
    return staging_root / f"{transfer_id}.part"


def _source_context(value: dict[str, object] | None) -> dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _error("invalid_source_context", "Source context must be an object.")
    result: dict[str, object] = {}
    for name, maximum in (("source_kind", 64), ("source_id", 128), ("source_label", 256)):
        raw = value.get(name)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw.strip() or len(raw) > maximum or any(ord(char) < 32 for char in raw):
            raise _error("invalid_source_context", f"{name} is invalid.")
        result[name] = raw.strip()
    relative = value.get("relative_path")
    if relative is not None:
        if not isinstance(relative, str):
            raise _error("invalid_source_context", "relative_path is invalid.")
        normalized = relative.replace("\\", "/")
        if normalized.startswith("/") or re.match(r"^[a-zA-Z]:", normalized) or "//" in normalized:
            raise _error("invalid_source_context", "relative_path must be a safe relative path.")
        parts = [part for part in normalized.split("/") if part not in {"", "."}]
        if not parts or any(part == ".." or any(ord(char) < 32 for char in part) for part in parts):
            raise _error("invalid_source_context", "relative_path must be a safe relative path.")
        result["relative_path"] = "/".join(parts)
    encoded = __import__("json").dumps(result, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_SOURCE_CONTEXT_BYTES:
        raise _error("invalid_source_context", "Source context exceeds the protocol limit.")
    return result


@dataclass(frozen=True)
class TransferSession:
    transfer_id: UUID
    installation_id: UUID
    user_id: UUID
    vault_id: UUID
    filename: str
    total_size: int
    expected_sha256: str
    media_type: str | None
    source_context: dict[str, object]
    protocol_version: int
    state: str
    bytes_received: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    finalized_at: datetime | None = None
    arrival_hall_filename: str | None = None
    failure_code: str | None = None


class TransferStore(Protocol):
    def initialize(self) -> None: ...
    def active_count(self) -> int: ...
    def has_duplicate(self, user_id: UUID, sha256: str) -> bool: ...
    def create(self, session: TransferSession) -> TransferSession: ...
    def get(self, transfer_id: UUID) -> TransferSession | None: ...
    def set_progress(self, transfer_id: UUID, expected_bytes: int, new_bytes: int, state: str = "receiving") -> TransferSession | None: ...
    def reconcile(self, transfer_id: UUID, bytes_received: int, state: str | None = None) -> TransferSession | None: ...
    def begin_verification(self, transfer_id: UUID, filename: str) -> TransferSession | None: ...
    def finalize(self, transfer_id: UUID) -> TransferSession | None: ...
    def fail(self, transfer_id: UUID, code: str) -> TransferSession | None: ...
    def abort(self, transfer_id: UUID) -> TransferSession | None: ...
    def list_finalized_with_source_context(self) -> list[TransferSession]: ...


class MemoryTransferStore:
    def __init__(self) -> None:
        self.sessions: dict[UUID, TransferSession] = {}
        self._lock = threading.RLock()

    def initialize(self) -> None:
        return None

    def active_count(self) -> int:
        with self._lock:
            return sum(item.state in ACTIVE_STATES for item in self.sessions.values())

    def has_duplicate(self, user_id: UUID, sha256: str) -> bool:
        with self._lock:
            return any(item.user_id == user_id and item.expected_sha256 == sha256 and item.state == "finalized" for item in self.sessions.values())

    def create(self, session: TransferSession) -> TransferSession:
        with self._lock:
            if self.active_count():
                raise ValueError("intake_busy")
            self.sessions[session.transfer_id] = session
            return session

    def get(self, transfer_id: UUID) -> TransferSession | None:
        with self._lock:
            return self.sessions.get(transfer_id)

    def set_progress(self, transfer_id: UUID, expected_bytes: int, new_bytes: int, state: str = "receiving") -> TransferSession | None:
        with self._lock:
            item = self.sessions.get(transfer_id)
            if item is None or item.bytes_received != expected_bytes or item.state not in {"created", "receiving", "paused"}:
                return None
            item = replace(item, bytes_received=new_bytes, state=state, updated_at=_now())
            self.sessions[transfer_id] = item
            return item

    def reconcile(self, transfer_id: UUID, bytes_received: int, state: str | None = None) -> TransferSession | None:
        with self._lock:
            item = self.sessions.get(transfer_id)
            if item is None:
                return None
            item = replace(item, bytes_received=bytes_received, state=state or item.state, updated_at=_now())
            self.sessions[transfer_id] = item
            return item

    def begin_verification(self, transfer_id: UUID, filename: str) -> TransferSession | None:
        with self._lock:
            item = self.sessions.get(transfer_id)
            if item is None or item.state not in {"created", "receiving", "paused"}:
                return None
            item = replace(item, state="verifying", arrival_hall_filename=filename, updated_at=_now())
            self.sessions[transfer_id] = item
            return item

    def finalize(self, transfer_id: UUID) -> TransferSession | None:
        with self._lock:
            item = self.sessions.get(transfer_id)
            if item is None or item.state != "verifying":
                return None
            item = replace(item, state="finalized", finalized_at=_now(), updated_at=_now())
            self.sessions[transfer_id] = item
            return item

    def fail(self, transfer_id: UUID, code: str) -> TransferSession | None:
        with self._lock:
            item = self.sessions.get(transfer_id)
            if item is None:
                return None
            item = replace(item, state="failed", failure_code=code, updated_at=_now())
            self.sessions[transfer_id] = item
            return item

    def abort(self, transfer_id: UUID) -> TransferSession | None:
        with self._lock:
            item = self.sessions.get(transfer_id)
            if item is None or item.state in {"finalized", "aborted"}:
                return None
            item = replace(item, state="aborted", updated_at=_now())
            self.sessions[transfer_id] = item
            return item

    def list_finalized_with_source_context(self) -> list[TransferSession]:
        with self._lock:
            return [item for item in self.sessions.values() if item.state == "finalized" and item.arrival_hall_filename and item.source_context]


def _session_from_row(row: dict[str, object]) -> TransferSession:
    return TransferSession(
        transfer_id=UUID(str(row["transfer_id"])), installation_id=UUID(str(row["installation_id"])), user_id=UUID(str(row["user_id"])), vault_id=UUID(str(row["vault_id"])),
        filename=str(row["filename"]), total_size=int(row["total_size"]), expected_sha256=str(row["expected_sha256"]), media_type=str(row["media_type"]) if row["media_type"] else None,
        source_context=dict(row["source_context"] or {}), protocol_version=int(row["protocol_version"]), state=str(row["state"]), bytes_received=int(row["bytes_received"]),
        created_at=row["created_at"], updated_at=row["updated_at"], expires_at=row["expires_at"], finalized_at=row["finalized_at"],
        arrival_hall_filename=str(row["arrival_hall_filename"]) if row["arrival_hall_filename"] else None, failure_code=str(row["failure_code"]) if row["failure_code"] else None,
    )


class PostgresTransferStore:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_supplier_transfer_sessions (
                transfer_id UUID PRIMARY KEY,
                installation_id UUID NOT NULL REFERENCES vault_supplier_installations(installation_id),
                user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                vault_id UUID NOT NULL REFERENCES vaults(vault_id),
                filename TEXT NOT NULL, total_size BIGINT NOT NULL CHECK(total_size >= 0),
                expected_sha256 CHAR(64) NOT NULL, media_type TEXT, source_context JSONB NOT NULL DEFAULT '{}'::jsonb,
                protocol_version INTEGER NOT NULL CHECK(protocol_version = 1),
                state TEXT NOT NULL CHECK(state IN ('created','receiving','paused','verifying','finalized','failed','aborted')),
                bytes_received BIGINT NOT NULL DEFAULT 0 CHECK(bytes_received >= 0),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMPTZ NOT NULL, finalized_at TIMESTAMPTZ, arrival_hall_filename TEXT, failure_code TEXT
            )""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_supplier_transfer_sessions_active_idx ON vault_supplier_transfer_sessions(state,created_at) WHERE state IN ('created','receiving','paused','verifying')")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_supplier_transfer_sessions_hash_idx ON vault_supplier_transfer_sessions(user_id,expected_sha256) WHERE state='finalized'")

    def active_count(self) -> int:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT count(*) AS count FROM vault_supplier_transfer_sessions WHERE state IN ('created','receiving','paused','verifying') AND expires_at>CURRENT_TIMESTAMP")
            return int(cursor.fetchone()["count"])

    def has_duplicate(self, user_id: UUID, sha256: str) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT EXISTS(
                SELECT 1 FROM vault_supplier_transfer_sessions WHERE user_id=%s AND expected_sha256=%s AND state='finalized'
                UNION ALL
                SELECT 1 FROM vault_files f JOIN vault_assets a ON a.id=f.asset_id
                WHERE f.sha256=%s AND a.owner_user_id=%s AND a.lifecycle_state='active'
            ) AS duplicate""", (user_id, sha256, sha256, user_id))
            return bool(cursor.fetchone()["duplicate"])

    def create(self, session: TransferSession) -> TransferSession:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(913003)")
            cursor.execute("SELECT 1 FROM vault_supplier_transfer_sessions WHERE state IN ('created','receiving','paused','verifying') AND expires_at>CURRENT_TIMESTAMP LIMIT 1")
            if cursor.fetchone() is not None:
                raise ValueError("intake_busy")
            cursor.execute("""INSERT INTO vault_supplier_transfer_sessions(transfer_id,installation_id,user_id,vault_id,filename,total_size,expected_sha256,media_type,source_context,protocol_version,state,bytes_received,created_at,updated_at,expires_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (session.transfer_id,session.installation_id,session.user_id,session.vault_id,session.filename,session.total_size,session.expected_sha256,session.media_type,__import__('json').dumps(session.source_context),session.protocol_version,session.state,session.bytes_received,session.created_at,session.updated_at,session.expires_at))
            return _session_from_row(cursor.fetchone())

    def get(self, transfer_id: UUID) -> TransferSession | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_supplier_transfer_sessions WHERE transfer_id=%s", (transfer_id,))
            row = cursor.fetchone()
            return _session_from_row(row) if row else None

    def set_progress(self, transfer_id: UUID, expected_bytes: int, new_bytes: int, state: str = "receiving") -> TransferSession | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE vault_supplier_transfer_sessions SET bytes_received=%s,state=%s,updated_at=CURRENT_TIMESTAMP
                WHERE transfer_id=%s AND bytes_received=%s AND state IN ('created','receiving','paused') AND expires_at>CURRENT_TIMESTAMP RETURNING *""", (new_bytes,state,transfer_id,expected_bytes))
            row=cursor.fetchone(); return _session_from_row(row) if row else None

    def reconcile(self, transfer_id: UUID, bytes_received: int, state: str | None = None) -> TransferSession | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_supplier_transfer_sessions SET bytes_received=%s,state=COALESCE(%s,state),updated_at=CURRENT_TIMESTAMP WHERE transfer_id=%s RETURNING *", (bytes_received,state,transfer_id))
            row=cursor.fetchone(); return _session_from_row(row) if row else None

    def begin_verification(self, transfer_id: UUID, filename: str) -> TransferSession | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE vault_supplier_transfer_sessions SET state='verifying',arrival_hall_filename=%s,updated_at=CURRENT_TIMESTAMP
                WHERE transfer_id=%s AND state IN ('created','receiving','paused') AND expires_at>CURRENT_TIMESTAMP RETURNING *""", (filename,transfer_id))
            row=cursor.fetchone(); return _session_from_row(row) if row else None

    def finalize(self, transfer_id: UUID) -> TransferSession | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_supplier_transfer_sessions SET state='finalized',finalized_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE transfer_id=%s AND state='verifying' RETURNING *", (transfer_id,))
            row=cursor.fetchone(); return _session_from_row(row) if row else None

    def fail(self, transfer_id: UUID, code: str) -> TransferSession | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_supplier_transfer_sessions SET state='failed',failure_code=%s,updated_at=CURRENT_TIMESTAMP WHERE transfer_id=%s RETURNING *", (code,transfer_id))
            row=cursor.fetchone(); return _session_from_row(row) if row else None

    def abort(self, transfer_id: UUID) -> TransferSession | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_supplier_transfer_sessions SET state='aborted',updated_at=CURRENT_TIMESTAMP WHERE transfer_id=%s AND state NOT IN ('finalized','aborted') RETURNING *", (transfer_id,))
            row=cursor.fetchone(); return _session_from_row(row) if row else None

    def list_finalized_with_source_context(self) -> list[TransferSession]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_supplier_transfer_sessions WHERE state='finalized' AND arrival_hall_filename IS NOT NULL AND source_context <> '{}'::jsonb")
            return [_session_from_row(row) for row in cursor.fetchall()]


def get_transfer_store() -> TransferStore:
    return PostgresTransferStore(get_database_conninfo())


def backfill_arrival_hall_source_context(
    arrival_hall: Path,
    transfers: TransferStore,
) -> int:
    """Idempotently restore retained Supplier provenance to staged files only.

    No context is invented: only finalized sessions that still have a visible
    staged file and matching immutable owner identity can contribute evidence.
    """
    restored = 0
    for session in transfers.list_finalized_with_source_context():
        assert session.arrival_hall_filename is not None
        try:
            source_context = _source_context(session.source_context)
        except HTTPException:
            continue
        if not source_context:
            continue
        destination = arrival_hall / session.arrival_hall_filename
        if not destination.is_file():
            continue
        try:
            record_arrival_hall_file_source_context(
                arrival_hall, destination, session.user_id, source_context, session.transfer_id
            )
        except (OSError, ValueError):
            continue
        restored += 1
    return restored


@dataclass(frozen=True)
class SupplierTransferPrincipal:
    installation: SupplierInstallation
    user_id: UUID


def require_supplier_transfer_authorization(request: Request, supplier_store: VaultSupplierStore = Depends(get_vault_supplier_store)) -> SupplierTransferPrincipal:
    """Use the short-lived authorization minted by existing key challenge auth."""
    try:
        installation_id = UUID(request.headers["X-PV-Supplier-Installation-ID"])
        user_id = UUID(request.headers["X-PV-Supplier-User-ID"])
    except (KeyError, ValueError):
        raise _error("transfer_not_authorized", "Supplier installation authorization is required.", status.HTTP_401_UNAUTHORIZED) from None
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        raise _error("transfer_not_authorized", "Supplier authorization bearer token is required.", status.HTTP_401_UNAUTHORIZED)
    installation = supplier_store.authorize_request(installation_id, user_id, authorization[7:])
    if installation is None:
        raise _error("transfer_not_authorized", "Supplier installation authorization is invalid, expired, or revoked.", status.HTTP_403_FORBIDDEN)
    return SupplierTransferPrincipal(installation, user_id)


class TransferCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: int
    filename: str = Field(min_length=1, max_length=255)
    total_size: int = Field(ge=0, le=MAX_TOTAL_SIZE)
    sha256: str
    media_type: str | None = Field(default=None, max_length=255)
    source_context: dict[str, object] | None = None


class HashCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: int
    sha256: list[str] = Field(min_length=1, max_length=MAX_HASH_BATCH)


def _require_protocol(version: int) -> None:
    if version != TRANSFER_PROTOCOL_VERSION:
        raise _error("protocol_mismatch", "Unsupported Vault Supplier transfer protocol version.")


def _gate_state(intake: IntakeStore, transfers: TransferStore) -> Literal["READY", "BUSY", "PAUSED"]:
    if intake.gate_status()["state"] != "open":
        return "PAUSED"
    return "BUSY" if transfers.active_count() else "READY"


def _require_ready(intake: IntakeStore, transfers: TransferStore) -> None:
    current = _gate_state(intake, transfers)
    if current == "PAUSED":
        raise _error("intake_paused", "Vault intake is paused.", status.HTTP_503_SERVICE_UNAVAILABLE)
    if current == "BUSY":
        raise _error("intake_busy", "Vault Supplier receiver is busy.", status.HTTP_503_SERVICE_UNAVAILABLE)


def _owned(session: TransferSession | None, principal: SupplierTransferPrincipal) -> TransferSession:
    if session is None:
        raise _error("transfer_not_found", "Transfer session was not found.", status.HTTP_404_NOT_FOUND)
    if session.installation_id != principal.installation.installation_id or session.user_id != principal.user_id or session.vault_id != principal.installation.vault_id:
        raise _error("transfer_not_authorized", "Transfer session is not authorized for this Supplier installation.", status.HTTP_403_FORBIDDEN)
    return session


def _reconcile_part(session: TransferSession, root: Path, store: TransferStore) -> TransferSession:
    part = _part_path(root, session.transfer_id)
    actual = part.stat().st_size if part.exists() and part.is_file() and not part.is_symlink() else 0
    if actual > session.total_size:
        store.fail(session.transfer_id, "size_mismatch")
        raise _error("size_mismatch", "Staged transfer exceeds its declared size.", status.HTTP_409_CONFLICT)
    if actual != session.bytes_received:
        session = store.reconcile(session.transfer_id, actual, "paused" if session.state in ACTIVE_STATES else None) or session
    return session


def _response(session: TransferSession) -> dict[str, object]:
    return {"protocol_version": TRANSFER_PROTOCOL_VERSION, "transfer_id": str(session.transfer_id), "state": session.state, "total_size": session.total_size, "bytes_received": session.bytes_received, "sha256": session.expected_sha256, "upload_may_resume": session.state in {"created", "receiving", "paused"}, "expires_at": session.expires_at, "arrival_hall_receipt_id": str(session.transfer_id) if session.state == "finalized" else None, "arrival_hall_filename": session.arrival_hall_filename}


@router.get("/intake/state")
def intake_state(principal: SupplierTransferPrincipal = Depends(require_supplier_transfer_authorization), intake: IntakeStore = Depends(get_intake_store), transfers: TransferStore = Depends(get_transfer_store)) -> dict[str, object]:
    del principal
    value = _gate_state(intake, transfers)
    return {"protocol_version": TRANSFER_PROTOCOL_VERSION, "state": value, "reason": None if value == "READY" else value.casefold()}


@router.post("/intake/check-hashes")
def check_hashes(body: HashCheck, principal: SupplierTransferPrincipal = Depends(require_supplier_transfer_authorization), transfers: TransferStore = Depends(get_transfer_store)) -> dict[str, object]:
    _require_protocol(body.protocol_version)
    if len(set(body.sha256)) != len(body.sha256) or any(SHA256_RE.fullmatch(value) is None for value in body.sha256):
        raise _error("invalid_checksum", "SHA-256 values must be unique lowercase hexadecimal digests.")
    return {"protocol_version": TRANSFER_PROTOCOL_VERSION, "hashes": [{"sha256": value, "duplicate": transfers.has_duplicate(principal.user_id, value)} for value in body.sha256]}


@router.post("/transfers", status_code=status.HTTP_201_CREATED)
def create_transfer(body: TransferCreate, principal: SupplierTransferPrincipal = Depends(require_supplier_transfer_authorization), intake: IntakeStore = Depends(get_intake_store), transfers: TransferStore = Depends(get_transfer_store), arrival_hall: Path = Depends(require_incoming_path), auth: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    _require_protocol(body.protocol_version)
    _require_ready(intake, transfers)
    if SHA256_RE.fullmatch(body.sha256) is None:
        raise _error("invalid_checksum", "sha256 must be 64 lowercase hexadecimal characters.")
    try:
        filename = validate_filename(body.filename)
    except HTTPException as error:
        raise _error("invalid_filename", "Filename is invalid.", error.status_code) from error
    source_context = _source_context(body.source_context)
    if transfers.has_duplicate(principal.user_id, body.sha256):
        raise _error("duplicate_content", "This checksum already has active duplicate authority.", status.HTTP_409_CONFLICT)
    now = _now()
    session = TransferSession(uuid4(), principal.installation.installation_id, principal.user_id, principal.installation.vault_id, filename, body.total_size, body.sha256, body.media_type, source_context, TRANSFER_PROTOCOL_VERSION, "created", 0, now, now, now + SESSION_LIFETIME)
    try:
        session = transfers.create(session)
        part = _part_path(_staging_root(arrival_hall), session.transfer_id)
        with part.open("xb") as output:
            output.flush(); os.fsync(output.fileno())
    except ValueError as error:
        raise _error(str(error), "Vault Supplier receiver is busy.", status.HTTP_503_SERVICE_UNAVAILABLE) from error
    except OSError as error:
        transfers.fail(session.transfer_id, "receiver_unavailable")
        raise _error("receiver_unavailable", "Vault Supplier receiver staging is unavailable.", status.HTTP_503_SERVICE_UNAVAILABLE) from error
    auth.record_security_event("vault_supplier_transfer_created", user_id=principal.user_id, actor_user_id=principal.user_id, metadata={"installation_id": str(principal.installation.installation_id), "transfer_id": str(session.transfer_id)})
    return {**_response(session), "chunk_size_min": MIN_CHUNK_BYTES, "chunk_size_recommended": RECOMMENDED_CHUNK_BYTES, "chunk_size_max": MAX_CHUNK_BYTES}


@router.get("/transfers/{transfer_id}")
def transfer_status(transfer_id: UUID, principal: SupplierTransferPrincipal = Depends(require_supplier_transfer_authorization), transfers: TransferStore = Depends(get_transfer_store), arrival_hall: Path = Depends(require_incoming_path)) -> dict[str, object]:
    session = _owned(transfers.get(transfer_id), principal)
    if session.state in ACTIVE_STATES:
        session = _reconcile_part(session, _staging_root(arrival_hall), transfers)
    return _response(session)


@router.put("/transfers/{transfer_id}/data")
async def upload_data(transfer_id: UUID, request: Request, principal: SupplierTransferPrincipal = Depends(require_supplier_transfer_authorization), intake: IntakeStore = Depends(get_intake_store), transfers: TransferStore = Depends(get_transfer_store), arrival_hall: Path = Depends(require_incoming_path), auth: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    session = _owned(transfers.get(transfer_id), principal)
    if intake.gate_status()["state"] != "open":
        raise _error("intake_paused", "Vault intake is paused.", status.HTTP_503_SERVICE_UNAVAILABLE)
    if session.expires_at <= _now():
        raise _error("transfer_expired", "Transfer session has expired.", status.HTTP_410_GONE)
    if session.state not in {"created", "receiving", "paused"}:
        raise _error("invalid_transfer_state", "Transfer session cannot receive data in its current state.", status.HTTP_409_CONFLICT)
    try:
        offset = int(request.headers.get("X-PV-Upload-Offset", ""))
        length = int(request.headers.get("Content-Length", ""))
    except ValueError:
        raise _error("invalid_offset", "Exact upload offset and Content-Length are required.") from None
    if offset < 0 or length < MIN_CHUNK_BYTES or length > MAX_CHUNK_BYTES or offset + length > session.total_size:
        raise _error("size_mismatch", "Chunk bounds do not fit the declared transfer size.")
    root = _staging_root(arrival_hall)
    session = _reconcile_part(session, root, transfers)
    if offset != session.bytes_received:
        raise _error("invalid_offset", "Upload offset does not match the server-authoritative resume offset.", status.HTTP_409_CONFLICT)
    part = _part_path(root, session.transfer_id)
    written = 0
    try:
        with part.open("ab", buffering=0) as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > length:
                    raise ValueError("size_mismatch")
                output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        if written != length:
            raise ValueError("size_mismatch")
    except ValueError as error:
        _reconcile_part(session, root, transfers)
        raise _error(str(error), "Chunk length did not match Content-Length.") from error
    except OSError as error:
        _reconcile_part(session, root, transfers)
        raise _error("receiver_unavailable", "Vault Supplier receiver could not write the chunk.", status.HTTP_503_SERVICE_UNAVAILABLE) from error
    updated = transfers.set_progress(session.transfer_id, offset, offset + written)
    if updated is None:
        _reconcile_part(session, root, transfers)
        raise _error("invalid_offset", "Transfer progress changed while the chunk was being written.", status.HTTP_409_CONFLICT)
    if session.state in {"created", "paused"}:
        auth.record_security_event("vault_supplier_transfer_resumed", user_id=principal.user_id, actor_user_id=principal.user_id, metadata={"installation_id": str(principal.installation.installation_id), "transfer_id": str(session.transfer_id)})
    return _response(updated)


def _final_filename(session: TransferSession) -> str:
    path = Path(session.filename)
    return f"{path.stem} (Vault Supplier {session.transfer_id.hex[:8]}){path.suffix}"


@router.post("/transfers/{transfer_id}/finalize")
def finalize_transfer(transfer_id: UUID, principal: SupplierTransferPrincipal = Depends(require_supplier_transfer_authorization), intake: IntakeStore = Depends(get_intake_store), transfers: TransferStore = Depends(get_transfer_store), arrival_hall: Path = Depends(require_incoming_path), auth: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    session = _owned(transfers.get(transfer_id), principal)
    if intake.gate_status()["state"] != "open":
        raise _error("intake_paused", "Vault intake is paused.", status.HTTP_503_SERVICE_UNAVAILABLE)
    if session.state == "finalized":
        return _response(session)
    if session.state not in {"created", "receiving", "paused", "verifying"}:
        raise _error("invalid_transfer_state", "Transfer session cannot be finalized in its current state.", status.HTTP_409_CONFLICT)
    root = _staging_root(arrival_hall)
    session = _reconcile_part(session, root, transfers)
    part = _part_path(root, session.transfer_id)
    if session.bytes_received != session.total_size or not part.is_file() or part.stat().st_size != session.total_size:
        raise _error("size_mismatch", "Transfer is incomplete or staged size does not match.", status.HTTP_409_CONFLICT)
    digest = hashlib.sha256()
    with part.open("rb") as input_file:
        while chunk := input_file.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != session.expected_sha256:
        transfers.fail(session.transfer_id, "checksum_mismatch")
        auth.record_security_event("vault_supplier_transfer_checksum_failed", user_id=principal.user_id, actor_user_id=principal.user_id, metadata={"installation_id": str(principal.installation.installation_id), "transfer_id": str(session.transfer_id)})
        raise _error("checksum_mismatch", "Staged content did not match the expected SHA-256 checksum.", status.HTTP_422_UNPROCESSABLE_ENTITY)
    filename = session.arrival_hall_filename or _final_filename(session)
    if session.state != "verifying":
        session = transfers.begin_verification(session.transfer_id, filename)
        if session is None:
            raise _error("invalid_transfer_state", "Transfer finalization changed concurrently.", status.HTTP_409_CONFLICT)
    destination = arrival_hall / filename
    if not destination.exists():
        # The final link is atomic on this same Arrival Hall filesystem and is
        # never a .part file.  Only the visible completed item reaches the
        # shared Arrival Hall publication boundary below.
        try:
            os.link(part, destination)
            part.unlink()
        except OSError as error:
            raise _error("receiver_unavailable", "Arrival Hall could not atomically publish the verified file.", status.HTTP_503_SERVICE_UNAVAILABLE) from error
        complete_arrival_hall_publication(
            arrival_hall,
            destination,
            SimpleNamespace(user_id=session.user_id),
            source_context=session.source_context,
            supplier_transfer_id=session.transfer_id,
        )
    finalized = transfers.finalize(session.transfer_id)
    if finalized is None:
        raise _error("invalid_transfer_state", "Transfer finalization could not be recorded.", status.HTTP_409_CONFLICT)
    auth.record_security_event("vault_supplier_transfer_finalized", user_id=principal.user_id, actor_user_id=principal.user_id, metadata={"installation_id": str(principal.installation.installation_id), "transfer_id": str(session.transfer_id)})
    return _response(finalized)


@router.delete("/transfers/{transfer_id}")
def abort_transfer(transfer_id: UUID, principal: SupplierTransferPrincipal = Depends(require_supplier_transfer_authorization), transfers: TransferStore = Depends(get_transfer_store), arrival_hall: Path = Depends(require_incoming_path), auth: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    session = _owned(transfers.get(transfer_id), principal)
    if session.state == "finalized":
        raise _error("invalid_transfer_state", "A finalized Arrival Hall transfer cannot be aborted.", status.HTTP_409_CONFLICT)
    aborted = transfers.abort(transfer_id)
    if aborted is None:
        raise _error("invalid_transfer_state", "Transfer cannot be aborted in its current state.", status.HTTP_409_CONFLICT)
    _part_path(_staging_root(arrival_hall), transfer_id).unlink(missing_ok=True)
    auth.record_security_event("vault_supplier_transfer_aborted", user_id=principal.user_id, actor_user_id=principal.user_id, metadata={"installation_id": str(principal.installation.installation_id), "transfer_id": str(transfer_id)})
    return _response(aborted)
