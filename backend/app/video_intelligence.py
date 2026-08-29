"""Post-publication Personal Video Intelligence foundation.

This module owns only durable job, sampled-frame, evidence and reconciliation
records.  It deliberately neither imports routing nor opens original videos;
specialist execution is a later stage.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Literal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.config import get_database_conninfo

VIDEO_INTELLIGENCE_TASK_VERSION = "personal-video-intelligence-v1"
VIDEO_SAMPLING_VERSION = "personal-video-sampling-v2"
VIDEO_RECONCILIATION_VERSION = "video-reconciliation-v1.1"
VIDEO_FRAME_CACHE_ROOT = "/var/cache/personal-vault/video-analysis"
END_SAMPLE_MAX_GUARD_MS = 250
FFMPEG_DIAGNOSTIC_LIMIT = 800
VideoJobStatus = Literal[
    "queued", "sampling", "analysing", "analysis_complete", "reconciling", "completed", "completed_with_warnings", "failed"
]
FrameStatus = Literal["pending", "extracting", "completed", "failed"]
SelectionReason = Literal["start", "midpoint", "end", "interval", "scene_change"]


@dataclass(frozen=True)
class VideoSamplingConfig:
    version: str = VIDEO_SAMPLING_VERSION
    target_interval_ms: int = 20_000
    max_regular_frames: int = 14
    scene_threshold: float = 0.30
    max_scene_frames: int = 4
    dedupe_window_ms: int = 10_000
    max_frames: int = 18


SAMPLING_CONFIG = VideoSamplingConfig()


@dataclass(frozen=True)
class VideoAnalysisJob:
    id: UUID
    asset_id: UUID
    requested_by: str
    owner_user_id: UUID | None
    requested_reanalysis: bool
    status: VideoJobStatus
    total_frames: int
    frames_completed: int
    frames_failed: int
    warning: str | None
    error: str | None
    task_version: str
    sampling_version: str
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass(frozen=True)
class VideoAnalysisFrame:
    id: UUID
    job_id: UUID
    asset_id: UUID
    timestamp_ms: int
    ordinal: int
    selection_reason: SelectionReason
    status: FrameStatus
    cache_key: str | None
    warning: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class VideoFrameEvidence:
    id: UUID
    frame_id: UUID
    provider: str
    model_id: str
    model_revision: str | None
    task_version: str
    raw_evidence: object
    processed_evidence: object | None
    processing_ms: int | None
    created_at: datetime


@dataclass(frozen=True)
class VideoReconciliationResult:
    id: UUID
    asset_id: UUID
    job_id: UUID
    generated_narrative: str | None
    reconciliation_version: str
    warnings: tuple[str, ...]
    evidence_references: object
    created_at: datetime


def select_deterministic_frame_positions(
    duration_ms: int,
    *,
    scene_change_timestamps_ms: tuple[int, ...] = (),
    config: VideoSamplingConfig = SAMPLING_CONFIG,
) -> tuple[tuple[int, SelectionReason], ...]:
    """Return bounded deterministic positions without reading a video file.

    Stage V1 persists this approved policy.  V2 supplies scene candidates from
    FFmpeg and extracts the corresponding rebuildable frames.
    """
    if duration_ms <= 0:
        raise ValueError("Video duration must be positive")
    # Container duration can identify the end of the final packet rather than
    # a timestamp ffmpeg can safely decode.  Keep the semantic end anchor, but
    # seek slightly inside the media: 250 ms for ordinary clips, or 10% for
    # shorter clips.  This leaves distinct start/midpoint/end samples whenever
    # the duration permits and remains deterministic for sampling-v2.
    end_guard_ms = min(END_SAMPLE_MAX_GUARD_MS, max(1, duration_ms // 10))
    final_ms = max(0, duration_ms - end_guard_ms)
    anchors: list[tuple[int, SelectionReason]] = [
        (0, "start"),
        (duration_ms // 2, "midpoint"),
        (final_ms, "end"),
    ]
    accepted: list[tuple[int, SelectionReason]] = []
    for timestamp_ms, reason in anchors:
        if not any(timestamp_ms == existing for existing, _ in accepted):
            accepted.append((timestamp_ms, reason))
    candidates: list[tuple[int, SelectionReason]] = []
    interval_count = min(
        config.max_regular_frames,
        max(0, (duration_ms - 1) // config.target_interval_ms - 1),
    )
    if interval_count:
        for index in range(1, interval_count + 1):
            candidates.append((round(duration_ms * index / (interval_count + 1)), "interval"))

    # A supplied V2 scene scan is made deterministic by sorting and retaining
    # the first candidate in each temporal quarter.
    selected_scene: list[int] = []
    quarters: set[int] = set()
    for timestamp_ms in sorted(set(scene_change_timestamps_ms)):
        if not 0 <= timestamp_ms <= final_ms:
            continue
        quarter = min(3, timestamp_ms * 4 // max(1, duration_ms))
        if quarter in quarters:
            continue
        quarters.add(quarter)
        selected_scene.append(timestamp_ms)
        if len(selected_scene) == config.max_scene_frames:
            break
    candidates.extend(
        (timestamp_ms, "scene_change") for timestamp_ms in selected_scene
    )

    priority = {"start": 0, "midpoint": 1, "end": 2, "scene_change": 3, "interval": 4}
    for timestamp_ms, reason in sorted(candidates, key=lambda value: (priority[value[1]], value[0])):
        if any(abs(timestamp_ms - existing) < config.dedupe_window_ms for existing, _ in accepted):
            continue
        accepted.append((timestamp_ms, reason))
    accepted.sort(key=lambda value: value[0])
    return tuple(accepted[: config.max_frames])


def get_video_analysis_cache_root() -> Path:
    return Path(os.getenv("PV_VIDEO_ANALYSIS_CACHE_PATH", VIDEO_FRAME_CACHE_ROOT))


def video_source_path(asset) -> Path:
    prefix = "/vault/Home Videos/"
    if asset.asset_type != "Home Videos" or not asset.vault_path.startswith(prefix):
        raise ValueError("Video Intelligence requires a published Home Video")
    return Path(os.getenv("PV_PERSONAL_VIDEOS_PATH", "/media/personal-videos")) / asset.vault_path.removeprefix(prefix)


def probe_video_duration_ms(source: Path) -> int:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", "--", str(source)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return max(1, round(float(result.stdout.strip()) * 1000))


def parse_scene_timestamps(stderr: str) -> tuple[int, ...]:
    return tuple(dict.fromkeys(round(float(value) * 1000) for value in re.findall(r"pts_time:([0-9.]+)", stderr)))


def _ffmpeg_diagnostic(
    error: subprocess.CalledProcessError, source: Path, destination: Path,
) -> str:
    stderr = error.stderr
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    detail = " ".join(str(stderr or "").split())
    detail = detail.replace(str(source), "<video>").replace(str(destination), "<frame>")
    if len(detail) > FFMPEG_DIAGNOSTIC_LIMIT:
        detail = detail[: FFMPEG_DIAGNOSTIC_LIMIT - 3] + "..."
    suffix = f": {detail}" if detail else ""
    return f"ffmpeg frame extraction failed (exit {error.returncode}){suffix}"


def extract_frame(source: Path, timestamp_ms: int, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{timestamp_ms / 1000:.3f}", "-i", str(source), "-frames:v", "1", "-q:v", "3", str(destination)],
            check=True, capture_output=True, timeout=120,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(_ffmpeg_diagnostic(error, source, destination)) from error


def _scene_candidates(source: Path, config: VideoSamplingConfig) -> tuple[int, ...]:
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(source), "-vf", f"select='gt(scene,{config.scene_threshold})',showinfo", "-vsync", "vfr", "-frames:v", str(config.max_scene_frames * 4), "-f", "null", "-"],
        check=False, capture_output=True, text=True, timeout=300,
    )
    return parse_scene_timestamps(result.stderr)


class MemoryVideoIntelligenceStore:
    def __init__(self) -> None:
        self.jobs: dict[UUID, VideoAnalysisJob] = {}
        self.frames: dict[UUID, VideoAnalysisFrame] = {}
        self.evidence: dict[UUID, VideoFrameEvidence] = {}
        self.reconciliations: dict[UUID, VideoReconciliationResult] = {}
        self.reconciliation_history: dict[UUID, list[VideoReconciliationResult]] = {}

    def queue(
        self, asset_id: UUID, username: str, owner_user_id: UUID, *, reanalyse: bool = False,
    ) -> VideoAnalysisJob:
        active = next((job for job in reversed(self.jobs.values()) if job.asset_id == asset_id and job.status in {"queued", "sampling", "analysing", "reconciling"}), None)
        if active:
            return active
        latest = self.latest_job(asset_id)
        if latest and not reanalyse:
            return latest
        job = VideoAnalysisJob(uuid4(), asset_id, username, owner_user_id, reanalyse, "queued", 0, 0, 0, None, None, VIDEO_INTELLIGENCE_TASK_VERSION, VIDEO_SAMPLING_VERSION, datetime.now(timezone.utc))
        self.jobs[job.id] = job
        return job

    def latest_job(self, asset_id: UUID) -> VideoAnalysisJob | None:
        return next((job for job in reversed(self.jobs.values()) if job.asset_id == asset_id), None)

    def claim_next_job(self) -> VideoAnalysisJob | None:
        job = next((value for value in self.jobs.values() if value.status == "queued"), None)
        return self.transition(job.id, "sampling") if job else None

    def transition(self, job_id: UUID, status: VideoJobStatus, *, warning: str | None = None, error: str | None = None) -> VideoAnalysisJob:
        job = self.jobs[job_id]
        now = datetime.now(timezone.utc)
        updated = replace(job, status=status, warning=warning if warning is not None else job.warning, error=error, started_at=job.started_at or (now if status != "queued" else None), completed_at=now if status in {"completed", "completed_with_warnings", "failed"} else None)
        self.jobs[job_id] = updated
        return updated

    def add_frames(self, job_id: UUID, positions: tuple[tuple[int, SelectionReason], ...]) -> list[VideoAnalysisFrame]:
        job = self.jobs[job_id]
        now = datetime.now(timezone.utc)
        frames = [VideoAnalysisFrame(uuid4(), job_id, job.asset_id, timestamp, ordinal, reason, "pending", None, None, None, now, now) for ordinal, (timestamp, reason) in enumerate(positions, start=1)]
        self.frames.update({frame.id: frame for frame in frames})
        self.jobs[job_id] = replace(job, total_frames=len(frames))
        return frames

    def frames_for_job(self, job_id: UUID) -> list[VideoAnalysisFrame]:
        return sorted((frame for frame in self.frames.values() if frame.job_id == job_id), key=lambda frame: frame.ordinal)

    def update_frame(self, frame_id: UUID, status: FrameStatus, *, warning: str | None = None, error: str | None = None) -> VideoAnalysisFrame:
        frame = self.frames[frame_id]
        updated = replace(frame, status=status, warning=warning, error=error, updated_at=datetime.now(timezone.utc))
        self.frames[frame_id] = updated
        job = self.jobs[frame.job_id]
        frames = self.frames_for_job(frame.job_id)
        self.jobs[job.id] = replace(job, frames_completed=sum(value.status == "completed" for value in frames), frames_failed=sum(value.status == "failed" for value in frames))
        return updated

    def save_evidence(self, evidence: VideoFrameEvidence) -> None:
        self.evidence[evidence.id] = evidence

    def evidence_for_frame(self, frame_id: UUID) -> list[VideoFrameEvidence]:
        return [value for value in self.evidence.values() if value.frame_id == frame_id]

    def save_reconciliation(self, result: VideoReconciliationResult) -> None:
        self.reconciliations[result.asset_id] = result
        self.reconciliation_history.setdefault(result.job_id, []).append(result)

    def latest_reconciliation(self, asset_id: UUID) -> VideoReconciliationResult | None:
        return self.reconciliations.get(asset_id)

    def claim_reconciliation(self, job_id: UUID, *, refresh: bool = False) -> VideoAnalysisJob | None:
        job = self.jobs.get(job_id)
        permitted = {"analysis_complete"}
        if refresh:
            permitted.update({"completed", "completed_with_warnings"})
        if job is None or job.status not in permitted:
            return None
        return self.transition(job_id, "reconciling")


class PostgresVideoIntelligenceStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_video_analysis_jobs (
                id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), requested_by TEXT NOT NULL,
                owner_user_id UUID REFERENCES auth_accounts(user_id),
                requested_reanalysis BOOLEAN NOT NULL DEFAULT FALSE,
                status TEXT NOT NULL CHECK(status IN ('queued','sampling','analysing','analysis_complete','reconciling','completed','completed_with_warnings','failed')),
                total_frames INTEGER NOT NULL DEFAULT 0, frames_completed INTEGER NOT NULL DEFAULT 0, frames_failed INTEGER NOT NULL DEFAULT 0,
                warning TEXT, error TEXT, task_version TEXT NOT NULL, sampling_version TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
                job_sequence BIGINT GENERATED BY DEFAULT AS IDENTITY)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_video_analysis_frames (
                id UUID PRIMARY KEY, job_id UUID NOT NULL REFERENCES vault_video_analysis_jobs(id), asset_id UUID NOT NULL REFERENCES vault_assets(id),
                timestamp_ms BIGINT NOT NULL CHECK(timestamp_ms >= 0), ordinal INTEGER NOT NULL,
                selection_reason TEXT NOT NULL CHECK(selection_reason IN ('start','midpoint','end','interval','scene_change')),
                status TEXT NOT NULL CHECK(status IN ('pending','extracting','completed','failed')), cache_key TEXT,
                warning TEXT, error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_id, ordinal), UNIQUE(job_id, timestamp_ms))""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_video_frame_evidence (
                id UUID PRIMARY KEY, frame_id UUID NOT NULL REFERENCES vault_video_analysis_frames(id), provider TEXT NOT NULL,
                model_id TEXT NOT NULL, model_revision TEXT, task_version TEXT NOT NULL, raw_evidence JSONB NOT NULL,
                processed_evidence JSONB, processing_ms INTEGER, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_video_reconciliation_results (
                id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), job_id UUID NOT NULL REFERENCES vault_video_analysis_jobs(id),
                generated_narrative TEXT, reconciliation_version TEXT NOT NULL, warnings JSONB NOT NULL DEFAULT '[]'::jsonb,
                evidence_references JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                result_sequence BIGINT GENERATED BY DEFAULT AS IDENTITY)""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_video_analysis_jobs_asset_idx ON vault_video_analysis_jobs(asset_id, job_sequence DESC)")
            cursor.execute("ALTER TABLE vault_video_analysis_jobs ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("ALTER TABLE vault_video_analysis_frames ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("ALTER TABLE vault_video_frame_evidence ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("ALTER TABLE vault_video_reconciliation_results ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("""UPDATE vault_video_analysis_jobs jobs SET owner_user_id=assets.owner_user_id FROM vault_assets assets
                WHERE jobs.owner_user_id IS NULL AND assets.id=jobs.asset_id AND assets.owner_user_id IS NOT NULL""")
            cursor.execute("""UPDATE vault_video_analysis_frames frames SET owner_user_id=jobs.owner_user_id FROM vault_video_analysis_jobs jobs
                WHERE frames.owner_user_id IS NULL AND frames.job_id=jobs.id AND jobs.owner_user_id IS NOT NULL""")
            cursor.execute("""UPDATE vault_video_frame_evidence evidence SET owner_user_id=frames.owner_user_id FROM vault_video_analysis_frames frames
                WHERE evidence.owner_user_id IS NULL AND evidence.frame_id=frames.id AND frames.owner_user_id IS NOT NULL""")
            cursor.execute("""UPDATE vault_video_reconciliation_results results SET owner_user_id=jobs.owner_user_id FROM vault_video_analysis_jobs jobs
                WHERE results.owner_user_id IS NULL AND results.job_id=jobs.id AND jobs.owner_user_id IS NOT NULL""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_video_analysis_jobs_owner_status_idx ON vault_video_analysis_jobs(owner_user_id,status,created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_video_analysis_frames_job_timestamp_idx ON vault_video_analysis_frames(job_id,timestamp_ms)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_video_frame_evidence_frame_provider_idx ON vault_video_frame_evidence(frame_id,provider)")
            # V3.1 preserves every reconciliation candidate for the same V2
            # evidence job rather than destructively replacing the older result.
            cursor.execute("ALTER TABLE vault_video_reconciliation_results DROP CONSTRAINT IF EXISTS vault_video_reconciliation_results_job_id_key")
            cursor.execute("ALTER TABLE vault_video_reconciliation_results ADD COLUMN IF NOT EXISTS result_sequence BIGINT GENERATED BY DEFAULT AS IDENTITY")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_video_reconciliation_results_asset_created_idx ON vault_video_reconciliation_results(asset_id,result_sequence DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_video_reconciliation_results_job_created_idx ON vault_video_reconciliation_results(job_id,result_sequence DESC)")
            cursor.execute("ALTER TABLE vault_video_analysis_jobs DROP CONSTRAINT IF EXISTS vault_video_analysis_jobs_status_check")
            cursor.execute("""ALTER TABLE vault_video_analysis_jobs ADD CONSTRAINT vault_video_analysis_jobs_status_check
                CHECK(status IN ('queued','sampling','analysing','analysis_complete','reconciling','completed','completed_with_warnings','failed'))""")

    def queue(
        self, asset_id: UUID, username: str, owner_user_id: UUID, *, reanalyse: bool = False,
    ) -> VideoAnalysisJob:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_video_analysis_jobs WHERE asset_id=%s ORDER BY job_sequence DESC LIMIT 1", (asset_id,))
            latest = cursor.fetchone()
            if latest and latest["status"] in {"queued", "sampling", "analysing", "reconciling"}:
                return _job(latest)
            if latest and not reanalyse:
                return _job(latest)
            cursor.execute("""INSERT INTO vault_video_analysis_jobs(id,asset_id,requested_by,owner_user_id,requested_reanalysis,status,task_version,sampling_version)
                SELECT %s,assets.id,%s,assets.owner_user_id,%s,'queued',%s,%s FROM vault_assets assets
                WHERE assets.id=%s AND assets.owner_user_id=%s RETURNING *""", (uuid4(), username, reanalyse, VIDEO_INTELLIGENCE_TASK_VERSION, VIDEO_SAMPLING_VERSION, asset_id, owner_user_id))
            if cursor.rowcount != 1:
                raise ValueError("Video Intelligence requires a resolved asset owner")
            return _job(cursor.fetchone())

    def latest_job(self, asset_id: UUID) -> VideoAnalysisJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_video_analysis_jobs WHERE asset_id=%s ORDER BY job_sequence DESC LIMIT 1", (asset_id,))
            row = cursor.fetchone()
        return _job(row) if row else None

    def claim_next_job(self) -> VideoAnalysisJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""WITH next AS (
                SELECT id FROM vault_video_analysis_jobs WHERE status='queued'
                ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
            ) UPDATE vault_video_analysis_jobs jobs SET status='sampling',
                started_at=CURRENT_TIMESTAMP WHERE jobs.id IN (SELECT id FROM next)
                RETURNING jobs.*""")
            row = cursor.fetchone()
        return _job(row) if row else None

    def transition(
        self, job_id: UUID, status: VideoJobStatus, *, warning: str | None = None,
        error: str | None = None,
    ) -> VideoAnalysisJob:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE vault_video_analysis_jobs SET status=%s,
                   warning=COALESCE(%s,warning), error=%s,
                   started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                   completed_at=CASE WHEN %s IN ('completed','completed_with_warnings','failed') THEN CURRENT_TIMESTAMP ELSE NULL END
                   WHERE id=%s RETURNING *""",
                (status, warning, error, status, job_id),
            )
            row = cursor.fetchone()
        if row is None:
            raise ValueError("Video analysis job was not found")
        return _job(row)

    def add_frames(
        self, job_id: UUID, positions: tuple[tuple[int, SelectionReason], ...],
    ) -> list[VideoAnalysisFrame]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT asset_id,owner_user_id FROM vault_video_analysis_jobs WHERE id=%s", (job_id,))
            job = cursor.fetchone()
            if job is None or job["owner_user_id"] is None:
                raise ValueError("Video analysis job was not found")
            cursor.execute("DELETE FROM vault_video_analysis_frames WHERE job_id=%s", (job_id,))
            rows = []
            for ordinal, (timestamp_ms, reason) in enumerate(positions, start=1):
                cursor.execute(
                    """INSERT INTO vault_video_analysis_frames(id,job_id,asset_id,owner_user_id,timestamp_ms,ordinal,selection_reason,status)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,'pending') RETURNING *""",
                    (uuid4(), job_id, job["asset_id"], job["owner_user_id"], timestamp_ms, ordinal, reason),
                )
                rows.append(cursor.fetchone())
            cursor.execute("UPDATE vault_video_analysis_jobs SET total_frames=%s WHERE id=%s", (len(rows), job_id))
        return [_frame(row) for row in rows]

    def frames_for_job(self, job_id: UUID) -> list[VideoAnalysisFrame]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_video_analysis_frames WHERE job_id=%s ORDER BY ordinal", (job_id,))
            rows = cursor.fetchall()
        return [_frame(row) for row in rows]

    def update_frame(
        self, frame_id: UUID, status: FrameStatus, *, warning: str | None = None,
        error: str | None = None,
    ) -> VideoAnalysisFrame:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE vault_video_analysis_frames SET status=%s,warning=%s,error=%s,updated_at=CURRENT_TIMESTAMP
                WHERE id=%s RETURNING *""", (status, warning, error, frame_id))
            row = cursor.fetchone()
            if row is None:
                raise ValueError("Video analysis frame was not found")
            cursor.execute("""UPDATE vault_video_analysis_jobs job SET
                frames_completed=(SELECT count(*) FROM vault_video_analysis_frames WHERE job_id=job.id AND status='completed'),
                frames_failed=(SELECT count(*) FROM vault_video_analysis_frames WHERE job_id=job.id AND status='failed')
                WHERE job.id=%s""", (row["job_id"],))
        return _frame(row)

    def save_evidence(self, evidence: VideoFrameEvidence) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO vault_video_frame_evidence(id,frame_id,provider,model_id,model_revision,task_version,raw_evidence,processed_evidence,processing_ms,owner_user_id)
                VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,(SELECT owner_user_id FROM vault_video_analysis_frames WHERE id=%s))""",
                (evidence.id, evidence.frame_id, evidence.provider, evidence.model_id, evidence.model_revision,
                 evidence.task_version, json.dumps(evidence.raw_evidence), json.dumps(evidence.processed_evidence), evidence.processing_ms, evidence.frame_id),
            )

    def save_reconciliation(self, result: VideoReconciliationResult) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO vault_video_reconciliation_results(id,asset_id,job_id,generated_narrative,reconciliation_version,warnings,evidence_references,owner_user_id)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,(SELECT owner_user_id FROM vault_video_analysis_jobs WHERE id=%s))
                """,
                (result.id, result.asset_id, result.job_id, result.generated_narrative,
                 result.reconciliation_version, json.dumps(result.warnings), json.dumps(result.evidence_references), result.job_id),
            )

    def latest_reconciliation(self, asset_id: UUID) -> VideoReconciliationResult | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_video_reconciliation_results WHERE asset_id=%s ORDER BY result_sequence DESC LIMIT 1", (asset_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        return VideoReconciliationResult(
            UUID(str(row["id"])), UUID(str(row["asset_id"])), UUID(str(row["job_id"])),
            row["generated_narrative"], str(row["reconciliation_version"]),
            tuple(str(value) for value in row["warnings"]), row["evidence_references"], row["created_at"],
        )

    def claim_reconciliation(self, job_id: UUID, *, refresh: bool = False) -> VideoAnalysisJob | None:
        permitted = ["analysis_complete", "completed", "completed_with_warnings"] if refresh else ["analysis_complete"]
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE vault_video_analysis_jobs SET status='reconciling'
                WHERE id=%s AND status = ANY(%s::text[]) RETURNING *""", (job_id, permitted))
            row = cursor.fetchone()
        return _job(row) if row else None

    def frames_for_job(self, job_id: UUID) -> list[VideoAnalysisFrame]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_video_analysis_frames WHERE job_id=%s ORDER BY timestamp_ms,ordinal", (job_id,))
            rows = cursor.fetchall()
        return [_frame(row) for row in rows]

    def evidence_for_frame(self, frame_id: UUID) -> list[VideoFrameEvidence]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_video_frame_evidence WHERE frame_id=%s ORDER BY provider,created_at", (frame_id,))
            rows = cursor.fetchall()
        return [_evidence(row) for row in rows]


