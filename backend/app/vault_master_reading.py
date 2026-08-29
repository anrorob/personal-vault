from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import lru_cache
import json
import os
from pathlib import Path, PurePosixPath
import re
import threading
import unicodedata
from typing import Callable, Protocol
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import get_database_conninfo, get_metadata_storage_root


PUBLICATION_SIDECAR_SCHEMA = "personal-vault.publication"
PUBLICATION_SIDECAR_VERSION = 1
PUBLICATION_SIDECAR_DIRECTORY = "publication-sidecars"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

PUBLICATION_TYPES = frozenset({"book", "magazine", "comic", "journal", "other"})
READING_MODES = frozenset({"reflowable", "fixed_layout", "hybrid"})
EXTRACTION_STATES = frozenset({"pending", "processing", "needs_review", "approved", "failed"})
FILE_ROLES = frozenset({
    "source_pdf", "front_cover", "back_cover", "reading_content",
    "illustration", "page_evidence", "sidecar",
})
BLOCK_TYPES = frozenset({
    "part", "chapter", "heading", "paragraph", "footnote", "illustration",
    "caption", "page_marker", "table", "other",
})
ISSUE_TYPES = frozenset({
    "unreadable_passage", "missing_page", "duplicated_page",
    "uncertain_character", "incorrect_rotation", "likely_ocr_mistake",
    "uncertain_structure", "uncertain_reading_order", "uncertain_image_placement",
})
ISSUE_SEVERITIES = frozenset({"advisory", "warning", "critical"})
ISSUE_STATES = frozenset({"open", "accepted", "resolved", "rejected"})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return dict(value)


def _json_safe(value: object, label: str) -> object:
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be JSON serializable") from error
    return value


def _vault_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or len(path.parts) < 3 or path.parts[1] != "vault" or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute /vault path")
    return str(path)


def _locator(value: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 240 or not re.fullmatch(r"[A-Za-z0-9._:/-]+", cleaned):
        raise ValueError("Publication locators must be stable ASCII identifiers")
    return cleaned


@dataclass(frozen=True)
class PublicationMetadata:
    asset_id: UUID
    publication_type: str
    reading_mode: str
    extraction_state: str
    language: str | None = None
    content_version: str | None = None
    detected: dict[str, object] = field(default_factory=dict)
    imported: dict[str, object] = field(default_factory=dict)
    user_overrides: dict[str, object] = field(default_factory=dict)
    effective: dict[str, object] = field(default_factory=dict)
    provenance: dict[str, object] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if self.publication_type not in PUBLICATION_TYPES:
            raise ValueError("Unsupported publication type")
        if self.reading_mode not in READING_MODES:
            raise ValueError("Unsupported publication reading mode")
        if self.extraction_state not in EXTRACTION_STATES:
            raise ValueError("Unsupported publication extraction state")
        if self.language is not None and not re.fullmatch(r"[a-z]{2,3}(?:-[A-Z]{2})?", self.language):
            raise ValueError("Publication language must be a BCP-47 language tag")
        if self.updated_at.tzinfo is None:
            raise ValueError("Publication timestamps must be timezone-aware")
        for label in ("detected", "imported", "user_overrides", "effective", "provenance"):
            _json_safe(getattr(self, label), f"publication.{label}")


@dataclass(frozen=True)
class PublicationFile:
    id: UUID
    asset_id: UUID
    role: str
    vault_path: str
    filename: str
    mime_type: str
    sha256: str
    original: bool
    ordinal: int = 0

    def __post_init__(self) -> None:
        if self.role not in FILE_ROLES:
            raise ValueError("Unsupported publication file role")
        path = _vault_path(self.vault_path, "Publication file path")
        if PurePosixPath(path).name != self.filename:
            raise ValueError("Publication filename must match its Vault path")
        if SHA256_PATTERN.fullmatch(self.sha256) is None:
            raise ValueError("Publication file checksum is invalid")
        if self.ordinal < 0:
            raise ValueError("Publication file ordinal cannot be negative")


@dataclass(frozen=True)
class PublicationBlock:
    id: UUID
    asset_id: UUID
    block_type: str
    ordinal: int
    locator: str
    parent_id: UUID | None = None
    content_html: str | None = None
    content_text: str | None = None
    source_page: int | None = None
    source_bbox: tuple[float, float, float, float] | None = None
    illustration_file_id: UUID | None = None
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.block_type not in BLOCK_TYPES:
            raise ValueError("Unsupported publication block type")
        if self.ordinal < 0:
            raise ValueError("Publication block ordinal cannot be negative")
        _locator(self.locator)
        if self.source_page is not None and self.source_page < 1:
            raise ValueError("Publication source page must be positive")
        if self.source_bbox is not None and (len(self.source_bbox) != 4 or any(value < 0 for value in self.source_bbox)):
            raise ValueError("Publication source bounds are invalid")
        _json_safe(self.metadata, "publication block metadata")


@dataclass(frozen=True)
class PublicationIssue:
    id: UUID
    asset_id: UUID
    issue_type: str
    severity: str
    state: str
    detail: str
    block_id: UUID | None = None
    source_page: int | None = None
    evidence: dict[str, object] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utcnow)
    resolved_at: datetime | None = None
    resolved_by: str | None = None

    def __post_init__(self) -> None:
        if self.issue_type not in ISSUE_TYPES:
            raise ValueError("Unsupported publication issue type")
        if self.severity not in ISSUE_SEVERITIES:
            raise ValueError("Unsupported publication issue severity")
        if self.state not in ISSUE_STATES:
            raise ValueError("Unsupported publication issue state")
        if not self.detail.strip():
            raise ValueError("Publication issue detail is required")
        if self.source_page is not None and self.source_page < 1:
            raise ValueError("Publication issue source page must be positive")
        if self.created_at.tzinfo is None or (self.resolved_at and self.resolved_at.tzinfo is None):
            raise ValueError("Publication issue timestamps must be timezone-aware")
        _json_safe(self.evidence, "publication issue evidence")


