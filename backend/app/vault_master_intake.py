from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
from typing import Literal, Protocol
from urllib.error import URLError
from urllib.request import urlopen
from uuid import UUID, uuid4

import anyio
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
import psycopg
from psycopg.rows import dict_row

from app.auth import ElevatedVaultControlAdministrator
from app.config import get_admin_username, get_database_conninfo, get_upload_max_bytes
from app.incoming import (
    get_incoming_path,
    publish_without_overwriting,
    require_incoming_path,
    validate_filename,
)


router = APIRouter(prefix="/api/vault-master", tags=["vault-master-intake"])
IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9._:-]{16,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_RATE_PER_MINUTE = 60
DEFAULT_FILES_PER_DAY = 1_000
DEFAULT_BYTES_PER_DAY = 100 * 1024**3
DEFAULT_MAX_PENDING = 2_000
DEFAULT_MIN_FREE_BYTES = 5 * 1024**3


@dataclass(frozen=True)
class IntakeSource:
    id: UUID
    owner: str
    name: str
    status: str
    rate_per_minute: int
    files_per_day: int
    bytes_per_day: int
    max_pending: int
    min_free_bytes: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class IntakeReceipt:
    id: UUID
    source_id: UUID
    idempotency_key: str
    filename: str
    declared_size: int
    declared_sha256: str
    status: str
    stored_name: str | None
    received_size: int | None
    computed_sha256: str | None
    rejection_reason: str | None
    attempt_count: int
    created_at: datetime
    updated_at: datetime


class IntakeRejected(Exception):
    def __init__(self, code: int, reason: str, retry_after: int | None = None):
        self.code = code
        self.reason = reason
        self.retry_after = retry_after


class IntakeStore(Protocol):
    def create_source(self, owner: str, name: str, **limits: int) -> tuple[IntakeSource, str]: ...
    def list_sources(self, owner: str) -> list[IntakeSource]: ...
    def set_source_status(self, source_id: UUID, owner: str, value: str) -> IntakeSource | None: ...
    def set_global_enabled(self, enabled: bool) -> None: ...
    def global_enabled(self) -> bool: ...
    def gate_status(self) -> dict[str, int | str]: ...
    def request_gate(self, action: Literal["pause", "resume"]) -> dict[str, int | str]: ...
    def begin_transfer(self) -> None: ...
    def finish_transfer(self) -> None: ...
    def reserve(self, source_id: UUID, token: str, key: str, filename: str, size: int, sha256: str) -> tuple[IntakeReceipt, bool]: ...
    def complete(self, receipt_id: UUID, stored_name: str, size: int, sha256: str) -> IntakeReceipt: ...
    def fail(self, receipt_id: UUID, reason: str) -> None: ...
    def list_receipts(self, owner: str, limit: int = 100) -> list[IntakeReceipt]: ...
    def pending_count(self) -> int: ...
    def source_min_free_bytes(self, source_id: UUID) -> int: ...


def _source_from_row(row):
    row.pop("token_hash", None)
    return IntakeSource(**row)


def _receipt_from_row(row):
    return IntakeReceipt(**row)


class PostgresIntakeStore:
    def __init__(self, conninfo: str):
        self.conninfo = conninfo

    def connect(self):
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def initialize(self):
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_intake_control (
                    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
                    enabled BOOLEAN NOT NULL DEFAULT TRUE,
                    gate_state TEXT NOT NULL DEFAULT 'open'
                      CHECK (gate_state IN ('open','pausing','paused','resuming','error')),
                    active_transfers INTEGER NOT NULL DEFAULT 0 CHECK (active_transfers >= 0),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO vault_intake_control (singleton) VALUES (TRUE)
                ON CONFLICT (singleton) DO NOTHING;
                ALTER TABLE vault_intake_control ADD COLUMN IF NOT EXISTS gate_state TEXT;
                ALTER TABLE vault_intake_control ADD COLUMN IF NOT EXISTS active_transfers INTEGER;
                UPDATE vault_intake_control
                SET gate_state=COALESCE(gate_state, 'open'),
                    active_transfers=COALESCE(active_transfers, 0),
                    enabled=(COALESCE(gate_state, 'open') = 'open');
                -- A streaming request cannot survive a backend restart. Do not
                -- leave a deliberate pause stuck in Pausing after that boundary.
                UPDATE vault_intake_control
                SET active_transfers=0, gate_state='paused', enabled=FALSE
                WHERE gate_state='pausing';
                CREATE TABLE IF NOT EXISTS vault_intake_sources (
                    id UUID PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
                    token_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'disabled'
                      CHECK (status IN ('enabled','paused','revoked','disabled')),
                    rate_per_minute INTEGER NOT NULL CHECK (rate_per_minute BETWEEN 1 AND 600),
                    files_per_day INTEGER NOT NULL CHECK (files_per_day BETWEEN 1 AND 100000),
                    bytes_per_day BIGINT NOT NULL CHECK (bytes_per_day > 0),
                    max_pending INTEGER NOT NULL CHECK (max_pending BETWEEN 1 AND 100000),
                    min_free_bytes BIGINT NOT NULL CHECK (min_free_bytes >= 0),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner,name)
                );
                CREATE TABLE IF NOT EXISTS vault_intake_receipts (
                    id UUID PRIMARY KEY, source_id UUID NOT NULL REFERENCES vault_intake_sources(id),
                    idempotency_key TEXT NOT NULL, filename TEXT NOT NULL,
                    declared_size BIGINT NOT NULL, declared_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('reserved','completed','failed','rejected')),
                    stored_name TEXT, received_size BIGINT, computed_sha256 TEXT,
                    rejection_reason TEXT, attempt_count INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source_id,idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS vault_intake_receipts_source_created
                  ON vault_intake_receipts(source_id,created_at DESC);
            """)

    def create_source(self, owner, name, **limits):
        token = secrets.token_urlsafe(32)
        values = dict(rate_per_minute=DEFAULT_RATE_PER_MINUTE, files_per_day=DEFAULT_FILES_PER_DAY,
                      bytes_per_day=DEFAULT_BYTES_PER_DAY, max_pending=DEFAULT_MAX_PENDING,
                      min_free_bytes=DEFAULT_MIN_FREE_BYTES) | limits
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_intake_sources
              (id,owner,name,token_hash,status,rate_per_minute,files_per_day,bytes_per_day,max_pending,min_free_bytes)
              VALUES (%s,%s,%s,%s,'disabled',%s,%s,%s,%s,%s) RETURNING *""",
              (uuid4(), owner, name, hashlib.sha256(token.encode()).hexdigest(), *values.values()))
            return _source_from_row(cursor.fetchone()), token

    def list_sources(self, owner):
        with self.connect() as c, c.cursor() as q:
            q.execute("SELECT * FROM vault_intake_sources WHERE owner=%s ORDER BY created_at", (owner,))
            return [_source_from_row(r) for r in q.fetchall()]

    def set_source_status(self, source_id, owner, value):
        if value not in {"enabled", "paused", "revoked", "disabled"}: return None
        with self.connect() as c, c.cursor() as q:
            q.execute("UPDATE vault_intake_sources SET status=%s,updated_at=now() WHERE id=%s AND owner=%s RETURNING *", (value,source_id,owner))
            row=q.fetchone(); return _source_from_row(row) if row else None

    def set_global_enabled(self, enabled):
        self.request_gate("resume" if enabled else "pause")

    def global_enabled(self):
        return self.gate_status()["state"] == "open"

    def gate_status(self):
        with self.connect() as c, c.cursor() as q:
            q.execute("SELECT gate_state,active_transfers FROM vault_intake_control WHERE singleton")
            row=q.fetchone(); return {"state": row["gate_state"], "active_transfers": int(row["active_transfers"])}

    def request_gate(self, action):
        with self.connect() as c, c.cursor() as q:
            q.execute("SELECT active_transfers FROM vault_intake_control WHERE singleton FOR UPDATE")
            active=int(q.fetchone()["active_transfers"])
            state="paused" if action == "pause" and active == 0 else "pausing" if action == "pause" else "open"
            q.execute("UPDATE vault_intake_control SET gate_state=%s,enabled=%s,updated_at=now() WHERE singleton", (state, state == "open"))
            return {"state": state, "active_transfers": active}

    def begin_transfer(self):
        with self.connect() as c, c.cursor() as q:
            q.execute("SELECT gate_state FROM vault_intake_control WHERE singleton FOR UPDATE")
            if q.fetchone()["gate_state"] != "open": raise IntakeRejected(503,"Global Intake is paused",60)
            q.execute("UPDATE vault_intake_control SET active_transfers=active_transfers+1,updated_at=now() WHERE singleton")

    def finish_transfer(self):
        with self.connect() as c, c.cursor() as q:
            self._finish_transfer_locked(q)

    def _finish_transfer_locked(self, q):
        q.execute("SELECT gate_state,active_transfers FROM vault_intake_control WHERE singleton FOR UPDATE")
        row=q.fetchone(); active=max(0,int(row["active_transfers"])-1)
        state="paused" if row["gate_state"] == "pausing" and active == 0 else row["gate_state"]
        q.execute("UPDATE vault_intake_control SET active_transfers=%s,gate_state=%s,updated_at=now() WHERE singleton", (active,state))

    def pending_count(self):
        with self.connect() as c, c.cursor() as q:
            q.execute("SELECT count(*) AS n FROM vault_master_items WHERE source_kind='incoming' AND state NOT IN ('moved','rejected','duplicate_removed')")
            return int(q.fetchone()["n"])

    def source_min_free_bytes(self, source_id):
        with self.connect() as c, c.cursor() as q:
            q.execute("SELECT min_free_bytes FROM vault_intake_sources WHERE id=%s",(source_id,))
            row=q.fetchone(); return int(row["min_free_bytes"]) if row else DEFAULT_MIN_FREE_BYTES

    def reserve(self, source_id, token, key, filename, size, sha256):
        with self.connect() as c, c.cursor() as q:
            q.execute("SELECT * FROM vault_intake_sources WHERE id=%s FOR UPDATE", (source_id,)); row=q.fetchone()
            if not row or not secrets.compare_digest(row["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
                raise IntakeRejected(401, "Invalid intake source credentials")
            source=_source_from_row(dict(row))
            q.execute("SELECT gate_state FROM vault_intake_control WHERE singleton FOR UPDATE");
            if q.fetchone()["gate_state"] != "open": raise IntakeRejected(503,"Global Intake is paused",60)
            if source.status != "enabled": raise IntakeRejected(503,f"Intake source is {source.status}",60)
            q.execute("SELECT * FROM vault_intake_receipts WHERE source_id=%s AND idempotency_key=%s",(source_id,key)); prior=q.fetchone()
            if prior:
                receipt=_receipt_from_row(prior)
                if (receipt.filename,receipt.declared_size,receipt.declared_sha256)!=(filename,size,sha256):
                    raise IntakeRejected(409,"Idempotency key was reused with different file facts")
                if receipt.status=="completed": return receipt, True
                raise IntakeRejected(409,"This idempotency key already has an unfinished or failed attempt")
            q.execute("SELECT count(*) AS n,coalesce(sum(declared_size),0) AS bytes FROM vault_intake_receipts WHERE source_id=%s AND created_at>now()-interval '24 hours' AND status IN ('reserved','completed')",(source_id,)); day=q.fetchone()
            q.execute("SELECT count(*) AS n FROM vault_intake_receipts WHERE source_id=%s AND created_at>now()-interval '1 minute'",(source_id,)); minute=q.fetchone()
            if minute["n"] >= source.rate_per_minute: raise IntakeRejected(429,"Per-minute source rate limit reached",60)
            if day["n"] >= source.files_per_day or day["bytes"]+size > source.bytes_per_day: raise IntakeRejected(429,"Rolling source quota reached",3600)
            if self.pending_count() >= source.max_pending: raise IntakeRejected(503,"Arrival Hall backlog limit reached",300)
            q.execute("SELECT count(*) AS n FROM vault_intake_receipts WHERE source_id=%s AND declared_sha256=%s AND status='completed' AND created_at>now()-interval '24 hours'",(source_id,sha256))
            duplicate_count=int(q.fetchone()["n"])
            if duplicate_count:
                q.execute("SELECT count(*) AS n FROM vault_intake_receipts WHERE source_id=%s AND rejection_reason='duplicate_checksum' AND created_at>now()-interval '10 minutes'",(source_id,))
                storm=int(q.fetchone()["n"])+1
                q.execute("""INSERT INTO vault_intake_receipts
                  (id,source_id,idempotency_key,filename,declared_size,declared_sha256,status,rejection_reason)
                  VALUES (%s,%s,%s,%s,%s,%s,'rejected','duplicate_checksum')""",(uuid4(),source_id,key,filename,size,sha256))
                if storm>=10:
                    q.execute("UPDATE vault_intake_sources SET status='paused',updated_at=now() WHERE id=%s",(source_id,))
                c.commit()
                raise IntakeRejected(409,"This source already submitted the same checksum")
            receipt_id=uuid4(); q.execute("""INSERT INTO vault_intake_receipts
              (id,source_id,idempotency_key,filename,declared_size,declared_sha256,status)
              VALUES (%s,%s,%s,%s,%s,%s,'reserved') RETURNING *""",(receipt_id,source_id,key,filename,size,sha256))
            receipt=_receipt_from_row(q.fetchone())
            q.execute("UPDATE vault_intake_control SET active_transfers=active_transfers+1,updated_at=now() WHERE singleton")
            return receipt, False

    def complete(self, receipt_id, stored_name, size, sha256):
        with self.connect() as c, c.cursor() as q:
            q.execute("UPDATE vault_intake_receipts SET status='completed',stored_name=%s,received_size=%s,computed_sha256=%s,updated_at=now() WHERE id=%s AND status='reserved' RETURNING *",(stored_name,size,sha256,receipt_id))
            row=q.fetchone()
            if row: self._finish_transfer_locked(q)
            return _receipt_from_row(row) if row else None

    def fail(self, receipt_id, reason):
        with self.connect() as c, c.cursor() as q:
            q.execute("UPDATE vault_intake_receipts SET status='failed',rejection_reason=%s,updated_at=now() WHERE id=%s AND status='reserved' RETURNING source_id",(reason,receipt_id))
            row=q.fetchone()
            if row:
                self._finish_transfer_locked(q)
                q.execute("SELECT count(*) AS n FROM vault_intake_receipts WHERE source_id=%s AND status IN ('failed','rejected') AND created_at>now()-interval '10 minutes'",(row["source_id"],))
                if int(q.fetchone()["n"])>=5:
                    q.execute("UPDATE vault_intake_sources SET status='paused',updated_at=now() WHERE id=%s",(row["source_id"],))

    def list_receipts(self, owner, limit=100):
        with self.connect() as c, c.cursor() as q:
            q.execute("SELECT r.* FROM vault_intake_receipts r JOIN vault_intake_sources s ON s.id=r.source_id WHERE s.owner=%s ORDER BY r.created_at DESC LIMIT %s",(owner,limit))
            return [_receipt_from_row(r) for r in q.fetchall()]


class MemoryIntakeStore:
    def __init__(self):
        self.sources: dict[UUID, IntakeSource] = {}
        self.token_hashes: dict[UUID, str] = {}
        self.receipts: dict[UUID, IntakeReceipt] = {}
        self.state = "open"
        self.active_transfers = 0
        self.pending = 0

    def create_source(self, owner, name, **limits):
        token=secrets.token_urlsafe(32); now=datetime.now(timezone.utc)
        values=dict(rate_per_minute=DEFAULT_RATE_PER_MINUTE,files_per_day=DEFAULT_FILES_PER_DAY,
                    bytes_per_day=DEFAULT_BYTES_PER_DAY,max_pending=DEFAULT_MAX_PENDING,
                    min_free_bytes=0)|limits
        source=IntakeSource(uuid4(),owner,name,"disabled",**values,created_at=now,updated_at=now)
        self.sources[source.id]=source; self.token_hashes[source.id]=hashlib.sha256(token.encode()).hexdigest()
        return source,token

    def list_sources(self, owner): return [s for s in self.sources.values() if s.owner==owner]
    def set_source_status(self, source_id, owner, value):
        source=self.sources.get(source_id)
        if not source or source.owner!=owner or value not in {"enabled","paused","disabled","revoked"}: return None
        updated=IntakeSource(**{**source.__dict__,"status":value,"updated_at":datetime.now(timezone.utc)})
        self.sources[source_id]=updated; return updated
    def set_global_enabled(self, enabled): self.request_gate("resume" if enabled else "pause")
    def global_enabled(self): return self.state == "open"
    def gate_status(self): return {"state": self.state, "active_transfers": self.active_transfers}
    def request_gate(self, action):
        if action == "pause": self.state="paused" if self.active_transfers == 0 else "pausing"
        else: self.state="open"
        return self.gate_status()
    def begin_transfer(self):
        if self.state != "open": raise IntakeRejected(503,"Global Intake is paused",60)
        self.active_transfers += 1
    def finish_transfer(self):
        self.active_transfers=max(0,self.active_transfers-1)
        if self.state == "pausing" and self.active_transfers == 0: self.state="paused"
    def pending_count(self): return self.pending
    def source_min_free_bytes(self, source_id): return self.sources[source_id].min_free_bytes

    def reserve(self, source_id, token, key, filename, size, sha256):
        source=self.sources.get(source_id)
        if not source or not secrets.compare_digest(self.token_hashes[source_id],hashlib.sha256(token.encode()).hexdigest()): raise IntakeRejected(401,"Invalid intake source credentials")
        if self.state != "open": raise IntakeRejected(503,"Global Intake is paused",60)
        if source.status!="enabled": raise IntakeRejected(503,f"Intake source is {source.status}",60)
        prior=next((r for r in self.receipts.values() if r.source_id==source_id and r.idempotency_key==key),None)
        if prior:
            if (prior.filename,prior.declared_size,prior.declared_sha256)!=(filename,size,sha256): raise IntakeRejected(409,"Idempotency key was reused with different file facts")
            if prior.status=="completed": return prior,True
            raise IntakeRejected(409,"This idempotency key already has an unfinished or failed attempt")
        now=datetime.now(timezone.utc)
        recent_day=[r for r in self.receipts.values() if r.source_id==source_id and (now-r.created_at).total_seconds()<86400 and r.status in {"reserved","completed"}]
        recent_minute=[r for r in self.receipts.values() if r.source_id==source_id and (now-r.created_at).total_seconds()<60]
        if len(recent_minute)>=source.rate_per_minute: raise IntakeRejected(429,"Per-minute source rate limit reached",60)
        if len(recent_day)>=source.files_per_day or sum(r.declared_size for r in recent_day)+size>source.bytes_per_day: raise IntakeRejected(429,"Rolling source quota reached",3600)
        if self.pending>=source.max_pending: raise IntakeRejected(503,"Arrival Hall backlog limit reached",300)
        duplicate=any(r.source_id==source_id and r.declared_sha256==sha256 and r.status=="completed" for r in self.receipts.values())
        if duplicate:
            rejected=IntakeReceipt(uuid4(),source_id,key,filename,size,sha256,"rejected",None,None,None,"duplicate_checksum",1,now,now)
            self.receipts[rejected.id]=rejected
            storms=sum(1 for r in self.receipts.values() if r.source_id==source_id and r.rejection_reason=="duplicate_checksum" and (now-r.created_at).total_seconds()<600)
            if storms>=10: self.set_source_status(source_id,source.owner,"paused")
            raise IntakeRejected(409,"This source already submitted the same checksum")
        receipt=IntakeReceipt(uuid4(),source_id,key,filename,size,sha256,"reserved",None,None,None,None,1,now,now)
        self.receipts[receipt.id]=receipt; self.active_transfers += 1; return receipt,False

    def complete(self, receipt_id, stored_name, size, sha256):
        receipt=self.receipts[receipt_id]; updated=IntakeReceipt(**{**receipt.__dict__,"status":"completed","stored_name":stored_name,"received_size":size,"computed_sha256":sha256,"updated_at":datetime.now(timezone.utc)})
        self.receipts[receipt_id]=updated; self.finish_transfer(); return updated
    def fail(self, receipt_id, reason):
        receipt=self.receipts[receipt_id]; self.receipts[receipt_id]=IntakeReceipt(**{**receipt.__dict__,"status":"failed","rejection_reason":reason,"updated_at":datetime.now(timezone.utc)}); self.finish_transfer()
        recent_failures=sum(1 for r in self.receipts.values() if r.source_id==receipt.source_id and r.status in {"failed","rejected"} and (datetime.now(timezone.utc)-r.created_at).total_seconds()<600)
        if recent_failures>=5: self.set_source_status(receipt.source_id,self.sources[receipt.source_id].owner,"paused")
    def list_receipts(self, owner, limit=100):
        source_ids={s.id for s in self.list_sources(owner)}
        return sorted((r for r in self.receipts.values() if r.source_id in source_ids),key=lambda r:r.created_at,reverse=True)[:limit]


@lru_cache
def get_intake_store() -> IntakeStore:
    return PostgresIntakeStore(get_database_conninfo())


class SourceCreate(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name: str=Field(min_length=3,max_length=80,pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]+$")
    rate_per_minute: int=Field(DEFAULT_RATE_PER_MINUTE,ge=1,le=600)
    files_per_day: int=Field(DEFAULT_FILES_PER_DAY,ge=1,le=100000)
    bytes_per_day: int=Field(DEFAULT_BYTES_PER_DAY,gt=0)
    max_pending: int=Field(DEFAULT_MAX_PENDING,ge=1,le=100000)
    min_free_bytes: int=Field(DEFAULT_MIN_FREE_BYTES,ge=0)


class SourceStatus(BaseModel):
    model_config=ConfigDict(extra="forbid")
    status: Literal["enabled", "paused", "disabled", "revoked"]


class SourceResult(BaseModel):
    model_config=ConfigDict(from_attributes=True)
    id: UUID; owner: str; name: str; status: str; rate_per_minute: int
    files_per_day: int; bytes_per_day: int; max_pending: int; min_free_bytes: int
    created_at: datetime; updated_at: datetime


class SourceCreated(BaseModel):
    source: SourceResult
    token: str


class ControlResult(BaseModel):
    enabled: bool
    sources: list[SourceResult]
    receipts: list[dict]
    pending_items: int
    health: dict[str, dict[str, str | int | bool | None]]


def get_operational_health(
    incoming_path: Path = Depends(get_incoming_path),
) -> dict[str, dict[str, str | int | bool | None]]:
    storage: dict[str, str | int | bool | None] = {
        "status": "unavailable",
        "writable": False,
        "free_bytes": None,
    }
    try:
        resolved = incoming_path.resolve(strict=True)
        usage = shutil.disk_usage(resolved)
        storage = {
            "status": "ok" if resolved.is_dir() and os.access(resolved, os.W_OK) else "unavailable",
            "writable": resolved.is_dir() and os.access(resolved, os.W_OK),
            "free_bytes": usage.free,
        }
    except OSError:
        pass

    florence: dict[str, str | int | bool | None] = {
        "status": "unavailable",
        "model": None,
        "device": None,
    }
    endpoint = os.getenv("PV_FLORENCE_URL", "http://pv-florence2:8080/ocr")
    health_url = endpoint.rsplit("/", 1)[0] + "/health"
    try:
        with urlopen(health_url, timeout=5) as response:  # nosec B310 - fixed local service URL
            import json

            payload = json.load(response)
            florence = {
                "status": "ok" if payload.get("status") == "ok" else "degraded",
                "model": payload.get("model"),
                "device": payload.get("device"),
                "active_requests": payload.get("active_requests"),
            }
    except (OSError, URLError, ValueError):
        pass

    return {
        "database": {"status": "ok"},
        "arrival_hall": storage,
        "florence": florence,
        "backup": {"status": "host-managed"},
    }


@router.get("/control",response_model=ControlResult)
def control(username: ElevatedVaultControlAdministrator, response: Response, store: IntakeStore=Depends(get_intake_store), health: dict=Depends(get_operational_health)):
    response.headers["Cache-Control"]="private, no-store"
    return ControlResult(enabled=store.global_enabled(),sources=[SourceResult.model_validate(x) for x in store.list_sources(username)],receipts=[r.__dict__ for r in store.list_receipts(username)],pending_items=store.pending_count(),health=health)


@router.post("/control/sources",response_model=SourceCreated)
def create_source(body: SourceCreate, username: ElevatedVaultControlAdministrator, store: IntakeStore=Depends(get_intake_store)):
    source,token=store.create_source(username,body.name,**body.model_dump(exclude={"name"}))
    return SourceCreated(source=SourceResult.model_validate(source),token=token)


@router.patch("/control/sources/{source_id}",response_model=SourceResult)
def source_status(source_id: UUID, body: SourceStatus, username: ElevatedVaultControlAdministrator, store: IntakeStore=Depends(get_intake_store)):
    source=store.set_source_status(source_id,username,body.status)
    if not source: raise HTTPException(status_code=404)
    return SourceResult.model_validate(source)


@router.post("/control/intake/{value}")
def global_status(value: str, username: ElevatedVaultControlAdministrator, store: IntakeStore=Depends(get_intake_store)):
    if value not in {"enable","pause"}: raise HTTPException(status_code=422)
    store.set_global_enabled(value=="enable"); return {"enabled":value=="enable"}


@router.post("/intake")
async def intake(request: Request, response: Response,
    source_id: UUID=Header(alias="X-PV-Source-ID"), key: str=Header(alias="X-PV-Idempotency-Key"),
    raw_filename: str=Header(alias="X-PV-Filename"), declared_sha256: str=Header(alias="X-PV-SHA256"),
    authorization: str=Header(alias="Authorization"), incoming_path: Path=Depends(require_incoming_path),
    max_upload_bytes: int=Depends(get_upload_max_bytes), store: IntakeStore=Depends(get_intake_store)):
    response.headers["Cache-Control"]="private, no-store"
    if not authorization.startswith("Bearer "): raise HTTPException(status_code=401,detail="Source bearer token required")
    if not IDEMPOTENCY_KEY.fullmatch(key) or not SHA256.fullmatch(declared_sha256): raise HTTPException(status_code=400,detail="Invalid idempotency key or SHA-256")
    filename=validate_filename(raw_filename)
    try: size=int(request.headers.get("Content-Length", ""))
    except ValueError: raise HTTPException(status_code=411,detail="Exact Content-Length is required")
    if size < 0 or size > max_upload_bytes: raise HTTPException(status_code=413,detail="File exceeds the intake size limit")
    try: receipt,replayed=store.reserve(source_id,authorization[7:],key,filename,size,declared_sha256)
    except IntakeRejected as e: raise HTTPException(status_code=e.code,detail=e.reason,headers={"Retry-After":str(e.retry_after)} if e.retry_after else None)
    if replayed: return {"status":"completed","receipt_id":str(receipt.id),"stored_name":receipt.stored_name,"idempotent_replay":True}
    if shutil.disk_usage(incoming_path).free-size < store.source_min_free_bytes(source_id):
        store.fail(receipt.id,"free_space_reserve"); raise HTTPException(status_code=507,detail="Arrival Hall free-space reserve would be breached")
    temporary=incoming_path/f".pv-upload-{uuid4().hex}.part"; received=0; digest=hashlib.sha256()
    try:
        async with await anyio.open_file(temporary,"xb") as output:
            async for chunk in request.stream():
                received+=len(chunk); digest.update(chunk)
                if received>size: raise ValueError("received_more_than_declared")
                await output.write(chunk)
        computed=digest.hexdigest()
        if received!=size or computed!=declared_sha256: raise ValueError("length_or_checksum_mismatch")
        destination=publish_without_overwriting(temporary,incoming_path,filename)
        completed=store.complete(receipt.id,destination.name,received,computed)
        return {"status":"completed","receipt_id":str(completed.id),"stored_name":destination.name,"idempotent_replay":False}
    except ValueError as error:
        temporary.unlink(missing_ok=True); store.fail(receipt.id,str(error)); raise HTTPException(status_code=422,detail="Received file facts did not match the declaration")
    except Exception:
        temporary.unlink(missing_ok=True); store.fail(receipt.id,"storage_failure"); raise