def _asset_for_job(vault_store, job: VideoAnalysisJob):
    if job.owner_user_id is None:
        return None
    return next(
        (
            asset for asset in vault_store.list_owned_catalogued_assets_by_user_id(job.owner_user_id)
            if asset.id == job.asset_id
        ),
        None,
    )


def _save_provider_failure(store, frame: VideoAnalysisFrame, provider: str, error: Exception, started: float) -> str:
    store.save_evidence(VideoFrameEvidence(
        uuid4(), frame.id, provider, "unavailable", None,
        VIDEO_INTELLIGENCE_TASK_VERSION,
        {"error": str(error)}, None, round((time.perf_counter() - started) * 1000),
        datetime.now(timezone.utc),
    ))
    return f"{provider} failed at {frame.timestamp_ms}ms: {error}"


def process_next_video_analysis_job(store, vault_store, people_store=None) -> UUID | None:
    """Process one selected Video Intelligence job sequentially.

    This is deliberately post-publication only and persists specialist evidence
    without publishing video People, tags, or a narrative.
    """
    job = store.claim_next_job()
    if job is None:
        return None
    asset = _asset_for_job(vault_store, job)
    if asset is None:
        store.transition(job.id, "failed", error="Published Home Video was not found for the owner")
        return job.id
    try:
        source = video_source_path(asset)
        if not source.is_file():
            raise FileNotFoundError("Published Home Video source is unavailable")
        duration_ms = probe_video_duration_ms(source)
        positions = select_deterministic_frame_positions(
            duration_ms, scene_change_timestamps_ms=_scene_candidates(source, SAMPLING_CONFIG)
        )
        frames = store.add_frames(job.id, positions)
        store.transition(job.id, "analysing")
    except Exception as error:
        store.transition(job.id, "failed", error=str(error))
        return job.id

    from app.gallery_intelligence import request_rampp_tags
    from app.gallery_people import get_gallery_people_store
    from app.gallery_people_worker import request_people_analysis
    from app.vault_master_ingestion_ai import request_florence_analysis

    persistence = people_store or get_gallery_people_store()
    references = (
        persistence.reference_embeddings_by_user_id(job.owner_user_id)
        if job.owner_user_id is not None and hasattr(persistence, "reference_embeddings_by_user_id")
        else persistence.reference_embeddings(job.requested_by)
    )
    warnings: list[str] = []
    cache_dir = get_video_analysis_cache_root() / str(job.id)
    try:
        for frame in frames:
            cache_path = cache_dir / f"{frame.ordinal:03d}-{frame.timestamp_ms}.jpg"
            try:
                store.update_frame(frame.id, "extracting")
                extract_frame(source, frame.timestamp_ms, cache_path)
            except Exception as error:
                store.update_frame(frame.id, "failed", error=str(error))
                warnings.append(f"Frame extraction failed at {frame.timestamp_ms}ms: {error}")
                continue
            frame_warnings: list[str] = []
            try:
                started = time.perf_counter()
                description, task_version, processing_ms = request_florence_analysis(cache_path)
                store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "florence", "florence-2-large", None, task_version, {"description": description}, {"description": description}, processing_ms or round((time.perf_counter() - started) * 1000), datetime.now(timezone.utc)))
            except Exception as error:
                frame_warnings.append(_save_provider_failure(store, frame, "florence", error, started if "started" in locals() else time.perf_counter()))
            try:
                started = time.perf_counter()
                classification = request_rampp_tags(cache_path)
                raw_tags = json.loads(classification.raw_response)
                store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, classification.provider, classification.model_id, classification.model_revision, classification.task_version, {"tags": raw_tags, "raw_response": classification.raw_response}, {"tags": raw_tags}, classification.processing_ms or round((time.perf_counter() - started) * 1000), datetime.now(timezone.utc)))
            except Exception as error:
                frame_warnings.append(_save_provider_failure(store, frame, "rampp", error, started if "started" in locals() else time.perf_counter()))
            try:
                started = time.perf_counter()
                people = request_people_analysis(cache_path, references)
                store.save_evidence(VideoFrameEvidence(uuid4(), frame.id, "people", "mediapipe-facenet512-yolox-tiny", None, str(people.get("task_version", "gallery-people-v2")), people, people, round((time.perf_counter() - started) * 1000), datetime.now(timezone.utc)))
            except Exception as error:
                frame_warnings.append(_save_provider_failure(store, frame, "people", error, started if "started" in locals() else time.perf_counter()))
            try:
                cache_path.unlink(missing_ok=True)
            except OSError as error:
                frame_warnings.append(f"Frame cache cleanup failed at {frame.timestamp_ms}ms: {error}")
            store.update_frame(frame.id, "completed", warning="; ".join(frame_warnings) or None)
            warnings.extend(frame_warnings)
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)
    final_warning = "; ".join(warnings) if warnings else None
    store.transition(job.id, "analysis_complete", warning=final_warning)
    return job.id