@dataclass(frozen=True)
class ReaderPosition:
    username: str
    asset_id: UUID
    locator: str
    character_offset: int = 0
    completed: bool = False
    preferences: dict[str, object] = field(default_factory=dict)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.username.strip():
            raise ValueError("Reader username is required")
        _locator(self.locator)
        if self.character_offset < 0:
            raise ValueError("Reader character offset cannot be negative")
        _json_safe(self.preferences, "reader preferences")
        if self.updated_at.tzinfo is None:
            raise ValueError("Reader timestamps must be timezone-aware")


@dataclass(frozen=True)
class ReaderBookmark:
    id: UUID
    username: str
    asset_id: UUID
    locator: str
    character_offset: int = 0
    label: str | None = None
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.username.strip():
            raise ValueError("Bookmark username is required")
        _locator(self.locator)
        if self.character_offset < 0:
            raise ValueError("Bookmark character offset cannot be negative")
        if self.label is not None and len(self.label) > 240:
            raise ValueError("Bookmark label is too long")
        if self.created_at.tzinfo is None:
            raise ValueError("Bookmark timestamps must be timezone-aware")


@dataclass(frozen=True)
class PublicationSearchHit:
    asset_id: UUID
    locator: str
    block_type: str
    text: str
    rank: float


@dataclass(frozen=True)
class PublicationSnapshot:
    metadata: PublicationMetadata
    files: tuple[PublicationFile, ...] = ()
    blocks: tuple[PublicationBlock, ...] = ()
    issues: tuple[PublicationIssue, ...] = ()

    def __post_init__(self) -> None:
        asset_id = self.metadata.asset_id
        if any(item.asset_id != asset_id for item in (*self.files, *self.blocks, *self.issues)):
            raise ValueError("Publication snapshot records must share one asset id")
        file_ids = {item.id for item in self.files}
        block_ids = {item.id for item in self.blocks}
        if len(file_ids) != len(self.files) or len(block_ids) != len(self.blocks):
            raise ValueError("Publication snapshot ids must be unique")
        if len({item.locator for item in self.blocks}) != len(self.blocks):
            raise ValueError("Publication block locators must be unique")
        if any(item.parent_id and item.parent_id not in block_ids for item in self.blocks):
            raise ValueError("Publication block parent is missing")
        if any(item.illustration_file_id and item.illustration_file_id not in file_ids for item in self.blocks):
            raise ValueError("Publication illustration file is missing")
        if any(item.block_id and item.block_id not in block_ids for item in self.issues):
            raise ValueError("Publication issue block is missing")


def compose_publication_metadata(metadata: PublicationMetadata) -> PublicationMetadata:
    effective = {**metadata.detected, **metadata.imported, **metadata.user_overrides}
    provenance: dict[str, object] = dict(metadata.provenance)
    for key in effective:
        if key in metadata.user_overrides:
            provenance[key] = "user_override"
        elif key in metadata.imported:
            provenance[key] = "imported"
        elif key in metadata.detected:
            provenance[key] = "detected"
    return replace(metadata, effective=effective, provenance=provenance, updated_at=_utcnow())


