"""Stage 4 read-only Vault Services status and the bounded Jellyfin scan action."""
from __future__ import annotations

from datetime import UTC, datetime
import os
import threading
import time
from typing import Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response

from app.auth import AuthenticatedAdministrator, require_vault_control_elevated_administrator
from app.config import get_admin_username, get_database_conninfo
from app.incoming import get_arrival_hall_path
from app.vault_control import _collect_database_and_jobs
from app.vault_master import VaultMasterStore, get_vault_master_store
from app.vault_master_ingestion_ai import get_ingestion_ai_store
from app.vault_master_jellyfin import (
    JellyfinUnavailableError,
    get_jellyfin_service_status,
    request_jellyfin_library_scan,
)
from app.vault_master_intake import get_operational_health


router = APIRouter(prefix="/api/vault-control/services", tags=["vault-control-services"], dependencies=[Depends(require_vault_control_elevated_administrator)])
STARTED_AT = time.monotonic()
SCAN_COOLDOWN_SECONDS = 15
_state_lock = threading.Lock()
_worker_state = "starting"
_last_scan_requested_at: datetime | None = None
_last_scan_monotonic = 0.0


def set_worker_state(value: str) -> None:
    global _worker_state
    with _state_lock:
        _worker_state = value



def _unavailable() -> str:
    return "Unavailable"


def _vault_master(store: VaultMasterStore) -> dict[str, object]:
    try:
        batches = store.list_batches()
        active = next((item for item in batches if item.get("status") == "scanning"), None)
        queued = sum(1 for item in batches if item.get("status") == "queued")
        failed = sum(1 for item in batches if item.get("status") == "failed")
        completed = next((item for item in batches if item.get("completed_at")), None)
        activity = store.list_activity(1, include_file_inventory=False, include_file_analysis=False, include_empty_scans=False)
    except Exception:
        return {"status": "unavailable", "current_job": None, "queue_length": None, "last_completed_job": None, "failed_jobs": None, "last_activity": None, "warning": "Vault Master status could not be retrieved."}
    return {
        "status": "running" if active else "idle",
        "current_job": _job(active),
        "queue_length": queued,
        "last_completed_job": _job(completed),
        "failed_jobs": failed,
        "last_activity": activity[0].created_at.isoformat() if activity else None,
        "warning": None,
    }


def _job(value: dict[str, object] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {"source": value.get("source_kind"), "status": value.get("status"), "item_count": value.get("item_count"), "completed_at": _iso(value.get("completed_at")), "error": value.get("error")}


def _iso(value: object) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _florence() -> dict[str, object]:
    health = get_operational_health(get_arrival_hall_path()).get("florence", {})
    base_status = health.get("status")
    if base_status not in {"ok", "degraded"}:
        return {"status": "unavailable", "model": None, "device": None, "gpu_usage": None, "current_job": None, "queue_length": None, "last_successful_inference": None, "warnings": ["Florence did not respond."], "last_activity": None}
    try:
        store = get_ingestion_ai_store()
        jobs = store.list_all_jobs()
        evidence = store.list_all_evidence()
        current = next((job for job in jobs if job.status == "processing"), None)
        queued = sum(1 for job in jobs if job.status == "queued")
        failures = [job.error for job in jobs if job.status == "failed" and job.error]
        latest = evidence[0] if evidence else None
    except Exception:
        current = None; queued = None; failures = ["Florence job telemetry is unavailable."]; latest = None
    return {
        "status": "busy" if current else "ready" if base_status == "ok" else "unavailable",
        "model": health.get("model"), "device": health.get("device"),
        "gpu_usage": "Active inference" if health.get("device") == "GPU" and int(health.get("active_requests") or 0) else "Idle" if health.get("device") == "GPU" else None,
        "current_job": {"status": current.status} if current else None,
        "queue_length": queued,
        "last_successful_inference": latest.created_at.isoformat() if latest else None,
        "warnings": failures, "last_activity": latest.created_at.isoformat() if latest else None,
    }


def _jellyfin() -> dict[str, object]:
    try:
        details = get_jellyfin_service_status()
    except (JellyfinUnavailableError, OSError, RuntimeError):
        return {"status": "unavailable", "version": None, "active_streams": None, "scan_state": "Unavailable", "last_completed_scan": None, "warnings": ["Jellyfin did not respond."], "last_scan_requested_at": _last_scan()}
    return {"status": "healthy", **details, "warnings": [], "last_scan_requested_at": _last_scan()}


def _last_scan() -> str | None:
    with _state_lock:
        return _last_scan_requested_at.isoformat() if _last_scan_requested_at else None


def _database() -> dict[str, object]:
    database, _jobs, _issues = _collect_database_and_jobs()
    if database["status"] == "offline":
        return {**database, "size_bytes": None, "active_connections": None, "schema_version": None}
    try:
        with psycopg.connect(get_database_conninfo()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_database_size(current_database()), count(*) FILTER (WHERE state <> 'idle') FROM pg_stat_activity WHERE datname=current_database()")
            size, active = cursor.fetchone()
    except (OSError, RuntimeError, psycopg.Error):
        size = active = None
    return {**database, "size_bytes": size, "active_connections": active, "schema_version": None}


def _backend() -> dict[str, object]:
    with _state_lock:
        worker = _worker_state
    return {"status": "healthy", "version": "0.1.0", "uptime_seconds": int(time.monotonic() - STARTED_AT), "request_errors": None, "worker": {"architecture": "embedded Vault Master worker", "state": worker}, "warning": None}


def _aggregate(services: dict[str, dict[str, object]]) -> dict[str, object]:
    failed = [name for name, item in services.items() if item["status"] in {"offline", "unavailable"}]
    warnings = [name for name, item in services.items() if item["status"] in {"degraded", "busy"}]
    operational = len(services) - len(failed)
    status = "critical" if "database" in failed or "backend" in failed else "attention_required" if failed or warnings else "healthy"
    return {"status": status, "operational": operational, "warnings": len(warnings), "failures": len(failed), "affected_services": failed + warnings}


def collect_services(store: VaultMasterStore | None = None) -> dict[str, object]:
    services: dict[str, dict[str, object]] = {
        "vault_master": _vault_master(store or get_vault_master_store()),
        "florence": _florence(), "jellyfin": _jellyfin(), "database": _database(), "backend": _backend(),
    }
    return {"generated_at": datetime.now(UTC).isoformat(), "overall": _aggregate(services), **services}


@router.get("")
def get_services(response: Response, _: AuthenticatedAdministrator, store: VaultMasterStore = Depends(get_vault_master_store)) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    return collect_services(store)


@router.post("/jellyfin/scan")
def scan_jellyfin_library(_: AuthenticatedAdministrator) -> dict[str, str]:
    global _last_scan_monotonic, _last_scan_requested_at
    with _state_lock:
        if time.monotonic() - _last_scan_monotonic < SCAN_COOLDOWN_SECONDS:
            raise HTTPException(status_code=429, detail="A Jellyfin scan was requested recently. Please wait.")
        try:
            request_jellyfin_library_scan()
        except JellyfinUnavailableError as error:
            raise HTTPException(status_code=503, detail="Jellyfin library scan could not be started.") from error
        _last_scan_monotonic = time.monotonic(); _last_scan_requested_at = datetime.now(UTC)
    return {"status": "triggered", "requested_at": _last_scan_requested_at.isoformat()}