def _known_people_from_evidence(evidence: list[VideoFrameEvidence], people_store, owner: str) -> tuple[UUID, ...]:
    people: set[UUID] = set()
    for item in evidence:
        if item.provider != "people" or not isinstance(item.processed_evidence, dict):
            continue
        faces = item.processed_evidence.get("faces")
        boxes = faces.get("boxes", []) if isinstance(faces, dict) else []
        for face in boxes if isinstance(boxes, list) else []:
            if not isinstance(face, dict) or face.get("recognition_result") != "known":
                continue
            candidate = face.get("candidate_person_id")
            try:
                person_id = UUID(str(candidate))
            except (TypeError, ValueError):
                continue
            if people_store.get_person(person_id, owner) is not None:
                people.add(person_id)
    return tuple(sorted(people, key=str))


@dataclass(frozen=True)
class _SceneSummary:
    """Deterministic, reconciliation-only interpretation of one sampled frame."""

    activity: str | None
    setting: str | None
    people_count: int | None
    details: tuple[str, ...]
    fallback: str

    @property
    def signature(self) -> tuple[str | None, str | None]:
        # This deliberately compares scene structure, not model confidence or
        # fragile exact caption text.  Incidental details do not split a scene.
        return self.activity, self.setting


def _normalise_caption(caption: str) -> str:
    cleaned = " ".join(caption.strip().split()).rstrip(". ")
    cleaned = re.sub(r"^(?:the |this )?image shows\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bin the image\b[, ]*", "", cleaned, flags=re.IGNORECASE)
    # Sky/weather clauses recur often in Florence captions but are rarely a
    # useful event distinction. Raw provider evidence remains untouched.
    cleaned = re.sub(r"(?:,?\s*(?:the )?sky is [^.]+|,?\s*the weather appears [^.]+)$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" ,.")


def _caption_for_frame(evidence: list[VideoFrameEvidence], frame_id: UUID) -> str:
    for item in evidence:
        if item.frame_id != frame_id or item.provider != "florence" or not isinstance(item.processed_evidence, dict):
            continue
        description = item.processed_evidence.get("description")
        if isinstance(description, str) and description.strip():
            return _normalise_caption(description)
    return ""


def _tags_for_frame(evidence: list[VideoFrameEvidence], frame_id: UUID) -> set[str]:
    tags: set[str] = set()
    for item in evidence:
        if item.frame_id != frame_id or item.provider != "rampp" or not isinstance(item.processed_evidence, dict):
            continue
        values = item.processed_evidence.get("tags")
        if isinstance(values, list):
            tags.update(str(value).casefold() for value in values if isinstance(value, str))
    return tags


def _scene_summary(caption: str, tags: set[str]) -> _SceneSummary | None:
    text = caption.casefold()
    words = set(re.findall(r"[a-z]+", text)) | tags
    if not caption and not words:
        return None
    has = lambda *terms: any(term in text or term in words for term in terms)
    activity = (
        "kayaking" if has("kayak", "canoe", "rowboat") else
        "riding motorcycles" if has("motorcycle", "motorbike") and has("riding", "ride") else
        "swimming" if has("swimming", "swim") else
        "walking" if has("walking", "walk") else
        None
    )
    setting = (
        "a calm lake" if has("lake") and has("calm") else
        "a lake" if has("lake") else
        "the beach" if has("beach") else
        "a restaurant" if has("restaurant") else
        "the coast" if has("coast", "coastal") else
        None
    )
    people_count = (
        2 if re.search(r"\b(two people|man and (?:a )?woman|woman and (?:a )?man|couple|both)\b", text) or "couple" in words else
        1 if re.search(r"\b(?:a|one) (?:man|woman|person)\b", text) else
        None
    )
    details: list[str] = []
    if activity == "kayaking" and ("yellow kayak" in text or "yellow" in words):
        details.append("in a yellow kayak")
    if has("life jacket", "life jackets"):
        details.append("wearing life jackets")
    if has("blue paddles", "blue paddle"):
        details.append("using blue paddles")
    environment = []
    if has("tree", "trees"):
        environment.append("trees")
    if has("cliff", "cliffs"):
        environment.append("cliffs")
    if has("campsite", "camping", "tent", "tents"):
        environment.append("a campsite")
    if environment:
        details.append("with " + _join_words(environment) + " along the shore")
    return _SceneSummary(activity, setting, people_count, tuple(dict.fromkeys(details)), caption)


def _join_words(values: list[str]) -> str:
    if len(values) < 2:
        return values[0] if values else ""
    if len(values) == 2:
        return f"{values[0]} and {values[1]}"
    return f"{', '.join(values[:-1])} and {values[-1]}"


def _subject_phrase(people_names: list[str], people_count: int | None) -> str:
    if len(people_names) >= 2:
        return _join_words(people_names)
    if len(people_names) == 1:
        return f"{people_names[0]} with another person" if people_count and people_count > 1 else people_names[0]
    if people_count and people_count > 1:
        return "two people" if people_count == 2 else "several people"
    return "a person" if people_count == 1 else ""


def _scene_phrase(scene: _SceneSummary, people_names: list[str]) -> str:
    subject = _subject_phrase(people_names, scene.people_count)
    if scene.activity == "kayaking":
        phrase = f"{subject or 'People'} kayaking"
        if scene.setting:
            phrase += f" on {scene.setting}" if scene.setting.startswith("a ") else f" at {scene.setting}"
    elif scene.activity:
        phrase = f"{subject or 'People'} {scene.activity}"
        if scene.setting:
            phrase += f" at {scene.setting}" if scene.setting.startswith("the ") else f" by {scene.setting}"
    elif scene.setting:
        phrase = f"{subject or 'People'} at {scene.setting}"
    else:
        phrase = scene.fallback
    if scene.details:
        phrase += ", " + ", ".join(scene.details)
    return phrase


def _narrative_from_persisted_evidence(
    frames: list[VideoAnalysisFrame], evidence: list[VideoFrameEvidence], people_names: list[str],
) -> str | None:
    """Compress persisted frame observations without invoking a specialist."""
    scenes = [
        summary for frame in frames
        if (summary := _scene_summary(_caption_for_frame(evidence, frame.id), _tags_for_frame(evidence, frame.id)))
    ]
    if not scenes:
        return None
    compressed: list[_SceneSummary] = []
    for scene in scenes:
        if not compressed or scene.signature != compressed[-1].signature:
            compressed.append(scene)
    phrases = [_scene_phrase(scene, people_names) for scene in compressed]
    if len(phrases) == 1:
        return f"A short video showing {phrases[0]}."
    if len(phrases) == 2:
        return f"A short video showing {phrases[0]}, then {phrases[1]}."
    return f"A short video showing {phrases[0]}, then {phrases[1]}, before {phrases[2]}."


def reconcile_video_analysis_job(
    store, vault_store, people_store, metadata_store, job_id: UUID, *, refresh: bool = False,
) -> UUID | None:
    """Reconcile one explicit V2 result from persisted evidence only.

    This is intentionally not a broad historical sweep: callers pass the job
    produced by the selected-video V2 worker, or a specifically requested job.
    """
    job = store.claim_reconciliation(job_id, refresh=refresh)
    if job is None:
        return None
    asset = _asset_for_job(vault_store, job)
    if asset is None:
        store.transition(job.id, "failed", error="Published Home Video was not found for the owner")
        return job.id
    frames = store.frames_for_job(job.id)
    evidence: list[VideoFrameEvidence] = []
    warnings: list[str] = []
    for frame in frames:
        if frame.status == "failed":
            warnings.append(f"Frame analysis failed at {frame.timestamp_ms}ms")
        elif frame.warning:
            warnings.append(f"Frame analysis warning at {frame.timestamp_ms}ms: {frame.warning}")
        evidence.extend(store.evidence_for_frame(frame.id))
    provider_failures = [item for item in evidence if isinstance(item.raw_evidence, dict) and item.raw_evidence.get("error")]
    if provider_failures:
        warnings.append(f"{len(provider_failures)} specialist frame analyses failed")

    known_people = _known_people_from_evidence(evidence, people_store, job.requested_by)
    for person_id in known_people:
        # The existing effective_people query applies user include/exclude
        # authority; this association retains automated evidence only.
        people_store.associate(asset.id, person_id, "vault_master", created_by="video_reconciliation")
    effective_people = people_store.effective_people(
        asset.id, job.owner_user_id if job.owner_user_id is not None else job.requested_by
    )
    people_names = [person.display_name for person in effective_people]

    counts: dict[tuple[str, str], int] = {}
    for item in evidence:
        if item.provider == "rampp" and isinstance(item.processed_evidence, dict):
            tags = item.processed_evidence.get("tags")
            if isinstance(tags, list):
                # Count a resolved concept once per frame.  This makes
                # recurrence meaningful without allowing duplicate raw tags
                # inside one provider response to promote a video-level tag.
                for term in metadata_store.resolve_raw_tags(tags):
                    counts[term] = counts.get(term, 0) + 1
    # Repetition is relevance evidence, not a model-confidence threshold.
    video_terms = tuple(sorted(
        term for term, count in counts.items()
        if term[0] == "content_tag" and count >= 2
    ))
    metadata_store.persist_canonical_assignments(
        asset.id, video_terms, model_id="ram_plus_swin_large_14m",
        model_revision=None, task_version=VIDEO_RECONCILIATION_VERSION,
    )

    narrative = _narrative_from_persisted_evidence(frames, evidence, people_names)
    if narrative is None:
        warnings.append("No meaningful Florence frame description was available for a video narrative")
    references = {
        "frame_ids": [str(frame.id) for frame in frames],
        "florence_frame_ids": [str(item.frame_id) for item in evidence if item.provider == "florence"],
        "rampp_frame_ids": [str(item.frame_id) for item in evidence if item.provider == "rampp"],
        "people_frame_ids": [str(item.frame_id) for item in evidence if item.provider == "people"],
        "person_ids": [str(person_id) for person_id in known_people],
        "canonical_terms": [{"namespace": namespace, "slug": slug} for namespace, slug in video_terms],
        "narrative_strategy": "deterministic-scene-compression-v1",
    }
    result = VideoReconciliationResult(
        uuid4(), asset.id, job.id, narrative, VIDEO_RECONCILIATION_VERSION,
        tuple(dict.fromkeys(warnings)), references, datetime.now(timezone.utc),
    )
    store.save_reconciliation(result)
    store.transition(job.id, "completed_with_warnings" if warnings else "completed", warning="; ".join(result.warnings) or None)
    return job.id


def _job(row: dict[str, object]) -> VideoAnalysisJob:
    return VideoAnalysisJob(UUID(str(row["id"])), UUID(str(row["asset_id"])), str(row["requested_by"]), UUID(str(row["owner_user_id"])) if row.get("owner_user_id") else None, bool(row["requested_reanalysis"]), str(row["status"]), int(row["total_frames"]), int(row["frames_completed"]), int(row["frames_failed"]), row["warning"], row["error"], str(row["task_version"]), str(row["sampling_version"]), row["created_at"], row["started_at"], row["completed_at"])  # type: ignore[arg-type]


def _frame(row: dict[str, object]) -> VideoAnalysisFrame:
    return VideoAnalysisFrame(
        UUID(str(row["id"])), UUID(str(row["job_id"])), UUID(str(row["asset_id"])),
        int(row["timestamp_ms"]), int(row["ordinal"]), str(row["selection_reason"]),
        str(row["status"]), row["cache_key"], row["warning"], row["error"],
        row["created_at"], row["updated_at"],
    )  # type: ignore[arg-type]


def _evidence(row: dict[str, object]) -> VideoFrameEvidence:
    return VideoFrameEvidence(
        UUID(str(row["id"])), UUID(str(row["frame_id"])), str(row["provider"]),
        str(row["model_id"]), str(row["model_revision"]) if row["model_revision"] else None,
        str(row["task_version"]), row["raw_evidence"], row["processed_evidence"],
        int(row["processing_ms"]) if row["processing_ms"] is not None else None, row["created_at"],
    )


@lru_cache(maxsize=1)
def get_video_intelligence_store() -> PostgresVideoIntelligenceStore:
    return PostgresVideoIntelligenceStore(get_database_conninfo())