def publication_sidecar_document(snapshot: PublicationSnapshot, exported_at: datetime | None = None) -> dict[str, object]:
    timestamp = exported_at or _utcnow()
    if timestamp.tzinfo is None:
        raise ValueError("Publication sidecar timestamp must be timezone-aware")
    metadata = snapshot.metadata
    return {
        "schema": PUBLICATION_SIDECAR_SCHEMA,
        "version": PUBLICATION_SIDECAR_VERSION,
        "exported_at": timestamp.astimezone(timezone.utc).isoformat(),
        "asset_id": str(metadata.asset_id),
        "publication": {
            "publication_type": metadata.publication_type,
            "reading_mode": metadata.reading_mode,
            "extraction_state": metadata.extraction_state,
            "language": metadata.language,
            "content_version": metadata.content_version,
            "updated_at": metadata.updated_at.astimezone(timezone.utc).isoformat(),
            "detected": metadata.detected,
            "imported": metadata.imported,
            "user_overrides": metadata.user_overrides,
            "effective": metadata.effective,
            "provenance": metadata.provenance,
        },
        "files": [
            {"id": str(item.id), "role": item.role, "vault_path": item.vault_path,
             "filename": item.filename, "mime_type": item.mime_type,
             "sha256": item.sha256, "original": item.original, "ordinal": item.ordinal}
            for item in snapshot.files
        ],
        "blocks": [
            {"id": str(item.id), "block_type": item.block_type, "ordinal": item.ordinal,
             "locator": item.locator, "parent_id": str(item.parent_id) if item.parent_id else None,
             "content_html": item.content_html, "content_text": item.content_text,
             "source_page": item.source_page, "source_bbox": list(item.source_bbox) if item.source_bbox else None,
             "illustration_file_id": str(item.illustration_file_id) if item.illustration_file_id else None,
             "metadata": item.metadata}
            for item in snapshot.blocks
        ],
        "issues": [
            {"id": str(item.id), "issue_type": item.issue_type, "severity": item.severity,
             "state": item.state, "detail": item.detail,
             "block_id": str(item.block_id) if item.block_id else None,
             "source_page": item.source_page, "evidence": item.evidence,
             "created_at": item.created_at.astimezone(timezone.utc).isoformat(),
             "resolved_at": item.resolved_at.astimezone(timezone.utc).isoformat() if item.resolved_at else None,
             "resolved_by": item.resolved_by}
            for item in snapshot.issues
        ],
    }


def publication_snapshot_from_document(document: dict[str, object]) -> PublicationSnapshot:
    if set(document) != {"schema", "version", "exported_at", "asset_id", "publication", "files", "blocks", "issues"}:
        raise ValueError("Publication sidecar fields do not match the schema")
    if document.get("schema") != PUBLICATION_SIDECAR_SCHEMA or document.get("version") != PUBLICATION_SIDECAR_VERSION:
        raise ValueError("Publication sidecar schema or version is unsupported")
    try:
        exported_at = datetime.fromisoformat(str(document["exported_at"]))
        asset_id = UUID(str(document["asset_id"]))
    except (ValueError, TypeError) as error:
        raise ValueError("Publication sidecar identity is invalid") from error
    if exported_at.tzinfo is None:
        raise ValueError("Publication sidecar timestamp must be timezone-aware")
    pub = _mapping(document["publication"], "publication")
    metadata = PublicationMetadata(
        asset_id=asset_id, publication_type=str(pub["publication_type"]),
        reading_mode=str(pub["reading_mode"]), extraction_state=str(pub["extraction_state"]),
        language=str(pub["language"]) if pub.get("language") else None,
        content_version=str(pub["content_version"]) if pub.get("content_version") else None,
        detected=_mapping(pub.get("detected", {}), "publication.detected"),
        imported=_mapping(pub.get("imported", {}), "publication.imported"),
        user_overrides=_mapping(pub.get("user_overrides", {}), "publication.user_overrides"),
        effective=_mapping(pub.get("effective", {}), "publication.effective"),
        provenance=_mapping(pub.get("provenance", {}), "publication.provenance"),
        updated_at=datetime.fromisoformat(str(pub["updated_at"])),
    )
    file_rows = document["files"]
    block_rows = document["blocks"]
    issue_rows = document["issues"]
    if not isinstance(file_rows, list) or not isinstance(block_rows, list) or not isinstance(issue_rows, list):
        raise ValueError("Publication sidecar collections are invalid")
    files = tuple(PublicationFile(
        id=UUID(str(row["id"])), asset_id=asset_id, role=str(row["role"]),
        vault_path=str(row["vault_path"]), filename=str(row["filename"]),
        mime_type=str(row["mime_type"]), sha256=str(row["sha256"]),
        original=bool(row["original"]), ordinal=int(row["ordinal"]),
    ) for raw in file_rows for row in [_mapping(raw, "file")])
    blocks = tuple(PublicationBlock(
        id=UUID(str(row["id"])), asset_id=asset_id, block_type=str(row["block_type"]),
        ordinal=int(row["ordinal"]), locator=str(row["locator"]),
        parent_id=UUID(str(row["parent_id"])) if row.get("parent_id") else None,
        content_html=str(row["content_html"]) if row.get("content_html") is not None else None,
        content_text=str(row["content_text"]) if row.get("content_text") is not None else None,
        source_page=int(row["source_page"]) if row.get("source_page") is not None else None,
        source_bbox=tuple(float(v) for v in row["source_bbox"]) if row.get("source_bbox") else None,
        illustration_file_id=UUID(str(row["illustration_file_id"])) if row.get("illustration_file_id") else None,
        metadata=_mapping(row.get("metadata", {}), "block.metadata"),
    ) for raw in block_rows for row in [_mapping(raw, "block")])
    issues = tuple(PublicationIssue(
        id=UUID(str(row["id"])), asset_id=asset_id, issue_type=str(row["issue_type"]),
        severity=str(row["severity"]), state=str(row["state"]), detail=str(row["detail"]),
        block_id=UUID(str(row["block_id"])) if row.get("block_id") else None,
        source_page=int(row["source_page"]) if row.get("source_page") is not None else None,
        evidence=_mapping(row.get("evidence", {}), "issue.evidence"),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        resolved_at=datetime.fromisoformat(str(row["resolved_at"])) if row.get("resolved_at") else None,
        resolved_by=str(row["resolved_by"]) if row.get("resolved_by") else None,
    ) for raw in issue_rows for row in [_mapping(raw, "issue")])
    return PublicationSnapshot(metadata, files, blocks, issues)


