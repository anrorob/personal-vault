from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.config import get_database_conninfo
from app.vault_master import (
    INCOMING_SOURCE,
    ImportItem,
    VaultMasterStore,
    has_hard_coded_screenshot_marker,
    require_file_within_root,
    sha256_file,
)
from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
from app.reading_room_intake import build_publication_bundles
from app.vault_master_ingestion_ai import (
    AUTO_PILOT_ELIGIBILITY_SCORE,
    INGESTION_TASK_VERSION,
    IngestionAiEvidence,
    IngestionAiStore,
)


AUTOPILOT_POLICY_VERSION = "universal-autopilot-v3"
AUTOPILOT_SAFE_CONTENT_DESTINATIONS = {
    "personal_photo": "Gallery",
    "receipt": "Documents",
    "financial_document": "Ledger",
    "general_document": "Documents",
    "artwork": "Archives",
}
AUTOPILOT_MAX_ITEMS = 100
AUTOPILOT_MAX_FAILURES = 3
AUTOPILOT_MAX_FAILURE_PERCENT = 10
AUTOPILOT_ACTIVITY_USERNAME = "Vault Master auto-pilot"
_AUTOPILOT_PROCESS_LOCK = Lock()


@dataclass(frozen=True)
class AutopilotPolicy:
    id: UUID
    owner_user_id: UUID | None
    requested_by: str
    source: str
    content_type: str
    destination: str
    threshold: int
    max_items: int
    max_failures: int
    max_failure_percent: int
    status: str
    policy_version: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AutopilotRun:
    id: UUID
    policy_id: UUID
    requested_by: str
    status: str
    item_ids: tuple[UUID, ...]
    outcomes: dict[str, str]
    stop_reason: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class GalleryScreenshotSuspect:
    item_id: UUID
    run_id: UUID
    filename: str
    vault_path: str
    sha256: str
    reasons: tuple[str, ...]
    moved_at: datetime


class AutopilotStore(Protocol):
    def list_policies(self, owner_user_id: UUID | None = None) -> list[AutopilotPolicy]: ...
    def upsert_policy(
        self,
        owner_user_id: UUID,
        username: str,
        content_type: str,
        destination: str,
        threshold: int,
        max_items: int,
        max_failures: int,
        max_failure_percent: int,
    ) -> AutopilotPolicy: ...
    def set_policy_status(
        self, policy_id: UUID, owner_user_id: UUID, status: str
    ) -> AutopilotPolicy | None: ...
    def create_run(
        self, policy: AutopilotPolicy, item_ids: tuple[UUID, ...]
    ) -> AutopilotRun: ...
    def update_run(
        self,
        run_id: UUID,
        outcomes: dict[str, str],
        status: str,
        stop_reason: str | None = None,
    ) -> AutopilotRun | None: ...
    def list_runs(self, owner_user_id: UUID) -> list[AutopilotRun]: ...
    def list_open_runs(self) -> list[AutopilotRun]: ...


def _validate_policy(
    content_type: str,
    destination: str,
    threshold: int,
    max_items: int,
    max_failures: int,
    max_failure_percent: int,
) -> None:
    if AUTOPILOT_SAFE_CONTENT_DESTINATIONS.get(content_type) != destination:
        raise ValueError("This content type and destination are not eligible for auto-pilot")
    if not AUTO_PILOT_ELIGIBILITY_SCORE <= threshold <= 100:
        raise ValueError("Auto-pilot threshold cannot be below 80")
    if not 1 <= max_items <= AUTOPILOT_MAX_ITEMS:
        raise ValueError("Auto-pilot batches must contain between 1 and 100 files")
    if not 1 <= max_failures <= AUTOPILOT_MAX_FAILURES:
        raise ValueError("Auto-pilot failure limit must be between 1 and 3")
    if not 1 <= max_failure_percent <= AUTOPILOT_MAX_FAILURE_PERCENT:
        raise ValueError("Auto-pilot failure percentage must be between 1 and 10")


