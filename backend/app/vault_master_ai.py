from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.config import get_database_conninfo
from app.vault_master import CataloguedAsset, VaultMasterStore, sha256_file


AI_MODEL_ID = "microsoft/Florence-2-large"
AI_MODEL_REVISION = "21a599d414c4d928c9032694c424fb94458e3594"
AI_TASK_VERSION = "gallery-ocr-v1"
AiReviewStatus = Literal["pending", "accepted", "rejected", "deferred"]


@dataclass(frozen=True)
class AiJob:
    id: UUID
    asset_id: UUID
    requested_by: str
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    owner_user_id: UUID | None = None


@dataclass(frozen=True)
class AiSuggestion:
    id: UUID
    job_id: UUID
    asset_id: UUID
    suggestion_type: str
    raw_value: str
    reviewed_value: str | None
    confidence: float | None
    model_id: str
    model_revision: str
    task_version: str
    processing_ms: int
    status: AiReviewStatus
    requested_by: str
    reviewed_by: str | None
    created_at: datetime
    reviewed_at: datetime | None
    owner_user_id: UUID | None = None


class AiStore(Protocol):
    def queue_ocr(self, asset_id: UUID, requested_by: str, owner_user_id: UUID | None = None) -> AiJob: ...
    def list_jobs(self, asset_id: UUID, owner_user_id: UUID | str) -> list[AiJob]: ...
    def list_suggestions(
        self, asset_id: UUID, owner_user_id: UUID | str
    ) -> list[AiSuggestion]: ...
    def claim_next_job(self) -> AiJob | None: ...
    def complete_job(
        self,
        job_id: UUID,
        raw_text: str,
        confidence: float | None,
        processing_ms: int,
    ) -> AiSuggestion: ...
    def fail_job(self, job_id: UUID, error: str) -> None: ...
    def review_suggestion(
        self,
        suggestion_id: UUID,
        requested_by: str,
        owner_user_id: UUID | str,
        status: AiReviewStatus,
        reviewed_value: str | None,
    ) -> AiSuggestion | None: ...


