"""Owner-reviewed provisional Reading Room records for Arrival Hall sources."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
import html
import json
import os
import threading
from typing import Protocol
from uuid import UUID, uuid5, NAMESPACE_URL

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_database_conninfo
from app.vault_master_reading import (
    BLOCK_TYPES,
    PUBLICATION_TYPES,
    PublicationBlock,
    PublicationIssue,
    PublicationMetadata,
    PublicationSnapshot,
    publication_sidecar_document,
    publication_snapshot_from_document,
)
from app.vault_master_reading_extraction import build_structured_html


REVIEW_STATES = frozenset({"needs_review", "deferred", "rejected", "ready_to_publish", "published"})
EDITABLE_METADATA = frozenset({"author", "title", "publisher", "isbn", "edition"})
REVIEW_SCHEMA = "personal-vault.publication-review"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class PublicationReview:
    source_item_id: UUID
    owner_username: str
    state: str
    snapshot: PublicationSnapshot
    revision: int = 1
    updated_at: datetime = field(default_factory=_now)
    updated_by: str = "Vault Master"

    def __post_init__(self) -> None:
        if self.state not in REVIEW_STATES:
            raise ValueError("Unsupported publication review state")
        if self.snapshot.metadata.asset_id != self.source_item_id:
            raise ValueError("Publication review identity does not match its source")
        if not self.owner_username.strip() or not self.updated_by.strip():
            raise ValueError("Publication review ownership is required")
        if self.revision < 1 or self.updated_at.tzinfo is None:
            raise ValueError("Publication review revision or timestamp is invalid")


def review_document(review: PublicationReview) -> dict[str, object]:
    return {
        "schema": REVIEW_SCHEMA,
        "version": 1,
        "source_item_id": str(review.source_item_id),
        "owner_username": review.owner_username,
        "state": review.state,
        "revision": review.revision,
        "updated_at": review.updated_at.isoformat(),
        "updated_by": review.updated_by,
        "snapshot": publication_sidecar_document(review.snapshot),
    }


def review_from_document(value: dict[str, object]) -> PublicationReview:
    if value.get("schema") != REVIEW_SCHEMA or value.get("version") != 1:
        raise ValueError("Publication review schema is unsupported")
    snapshot_value = value.get("snapshot")
    if not isinstance(snapshot_value, dict):
        raise ValueError("Publication review snapshot is invalid")
    return PublicationReview(
        source_item_id=UUID(str(value["source_item_id"])),
        owner_username=str(value["owner_username"]),
        state=str(value["state"]),
        snapshot=publication_snapshot_from_document(snapshot_value),
        revision=int(value["revision"]),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
        updated_by=str(value["updated_by"]),
    )


def _revised(review: PublicationReview, username: str, **changes: object) -> PublicationReview:
    if review.state in {"rejected", "published"}:
        raise ValueError("This publication review can no longer be edited")
    return replace(review, revision=review.revision + 1, updated_at=_now(), updated_by=username, **changes)


def correct_review_metadata(
    review: PublicationReview,
    username: str,
    *,
    values: dict[str, str],
    language: str | None = None,
    publication_type: str | None = None,
) -> PublicationReview:
    if not values.keys() <= EDITABLE_METADATA:
        raise ValueError("Publication metadata correction contains unsupported fields")
    cleaned = {key: " ".join(value.split()) for key, value in values.items()}
    if any(not value or len(value) > 240 for value in cleaned.values()):
        raise ValueError("Publication metadata corrections must be 1 to 240 characters")
    metadata = review.snapshot.metadata
    updated = replace(
        metadata,
        language=language if language is not None else metadata.language,
        publication_type=publication_type or metadata.publication_type,
        user_overrides={**metadata.user_overrides, **cleaned},
        extraction_state="needs_review",
        updated_at=_now(),
    )
    if updated.publication_type not in PUBLICATION_TYPES:
        raise ValueError("Unsupported publication type")
    snapshot = PublicationSnapshot(updated, review.snapshot.files, review.snapshot.blocks, review.snapshot.issues)
    return _revised(review, username, snapshot=snapshot, state="needs_review")


def correct_review_block(
    review: PublicationReview,
    username: str,
    block_id: UUID,
    *,
    text: str,
    block_type: str | None = None,
) -> PublicationReview:
    cleaned = "\n".join(line.rstrip() for line in text.replace("\x00", "").splitlines()).strip()
    if not cleaned or len(cleaned) > 100_000:
        raise ValueError("Corrected publication text is invalid")
    blocks: list[PublicationBlock] = []
    found = False
    for block in review.snapshot.blocks:
        if block.id != block_id:
            blocks.append(block)
            continue
        found = True
        kind = block_type or block.block_type
        if kind not in BLOCK_TYPES or kind in {"illustration", "page_marker"}:
            raise ValueError("This publication block type cannot contain corrected text")
        tag = "h2" if kind in {"part", "chapter"} else "h3" if kind == "heading" else "p"
        blocks.append(replace(block, block_type=kind, content_text=cleaned, content_html=f"<{tag}>{html.escape(cleaned)}</{tag}>", metadata={**block.metadata, "corrected_by": username}))
    if not found:
        raise ValueError("Publication block was not found")
    snapshot = PublicationSnapshot(review.snapshot.metadata, review.snapshot.files, tuple(blocks), review.snapshot.issues)
    return _revised(review, username, snapshot=snapshot, state="needs_review")


def correct_page_order(
    review: PublicationReview,
    username: str,
    page_order: tuple[int, ...],
    rotations: dict[int, int],
) -> PublicationReview:
    page_count = int(review.snapshot.metadata.detected.get("page_count", 0))
    if page_count < 1 or sorted(page_order) != list(range(1, page_count + 1)):
        raise ValueError("Page order must contain every source page exactly once")
    if any(page < 1 or page > page_count or rotation not in {0, 90, 180, 270} for page, rotation in rotations.items()):
        raise ValueError("Page rotation correction is invalid")
    metadata = replace(
        review.snapshot.metadata,
        user_overrides={**review.snapshot.metadata.user_overrides, "page_order": list(page_order), "page_rotations": {str(key): value for key, value in rotations.items()}},
        updated_at=_now(),
    )
    return _revised(review, username, snapshot=PublicationSnapshot(metadata, review.snapshot.files, review.snapshot.blocks, review.snapshot.issues), state="needs_review")


def review_publication_issue(
    review: PublicationReview,
    username: str,
    issue_id: UUID,
    state: str,
) -> PublicationReview:
    if state not in {"accepted", "resolved", "rejected"}:
        raise ValueError("Publication issue review state is invalid")
    issues: list[PublicationIssue] = []
    found = False
    for issue in review.snapshot.issues:
        if issue.id != issue_id:
            issues.append(issue)
            continue
        found = True
        issues.append(replace(issue, state=state, resolved_at=_now(), resolved_by=username))
    if not found:
        raise ValueError("Publication issue was not found")
    snapshot = PublicationSnapshot(review.snapshot.metadata, review.snapshot.files, review.snapshot.blocks, tuple(issues))
    return _revised(review, username, snapshot=snapshot, state="needs_review")


def caption_illustration(review: PublicationReview, username: str, block_id: UUID, caption: str) -> PublicationReview:
    cleaned = " ".join(caption.split())
    if not cleaned or len(cleaned) > 1000:
        raise ValueError("Illustration caption is invalid")
    blocks = list(review.snapshot.blocks)
    illustration = next((block for block in blocks if block.id == block_id and block.block_type == "illustration"), None)
    if illustration is None:
        raise ValueError("Illustration block was not found")
    caption_id = uuid5(NAMESPACE_URL, f"personal-vault:caption:{block_id}")
    caption_block = PublicationBlock(caption_id, review.source_item_id, "caption", illustration.ordinal + 1, f"{illustration.locator}/caption", parent_id=illustration.id, content_text=cleaned, content_html=f"<p>{html.escape(cleaned)}</p>", source_page=illustration.source_page, metadata={"corrected_by": username})
    blocks = [block for block in blocks if block.id != caption_id]
    blocks.append(caption_block)
    blocks.sort(key=lambda block: (block.ordinal, 0 if block.id == illustration.id else 1, block.locator))
    snapshot = PublicationSnapshot(review.snapshot.metadata, review.snapshot.files, tuple(blocks), review.snapshot.issues)
    return _revised(review, username, snapshot=snapshot, state="needs_review")


def transition_review(review: PublicationReview, username: str, action: str) -> PublicationReview:
    if action == "retry":
        return _revised(review, username, state="needs_review")
    if action in {"defer", "reject"}:
        return _revised(review, username, state="deferred" if action == "defer" else "rejected")
    if action != "publish":
        raise ValueError("Publication review action is invalid")
    blocking = [issue for issue in review.snapshot.issues if issue.severity == "critical" and issue.state in {"open", "accepted"}]
    if review.snapshot.metadata.reading_mode != "reflowable":
        raise ValueError("Only reviewed reflowable publications can be published")
    if blocking:
        raise ValueError("Critical publication issues must be resolved before publishing")
    return _revised(review, username, state="ready_to_publish")


def write_reviewed_html(review: PublicationReview, destination: os.PathLike[str]) -> None:
    path = os.fspath(destination)
    temporary = f"{path}.{os.getpid()}.part"
    try:
        with open(temporary, "x", encoding="utf-8", newline="\n") as handle:
            handle.write(build_structured_html(review.snapshot.blocks, review.snapshot.metadata.language))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class PublicationReviewStore(Protocol):
    def save(self, review: PublicationReview) -> PublicationReview: ...
    def get(self, source_item_id: UUID, owner_username: str) -> PublicationReview | None: ...


class MemoryPublicationReviewStore:
    def __init__(self) -> None:
        self.records: dict[UUID, PublicationReview] = {}
        self._lock = threading.Lock()

    def save(self, review: PublicationReview) -> PublicationReview:
        with self._lock:
            current = self.records.get(review.source_item_id)
            if current and review.revision <= current.revision:
                raise ValueError("Publication review revision is stale")
            self.records[review.source_item_id] = review
        return review

    def get(self, source_item_id: UUID, owner_username: str) -> PublicationReview | None:
        review = self.records.get(source_item_id)
        return review if review and review.owner_username.casefold() == owner_username.casefold() else None


POSTGRES_REVIEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_publication_reviews (
    source_item_id UUID PRIMARY KEY REFERENCES vault_master_items(id) ON DELETE CASCADE,
    owner_username TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('needs_review','deferred','rejected','ready_to_publish','published')),
    revision INTEGER NOT NULL CHECK (revision > 0),
    document JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS vault_publication_reviews_owner_idx
ON vault_publication_reviews(owner_username, state, updated_at DESC);
"""


class PostgresPublicationReviewStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        """Run the additive review schema bootstrap from controlled startup only."""
        with self._connect() as connection:
            connection.execute(POSTGRES_REVIEW_SCHEMA)

    def save(self, review: PublicationReview) -> PublicationReview:
        with self._connect() as connection:
            row = connection.execute(
                """INSERT INTO vault_publication_reviews(source_item_id,owner_username,state,revision,document,updated_at,updated_by)
                VALUES(%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(source_item_id) DO UPDATE SET owner_username=EXCLUDED.owner_username,state=EXCLUDED.state,
                revision=EXCLUDED.revision,document=EXCLUDED.document,updated_at=EXCLUDED.updated_at,updated_by=EXCLUDED.updated_by
                WHERE vault_publication_reviews.revision < EXCLUDED.revision RETURNING source_item_id""",
                (review.source_item_id, review.owner_username.casefold(), review.state, review.revision, Jsonb(review_document(review)), review.updated_at, review.updated_by),
            ).fetchone()
        if row is None:
            raise ValueError("Publication review revision is stale")
        return review

    def get(self, source_item_id: UUID, owner_username: str) -> PublicationReview | None:
        with self._connect() as connection:
            row = connection.execute("SELECT document FROM vault_publication_reviews WHERE source_item_id=%s AND owner_username=%s", (source_item_id, owner_username.casefold())).fetchone()
        return review_from_document(dict(row["document"])) if row else None


@lru_cache(maxsize=1)
def get_publication_review_store() -> PostgresPublicationReviewStore:
    return PostgresPublicationReviewStore(get_database_conninfo())