def write_publication_sidecar(snapshot: PublicationSnapshot, storage_root: Path) -> Path:
    directory = storage_root / PUBLICATION_SIDECAR_DIRECTORY
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{snapshot.metadata.asset_id}.json"
    temporary = directory / f".{snapshot.metadata.asset_id}.{uuid4().hex}.json.part"
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(publication_sidecar_document(snapshot), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def read_publication_sidecar(path: Path) -> PublicationSnapshot:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Publication sidecar must be a regular file")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Publication sidecar is not valid UTF-8 JSON") from error
    snapshot = publication_snapshot_from_document(_mapping(document, "sidecar"))
    if path.name != f"{snapshot.metadata.asset_id}.json":
        raise ValueError("Publication sidecar filename does not match its asset id")
    return snapshot


class ReadingRoomStore(Protocol):
    def save_publication(self, snapshot: PublicationSnapshot) -> PublicationSnapshot: ...
    def get_publication(self, asset_id: UUID) -> PublicationSnapshot | None: ...
    def list_publications(self) -> list[PublicationSnapshot]: ...
    def restore_publication(self, snapshot: PublicationSnapshot) -> PublicationSnapshot: ...
    def save_position(self, position: ReaderPosition) -> ReaderPosition: ...
    def get_position(self, username: str, asset_id: UUID) -> ReaderPosition | None: ...
    def add_bookmark(self, bookmark: ReaderBookmark) -> ReaderBookmark: ...
    def list_bookmarks(self, username: str, asset_id: UUID) -> list[ReaderBookmark]: ...
    def delete_bookmark(self, username: str, asset_id: UUID, bookmark_id: UUID) -> bool: ...
    def search_publications(
        self, query: str, asset_ids: set[UUID], limit: int = 30
    ) -> list[PublicationSearchHit]: ...