class MemoryAiStore:
    def __init__(self) -> None:
        self.jobs: dict[UUID, AiJob] = {}
        self.suggestions: dict[UUID, AiSuggestion] = {}

    def queue_ocr(self, asset_id: UUID, requested_by: str, owner_user_id: UUID | None = None) -> AiJob:
        existing = next(
            (
                job
                for job in self.jobs.values()
                if job.asset_id == asset_id
                and job.status in {"queued", "processing"}
            ),
            None,
        )
        if existing is not None:
            return existing
        job = AiJob(
            id=uuid4(),
            asset_id=asset_id,
            requested_by=requested_by,
            status="queued",
            attempts=0,
            error=None,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            owner_user_id=owner_user_id,
        )
        self.jobs[job.id] = job
        return job

    def list_jobs(self, asset_id: UUID, owner_user_id: UUID | str) -> list[AiJob]:
        return sorted(
            (
                job
                for job in self.jobs.values()
                if job.asset_id == asset_id and (job.owner_user_id == owner_user_id if isinstance(owner_user_id, UUID) else job.requested_by == owner_user_id)
            ),
            key=lambda job: job.created_at,
            reverse=True,
        )

    def list_suggestions(
        self, asset_id: UUID, owner_user_id: UUID | str
    ) -> list[AiSuggestion]:
        return sorted(
            (
                suggestion
                for suggestion in self.suggestions.values()
                if suggestion.asset_id == asset_id
                and (suggestion.owner_user_id == owner_user_id if isinstance(owner_user_id, UUID) else suggestion.requested_by == owner_user_id)
            ),
            key=lambda suggestion: suggestion.created_at,
            reverse=True,
        )

    def claim_next_job(self) -> AiJob | None:
        queued = sorted(
            (job for job in self.jobs.values() if job.status == "queued"),
            key=lambda job: job.created_at,
        )
        if not queued:
            return None
        job = queued[0]
        claimed = AiJob(
            **{
                **job.__dict__,
                "status": "processing",
                "attempts": job.attempts + 1,
                "started_at": datetime.now(timezone.utc),
                "error": None,
            }
        )
        self.jobs[job.id] = claimed
        return claimed

    def complete_job(
        self,
        job_id: UUID,
        raw_text: str,
        confidence: float | None,
        processing_ms: int,
    ) -> AiSuggestion:
        job = self.jobs[job_id]
        if job.status != "processing":
            raise ValueError("AI job is not processing")
        now = datetime.now(timezone.utc)
        suggestion = AiSuggestion(
            id=uuid4(),
            job_id=job.id,
            asset_id=job.asset_id,
            suggestion_type="text_transcription",
            raw_value=raw_text,
            reviewed_value=None,
            confidence=confidence,
            model_id=AI_MODEL_ID,
            model_revision=AI_MODEL_REVISION,
            task_version=AI_TASK_VERSION,
            processing_ms=processing_ms,
            status="pending",
            requested_by=job.requested_by,
            reviewed_by=None,
            created_at=now,
            reviewed_at=None,
            owner_user_id=job.owner_user_id,
        )
        self.suggestions[suggestion.id] = suggestion
        self.jobs[job.id] = AiJob(
            **{**job.__dict__, "status": "completed", "completed_at": now}
        )
        return suggestion

    def fail_job(self, job_id: UUID, error: str) -> None:
        job = self.jobs[job_id]
        self.jobs[job.id] = AiJob(
            **{
                **job.__dict__,
                "status": "failed",
                "error": error,
                "completed_at": datetime.now(timezone.utc),
            }
        )

    def review_suggestion(
        self,
        suggestion_id: UUID,
        requested_by: str,
        owner_user_id: UUID | str | AiReviewStatus,
        status: AiReviewStatus | str | None = None,
        reviewed_value: str | None = None,
    ) -> AiSuggestion | None:
        if isinstance(owner_user_id, str) and owner_user_id in {"pending", "accepted", "rejected", "deferred"}:
            reviewed_value = status if isinstance(status, str) else reviewed_value
            status = owner_user_id
            owner_user_id = requested_by
        suggestion = self.suggestions.get(suggestion_id)
        if (
            suggestion is None
            or (suggestion.owner_user_id != owner_user_id if isinstance(owner_user_id, UUID) else suggestion.requested_by != owner_user_id)
            or suggestion.status != "pending"
        ):
            return None
        reviewed = AiSuggestion(
            **{
                **suggestion.__dict__,
                "status": status,  # type: ignore[arg-type]
                "reviewed_value": reviewed_value,
                "reviewed_by": requested_by,
                "reviewed_at": datetime.now(timezone.utc),
            }
        )
        self.suggestions[suggestion_id] = reviewed
        return reviewed


def _job_from_row(row: dict[str, object]) -> AiJob:
    return AiJob(**row)  # type: ignore[arg-type]


def _suggestion_from_row(row: dict[str, object]) -> AiSuggestion:
    return AiSuggestion(**row)  # type: ignore[arg-type]


class PostgresAiStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_ai_jobs (
                    id UUID PRIMARY KEY,
                    asset_id UUID NOT NULL
                        REFERENCES vault_assets(id) ON DELETE CASCADE,
                    requested_by TEXT NOT NULL,
                    owner_user_id UUID REFERENCES auth_accounts(user_id),
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'processing', 'completed', 'failed')
                    ),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                )
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS vault_ai_jobs_active_asset_idx
                ON vault_ai_jobs (asset_id)
                WHERE status IN ('queued', 'processing')
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_ai_suggestions (
                    id UUID PRIMARY KEY,
                    job_id UUID NOT NULL UNIQUE
                        REFERENCES vault_ai_jobs(id) ON DELETE CASCADE,
                    asset_id UUID NOT NULL
                        REFERENCES vault_assets(id) ON DELETE CASCADE,
                    suggestion_type TEXT NOT NULL CHECK (
                        suggestion_type IN ('text_transcription')
                    ),
                    raw_value TEXT NOT NULL,
                    reviewed_value TEXT,
                    confidence DOUBLE PRECISION,
                    model_id TEXT NOT NULL,
                    model_revision TEXT NOT NULL,
                    task_version TEXT NOT NULL,
                    processing_ms INTEGER NOT NULL CHECK (processing_ms >= 0),
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'accepted', 'rejected', 'deferred')
                    ),
                    requested_by TEXT NOT NULL,
                    owner_user_id UUID REFERENCES auth_accounts(user_id),
                    reviewed_by TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TIMESTAMPTZ
                )
                """
            )
            cursor.execute(
                """
                UPDATE vault_ai_jobs
                SET status = 'queued', started_at = NULL,
                    error = 'Recovered after worker restart'
                WHERE status = 'processing'
                """
            )
            cursor.execute("ALTER TABLE vault_ai_jobs ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("ALTER TABLE vault_ai_suggestions ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("""UPDATE vault_ai_jobs AS jobs SET owner_user_id=assets.owner_user_id
                FROM vault_assets AS assets WHERE jobs.owner_user_id IS NULL AND assets.id=jobs.asset_id
                AND assets.owner_user_id IS NOT NULL""")
            cursor.execute("""UPDATE vault_ai_suggestions AS suggestions SET owner_user_id=jobs.owner_user_id
                FROM vault_ai_jobs AS jobs WHERE suggestions.owner_user_id IS NULL AND jobs.id=suggestions.job_id
                AND jobs.owner_user_id IS NOT NULL""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_ai_jobs_owner_status_idx ON vault_ai_jobs(owner_user_id,status,created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_ai_suggestions_owner_asset_idx ON vault_ai_suggestions(owner_user_id,asset_id,created_at)")

    def queue_ocr(self, asset_id: UUID, requested_by: str, owner_user_id: UUID | None = None) -> AiJob:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM vault_ai_jobs
                WHERE asset_id = %s AND status IN ('queued', 'processing')
                ORDER BY created_at DESC LIMIT 1
                """,
                (asset_id,),
            )
            row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """
                    INSERT INTO vault_ai_jobs (
                        id, asset_id, requested_by, owner_user_id, status
                    ) VALUES (%s, %s, %s, %s, 'queued')
                    ON CONFLICT (asset_id)
                        WHERE status IN ('queued', 'processing')
                    DO NOTHING RETURNING *
                    """,
                    (uuid4(), asset_id, requested_by, owner_user_id),
                )
                row = cursor.fetchone()
            if row is None:
                cursor.execute(
                    """SELECT * FROM vault_ai_jobs
                    WHERE asset_id = %s
                        AND status IN ('queued', 'processing')
                    ORDER BY created_at DESC LIMIT 1""",
                    (asset_id,),
                )
                row = cursor.fetchone()
            assert row is not None
            return _job_from_row(row)

    def list_jobs(self, asset_id: UUID, owner_user_id: UUID | str) -> list[AiJob]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT * FROM vault_ai_jobs
                WHERE asset_id = %s AND """ + ("owner_user_id = %s" if isinstance(owner_user_id, UUID) else "requested_by = %s") + """
                ORDER BY created_at DESC""",
                (asset_id, owner_user_id),
            )
            return [_job_from_row(row) for row in cursor.fetchall()]

    def list_suggestions(
        self, asset_id: UUID, owner_user_id: UUID | str
    ) -> list[AiSuggestion]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT * FROM vault_ai_suggestions
                WHERE asset_id = %s AND """ + ("owner_user_id = %s" if isinstance(owner_user_id, UUID) else "requested_by = %s") + """
                ORDER BY created_at DESC""",
                (asset_id, owner_user_id),
            )
            return [_suggestion_from_row(row) for row in cursor.fetchall()]

    def claim_next_job(self) -> AiJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT * FROM vault_ai_jobs WHERE status = 'queued'
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
                """
            )
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute(
                """UPDATE vault_ai_jobs SET status = 'processing',
                attempts = attempts + 1, started_at = CURRENT_TIMESTAMP,
                error = NULL WHERE id = %s RETURNING *""",
                (row["id"],),
            )
            claimed = cursor.fetchone()
            assert claimed is not None
            return _job_from_row(claimed)

    def complete_job(
        self,
        job_id: UUID,
        raw_text: str,
        confidence: float | None,
        processing_ms: int,
    ) -> AiSuggestion:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM vault_ai_jobs WHERE id = %s FOR UPDATE",
                (job_id,),
            )
            job = cursor.fetchone()
            if job is None or job["status"] != "processing":
                raise ValueError("AI job is not processing")
            cursor.execute(
                """INSERT INTO vault_ai_suggestions (
                    id, job_id, asset_id, suggestion_type, raw_value,
                    confidence, model_id, model_revision, task_version,
                    processing_ms, status, requested_by, owner_user_id
                ) VALUES (
                    %s, %s, %s, 'text_transcription', %s, %s, %s, %s, %s,
                    %s, 'pending', %s, %s
                ) RETURNING *""",
                (
                    uuid4(), job_id, job["asset_id"], raw_text, confidence,
                    AI_MODEL_ID, AI_MODEL_REVISION, AI_TASK_VERSION,
                    processing_ms, job["requested_by"], job["owner_user_id"],
                ),
            )
            suggestion = cursor.fetchone()
            cursor.execute(
                """UPDATE vault_ai_jobs SET status = 'completed',
                completed_at = CURRENT_TIMESTAMP WHERE id = %s""",
                (job_id,),
            )
            assert suggestion is not None
            return _suggestion_from_row(suggestion)

    def fail_job(self, job_id: UUID, error: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vault_ai_jobs SET status = 'failed', error = %s,
                completed_at = CURRENT_TIMESTAMP WHERE id = %s""",
                (error[:2000], job_id),
            )

    def review_suggestion(
        self,
        suggestion_id: UUID,
        requested_by: str,
        owner_user_id: UUID | str | AiReviewStatus,
        status: AiReviewStatus | str | None = None,
        reviewed_value: str | None = None,
    ) -> AiSuggestion | None:
        if isinstance(owner_user_id, str) and owner_user_id in {"pending", "accepted", "rejected", "deferred"}:
            reviewed_value = status if isinstance(status, str) else reviewed_value
            status = owner_user_id
            owner_user_id = requested_by
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vault_ai_suggestions SET status = %s,
                reviewed_value = %s, reviewed_by = %s,
                reviewed_at = CURRENT_TIMESTAMP
                WHERE id = %s AND """ + ("owner_user_id = %s" if isinstance(owner_user_id, UUID) else "requested_by = %s") + """ AND status = 'pending'
                RETURNING *""",
                (status, reviewed_value, requested_by, suggestion_id, owner_user_id),
            )
            row = cursor.fetchone()
            return _suggestion_from_row(row) if row is not None else None


