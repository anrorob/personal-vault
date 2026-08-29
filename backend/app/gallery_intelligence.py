"""Post-publication Gallery Intelligence.

This module deliberately has no routing dependency: it never imports or calls
``assess_destination`` and never changes an Arrival Hall item or its score.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import mimetypes
import os
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row

from app.config import get_database_conninfo
from app.vault_master import CataloguedAsset, VaultMasterStore
from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
GALLERY_INTELLIGENCE_TASK_VERSION = "gallery-intelligence-rampp-v1"
GALLERY_INTELLIGENCE_RESOLVER_VERSION = "gallery-concept-resolver-v1"
BULK_COMPLETION_GRACE = timedelta(seconds=12)
Decision = Literal["include", "exclude"]

# Deliberately small, controlled Stage A vocabulary. Specialist evidence is
# resolved into it; no recognition confidence is used as an acceptance gate.
INITIAL_TERMS: tuple[tuple[str, str, str], ...] = (
    ("photo_type", "portrait", "Portrait"),
    ("photo_type", "selfie", "Selfie"),
    ("photo_type", "landscape", "Landscape"),
    ("photo_type", "animal", "Animal"),
    ("photo_type", "food", "Food"),
    ("photo_type", "document", "Document"),
    ("photo_type", "screenshot", "Screenshot"),
    ("photo_type", "architecture", "Building / Architecture"),
    ("photo_type", "vehicle", "Vehicle"),
    ("photo_type", "night_photo", "Night photo"),
    ("content_tag", "motorcycle", "Motorcycle"),
    ("content_tag", "outdoors", "Outdoors"),
    ("content_tag", "building", "Building"),
    ("content_tag", "animal", "Animal"),
    ("content_tag", "cat", "Cat"),
    ("content_tag", "beach", "Beach"),
    ("content_tag", "sea", "Sea"),
)


@dataclass(frozen=True)
class GalleryIntelligenceJob:
    id: UUID
    asset_id: UUID
    requested_by: str
    owner_user_id: UUID | None
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    bulk_run_id: UUID | None = None
    people_status: str = "pending"
    people_error: str | None = None
    people_only: bool = False
    skip_people: bool = False


@dataclass(frozen=True)
class GalleryIntelligenceBulkRunProgress:
    id: UUID
    total: int
    completed: int
    processing: int
    queued: int
    failed: int


@dataclass(frozen=True)
class MetadataAssignment:
    asset_id: UUID
    namespace: str
    slug: str
    display_name: str
    source: str
    confidence: float | None
    model_id: str | None
    model_revision: str | None
    task_version: str | None


@dataclass(frozen=True)
class GalleryIntelligenceClassification:
    raw_response: str
    model_id: str
    model_revision: str
    task_version: str
    processing_ms: int
    provider: str = "rampp"


@dataclass(frozen=True)
class GalleryIntelligenceEvidence:
    job_id: UUID
    asset_id: UUID
    raw_classification: str
    canonical_assignments: tuple[tuple[str, str], ...]
    model_id: str
    model_revision: str
    task_version: str
    processing_ms: int
    provider: str = "rampp"
    resolver_version: str = GALLERY_INTELLIGENCE_RESOLVER_VERSION


@dataclass(frozen=True)
class GalleryConcept:
    slug: str
    parent_slug: str | None = None
    evidence_role: Literal["direct", "supporting", "insufficient_alone"] = "direct"
    active: bool = True


# Bootstrap only. PostgreSQL is the authoritative interpretation source after
# schema initialisation; adding a new specialist term requires data, not code.
INITIAL_CONCEPTS: tuple[GalleryConcept, ...] = (
    GalleryConcept("animal"), GalleryConcept("bird", "animal"),
    GalleryConcept("stork", "bird"), GalleryConcept("wildlife", "animal"),
    GalleryConcept("cat", "animal"), GalleryConcept("pet", "animal"),
    GalleryConcept("tabby", "cat"), GalleryConcept("nest", "animal", "insufficient_alone"),
    GalleryConcept("vehicle"), GalleryConcept("motorcycle", "vehicle"),
    GalleryConcept("motorbike", "vehicle"),
    GalleryConcept("building"), GalleryConcept("palace", "building"),
    GalleryConcept("brick building", "building"),
    GalleryConcept("selfie"), GalleryConcept("food"), GalleryConcept("beach"),
    GalleryConcept("sea"), GalleryConcept("park"), GalleryConcept("outdoors"),
)
INITIAL_CONCEPT_TERMS: tuple[tuple[str, str, str], ...] = (
    ("animal", "photo_type", "animal"), ("animal", "content_tag", "animal"),
    ("cat", "content_tag", "cat"), ("vehicle", "photo_type", "vehicle"),
    ("motorcycle", "content_tag", "motorcycle"), ("motorbike", "content_tag", "motorcycle"),
    ("building", "photo_type", "architecture"), ("building", "content_tag", "building"),
    ("selfie", "photo_type", "selfie"), ("food", "photo_type", "food"),
    ("beach", "content_tag", "beach"), ("beach", "content_tag", "outdoors"),
    ("sea", "content_tag", "sea"), ("sea", "content_tag", "outdoors"),
    ("park", "content_tag", "outdoors"), ("outdoors", "content_tag", "outdoors"),
)


def resolve_gallery_concepts(
    raw_tags: object,
    concepts: tuple[GalleryConcept, ...],
    concept_terms: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[str, str], ...]:
    """Resolve direct specialist evidence through a bounded concept hierarchy."""
    if not isinstance(raw_tags, list) or not all(isinstance(tag, str) for tag in raw_tags):
        return ()
    by_slug = {concept.slug.casefold(): concept for concept in concepts if concept.active}
    terms_by_concept: dict[str, list[tuple[str, str]]] = {}
    for concept_slug, namespace, term_slug in concept_terms:
        terms_by_concept.setdefault(concept_slug.casefold(), []).append((namespace, term_slug))
    values: list[tuple[str, str]] = []
    for raw_tag in raw_tags:
        current = by_slug.get(raw_tag.strip().casefold())
        if current is None or current.evidence_role == "insufficient_alone":
            continue
        visited: set[str] = set()
        hierarchy: list[str] = []
        while current is not None and current.slug.casefold() not in visited:
            slug = current.slug.casefold()
            visited.add(slug)
            hierarchy.append(slug)
            current = by_slug.get(current.parent_slug.casefold()) if current.parent_slug else None
        for slug in reversed(hierarchy):
            values.extend(terms_by_concept.get(slug, ()))
    return tuple(dict.fromkeys(values))


def normalise_rampp_tags(raw_tags: object) -> tuple[tuple[str, str], ...]:
    """Bootstrap/test helper; production resolution is database-backed."""
    return resolve_gallery_concepts(raw_tags, INITIAL_CONCEPTS, INITIAL_CONCEPT_TERMS)


class MemoryGalleryIntelligenceStore:
    def __init__(self) -> None:
        self.terms = {(namespace, slug): display for namespace, slug, display in INITIAL_TERMS}
        self.concepts = list(INITIAL_CONCEPTS)
        self.concept_terms = list(INITIAL_CONCEPT_TERMS)
        self.jobs: dict[UUID, GalleryIntelligenceJob] = {}
        self.bulk_runs: dict[UUID, tuple[UUID, datetime]] = {}
        self.assignments: dict[tuple[UUID, str, str], MetadataAssignment] = {}
        self.decisions: dict[tuple[UUID, str, str], Decision] = {}
        self.evidence: dict[UUID, GalleryIntelligenceEvidence] = {}
        self.reconciliations: dict[UUID, object] = {}

    def save_reconciliation(self, result) -> None:
        self.reconciliations[result.asset_id] = result

    def reconciliation(self, asset_id: UUID):
        return self.reconciliations.get(asset_id)

    def latest_evidence(self, asset_id: UUID) -> GalleryIntelligenceEvidence | None:
        return next(
            (
                self.evidence[job_id]
                for job_id in reversed(self.jobs)
                if job_id in self.evidence and self.evidence[job_id].asset_id == asset_id
            ),
            None,
        )

    def queue(
        self, asset_id: UUID, username: str, force: bool = False, bulk_run_id: UUID | None = None, people_only: bool = False, skip_people: bool = False
    ) -> GalleryIntelligenceJob:
        existing = next((job for job in self.jobs.values() if job.asset_id == asset_id), None)
        if existing and not force:
            return existing
        job = GalleryIntelligenceJob(uuid4(), asset_id, username, None, "queued", 0, None, datetime.now(timezone.utc), bulk_run_id=bulk_run_id, people_only=people_only, skip_people=skip_people)
        self.jobs[job.id] = job
        return job

    def latest_job(self, asset_id: UUID) -> GalleryIntelligenceJob | None:
        # Dictionary insertion order is the in-memory equivalent of the
        # database's monotonic job_sequence. A retry is always the latest
        # attempt even when two test-clock timestamps are equal.
        return next(
            (self.jobs[job_id] for job_id in reversed(self.jobs) if self.jobs[job_id].asset_id == asset_id),
            None,
        )

    def latest_people_job(self, asset_id: UUID) -> GalleryIntelligenceJob | None:
        return next(
            (self.jobs[job_id] for job_id in reversed(self.jobs) if self.jobs[job_id].asset_id == asset_id and not self.jobs[job_id].skip_people),
            None,
        )

    def latest_successful_people_job(self, asset_id: UUID) -> GalleryIntelligenceJob | None:
        return next(
            (
                self.jobs[job_id]
                for job_id in reversed(self.jobs)
                if self.jobs[job_id].asset_id == asset_id
                and self.jobs[job_id].people_status == "completed"
            ),
            None,
        )

    def has_any_job(self, asset_id: UUID) -> bool:
        return any(job.asset_id == asset_id for job in self.jobs.values())

    def jobs_for_asset(self, asset_id: UUID) -> list[GalleryIntelligenceJob]:
        return [job for job in self.jobs.values() if job.asset_id == asset_id]

    def start_bulk_run(self, username: str, owner_user_id: UUID) -> UUID:
        run_id = uuid4()
        self.bulk_runs[run_id] = (owner_user_id, datetime.now(timezone.utc))
        return run_id

    def discard_bulk_run(self, run_id: UUID) -> None:
        self.bulk_runs.pop(run_id, None)

    def latest_bulk_run(self, owner_user_id: UUID) -> GalleryIntelligenceBulkRunProgress | None:
        candidates = [
            (run_id, created_at) for run_id, (owner, created_at) in self.bulk_runs.items() if owner == owner_user_id
        ]
        if not candidates:
            return None
        run_id, _ = max(candidates, key=lambda item: item[1])
        counts = {status: 0 for status in ("queued", "processing", "completed", "failed")}
        settled_at: datetime | None = None
        for job in self.jobs.values():
            if job.bulk_run_id == run_id:
                effective_status = (
                    "failed"
                    if job.status == "completed" and not job.skip_people and job.people_status == "failed"
                    else job.status
                )
                counts[effective_status] += 1
                if job.status in {"completed", "failed"} and job.completed_at:
                    settled_at = max(settled_at, job.completed_at) if settled_at else job.completed_at
        if counts["queued"] + counts["processing"] == 0 and (
            settled_at is None or datetime.now(timezone.utc) - settled_at > BULK_COMPLETION_GRACE
        ):
            return None
        return GalleryIntelligenceBulkRunProgress(run_id, sum(counts.values()), **counts)

    def claim_next_job(self) -> GalleryIntelligenceJob | None:
        job = next((entry for entry in self.jobs.values() if entry.status == "queued"), None)
        if not job:
            return None
        claimed = replace(job, status="processing", attempts=job.attempts + 1, started_at=datetime.now(timezone.utc))
        self.jobs[job.id] = claimed
        return claimed

    def complete(
        self,
        job_id: UUID,
        terms: tuple[tuple[str, str], ...],
        confidence: float | None,
        evidence: GalleryIntelligenceClassification | None = None,
    ) -> None:
        job = self.jobs[job_id]
        model_id = evidence.model_id if evidence else AI_MODEL_ID
        model_revision = evidence.model_revision if evidence else AI_MODEL_REVISION
        task_version = evidence.task_version if evidence else GALLERY_INTELLIGENCE_TASK_VERSION
        for namespace, slug in terms:
            display = self.terms[(namespace, slug)]
            self.assignments[(job.asset_id, namespace, slug)] = MetadataAssignment(job.asset_id, namespace, slug, display, "vault_master", confidence, model_id, model_revision, task_version)
        if evidence:
            self.evidence[job_id] = GalleryIntelligenceEvidence(
                job_id, job.asset_id, evidence.raw_response, terms, model_id,
                model_revision, task_version, evidence.processing_ms, evidence.provider,
            )
        self.jobs[job_id] = replace(job, status="completed", completed_at=datetime.now(timezone.utc))

    def persist_canonical_assignments(
        self, asset_id: UUID, terms: tuple[tuple[str, str], ...], *,
        model_id: str, model_revision: str | None, task_version: str,
    ) -> None:
        """Persist VM-resolved metadata for any canonical Vault asset.

        Gallery and Personal Video Intelligence deliberately share terms and
        the authoritative user include/exclude layer.
        """
        for namespace, slug in terms:
            display = self.terms.get((namespace, slug))
            if display:
                self.assignments[(asset_id, namespace, slug)] = MetadataAssignment(
                    asset_id, namespace, slug, display, "vault_master", None,
                    model_id, model_revision, task_version,
                )

    def fail(self, job_id: UUID, error: str) -> None:
        job = self.jobs[job_id]
        self.jobs[job_id] = replace(job, status="failed", error=error, completed_at=datetime.now(timezone.utc))

    def mark_people_status(self, job_id: UUID, status: str, error: str | None = None) -> None:
        self.jobs[job_id] = replace(self.jobs[job_id], people_status=status, people_error=error)

    def decide(
        self, asset_id: UUID, namespace: str, slug: str, decision: Decision, username: str | None = None
    ) -> None:
        self.decisions[(asset_id, namespace, slug)] = decision

    def effective(self, asset_id: UUID) -> list[MetadataAssignment]:
        values = []
        for (assigned_asset, namespace, slug), assignment in self.assignments.items():
            if assigned_asset == asset_id and self.decisions.get((asset_id, namespace, slug)) != "exclude":
                values.append(assignment)
        for (assigned_asset, namespace, slug), decision in self.decisions.items():
            if assigned_asset == asset_id and decision == "include" and (asset_id, namespace, slug) not in self.assignments:
                values.append(MetadataAssignment(asset_id, namespace, slug, self.terms[(namespace, slug)], "user", None, None, None, None))
        return values

    def list_terms(self) -> list[dict[str, str]]:
        return [
            {"namespace": namespace, "slug": slug, "display_name": display}
            for (namespace, slug), display in sorted(self.terms.items())
        ]

    def matching_asset_ids(
        self, photo_types: tuple[str, ...], content_tags: tuple[str, ...]
    ) -> set[UUID]:
        asset_ids = {asset_id for asset_id, _, _ in self.assignments} | {
            asset_id for asset_id, _, _ in self.decisions
        }
        matches: set[UUID] = set()
        for asset_id in asset_ids:
            terms = {(entry.namespace, entry.slug) for entry in self.effective(asset_id)}
            if photo_types and not any(("photo_type", slug) in terms for slug in photo_types):
                continue
            if content_tags and not any(("content_tag", slug) in terms for slug in content_tags):
                continue
            matches.add(asset_id)
        return matches

    def job_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for job in self.jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
        return counts

    def resolve_raw_tags(self, raw_tags: object) -> tuple[tuple[str, str], ...]:
        return resolve_gallery_concepts(raw_tags, tuple(self.concepts), tuple(self.concept_terms))


class PostgresGalleryIntelligenceStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_metadata_terms (
                id UUID PRIMARY KEY, namespace TEXT NOT NULL CHECK (namespace IN ('photo_type','content_tag')),
                slug TEXT NOT NULL, display_name TEXT NOT NULL, active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(namespace, slug))""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_asset_metadata_assignments (
                id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), term_id UUID NOT NULL REFERENCES vault_metadata_terms(id),
                source TEXT NOT NULL CHECK (source IN ('vault_master','user','imported')), confidence DOUBLE PRECISION,
                model_id TEXT, model_revision TEXT, task_version TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(asset_id, term_id, source, model_id, model_revision, task_version))""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_asset_metadata_decisions (
                id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), term_id UUID NOT NULL REFERENCES vault_metadata_terms(id),
                decision TEXT NOT NULL CHECK (decision IN ('include','exclude')), decided_by TEXT NOT NULL,
                active BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(asset_id, term_id))""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_gallery_intelligence_jobs (
                id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_assets(id), requested_by TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('queued','processing','completed','failed')), attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
                job_sequence BIGINT GENERATED BY DEFAULT AS IDENTITY)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_gallery_intelligence_bulk_runs (
                id UUID PRIMARY KEY, requested_by TEXT NOT NULL, owner_user_id UUID REFERENCES auth_accounts(user_id),
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_jobs ADD COLUMN IF NOT EXISTS job_sequence BIGINT GENERATED BY DEFAULT AS IDENTITY")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_jobs ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_jobs ADD COLUMN IF NOT EXISTS bulk_run_id UUID REFERENCES vault_gallery_intelligence_bulk_runs(id)")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_bulk_runs ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("""UPDATE vault_gallery_intelligence_bulk_runs AS runs SET owner_user_id=accounts.user_id
                FROM auth_accounts AS accounts WHERE runs.owner_user_id IS NULL AND runs.requested_by=accounts.username""")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_jobs ADD COLUMN IF NOT EXISTS people_status TEXT NOT NULL DEFAULT 'pending'")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_jobs ADD COLUMN IF NOT EXISTS people_error TEXT")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_jobs ADD COLUMN IF NOT EXISTS people_only BOOLEAN NOT NULL DEFAULT FALSE")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_jobs ADD COLUMN IF NOT EXISTS skip_people BOOLEAN NOT NULL DEFAULT FALSE")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_gallery_intelligence_evidence (
                id UUID PRIMARY KEY, job_id UUID NOT NULL UNIQUE REFERENCES vault_gallery_intelligence_jobs(id),
                asset_id UUID NOT NULL REFERENCES vault_assets(id), raw_classification TEXT NOT NULL,
                canonical_assignments JSONB NOT NULL, model_id TEXT NOT NULL, model_revision TEXT NOT NULL,
                task_version TEXT NOT NULL, processing_ms INTEGER NOT NULL,
                 created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_evidence ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'florence'")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_evidence ADD COLUMN IF NOT EXISTS resolver_version TEXT NOT NULL DEFAULT 'legacy-ramp-mapping-v1'")
            cursor.execute("ALTER TABLE vault_gallery_intelligence_evidence ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("""UPDATE vault_gallery_intelligence_jobs AS jobs SET owner_user_id=assets.owner_user_id
                FROM vault_assets AS assets WHERE jobs.owner_user_id IS NULL AND assets.id=jobs.asset_id
                AND assets.owner_user_id IS NOT NULL""")
            cursor.execute("""UPDATE vault_gallery_intelligence_evidence AS evidence SET owner_user_id=jobs.owner_user_id
                FROM vault_gallery_intelligence_jobs AS jobs WHERE evidence.owner_user_id IS NULL AND jobs.id=evidence.job_id
                AND jobs.owner_user_id IS NOT NULL""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_gallery_intelligence_concepts (
                id UUID PRIMARY KEY, slug TEXT NOT NULL UNIQUE, parent_concept_id UUID REFERENCES vault_gallery_intelligence_concepts(id),
                evidence_role TEXT NOT NULL CHECK (evidence_role IN ('direct','supporting','insufficient_alone')),
                active BOOLEAN NOT NULL DEFAULT TRUE, mapping_source TEXT NOT NULL, mapping_version TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_gallery_intelligence_concept_terms (
                concept_id UUID NOT NULL REFERENCES vault_gallery_intelligence_concepts(id), term_id UUID NOT NULL REFERENCES vault_metadata_terms(id),
                active BOOLEAN NOT NULL DEFAULT TRUE, mapping_source TEXT NOT NULL, mapping_version TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(concept_id, term_id))""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_asset_metadata_assignments_asset_idx ON vault_asset_metadata_assignments(asset_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_gallery_intelligence_jobs_status_idx ON vault_gallery_intelligence_jobs(status, created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_gallery_intelligence_jobs_latest_idx ON vault_gallery_intelligence_jobs(asset_id, job_sequence DESC)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_gallery_intelligence_jobs_bulk_run_idx ON vault_gallery_intelligence_jobs(bulk_run_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_gallery_intelligence_jobs_owner_status_idx ON vault_gallery_intelligence_jobs(owner_user_id,status,created_at)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_gallery_reconciliation_results (
                asset_id UUID PRIMARY KEY REFERENCES vault_assets(id), reconciliation_version TEXT NOT NULL,
                canonical_terms JSONB NOT NULL, effective_people JSONB NOT NULL,
                unresolved_person_presence BOOLEAN NOT NULL DEFAULT FALSE,
                evidence JSONB NOT NULL, reconciled_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            for namespace, slug, display in INITIAL_TERMS:
                cursor.execute("""INSERT INTO vault_metadata_terms(id, namespace, slug, display_name) VALUES (%s,%s,%s,%s)
                    ON CONFLICT(namespace,slug) DO UPDATE SET display_name=EXCLUDED.display_name, active=TRUE, updated_at=CURRENT_TIMESTAMP""", (uuid4(), namespace, slug, display))
            for concept in INITIAL_CONCEPTS:
                cursor.execute("""INSERT INTO vault_gallery_intelligence_concepts(id,slug,evidence_role,mapping_source,mapping_version)
                    VALUES(%s,%s,%s,'stage-a-bootstrap',%s) ON CONFLICT(slug) DO NOTHING""",
                    (uuid4(), concept.slug, concept.evidence_role, GALLERY_INTELLIGENCE_RESOLVER_VERSION))
            for concept in INITIAL_CONCEPTS:
                if concept.parent_slug:
                    cursor.execute("""UPDATE vault_gallery_intelligence_concepts child SET parent_concept_id=parent.id,updated_at=CURRENT_TIMESTAMP
                        FROM vault_gallery_intelligence_concepts parent WHERE child.slug=%s AND parent.slug=%s
                        AND child.parent_concept_id IS NULL""", (concept.slug, concept.parent_slug))
            for concept_slug, namespace, term_slug in INITIAL_CONCEPT_TERMS:
                cursor.execute("""INSERT INTO vault_gallery_intelligence_concept_terms(concept_id,term_id,mapping_source,mapping_version)
                    SELECT concepts.id,terms.id,'stage-a-bootstrap',%s FROM vault_gallery_intelligence_concepts concepts
                    JOIN vault_metadata_terms terms ON terms.namespace=%s AND terms.slug=%s WHERE concepts.slug=%s
                    ON CONFLICT(concept_id,term_id) DO NOTHING""",
                    (GALLERY_INTELLIGENCE_RESOLVER_VERSION, namespace, term_slug, concept_slug))

    def queue(
        self, asset_id: UUID, username: str, force: bool = False, bulk_run_id: UUID | None = None, people_only: bool = False, skip_people: bool = False
    ) -> GalleryIntelligenceJob:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_gallery_intelligence_jobs WHERE asset_id=%s ORDER BY job_sequence DESC LIMIT 1", (asset_id,))
            row = cursor.fetchone()
            if not row or force:
                cursor.execute("""INSERT INTO vault_gallery_intelligence_jobs(id,asset_id,requested_by,owner_user_id,status,bulk_run_id,people_only,skip_people)
                    SELECT %s,assets.id,%s,assets.owner_user_id,'queued',%s,%s,%s FROM vault_assets AS assets
                    WHERE assets.id=%s AND assets.owner_user_id IS NOT NULL RETURNING *""", (uuid4(), username, bulk_run_id, people_only, skip_people, asset_id))
                row = cursor.fetchone()
                if row is None:
                    raise ValueError("Gallery Intelligence requires a resolved asset owner")
        return _job_from_row(row)

    def latest_job(self, asset_id: UUID) -> GalleryIntelligenceJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM vault_gallery_intelligence_jobs WHERE asset_id=%s "
                "ORDER BY job_sequence DESC LIMIT 1",
                (asset_id,),
            )
            row = cursor.fetchone()
        return _job_from_row(row) if row else None

    def latest_people_job(self, asset_id: UUID) -> GalleryIntelligenceJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM vault_gallery_intelligence_jobs WHERE asset_id=%s AND NOT skip_people ORDER BY job_sequence DESC LIMIT 1",
                (asset_id,),
            )
            row = cursor.fetchone()
        return _job_from_row(row) if row else None

    def latest_successful_people_job(self, asset_id: UUID) -> GalleryIntelligenceJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM vault_gallery_intelligence_jobs "
                "WHERE asset_id=%s AND people_status='completed' "
                "ORDER BY job_sequence DESC LIMIT 1",
                (asset_id,),
            )
            row = cursor.fetchone()
        return _job_from_row(row) if row else None

    def has_any_job(self, asset_id: UUID) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT EXISTS(SELECT 1 FROM vault_gallery_intelligence_jobs WHERE asset_id=%s) AS exists",
                (asset_id,),
            )
            return bool(cursor.fetchone()["exists"])

    def jobs_for_asset(self, asset_id: UUID) -> list[GalleryIntelligenceJob]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_gallery_intelligence_jobs WHERE asset_id=%s ORDER BY job_sequence", (asset_id,))
            return [_job_from_row(row) for row in cursor.fetchall()]

    def start_bulk_run(self, username: str, owner_user_id: UUID) -> UUID:
        run_id = uuid4()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO vault_gallery_intelligence_bulk_runs(id,requested_by,owner_user_id) VALUES(%s,%s,%s)",
                (run_id, username, owner_user_id),
            )
        return run_id

    def discard_bulk_run(self, run_id: UUID) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM vault_gallery_intelligence_bulk_runs WHERE id=%s", (run_id,))

    def latest_bulk_run(self, owner_user_id: UUID) -> GalleryIntelligenceBulkRunProgress | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT runs.id,
                COUNT(jobs.id) AS total,
                COUNT(jobs.id) FILTER (
                    WHERE jobs.status='completed'
                      AND (jobs.skip_people OR jobs.people_status <> 'failed')
                ) AS completed,
                COUNT(jobs.id) FILTER (WHERE jobs.status='processing') AS processing,
                COUNT(jobs.id) FILTER (WHERE jobs.status='queued') AS queued,
                COUNT(jobs.id) FILTER (
                    WHERE jobs.status='failed'
                       OR (jobs.status='completed' AND NOT jobs.skip_people AND jobs.people_status='failed')
                ) AS failed,
                MAX(jobs.completed_at) AS settled_at
                FROM vault_gallery_intelligence_bulk_runs runs
                LEFT JOIN vault_gallery_intelligence_jobs jobs ON jobs.bulk_run_id=runs.id
                WHERE runs.owner_user_id=%s GROUP BY runs.id,runs.created_at
                ORDER BY runs.created_at DESC LIMIT 1""", (owner_user_id,))
            row = cursor.fetchone()
        if not row:
            return None
        settled_at = row["settled_at"]
        if int(row["queued"]) + int(row["processing"]) == 0 and (
            settled_at is None
            or datetime.now(timezone.utc) - settled_at > BULK_COMPLETION_GRACE
        ):
            return None
        return GalleryIntelligenceBulkRunProgress(
            UUID(str(row["id"])), *(int(row[key]) for key in ("total", "completed", "processing", "queued", "failed"))
        )

    def claim_next_job(self) -> GalleryIntelligenceJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""WITH next AS (SELECT id FROM vault_gallery_intelligence_jobs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                UPDATE vault_gallery_intelligence_jobs j SET status='processing', attempts=attempts+1, started_at=CURRENT_TIMESTAMP WHERE j.id IN (SELECT id FROM next) RETURNING j.*""")
            row = cursor.fetchone()
        return _job_from_row(row) if row else None

    def complete(
        self,
        job_id: UUID,
        terms: tuple[tuple[str, str], ...],
        confidence: float | None,
        evidence: GalleryIntelligenceClassification | None = None,
    ) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT asset_id FROM vault_gallery_intelligence_jobs WHERE id=%s", (job_id,)); row = cursor.fetchone()
            if not row: return
            model_id = evidence.model_id if evidence else AI_MODEL_ID
            model_revision = evidence.model_revision if evidence else AI_MODEL_REVISION
            task_version = evidence.task_version if evidence else GALLERY_INTELLIGENCE_TASK_VERSION
            for namespace, slug in terms:
                cursor.execute("SELECT id FROM vault_metadata_terms WHERE namespace=%s AND slug=%s", (namespace, slug)); term = cursor.fetchone()
                if term:
                    cursor.execute("""INSERT INTO vault_asset_metadata_assignments(id,asset_id,term_id,source,confidence,model_id,model_revision,task_version)
                        VALUES(%s,%s,%s,'vault_master',%s,%s,%s,%s) ON CONFLICT(asset_id,term_id,source,model_id,model_revision,task_version) DO UPDATE SET confidence=EXCLUDED.confidence,updated_at=CURRENT_TIMESTAMP""", (uuid4(), row["asset_id"], term["id"], confidence, model_id, model_revision, task_version))
            if evidence:
                cursor.execute("""INSERT INTO vault_gallery_intelligence_evidence(
                    id,job_id,asset_id,raw_classification,canonical_assignments,model_id,model_revision,task_version,processing_ms,provider,resolver_version,owner_user_id)
                    VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,(SELECT owner_user_id FROM vault_gallery_intelligence_jobs WHERE id=%s))
                    ON CONFLICT(job_id) DO UPDATE SET raw_classification=EXCLUDED.raw_classification,
                    canonical_assignments=EXCLUDED.canonical_assignments,model_id=EXCLUDED.model_id,
                    model_revision=EXCLUDED.model_revision,task_version=EXCLUDED.task_version,
                     processing_ms=EXCLUDED.processing_ms,provider=EXCLUDED.provider,resolver_version=EXCLUDED.resolver_version""", (
                    uuid4(), job_id, row["asset_id"], evidence.raw_response,
                    json.dumps([{"namespace": namespace, "slug": slug} for namespace, slug in terms]),
                    model_id, model_revision, task_version, evidence.processing_ms, evidence.provider, GALLERY_INTELLIGENCE_RESOLVER_VERSION, job_id,
                ))
            cursor.execute("UPDATE vault_gallery_intelligence_jobs SET status='completed',completed_at=CURRENT_TIMESTAMP WHERE id=%s", (job_id,))

    def persist_canonical_assignments(
        self, asset_id: UUID, terms: tuple[tuple[str, str], ...], *,
        model_id: str, model_revision: str | None, task_version: str,
    ) -> None:
        """Persist resolver output without fabricating a Gallery job/evidence row."""
        with self._connect() as connection, connection.cursor() as cursor:
            for namespace, slug in terms:
                cursor.execute(
                    "SELECT id FROM vault_metadata_terms WHERE namespace=%s AND slug=%s AND active",
                    (namespace, slug),
                )
                term = cursor.fetchone()
                if term:
                    cursor.execute(
                        """INSERT INTO vault_asset_metadata_assignments(
                            id,asset_id,term_id,source,confidence,model_id,model_revision,task_version
                        ) VALUES(%s,%s,%s,'vault_master',NULL,%s,%s,%s)
                        ON CONFLICT(asset_id,term_id,source,model_id,model_revision,task_version)
                        DO UPDATE SET updated_at=CURRENT_TIMESTAMP""",
                        (uuid4(), asset_id, term["id"], model_id, model_revision, task_version),
                    )

    def fail(self, job_id: UUID, error: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_gallery_intelligence_jobs SET status='failed',error=%s,completed_at=CURRENT_TIMESTAMP WHERE id=%s", (error, job_id))

    def save_reconciliation(self, result) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO vault_gallery_reconciliation_results(asset_id,reconciliation_version,canonical_terms,effective_people,unresolved_person_presence,evidence)
                VALUES(%s,%s,%s::jsonb,%s::jsonb,%s,%s::jsonb)
                ON CONFLICT(asset_id) DO UPDATE SET reconciliation_version=EXCLUDED.reconciliation_version,
                canonical_terms=EXCLUDED.canonical_terms,effective_people=EXCLUDED.effective_people,
                unresolved_person_presence=EXCLUDED.unresolved_person_presence,evidence=EXCLUDED.evidence,reconciled_at=CURRENT_TIMESTAMP""",
                (result.asset_id, result.version, json.dumps(result.terms), json.dumps([str(value) for value in result.people_ids]), result.unresolved_person_presence, json.dumps(result.evidence)))

    def reconciliation(self, asset_id: UUID):
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT unresolved_person_presence,reconciliation_version,reconciled_at FROM vault_gallery_reconciliation_results WHERE asset_id=%s", (asset_id,))
            return cursor.fetchone()

    def latest_evidence(self, asset_id: UUID) -> GalleryIntelligenceEvidence | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT evidence.* FROM vault_gallery_intelligence_evidence AS evidence
                JOIN vault_gallery_intelligence_jobs AS jobs ON jobs.id=evidence.job_id
                WHERE evidence.asset_id=%s ORDER BY jobs.job_sequence DESC LIMIT 1""", (asset_id,))
            row = cursor.fetchone()
        if row is None:
            return None
        assignments = tuple(tuple(value) for value in row["canonical_assignments"])
        return GalleryIntelligenceEvidence(
            UUID(str(row["job_id"])), UUID(str(row["asset_id"])), str(row["raw_classification"]),
            assignments, str(row["model_id"]), str(row["model_revision"]),
            str(row["task_version"]), int(row["processing_ms"]), str(row["provider"]),
            str(row["resolver_version"]),
        )

    def mark_people_status(self, job_id: UUID, status: str, error: str | None = None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_gallery_intelligence_jobs SET people_status=%s,people_error=%s WHERE id=%s", (status, error, job_id))

    def decide(self, asset_id: UUID, namespace: str, slug: str, decision: Decision, username: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM vault_metadata_terms WHERE namespace=%s AND slug=%s", (namespace, slug)); term = cursor.fetchone()
            if not term: raise ValueError("Unknown Gallery Intelligence term")
            cursor.execute("""INSERT INTO vault_asset_metadata_decisions(id,asset_id,term_id,decision,decided_by) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(asset_id,term_id) DO UPDATE SET decision=EXCLUDED.decision,decided_by=EXCLUDED.decided_by,active=TRUE,updated_at=CURRENT_TIMESTAMP""", (uuid4(), asset_id, term["id"], decision, username))

    def effective(self, asset_id: UUID) -> list[dict[str, object]]:
        """Return published terms after the persistent user include/exclude layer."""
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT DISTINCT ON (terms.namespace, terms.slug)
                terms.namespace, terms.slug, terms.display_name,
                COALESCE(decisions.decision, assignments.source) AS effective_source
                FROM vault_metadata_terms terms
                LEFT JOIN vault_asset_metadata_assignments assignments
                  ON assignments.term_id=terms.id AND assignments.asset_id=%s
                LEFT JOIN vault_asset_metadata_decisions decisions
                  ON decisions.term_id=terms.id AND decisions.asset_id=%s AND decisions.active
                WHERE (assignments.id IS NOT NULL OR decisions.decision='include')
                  AND COALESCE(decisions.decision, '') <> 'exclude'
                ORDER BY terms.namespace, terms.slug, assignments.created_at DESC NULLS LAST""", (asset_id, asset_id))
            return [dict(row) for row in cursor.fetchall()]

    def list_terms(self) -> list[dict[str, object]]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT namespace,slug,display_name FROM vault_metadata_terms WHERE active ORDER BY namespace,display_name")
            return [dict(row) for row in cursor.fetchall()]

    def matching_asset_ids(
        self, photo_types: tuple[str, ...], content_tags: tuple[str, ...]
    ) -> set[UUID]:
        conditions: list[str] = []
        values: list[object] = []
        for namespace, slugs in (("photo_type", photo_types), ("content_tag", content_tags)):
            if not slugs:
                continue
            conditions.append(
                """EXISTS (
                    SELECT 1 FROM vault_metadata_terms terms
                    WHERE terms.namespace=%s AND terms.slug = ANY(%s)
                      AND (
                        EXISTS (SELECT 1 FROM vault_asset_metadata_decisions included
                                WHERE included.asset_id=assets.id AND included.term_id=terms.id
                                  AND included.active AND included.decision='include')
                        OR (
                            EXISTS (SELECT 1 FROM vault_asset_metadata_assignments assignments
                                    WHERE assignments.asset_id=assets.id AND assignments.term_id=terms.id)
                            AND NOT EXISTS (SELECT 1 FROM vault_asset_metadata_decisions excluded
                                            WHERE excluded.asset_id=assets.id AND excluded.term_id=terms.id
                                              AND excluded.active AND excluded.decision='exclude')
                        )
                      )
                )"""
            )
            values.extend((namespace, list(slugs)))
        if not conditions:
            return set()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT assets.id FROM vault_assets assets WHERE " + " AND ".join(conditions),
                values,
            )
            return {UUID(str(row["id"])) for row in cursor.fetchall()}

    def job_counts(self) -> dict[str, int]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT status,COUNT(*) AS count FROM vault_gallery_intelligence_jobs GROUP BY status")
            return {str(row["status"]): int(row["count"]) for row in cursor.fetchall()}

    def resolve_raw_tags(self, raw_tags: object) -> tuple[tuple[str, str], ...]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT child.slug, parent.slug AS parent_slug, child.evidence_role, child.active
                FROM vault_gallery_intelligence_concepts child
                LEFT JOIN vault_gallery_intelligence_concepts parent ON parent.id=child.parent_concept_id""")
            concepts = tuple(
                GalleryConcept(
                    str(row["slug"]), str(row["parent_slug"]) if row["parent_slug"] else None,
                    str(row["evidence_role"]), bool(row["active"]),
                )
                for row in cursor.fetchall()
            )
            cursor.execute("""SELECT concepts.slug AS concept_slug,terms.namespace,terms.slug AS term_slug
                FROM vault_gallery_intelligence_concept_terms mappings
                JOIN vault_gallery_intelligence_concepts concepts ON concepts.id=mappings.concept_id
                JOIN vault_metadata_terms terms ON terms.id=mappings.term_id
                WHERE mappings.active AND terms.active""")
            concept_terms = tuple(
                (str(row["concept_slug"]), str(row["namespace"]), str(row["term_slug"]))
                for row in cursor.fetchall()
            )
        return resolve_gallery_concepts(raw_tags, concepts, concept_terms)


def _job_from_row(row: dict[str, object]) -> GalleryIntelligenceJob:
    return GalleryIntelligenceJob(UUID(str(row["id"])), UUID(str(row["asset_id"])), str(row["requested_by"]), UUID(str(row["owner_user_id"])) if row.get("owner_user_id") else None, str(row["status"]), int(row["attempts"]), str(row["error"]) if row["error"] else None, row["created_at"], row.get("started_at"), row.get("completed_at"), UUID(str(row["bulk_run_id"])) if row.get("bulk_run_id") else None, str(row.get("people_status") or "pending"), str(row["people_error"]) if row.get("people_error") else None, bool(row.get("people_only")), bool(row.get("skip_people")))  # type: ignore[arg-type]


def _gallery_path_for_asset(asset: CataloguedAsset) -> Path | None:
    prefix = "/vault/Gallery/"
    if not asset.vault_path.startswith(prefix): return None
    return Path(os.getenv("PV_GALLERY_PATH", "/media/gallery")) / asset.vault_path.removeprefix(prefix)


def request_rampp_tags(source: Path) -> GalleryIntelligenceClassification:
    endpoint = os.getenv("PV_RAMPP_URL", "http://pv-rampp:8080/tag")
    request = Request(
        endpoint,
        data=source.read_bytes(),
        method="POST",
        headers={"Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream"},
    )
    try:
        with urlopen(request, timeout=180) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Local RAM++ Gallery tagging service is unavailable") from error
    tags = payload.get("tags")
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise RuntimeError("Local RAM++ service returned invalid Gallery tags")
    return GalleryIntelligenceClassification(
        raw_response=json.dumps(tags),
        model_id=str(payload.get("model") or "ram_plus_swin_large_14m"),
        model_revision=str(payload.get("checkpoint_sha256") or "unknown"),
        task_version=str(payload.get("task_version") or GALLERY_INTELLIGENCE_TASK_VERSION),
        processing_ms=int(payload.get("processing_ms")) if isinstance(payload.get("processing_ms"), int) else 0,
    )


def process_next_gallery_intelligence_job(
    store,
    vault_store: VaultMasterStore,
    people_store=None,
    ingestion_ai_store=None,
) -> UUID | None:
    job = store.claim_next_job()
    if job is None: return None
    try:
        asset = vault_store.get_catalogued_asset_by_id(job.asset_id)
        source = _gallery_path_for_asset(asset) if asset else None
        if source is None or not source.is_file(): raise ValueError("Published Gallery source is unavailable")
        terms: tuple[tuple[str, str], ...] = ()
        raw_tags: object = []
        classification = None
        if not job.people_only:
            classification = request_rampp_tags(source)
            raw_tags = json.loads(classification.raw_response)
            terms = store.resolve_raw_tags(raw_tags)
        else:
            terms = tuple((term.namespace, term.slug) for term in store.effective(asset.id))
            retained_rampp = store.latest_evidence(asset.id)
            if retained_rampp is not None:
                raw_tags = json.loads(retained_rampp.raw_classification)
        # Stage B is a second, failure-isolated post-publication evidence
        # pass.  RAM++ metadata remains published even if local recognition is
        # unavailable; neither result can affect routing.
        persistence = people_store
        try:
            from app.gallery_people import get_gallery_people_store

            persistence = persistence or get_gallery_people_store()
            if not job.skip_people:
                from app.gallery_people_worker import persist_people_evidence, request_people_analysis

                owner = asset.owner_user_id if asset.owner_user_id is not None else asset.owner_username
                references = (persistence.reference_embeddings_by_user_id(owner)
                              if isinstance(owner, UUID) else persistence.reference_embeddings(owner))
                evidence = request_people_analysis(source, references)
                persist_people_evidence(persistence, asset.id, owner, evidence, job.id)
                store.mark_people_status(job.id, "completed")
        except Exception as error:
            # The primary GI job has already completed.  The specialist error
            # must not withdraw Gallery metadata, alter availability, or
            # create an Arrival Hall/routing consequence.
            store.mark_people_status(job.id, "failed", str(error))
        from app.gallery_reconciliation import (
            latest_retained_florence_visual_evidence,
            reconcile_gallery_evidence,
        )
        people = []
        faces = []
        body_count = 0
        if persistence is not None:
            try:
                owner = asset.owner_user_id if asset.owner_user_id is not None else asset.owner_username
                people = persistence.effective_people(asset.id, owner)
                current_people_job = store.latest_successful_people_job(asset.id)
                if current_people_job is not None:
                    faces = persistence.face_detections_for_asset(
                        asset.id,
                        owner,
                        current_people_job.id,
                        current_people_job.started_at,
                    )
                    body_count = persistence.person_detection_count(
                        asset.id, current_people_job.id
                    )
            except Exception:
                pass
        florence_evidence = None
        florence_error = None
        try:
            florence_evidence = latest_retained_florence_visual_evidence(
                vault_store, ingestion_ai_store, asset
            )
        except Exception as error:
            # A retained-description lookup is independent evidence.  Its
            # failure remains visible in reconciliation provenance but does not
            # discard successful RAM++ or People evidence.
            florence_error = str(error)
        reconciliation = reconcile_gallery_evidence(
            asset.id, terms, (value.person_id for value in people), raw_tags, len(faces),
            body_count, florence_evidence, florence_error,
        )
        store.save_reconciliation(reconciliation)
        store.complete(job.id, reconciliation.terms, None, classification)
    except Exception as error:
        store.fail(job.id, str(error))
    return job.id


def queue_published_gallery_assets(
    store,
    vault_store: VaultMasterStore,
    username: str,
    owner_user_id: UUID | None = None,
    limit: int = 50,
    *,
    force: bool = False,
    bulk_run_id: UUID | None = None,
) -> int:
    """Queue bounded published-Gallery enrichment without touching files.

    Normal backfill advances only through Gallery assets with no GI-job
    history. ``force`` is reserved for explicit owner/admin reanalysis.
    """
    queued = 0
    assets = (vault_store.list_owned_catalogued_assets_by_user_id(owner_user_id)
              if owner_user_id is not None else vault_store.list_owned_catalogued_assets(username))
    for asset in assets:
        if queued >= limit: break
        if asset.asset_type != "Gallery" or not asset.vault_path.startswith("/vault/Gallery/"):
            continue
        if not force and store.has_any_job(asset.id):
            continue
        store.queue(asset.id, username, force=force, bulk_run_id=bulk_run_id)
        queued += 1
    return queued


def queue_gallery_analysis_catchup(
    store,
    vault_store: VaultMasterStore,
    username: str,
    owner_user_id: UUID | None = None,
    limit: int = 50,
    *,
    bulk_run_id: UUID | None = None,
) -> int:
    """Queue only missing enabled post-publication stages for existing Gallery assets.

    A failed or active attempt is historical state, not permission to silently
    retry it.  One initially-unanalysed asset receives one combined job; mixed
    states receive a stage-specific job only for the missing stage.
    """
    queued = 0
    for asset, queue_options in gallery_analysis_catchup_candidates(
        store, vault_store, username, owner_user_id
    )[:limit]:
        store.queue(asset.id, username, force=True, bulk_run_id=bulk_run_id, **queue_options)
        queued += 1
    return queued


def gallery_analysis_catchup_candidates(
    store,
    vault_store: VaultMasterStore,
    username: str,
    owner_user_id: UUID | None = None,
) -> list[tuple[object, dict[str, bool]]]:
    """Return the authenticated owner's historical Gallery stages still eligible for catch-up.

    This is intentionally the same decision boundary used by the explicit
    backfill action, so a status response cannot advertise another owner's
    assets or a count that the action cannot actually queue.
    """
    candidates: list[tuple[object, dict[str, bool]]] = []
    assets = (vault_store.list_owned_catalogued_assets_by_user_id(owner_user_id)
              if owner_user_id is not None else vault_store.list_owned_catalogued_assets(username))
    for asset in sorted(assets, key=lambda item: (item.filename.casefold(), str(item.id))):
        if asset.asset_type != "Gallery" or not asset.vault_path.startswith("/vault/Gallery/"):
            continue
        jobs = [job for job in (store.jobs.values() if hasattr(store, "jobs") else ()) if job.asset_id == asset.id]
        if not jobs and not hasattr(store, "jobs"):
            # Postgres store supplies its persisted history method below.
            jobs = store.jobs_for_asset(asset.id)
        stage_a = [job for job in jobs if not job.people_only]
        people = [job for job in jobs if job.people_only or job.people_status != "pending"]
        stage_a_complete = any(job.status == "completed" for job in stage_a)
        people_complete = any(job.people_status == "completed" for job in people)
        stage_a_seen = bool(stage_a)
        people_seen = bool(people) or any(job.status == "failed" for job in stage_a)
        if not stage_a_seen and not people_seen:
            candidates.append((asset, {}))
        elif stage_a_complete and not people_seen:
            candidates.append((asset, {"people_only": True}))
        elif people_complete and not stage_a_seen:
            candidates.append((asset, {"skip_people": True}))
    return candidates


@lru_cache
def get_gallery_intelligence_store():
    return PostgresGalleryIntelligenceStore(get_database_conninfo())