class MemoryReadingRoomStore:
    def __init__(self, *, asset_exists: Callable[[UUID], bool] | None = None, sidecar_root: Path | None = None) -> None:
        self._asset_exists = asset_exists or (lambda _asset_id: True)
        self._sidecar_root = sidecar_root
        self.publications: dict[UUID, PublicationSnapshot] = {}
        self.positions: dict[tuple[str, UUID], ReaderPosition] = {}
        self.bookmarks: dict[UUID, ReaderBookmark] = {}
        self._lock = threading.Lock()

    def save_publication(self, snapshot: PublicationSnapshot) -> PublicationSnapshot:
        if not self._asset_exists(snapshot.metadata.asset_id):
            raise ValueError("Publication asset does not exist in the canonical catalogue")
        composed = PublicationSnapshot(compose_publication_metadata(snapshot.metadata), snapshot.files, snapshot.blocks, snapshot.issues)
        with self._lock:
            self.publications[composed.metadata.asset_id] = composed
        if self._sidecar_root is not None:
            write_publication_sidecar(composed, self._sidecar_root)
        return composed

    def get_publication(self, asset_id: UUID) -> PublicationSnapshot | None:
        return self.publications.get(asset_id)

    def list_publications(self) -> list[PublicationSnapshot]:
        return sorted(
            self.publications.values(),
            key=lambda item: (
                str(item.metadata.effective.get("title", "")).casefold(),
                str(item.metadata.asset_id),
            ),
        )

    def restore_publication(self, snapshot: PublicationSnapshot) -> PublicationSnapshot:
        if snapshot.metadata.asset_id in self.publications:
            raise ValueError("Publication already exists")
        return self.save_publication(snapshot)

    def save_position(self, position: ReaderPosition) -> ReaderPosition:
        if not self._asset_exists(position.asset_id):
            raise ValueError("Reader asset does not exist in the canonical catalogue")
        with self._lock:
            self.positions[(position.username.casefold(), position.asset_id)] = position
        return position

    def get_position(self, username: str, asset_id: UUID) -> ReaderPosition | None:
        return self.positions.get((username.casefold(), asset_id))

    def add_bookmark(self, bookmark: ReaderBookmark) -> ReaderBookmark:
        if not self._asset_exists(bookmark.asset_id):
            raise ValueError("Bookmark asset does not exist in the canonical catalogue")
        with self._lock:
            self.bookmarks[bookmark.id] = bookmark
        return bookmark

    def list_bookmarks(self, username: str, asset_id: UUID) -> list[ReaderBookmark]:
        return sorted((item for item in self.bookmarks.values() if item.username.casefold() == username.casefold() and item.asset_id == asset_id), key=lambda item: item.created_at)

    def delete_bookmark(self, username: str, asset_id: UUID, bookmark_id: UUID) -> bool:
        bookmark = self.bookmarks.get(bookmark_id)
        if (
            bookmark is None
            or bookmark.username.casefold() != username.casefold()
            or bookmark.asset_id != asset_id
        ):
            return False
        with self._lock:
            self.bookmarks.pop(bookmark_id, None)
        return True

    def search_publications(
        self, query: str, asset_ids: set[UUID], limit: int = 30
    ) -> list[PublicationSearchHit]:
        needle = _search_normalize(query)
        if not needle:
            return []
        hits: list[PublicationSearchHit] = []
        for asset_id in asset_ids:
            snapshot = self.publications.get(asset_id)
            if snapshot is None or snapshot.metadata.extraction_state != "approved":
                continue
            metadata = snapshot.metadata.effective
            metadata_text = " ".join(str(metadata.get(key, "")) for key in (
                "title", "author", "edition", "publisher", "isbn", "publication_details"
            ))
            ordered = sorted(snapshot.blocks, key=lambda item: (item.ordinal, str(item.id)))
            if needle in _search_normalize(metadata_text) and ordered:
                hits.append(PublicationSearchHit(asset_id, ordered[0].locator, "publication", metadata_text.strip(), 4.0))
            for block in ordered:
                text = (block.content_text or "").strip()
                if text and needle in _search_normalize(text):
                    weight = 3.0 if block.block_type in {"part", "chapter", "heading"} else 1.5 if block.block_type in {"footnote", "caption"} else 1.0
                    hits.append(PublicationSearchHit(asset_id, block.locator, block.block_type, text, weight))
        return sorted(hits, key=lambda item: (-item.rank, str(item.asset_id), item.locator))[:limit]


def _search_normalize(value: str) -> str:
    return "".join(
        character for character in unicodedata.normalize("NFKD", value.casefold())
        if not unicodedata.combining(character)
    ).replace("ł", "l")