class MemoryAutopilotStore:
    def __init__(self) -> None:
        self.policies: dict[UUID, AutopilotPolicy] = {}
        self.runs: dict[UUID, AutopilotRun] = {}

    def list_policies(self, owner_user_id: UUID | None = None) -> list[AutopilotPolicy]:
        return sorted(
            (
                policy
                for policy in self.policies.values()
                if owner_user_id is None or policy.owner_user_id == owner_user_id
            ),
            key=lambda policy: policy.updated_at,
            reverse=True,
        )

    def upsert_policy(
        self,
        owner_user_id: UUID,
        username: str,
        content_type: str,
        destination: str,
        threshold: int,
        max_items: int,
        max_failures: int,
        max_failure_percent: int,
    ) -> AutopilotPolicy:
        _validate_policy(
            content_type,
            destination,
            threshold,
            max_items,
            max_failures,
            max_failure_percent,
        )
        existing = next(
            (
                policy
                for policy in self.policies.values()
                if policy.owner_user_id == owner_user_id
                and policy.source == "arrival_hall"
                and policy.content_type == content_type
                and policy.destination == destination
            ),
            None,
        )
        now = datetime.now(timezone.utc)
        policy = AutopilotPolicy(
            id=existing.id if existing else uuid4(),
            owner_user_id=owner_user_id,
            requested_by=username,
            source="arrival_hall",
            content_type=content_type,
            destination=destination,
            threshold=threshold,
            max_items=max_items,
            max_failures=max_failures,
            max_failure_percent=max_failure_percent,
            status=existing.status if existing else "disabled",
            policy_version=AUTOPILOT_POLICY_VERSION,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
        self.policies[policy.id] = policy
        return policy

    def set_policy_status(
        self, policy_id: UUID, owner_user_id: UUID, status: str
    ) -> AutopilotPolicy | None:
        policy = self.policies.get(policy_id)
        if (
            policy is None
            or policy.owner_user_id != owner_user_id
            or status not in {"enabled", "paused", "disabled"}
        ):
            return None
        updated = AutopilotPolicy(
            **{
                **policy.__dict__,
                "status": status,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.policies[policy_id] = updated
        return updated

    def create_run(
        self, policy: AutopilotPolicy, item_ids: tuple[UUID, ...]
    ) -> AutopilotRun:
        now = datetime.now(timezone.utc)
        run = AutopilotRun(
            uuid4(), policy.id, policy.requested_by, "running", item_ids, {}, None, now, now, None
        )
        self.runs[run.id] = run
        return run

    def update_run(
        self,
        run_id: UUID,
        outcomes: dict[str, str],
        status: str,
        stop_reason: str | None = None,
    ) -> AutopilotRun | None:
        run = self.runs.get(run_id)
        if run is None or status not in {"running", "queued", "completed", "stopped"}:
            return None
        now = datetime.now(timezone.utc)
        updated = AutopilotRun(
            **{
                **run.__dict__,
                "outcomes": outcomes,
                "status": status,
                "stop_reason": stop_reason,
                "updated_at": now,
                "completed_at": now if status in {"completed", "stopped"} else None,
            }
        )
        self.runs[run_id] = updated
        return updated

    def list_runs(self, owner_user_id: UUID) -> list[AutopilotRun]:
        return sorted(
            (
                run
                for run in self.runs.values()
                if self.policies[run.policy_id].owner_user_id == owner_user_id
            ),
            key=lambda run: run.created_at,
            reverse=True,
        )

    def list_open_runs(self) -> list[AutopilotRun]:
        return [run for run in self.runs.values() if run.status == "queued"]


def _policy_from_row(row: dict[str, object]) -> AutopilotPolicy:
    return AutopilotPolicy(**row)  # type: ignore[arg-type]


def _run_from_row(row: dict[str, object]) -> AutopilotRun:
    row["item_ids"] = tuple(UUID(str(value)) for value in row["item_ids"])  # type: ignore[arg-type]
    row["outcomes"] = dict(row["outcomes"])  # type: ignore[arg-type]
    return AutopilotRun(**row)  # type: ignore[arg-type]


class PostgresAutopilotStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_autopilot_policies (
                    id UUID PRIMARY KEY,
                    owner_user_id UUID REFERENCES auth_accounts(user_id),
                    requested_by TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (source = 'arrival_hall'),
                    content_type TEXT NOT NULL,
                    destination TEXT NOT NULL,
                    threshold INTEGER NOT NULL CHECK (threshold BETWEEN 80 AND 100),
                    max_items INTEGER NOT NULL CHECK (max_items BETWEEN 1 AND 100),
                    max_failures INTEGER NOT NULL CHECK (max_failures BETWEEN 1 AND 3),
                    max_failure_percent INTEGER NOT NULL CHECK (max_failure_percent BETWEEN 1 AND 10),
                    status TEXT NOT NULL DEFAULT 'disabled'
                        CHECK (status IN ('enabled','paused','disabled')),
                    policy_version TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (owner_user_id, source, content_type, destination)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS vault_autopilot_runs (
                    id UUID PRIMARY KEY,
                    policy_id UUID NOT NULL REFERENCES vault_autopilot_policies(id),
                    requested_by TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running','queued','completed','stopped')),
                    item_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
                    outcomes JSONB NOT NULL DEFAULT '{}'::jsonb,
                    stop_reason TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMPTZ
                )
                """
            )
            cursor.execute(
                """
                ALTER TABLE vault_autopilot_policies
                ADD COLUMN IF NOT EXISTS owner_user_id UUID
                    REFERENCES auth_accounts(user_id)
                """
            )
            cursor.execute(
                """
                UPDATE vault_autopilot_policies AS policy
                SET owner_user_id = account.user_id
                FROM auth_accounts AS account
                WHERE policy.owner_user_id IS NULL
                  AND policy.requested_by = account.username
                """
            )
            cursor.execute(
                """
                UPDATE vault_autopilot_policies
                SET status = 'disabled', updated_at = CURRENT_TIMESTAMP
                WHERE owner_user_id IS NULL AND status = 'enabled'
                """
            )
            # PostgreSQL truncates generated constraint names at 63 bytes.  A
            # name-based DROP left the old requested_by uniqueness constraint
            # in place on deployed databases, so a new owner's policy could
            # fail before the UUID owner key was considered.  Match the
            # specific legacy constraint definition instead of trusting its
            # generated name.
            cursor.execute(
                """
                DO $$
                DECLARE legacy_constraint TEXT;
                BEGIN
                    SELECT conname INTO legacy_constraint
                    FROM pg_constraint
                    WHERE conrelid = 'vault_autopilot_policies'::regclass
                      AND contype = 'u'
                      AND pg_get_constraintdef(oid) =
                          'UNIQUE (requested_by, source, content_type, destination)';
                    IF legacy_constraint IS NOT NULL THEN
                        EXECUTE format(
                            'ALTER TABLE vault_autopilot_policies DROP CONSTRAINT %I',
                            legacy_constraint
                        );
                    END IF;
                END $$;
                """
            )
            cursor.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                    vault_autopilot_policies_owner_destination_idx
                ON vault_autopilot_policies (
                    owner_user_id, source, content_type, destination
                ) WHERE owner_user_id IS NOT NULL
                """
            )
            cursor.execute(
                """
                ALTER TABLE vault_autopilot_policies
                DROP CONSTRAINT IF EXISTS
                    vault_autopilot_policies_threshold_check
                """
            )
            cursor.execute(
                """
                ALTER TABLE vault_autopilot_policies
                ADD CONSTRAINT vault_autopilot_policies_threshold_check
                CHECK (threshold BETWEEN 80 AND 100)
                """
            )
            cursor.execute(
                """
                UPDATE vault_autopilot_policies
                SET threshold = 80,
                    policy_version = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE threshold = 85
                  AND policy_version = 'conservative-autopilot-v1'
                """,
                (AUTOPILOT_POLICY_VERSION,),
            )
            cursor.execute(
                """
                WITH stopped AS (
                    UPDATE vault_autopilot_runs
                    SET status='stopped',
                        stop_reason='Backend restarted; explicit owner resume required',
                        completed_at=CURRENT_TIMESTAMP,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE status IN ('running','queued')
                    RETURNING policy_id
                )
                UPDATE vault_autopilot_policies AS policies
                SET status='paused', updated_at=CURRENT_TIMESTAMP
                WHERE status='enabled'
                  AND policies.id IN (SELECT policy_id FROM stopped)
                """
            )

    def list_policies(self, owner_user_id: UUID | None = None) -> list[AutopilotPolicy]:
        with self._connect() as connection, connection.cursor() as cursor:
            if owner_user_id is None:
                cursor.execute("SELECT * FROM vault_autopilot_policies ORDER BY updated_at DESC")
            else:
                cursor.execute(
                    "SELECT * FROM vault_autopilot_policies WHERE owner_user_id=%s ORDER BY updated_at DESC",
                    (owner_user_id,),
                )
            return [_policy_from_row(row) for row in cursor.fetchall()]

    def upsert_policy(
        self,
        owner_user_id: UUID,
        username: str,
        content_type: str,
        destination: str,
        threshold: int,
        max_items: int,
        max_failures: int,
        max_failure_percent: int,
    ) -> AutopilotPolicy:
        _validate_policy(content_type, destination, threshold, max_items, max_failures, max_failure_percent)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO vault_autopilot_policies (
                    id,owner_user_id,requested_by,source,content_type,destination,threshold,
                    max_items,max_failures,max_failure_percent,status,policy_version
                ) VALUES (%s,%s,%s,'arrival_hall',%s,%s,%s,%s,%s,%s,'disabled',%s)
                ON CONFLICT (owner_user_id,source,content_type,destination)
                    WHERE owner_user_id IS NOT NULL
                DO UPDATE SET threshold=EXCLUDED.threshold,max_items=EXCLUDED.max_items,
                    max_failures=EXCLUDED.max_failures,
                    max_failure_percent=EXCLUDED.max_failure_percent,
                    policy_version=EXCLUDED.policy_version,updated_at=CURRENT_TIMESTAMP
                RETURNING *
                """,
                (uuid4(), owner_user_id, username, content_type, destination, threshold, max_items,
                 max_failures, max_failure_percent, AUTOPILOT_POLICY_VERSION),
            )
            row = cursor.fetchone()
            assert row is not None
            return _policy_from_row(row)

    def set_policy_status(self, policy_id: UUID, owner_user_id: UUID, status: str) -> AutopilotPolicy | None:
        if status not in {"enabled", "paused", "disabled"}:
            return None
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE vault_autopilot_policies SET status=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s RETURNING *",
                (status, policy_id, owner_user_id),
            )
            row = cursor.fetchone()
            return _policy_from_row(row) if row else None

    def create_run(self, policy: AutopilotPolicy, item_ids: tuple[UUID, ...]) -> AutopilotRun:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO vault_autopilot_runs (id,policy_id,requested_by,status,item_ids) VALUES (%s,%s,%s,'running',%s) RETURNING *",
                (uuid4(), policy.id, policy.requested_by, json.dumps([str(value) for value in item_ids])),
            )
            row = cursor.fetchone()
            assert row is not None
            return _run_from_row(row)

    def update_run(self, run_id: UUID, outcomes: dict[str, str], status: str, stop_reason: str | None = None) -> AutopilotRun | None:
        if status not in {"running", "queued", "completed", "stopped"}:
            return None
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE vault_autopilot_runs SET outcomes=%s,status=%s,stop_reason=%s,
                    updated_at=CURRENT_TIMESTAMP,
                    completed_at=CASE WHEN %s IN ('completed','stopped') THEN CURRENT_TIMESTAMP ELSE NULL END
                WHERE id=%s RETURNING *
                """,
                (json.dumps(outcomes), status, stop_reason, status, run_id),
            )
            row = cursor.fetchone()
            return _run_from_row(row) if row else None

    def list_runs(self, owner_user_id: UUID) -> list[AutopilotRun]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT runs.* FROM vault_autopilot_runs AS runs
                JOIN vault_autopilot_policies AS policies ON policies.id = runs.policy_id
                WHERE policies.owner_user_id=%s
                ORDER BY runs.created_at DESC LIMIT 50
                """,
                (owner_user_id,),
            )
            return [_run_from_row(row) for row in cursor.fetchall()]

    def list_open_runs(self) -> list[AutopilotRun]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_autopilot_runs WHERE status='queued' ORDER BY created_at")
            return [_run_from_row(row) for row in cursor.fetchall()]


def _latest_evidence(
    store: IngestionAiStore, owner_user_id: UUID, owned_item_ids: set[UUID]
) -> dict[UUID, IngestionAiEvidence]:
    latest: dict[UUID, IngestionAiEvidence] = {}
    for evidence in store.list_all_evidence():
        if evidence.item_id in owned_item_ids:
            latest.setdefault(evidence.item_id, evidence)
    return latest


def _eligible(
    item: ImportItem,
    evidence: IngestionAiEvidence,
    policy: AutopilotPolicy,
    publication_item_ids: set[UUID],
) -> bool:
    return (
        item.source_kind == INCOMING_SOURCE
        and item.state == "needs_review"
        and item.duplicate_of_id is None
        and item.id not in publication_item_ids
        and item.proposed_category == policy.destination
        and evidence.content_type == policy.content_type
        and evidence.recommended_destination == policy.destination
        and evidence.routing_band == "automatic_eligible"
        and evidence.decision_score >= policy.threshold
        and not evidence.conflicts
        and not evidence.automatic_disqualifiers
        and evidence.model_id == AI_MODEL_ID
        and evidence.model_revision == AI_MODEL_REVISION
        and evidence.task_version == INGESTION_TASK_VERSION
    )


def _preflight(
    item: ImportItem,
    incoming_root: Path,
    destination_roots: dict[str, Path],
) -> str | None:
    try:
        root = incoming_root.resolve(strict=True)
        source = require_file_within_root(
            root.joinpath(*item.relative_path.split("/")), root
        )
        if source.stat().st_size != item.size_bytes or sha256_file(source) != item.sha256:
            return "source_changed"
        destination_root = destination_roots[item.proposed_category or ""].resolve(strict=True)
        if not destination_root.is_dir():
            return "destination_mount_unavailable"
        if not item.proposed_destination:
            return "destination_missing"
        relative = item.proposed_destination.removeprefix(f"/vault/{item.proposed_category}/")
        destination = destination_root.joinpath(*relative.split("/")).resolve(strict=False)
        if not destination.is_relative_to(destination_root):
            return "destination_path_unsafe"
        if destination.exists():
            return "destination_collision"
    except (KeyError, OSError, ValueError):
        return "preflight_failed"
    return None


def _process_autopilot_batch_unlocked(
    policy_store: AutopilotStore,
    ai_store: IngestionAiStore,
    vault_store: VaultMasterStore,
    incoming_root: Path,
    destination_roots: dict[str, Path],
) -> UUID | None:
    for policy in policy_store.list_policies():
        if policy.status != "enabled" or policy.owner_user_id is None:
            continue
        owned_items = [
            item for item in vault_store.list_items()
            if item.owner_user_id == policy.owner_user_id
        ]
        owned_item_ids = {item.id for item in owned_items}
        evidence_by_item = _latest_evidence(
            ai_store, policy.owner_user_id, owned_item_ids
        )
        version_mismatches = [
            item
            for item in owned_items
            if item.source_kind == INCOMING_SOURCE
            and item.state == "needs_review"
            and (evidence := evidence_by_item.get(item.id)) is not None
            and evidence.content_type == policy.content_type
            and evidence.recommended_destination == policy.destination
            and (
                evidence.model_id != AI_MODEL_ID
                or evidence.model_revision != AI_MODEL_REVISION
                or evidence.task_version != INGESTION_TASK_VERSION
            )
        ]
        if version_mismatches:
            affected = tuple(item.id for item in version_mismatches[: policy.max_items])
            run = policy_store.create_run(policy, affected)
            outcomes = {str(item_id): "model_or_task_version_mismatch" for item_id in affected}
            reason = "Model or task version changed; explicit owner review and resume required"
            policy_store.set_policy_status(policy.id, policy.owner_user_id, "paused")
            policy_store.update_run(run.id, outcomes, "stopped", reason)
            return run.id
        publication_item_ids = {
            item_id
            for bundle in build_publication_bundles(vault_store.list_items())
            for item_id in (
                *bundle.source_item_ids,
                *bundle.front_cover_item_ids,
                *bundle.back_cover_item_ids,
            )
        }
        candidates = [
            item
            for item in owned_items
            if (evidence := evidence_by_item.get(item.id)) is not None
            and _eligible(item, evidence, policy, publication_item_ids)
        ][: policy.max_items]
        if not candidates:
            continue
        run = policy_store.create_run(policy, tuple(item.id for item in candidates))
        outcomes: dict[str, str] = {}
        failures = 0
        stop_reason = None
        for item in candidates:
            failure = _preflight(item, incoming_root, destination_roots)
            if failure:
                outcomes[str(item.id)] = failure
                failures += 1
            else:
                approved = vault_store.record_decision(
                    item.id,
                    "approved",
                    AUTOPILOT_ACTIVITY_USERNAME,
                )
                queued = (
                    vault_store.queue_move(
                        item.id,
                        AUTOPILOT_ACTIVITY_USERNAME,
                    )
                    if approved
                    else None
                )
                outcomes[str(item.id)] = "queued" if queued else "queue_failed"
                failures += 0 if queued else 1
            failure_percent = failures * 100 / max(1, len(candidates))
            if failures >= policy.max_failures or failure_percent >= policy.max_failure_percent:
                stop_reason = "Auto-pilot circuit breaker reached during preflight"
                break
        if stop_reason:
            for item in candidates:
                outcomes.setdefault(str(item.id), "not_processed_circuit_breaker")
            policy_store.set_policy_status(policy.id, policy.owner_user_id, "paused")
            policy_store.update_run(run.id, outcomes, "stopped", stop_reason)
        else:
            policy_store.update_run(run.id, outcomes, "queued")
        return run.id
    return None


def audit_recent_gallery_screenshots(
    policy_store: AutopilotStore,
    ai_store: IngestionAiStore,
    vault_store: VaultMasterStore,
    username: str,
    limit: int = 100,
) -> list[GalleryScreenshotSuspect]:
    """Report auto-moved Gallery files with screenshot facts; never mutate them."""
    suspects: list[GalleryScreenshotSuspect] = []
    seen_items: set[UUID] = set()
    for run in (
        run
        for policy in policy_store.list_policies()
        if policy.owner_user_id is not None
        for run in policy_store.list_runs(policy.owner_user_id)
    ):
        moved_at = run.completed_at or run.updated_at
        for item_id in run.item_ids:
            if len(suspects) >= limit:
                return suspects
            if item_id in seen_items or run.outcomes.get(str(item_id)) != "moved":
                continue
            seen_items.add(item_id)
            item = vault_store.get_item(item_id)
            if (
                item is None
                or item.proposed_category != "Gallery"
                or not item.proposed_destination
            ):
                continue
            reasons: list[str] = []
            if has_hard_coded_screenshot_marker(item.filename, item.metadata):
                reasons.append(
                    "Filename or embedded metadata explicitly identifies a screenshot"
                )
            evidence_rows = ai_store.list_evidence_for_learning(item.id)
            if evidence_rows:
                latest = evidence_rows[0]
                if latest.content_type == "screenshot":
                    reasons.append("Florence classified the file as a screenshot")
                evidence_text = f"{latest.caption}\n{latest.ocr_text}".casefold()
                if any(
                    marker in evidence_text
                    for marker in (
                        "screenshot",
                        "user interface",
                        "phone screen",
                        "google maps",
                        "mobile app",
                        "application interface",
                        "status bar",
                    )
                ):
                    reasons.append("Florence or OCR evidence describes an application screen")
            if not reasons:
                continue
            asset = vault_store.get_catalogued_asset(item.proposed_destination)
            if asset is None or asset.sha256 != item.sha256:
                continue
            suspects.append(
                GalleryScreenshotSuspect(
                    item.id,
                    run.id,
                    item.filename,
                    item.proposed_destination,
                    item.sha256,
                    tuple(dict.fromkeys(reasons)),
                    moved_at,
                )
            )
    return suspects


def process_autopilot_batch(
    policy_store: AutopilotStore,
    ai_store: IngestionAiStore,
    vault_store: VaultMasterStore,
    incoming_root: Path,
    destination_roots: dict[str, Path],
) -> UUID | None:
    if not _AUTOPILOT_PROCESS_LOCK.acquire(blocking=False):
        return None
    try:
        return _process_autopilot_batch_unlocked(
            policy_store,
            ai_store,
            vault_store,
            incoming_root,
            destination_roots,
        )
    finally:
        _AUTOPILOT_PROCESS_LOCK.release()


def reconcile_autopilot_runs(policy_store: AutopilotStore, vault_store: VaultMasterStore) -> UUID | None:
    for run in policy_store.list_open_runs():
        outcomes = dict(run.outcomes)
        pending = False
        failures = 0
        for item_id in run.item_ids:
            if outcomes.get(str(item_id)) != "queued":
                failures += 1
                continue
            item = vault_store.get_item(item_id)
            if item is None:
                outcomes[str(item_id)] = "catalogue_missing"
                failures += 1
            elif item.state == "moved":
                outcomes[str(item_id)] = "moved"
            elif item.state == "move_failed":
                outcomes[str(item_id)] = "move_failed"
                failures += 1
            else:
                pending = True
        if pending:
            continue
        policy = next((value for value in policy_store.list_policies() if value.id == run.policy_id), None)
        if failures and policy:
            if policy.owner_user_id is not None:
                policy_store.set_policy_status(policy.id, policy.owner_user_id, "paused")
            policy_store.update_run(run.id, outcomes, "stopped", "A queued automatic move failed; explicit owner resume required")
        else:
            policy_store.update_run(run.id, outcomes, "completed")
        return run.id
    return None


@lru_cache
def get_autopilot_store() -> AutopilotStore:
    return PostgresAutopilotStore(get_database_conninfo())