@lru_cache
def get_ai_store() -> AiStore:
    return PostgresAiStore(get_database_conninfo())


def _gallery_file(asset: CataloguedAsset) -> Path:
    vault_root = PurePosixPath("/vault/Gallery")
    vault_path = PurePosixPath(asset.vault_path)
    if asset.asset_type != "Gallery" or not vault_path.is_relative_to(vault_root):
        raise ValueError("Only Gallery images can enter the OCR pilot")
    root = Path(os.getenv("PV_GALLERY_PATH", "/media/gallery")).resolve(strict=True)
    source = root.joinpath(*vault_path.relative_to(vault_root).parts).resolve(
        strict=True
    )
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("Gallery source is unavailable")
    if not asset.mime_type.startswith("image/"):
        raise ValueError("Only Gallery images can enter the OCR pilot")
    if source.stat().st_size != asset.size_bytes or sha256_file(source) != asset.sha256:
        raise ValueError("Gallery source no longer matches its catalogue record")
    return source


def request_florence_ocr(source: Path) -> tuple[str, float | None, int]:
    endpoint = os.getenv("PV_FLORENCE_URL", "http://pv-florence2:8080/ocr")
    started = datetime.now(timezone.utc)
    request = Request(
        endpoint,
        data=source.read_bytes(),
        method="POST",
        headers={
            "Content-Type": mimetypes.guess_type(source.name)[0]
            or "application/octet-stream"
        },
    )
    try:
        with urlopen(request, timeout=120) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Local Florence-2 service is unavailable") from error
    text = payload.get("text")
    if not isinstance(text, str):
        raise RuntimeError("Local Florence-2 service returned invalid OCR")
    confidence = payload.get("confidence")
    elapsed = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    return (
        text.strip(),
        float(confidence) if isinstance(confidence, (int, float)) else None,
        elapsed,
    )


def process_next_ai_job(
    ai_store: AiStore,
    vault_store: VaultMasterStore,
) -> UUID | None:
    job = ai_store.claim_next_job()
    if job is None:
        return None
    try:
        asset = vault_store.get_catalogued_asset_by_id(job.asset_id)
        if asset is None or (
            asset.owner_user_id != job.owner_user_id
            if job.owner_user_id is not None
            else asset.owner_username != job.requested_by
        ):
            raise ValueError("AI job asset is no longer owned by its requester")
        source = _gallery_file(asset)
        text, confidence, processing_ms = request_florence_ocr(source)
        ai_store.complete_job(job.id, text, confidence, processing_ms)
    except Exception as error:
        ai_store.fail_job(job.id, str(error))
    return job.id