POSTGRES_SCHEMA = """
CREATE OR REPLACE FUNCTION vault_search_normalize(value TEXT) RETURNS TEXT
LANGUAGE SQL IMMUTABLE PARALLEL SAFE
AS 'SELECT translate(lower(COALESCE(value, '''')), ''ąćęłńóśźż'', ''acelnoszz'')';
CREATE TABLE IF NOT EXISTS vault_publications (
    asset_id UUID PRIMARY KEY REFERENCES vault_assets(id) ON DELETE CASCADE,
    publication_type TEXT NOT NULL CHECK (publication_type IN ('book','magazine','comic','journal','other')),
    reading_mode TEXT NOT NULL CHECK (reading_mode IN ('reflowable','fixed_layout','hybrid')),
    extraction_state TEXT NOT NULL CHECK (extraction_state IN ('pending','processing','needs_review','approved','failed')),
    language TEXT, content_version TEXT,
    detected JSONB NOT NULL DEFAULT '{}'::jsonb, imported JSONB NOT NULL DEFAULT '{}'::jsonb,
    user_overrides JSONB NOT NULL DEFAULT '{}'::jsonb, effective JSONB NOT NULL DEFAULT '{}'::jsonb,
    provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS vault_publication_files (
    id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_publications(asset_id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('source_pdf','front_cover','back_cover','reading_content','illustration','page_evidence','sidecar')),
    vault_path TEXT NOT NULL, filename TEXT NOT NULL, mime_type TEXT NOT NULL, sha256 CHAR(64) NOT NULL,
    original BOOLEAN NOT NULL, ordinal INTEGER NOT NULL DEFAULT 0 CHECK (ordinal >= 0),
    UNIQUE(asset_id, role, vault_path)
);
CREATE TABLE IF NOT EXISTS vault_publication_blocks (
    id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_publications(asset_id) ON DELETE CASCADE,
    parent_id UUID REFERENCES vault_publication_blocks(id) ON DELETE CASCADE,
    block_type TEXT NOT NULL CHECK (block_type IN ('part','chapter','heading','paragraph','footnote','illustration','caption','page_marker','table','other')),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0), locator TEXT NOT NULL,
    content_html TEXT, content_text TEXT, source_page INTEGER CHECK (source_page > 0),
    source_bbox JSONB, illustration_file_id UUID REFERENCES vault_publication_files(id) ON DELETE SET NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb, UNIQUE(asset_id, locator)
);
CREATE TABLE IF NOT EXISTS vault_publication_issues (
    id UUID PRIMARY KEY, asset_id UUID NOT NULL REFERENCES vault_publications(asset_id) ON DELETE CASCADE,
    block_id UUID REFERENCES vault_publication_blocks(id) ON DELETE SET NULL,
    issue_type TEXT NOT NULL CHECK (issue_type IN ('unreadable_passage','missing_page','duplicated_page','uncertain_character','incorrect_rotation','likely_ocr_mistake','uncertain_structure','uncertain_reading_order','uncertain_image_placement')),
    severity TEXT NOT NULL CHECK (severity IN ('advisory','warning','critical')),
    state TEXT NOT NULL CHECK (state IN ('open','accepted','resolved','rejected')),
    detail TEXT NOT NULL, source_page INTEGER CHECK (source_page > 0), evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL, resolved_at TIMESTAMPTZ, resolved_by TEXT
);
CREATE TABLE IF NOT EXISTS user_reading_state (
    username TEXT NOT NULL, asset_id UUID NOT NULL REFERENCES vault_publications(asset_id) ON DELETE CASCADE,
    locator TEXT NOT NULL, character_offset INTEGER NOT NULL DEFAULT 0 CHECK (character_offset >= 0),
    completed BOOLEAN NOT NULL DEFAULT FALSE, preferences JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(username, asset_id)
);
CREATE TABLE IF NOT EXISTS user_reading_bookmarks (
    id UUID PRIMARY KEY, username TEXT NOT NULL,
    asset_id UUID NOT NULL REFERENCES vault_publications(asset_id) ON DELETE CASCADE,
    locator TEXT NOT NULL, character_offset INTEGER NOT NULL DEFAULT 0 CHECK (character_offset >= 0),
    label TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS vault_publication_blocks_order_idx ON vault_publication_blocks(asset_id, ordinal);
CREATE INDEX IF NOT EXISTS vault_publication_issues_state_idx ON vault_publication_issues(asset_id, state, severity);
CREATE INDEX IF NOT EXISTS user_reading_bookmarks_asset_idx ON user_reading_bookmarks(username, asset_id, created_at);
CREATE INDEX IF NOT EXISTS vault_publication_blocks_search_idx ON vault_publication_blocks
USING GIN (to_tsvector('simple', vault_search_normalize(COALESCE(content_text, ''))));
"""


