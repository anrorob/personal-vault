from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
import time
from typing import Literal

import psycopg
from fastapi import APIRouter, Depends, Response

from app.auth import AuthenticatedAdministrator, require_vault_control_elevated_administrator
from app.config import get_database_conninfo
from app.vault_master_api import (
    get_catalogue_preview_roots,
    get_destination_paths,
    get_quarantine_root,
)


router = APIRouter(prefix="/api/vault-control", tags=["vault-control"], dependencies=[Depends(require_vault_control_elevated_administrator)])

HealthState = Literal["healthy", "attention_required", "critical"]
IssueSeverity = Literal["warning", "critical"]

LOW_SPACE_PERCENT = 10
DEGRADED_DATABASE_RESPONSE_MS = 250
UNFINISHED_JOB_MINUTES = 30
FAILED_JOB_RETENTION_DAYS = 3


@dataclass(frozen=True)
class OverviewIssue:
    severity: IssueSeverity
    message: str


def _format_storage_name(vault_path: str) -> str:
    return vault_path.removeprefix("/vault/") or "Vault storage"


def _collect_storage() -> tuple[dict[str, object], list[OverviewIssue]]:
    volumes: dict[int, dict[str, object]] = {}
    issues: list[OverviewIssue] = []
    writable_paths = {*get_destination_paths().values(), get_quarantine_root()}

    for vault_path, path in get_catalogue_preview_roots().items():
        try:
            stats = os.statvfs(path)
            device = path.stat().st_dev
        except OSError:
            issues.append(
                OverviewIssue(
                    "critical",
                    f"{_format_storage_name(vault_path)} storage is unavailable.",
                )
            )
            continue

        if path in writable_paths and stats.f_flag & getattr(os, "ST_RDONLY", 1):
            issues.append(
                OverviewIssue(
                    "critical",
                    f"{_format_storage_name(vault_path)} storage is read-only.",
                )
            )

        total_bytes = stats.f_blocks * stats.f_frsize
        free_bytes = stats.f_bavail * stats.f_frsize
        volume = volumes.setdefault(
            device,
            {"total_bytes": total_bytes, "free_bytes": free_bytes, "paths": []},
        )
        volume["paths"].append(vault_path)  # type: ignore[index]

    if not volumes:
        return {"total_bytes": None, "free_bytes": None, "low_space": []}, issues

    low_space: list[str] = []
    for volume in volumes.values():
        total_bytes = int(volume["total_bytes"])
        free_bytes = int(volume["free_bytes"])
        paths = volume["paths"]
        if total_bytes and free_bytes / total_bytes * 100 < LOW_SPACE_PERCENT:
            name = _format_storage_name(paths[0])  # type: ignore[index]
            low_space.append(name)
            issues.append(
                OverviewIssue("warning", f"{name} storage is low on free space.")
            )

    return {
        "total_bytes": sum(int(volume["total_bytes"]) for volume in volumes.values()),
        "free_bytes": sum(int(volume["free_bytes"]) for volume in volumes.values()),
        "low_space": low_space,
    }, issues


def _collect_jobs() -> dict[str, int] | None:
    try:
        with psycopg.connect(get_database_conninfo()) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(running), 0)::int,
                    COALESCE(SUM(queued), 0)::int,
                    COALESCE(SUM(failed), 0)::int,
                    COALESCE(SUM(unfinished), 0)::int
                FROM (
                    SELECT
                        count(*) FILTER (WHERE status = 'scanning') AS running,
                        count(*) FILTER (WHERE status = 'queued') AS queued,
                        count(*) FILTER (
                            WHERE status = 'failed'
                              AND created_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                        ) AS failed,
                        count(*) FILTER (
                            WHERE status = 'scanning'
                              AND created_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')
                        ) AS unfinished
                    FROM vault_master_batches
                    UNION ALL
                    SELECT
                        count(*) FILTER (WHERE status = 'processing'),
                        count(*) FILTER (WHERE status = 'queued'),
                        count(*) FILTER (
                            WHERE status = 'failed'
                              AND created_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                        ),
                        count(*) FILTER (
                            WHERE status = 'processing'
                              AND started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')
                        )
                    FROM vault_ai_jobs
                    UNION ALL
                    SELECT
                        count(*) FILTER (WHERE status = 'processing'),
                        count(*) FILTER (WHERE status = 'queued'),
                        count(*) FILTER (
                            WHERE status = 'failed'
                              AND created_at >= CURRENT_TIMESTAMP - (%s * INTERVAL '1 day')
                        ),
                        count(*) FILTER (
                            WHERE status = 'processing'
                              AND started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 minute')
                        )
                    FROM vault_ingestion_ai_jobs
                ) AS job_counts
                """,
                (
                    FAILED_JOB_RETENTION_DAYS,
                    UNFINISHED_JOB_MINUTES,
                    FAILED_JOB_RETENTION_DAYS,
                    UNFINISHED_JOB_MINUTES,
                    FAILED_JOB_RETENTION_DAYS,
                    UNFINISHED_JOB_MINUTES,
                ),
            )
            running, queued, failed, unfinished = cursor.fetchone()
    except (OSError, RuntimeError, psycopg.Error):
        return None
    return {
        "running": running,
        "queued": queued,
        "failed": failed,
        "unfinished": unfinished,
    }


def _collect_database_and_jobs() -> tuple[dict[str, object], dict[str, int] | None, list[OverviewIssue]]:
    started = time.perf_counter()
    issues: list[OverviewIssue] = []
    try:
        with psycopg.connect(get_database_conninfo()) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
            response_ms = round((time.perf_counter() - started) * 1000, 1)

            status = "healthy"
            if response_ms >= DEGRADED_DATABASE_RESPONSE_MS:
                status = "degraded"
                issues.append(OverviewIssue("warning", "Database response is degraded."))

    except (OSError, RuntimeError, psycopg.Error):
        return {"status": "offline", "response_ms": None}, None, [
            OverviewIssue("critical", "Database is offline."),
        ]

    jobs = _collect_jobs()
    if jobs is None:
        issues.append(OverviewIssue("warning", "Background job status is unavailable."))
        return {"status": status, "response_ms": response_ms}, None, issues
    if jobs["failed"]:
        issues.append(
            OverviewIssue("warning", f"{jobs['failed']} background job(s) have failed.")
        )
    if jobs["unfinished"]:
        issues.append(
            OverviewIssue(
                "warning",
                f"{jobs['unfinished']} background job(s) have been unfinished for over {UNFINISHED_JOB_MINUTES} minutes.",
            )
        )
    return {"status": status, "response_ms": response_ms}, jobs, issues


def _collect_cpu() -> dict[str, float | None]:
    try:
        load = round(os.getloadavg()[0], 2)
    except OSError:
        load = None
    return {"load": load, "temperature_c": None}


def evaluate_overall_health(issues: list[OverviewIssue]) -> HealthState:
    if any(issue.severity == "critical" for issue in issues):
        return "critical"
    if issues:
        return "attention_required"
    return "healthy"


def collect_overview() -> dict[str, object]:
    storage, storage_issues = _collect_storage()
    database, jobs, database_issues = _collect_database_and_jobs()
    issues = storage_issues + database_issues
    health = evaluate_overall_health(issues)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_health": health,
        "database": database,
        "capacity": storage,
        "cpu": _collect_cpu(),
        "gpu": {"load": None, "temperature_c": None},
        "jobs": jobs,
        "issues": [issue.__dict__ for issue in issues],
        "attention": [issue.message for issue in issues],
    }


@router.get("/overview")
def get_overview(response: Response, _: AuthenticatedAdministrator) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    return collect_overview()