class PostgresReadingRoomStore:
    def __init__(self, conninfo: str, *, sidecar_root: Path | None = None) -> None:
        self._conninfo = conninfo
        self._sidecar_root = sidecar_root

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(POSTGRES_SCHEMA)

    def save_publication(self, snapshot: PublicationSnapshot) -> PublicationSnapshot:
        snapshot = PublicationSnapshot(compose_publication_metadata(snapshot.metadata), snapshot.files, snapshot.blocks, snapshot.issues)
        m = snapshot.metadata
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""INSERT INTO vault_publications
                    (asset_id,publication_type,reading_mode,extraction_state,language,content_version,detected,imported,user_overrides,effective,provenance,updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(asset_id) DO UPDATE SET publication_type=EXCLUDED.publication_type,reading_mode=EXCLUDED.reading_mode,
                    extraction_state=EXCLUDED.extraction_state,language=EXCLUDED.language,content_version=EXCLUDED.content_version,
                    detected=EXCLUDED.detected,imported=EXCLUDED.imported,user_overrides=EXCLUDED.user_overrides,
                    effective=EXCLUDED.effective,provenance=EXCLUDED.provenance,updated_at=EXCLUDED.updated_at""",
                    (m.asset_id,m.publication_type,m.reading_mode,m.extraction_state,m.language,m.content_version,
                     Jsonb(m.detected),Jsonb(m.imported),Jsonb(m.user_overrides),Jsonb(m.effective),Jsonb(m.provenance),m.updated_at))
                for table in ("vault_publication_issues", "vault_publication_blocks", "vault_publication_files"):
                    cursor.execute(f"DELETE FROM {table} WHERE asset_id = %s", (m.asset_id,))
                for item in snapshot.files:
                    cursor.execute("INSERT INTO vault_publication_files VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                                   (item.id,item.asset_id,item.role,item.vault_path,item.filename,item.mime_type,item.sha256,item.original,item.ordinal))
                pending = list(snapshot.blocks)
                inserted: set[UUID] = set()
                while pending:
                    ready = [item for item in pending if item.parent_id is None or item.parent_id in inserted]
                    if not ready:
                        raise ValueError("Publication block hierarchy contains a cycle")
                    for item in ready:
                        cursor.execute("""INSERT INTO vault_publication_blocks
                            (id,asset_id,parent_id,block_type,ordinal,locator,content_html,content_text,source_page,source_bbox,illustration_file_id,metadata)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                            (item.id,item.asset_id,item.parent_id,item.block_type,item.ordinal,item.locator,item.content_html,item.content_text,
                             item.source_page,Jsonb(list(item.source_bbox)) if item.source_bbox else None,item.illustration_file_id,Jsonb(item.metadata)))
                        inserted.add(item.id); pending.remove(item)
                for item in snapshot.issues:
                    cursor.execute("""INSERT INTO vault_publication_issues
                        (id,asset_id,block_id,issue_type,severity,state,detail,source_page,evidence,created_at,resolved_at,resolved_by)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (item.id,item.asset_id,item.block_id,item.issue_type,item.severity,item.state,item.detail,item.source_page,
                         Jsonb(item.evidence),item.created_at,item.resolved_at,item.resolved_by))
        if self._sidecar_root is not None:
            write_publication_sidecar(snapshot, self._sidecar_root)
        return snapshot

    def get_publication(self, asset_id: UUID) -> PublicationSnapshot | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM vault_publications WHERE asset_id=%s", (asset_id,)); row = cursor.fetchone()
                if not row: return None
                cursor.execute("SELECT * FROM vault_publication_files WHERE asset_id=%s ORDER BY ordinal,id", (asset_id,)); file_rows=cursor.fetchall()
                cursor.execute("SELECT * FROM vault_publication_blocks WHERE asset_id=%s ORDER BY ordinal,id", (asset_id,)); block_rows=cursor.fetchall()
                cursor.execute("SELECT * FROM vault_publication_issues WHERE asset_id=%s ORDER BY created_at,id", (asset_id,)); issue_rows=cursor.fetchall()
        metadata=PublicationMetadata(asset_id, row["publication_type"], row["reading_mode"], row["extraction_state"], row["language"], row["content_version"],
                                     dict(row["detected"]),dict(row["imported"]),dict(row["user_overrides"]),dict(row["effective"]),dict(row["provenance"]),row["updated_at"])
        files=tuple(PublicationFile(r["id"],asset_id,r["role"],r["vault_path"],r["filename"],r["mime_type"],r["sha256"],r["original"],r["ordinal"]) for r in file_rows)
        blocks=tuple(PublicationBlock(r["id"],asset_id,r["block_type"],r["ordinal"],r["locator"],r["parent_id"],r["content_html"],r["content_text"],r["source_page"],tuple(r["source_bbox"]) if r["source_bbox"] else None,r["illustration_file_id"],dict(r["metadata"])) for r in block_rows)
        issues=tuple(PublicationIssue(r["id"],asset_id,r["issue_type"],r["severity"],r["state"],r["detail"],r["block_id"],r["source_page"],dict(r["evidence"]),r["created_at"],r["resolved_at"],r["resolved_by"]) for r in issue_rows)
        return PublicationSnapshot(metadata,files,blocks,issues)

    def list_publications(self) -> list[PublicationSnapshot]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT asset_id FROM vault_publications ORDER BY lower(COALESCE(effective->>'title', '')), asset_id"
                )
                asset_ids = [row["asset_id"] for row in cursor.fetchall()]
        return [
            snapshot
            for asset_id in asset_ids
            if (snapshot := self.get_publication(asset_id)) is not None
        ]

    def restore_publication(self, snapshot: PublicationSnapshot) -> PublicationSnapshot:
        if self.get_publication(snapshot.metadata.asset_id) is not None:
            raise ValueError("Publication already exists")
        return self.save_publication(snapshot)

    def save_position(self, position: ReaderPosition) -> ReaderPosition:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("""INSERT INTO user_reading_state(username,asset_id,locator,character_offset,completed,preferences,updated_at)
                    VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(username,asset_id) DO UPDATE SET locator=EXCLUDED.locator,
                    character_offset=EXCLUDED.character_offset,completed=EXCLUDED.completed,preferences=EXCLUDED.preferences,updated_at=EXCLUDED.updated_at""",
                    (position.username.casefold(),position.asset_id,position.locator,position.character_offset,position.completed,Jsonb(position.preferences),position.updated_at))
        return position

    def get_position(self, username: str, asset_id: UUID) -> ReaderPosition | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM user_reading_state WHERE username=%s AND asset_id=%s",(username.casefold(),asset_id)); row=cursor.fetchone()
        return ReaderPosition(row["username"],asset_id,row["locator"],row["character_offset"],row["completed"],dict(row["preferences"]),row["updated_at"]) if row else None

    def add_bookmark(self, bookmark: ReaderBookmark) -> ReaderBookmark:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("INSERT INTO user_reading_bookmarks(id,username,asset_id,locator,character_offset,label,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                               (bookmark.id,bookmark.username.casefold(),bookmark.asset_id,bookmark.locator,bookmark.character_offset,bookmark.label,bookmark.created_at))
        return bookmark

    def list_bookmarks(self, username: str, asset_id: UUID) -> list[ReaderBookmark]:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM user_reading_bookmarks WHERE username=%s AND asset_id=%s ORDER BY created_at,id",(username.casefold(),asset_id)); rows=cursor.fetchall()
        return [ReaderBookmark(r["id"],r["username"],asset_id,r["locator"],r["character_offset"],r["label"],r["created_at"]) for r in rows]

    def delete_bookmark(self, username: str, asset_id: UUID, bookmark_id: UUID) -> bool:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM user_reading_bookmarks WHERE id=%s AND username=%s AND asset_id=%s",
                    (bookmark_id, username.casefold(), asset_id),
                )
                return cursor.rowcount == 1

    def search_publications(
        self, query: str, asset_ids: set[UUID], limit: int = 30
    ) -> list[PublicationSearchHit]:
        if not query.strip() or not asset_ids:
            return []
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """WITH search_query AS (
                           SELECT websearch_to_tsquery('simple', vault_search_normalize(%s)) AS value
                       ), candidates AS (
                           SELECT p.asset_id,
                                  COALESCE((SELECT locator FROM vault_publication_blocks b0 WHERE b0.asset_id=p.asset_id ORDER BY ordinal,id LIMIT 1), '') AS locator,
                                  'publication'::text AS block_type,
                                  concat_ws(' ', p.effective->>'title', p.effective->>'author', p.effective->>'edition', p.effective->>'publisher', p.effective->>'isbn', p.effective->>'publication_details') AS content,
                                  4.0::real AS weight
                           FROM vault_publications p
                           WHERE p.asset_id = ANY(%s) AND p.extraction_state='approved' AND p.reading_mode='reflowable'
                           UNION ALL
                           SELECT b.asset_id, b.locator, b.block_type, COALESCE(b.content_text,''),
                                  CASE WHEN b.block_type IN ('part','chapter','heading') THEN 3.0
                                       WHEN b.block_type IN ('footnote','caption') THEN 1.5 ELSE 1.0 END::real
                           FROM vault_publication_blocks b JOIN vault_publications p USING(asset_id)
                           WHERE b.asset_id = ANY(%s) AND p.extraction_state='approved' AND p.reading_mode='reflowable'
                       ), ranked AS (
                           SELECT c.*, ts_rank_cd(to_tsvector('simple', vault_search_normalize(c.content)), q.value) * c.weight AS rank,
                                  ts_headline('simple', c.content, q.value, 'StartSel=<<, StopSel=>>, MaxWords=24, MinWords=8, ShortWord=2') AS snippet
                           FROM candidates c CROSS JOIN search_query q
                           WHERE c.locator <> '' AND to_tsvector('simple', vault_search_normalize(c.content)) @@ q.value
                       )
                       SELECT asset_id, locator, block_type, snippet, rank FROM ranked
                       ORDER BY rank DESC, asset_id, locator LIMIT %s""",
                    (query, list(asset_ids), list(asset_ids), limit),
                )
                rows = cursor.fetchall()
        return [PublicationSearchHit(row["asset_id"], row["locator"], row["block_type"], row["snippet"], float(row["rank"])) for row in rows]


@lru_cache(maxsize=1)
def get_reading_room_store() -> PostgresReadingRoomStore:
    return PostgresReadingRoomStore(
        get_database_conninfo(),
        sidecar_root=get_metadata_storage_root(),
    )
