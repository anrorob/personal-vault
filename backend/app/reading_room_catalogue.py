"""Access-scoped catalogue APIs for approved Reading Room publications."""

from pathlib import Path, PurePosixPath
from typing import Annotated
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.auth import AuthenticatedUsername
from app.vault_master import (
    CataloguedAsset,
    VaultMasterStore,
    get_vault_master_store,
    require_file_within_root,
    sha256_file,
)
from app.vault_master_api import get_destination_paths
from app.vault_master_reading import (
    PublicationSnapshot,
    ReaderBookmark,
    ReaderPosition,
    ReadingRoomStore,
    get_reading_room_store,
)


router = APIRouter(prefix="/api/reading-room", tags=["reading-room"])
CatalogueStore = Annotated[VaultMasterStore, Depends(get_vault_master_store)]
PublicationStore = Annotated[ReadingRoomStore, Depends(get_reading_room_store)]
MAX_READER_BLOCKS = 50_000
MAX_READER_BLOCK_TEXT = 250_000
MAX_READER_TEXT = 32 * 1024 * 1024
MAX_BOOKMARKS_PER_PUBLICATION = 1_000


class ReadingProgress(BaseModel):
    locator: str
    character_offset: int
    completed: bool
    percent: int


class ReadingRoomPublication(BaseModel):
    id: UUID
    title: str
    author: str
    publication_type: str
    edition: str | None
    language: str | None
    description: str | None
    publisher: str | None
    isbn: str | None
    publication_details: str | None
    cover_url: str | None
    chapter_count: int
    progress: ReadingProgress | None


class ReadingRoomChapter(BaseModel):
    locator: str
    title: str
    level: int


class ReadingRoomPublicationDetail(ReadingRoomPublication):
    chapters: list[ReadingRoomChapter]


class ReadingRoomSearchResult(BaseModel):
    publication_id: UUID
    title: str
    author: str
    language: str | None
    locator: str
    block_type: str
    snippet: str
    rank: float


class ReaderBlock(BaseModel):
    locator: str
    parent_locator: str | None
    block_type: Literal[
        "part",
        "chapter",
        "heading",
        "paragraph",
        "footnote",
        "illustration",
        "caption",
        "page_marker",
        "table",
        "other",
    ]
    text: str | None
    illustration_url: str | None


class ReaderBookmarkDocument(BaseModel):
    id: UUID
    locator: str
    character_offset: int
    label: str | None
    created_at: datetime


class ReaderDocument(BaseModel):
    id: UUID
    title: str
    author: str
    language: str | None
    content_version: str | None
    blocks: list[ReaderBlock]
    chapters: list[ReadingRoomChapter]
    position: ReadingProgress | None
    preferences: dict[str, object]
    bookmarks: list[ReaderBookmarkDocument]


class ReaderPositionUpdate(BaseModel):
    locator: str
    character_offset: int = 0
    completed: bool = False
    theme: Literal["light", "dark", "sepia"] = "sepia"
    font_family: Literal["serif", "sans"] = "serif"
    font_size: int = 18


class ReaderBookmarkCreate(BaseModel):
    locator: str
    character_offset: int = 0
    label: str | None = None


def _text(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _approved_visible_publication(
    asset_id: UUID,
    username: str,
    catalogue: VaultMasterStore,
    publications: ReadingRoomStore,
) -> tuple[CataloguedAsset, PublicationSnapshot]:
    asset = catalogue.get_visible_catalogued_asset_by_id(asset_id, username)
    snapshot = publications.get_publication(asset_id)
    if (
        asset is None
        or asset.asset_type != "Library"
        or snapshot is None
        or snapshot.metadata.extraction_state != "approved"
        or snapshot.metadata.reading_mode != "reflowable"
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Publication not found")
    return asset, snapshot


def _reader_bookmark(bookmark: ReaderBookmark) -> ReaderBookmarkDocument:
    return ReaderBookmarkDocument(
        id=bookmark.id,
        locator=bookmark.locator,
        character_offset=bookmark.character_offset,
        label=bookmark.label,
        created_at=bookmark.created_at,
    )


def _reader_blocks(snapshot: PublicationSnapshot) -> list[ReaderBlock]:
    if len(snapshot.blocks) > MAX_READER_BLOCKS:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Publication exceeds the native reader block limit")
    text_lengths = [len(block.content_text or "") for block in snapshot.blocks]
    if any(length > MAX_READER_BLOCK_TEXT for length in text_lengths) or sum(text_lengths) > MAX_READER_TEXT:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Publication exceeds the native reader text limit")
    locators = {block.id: block.locator for block in snapshot.blocks}
    return [
        ReaderBlock(
            locator=block.locator,
            parent_locator=locators.get(block.parent_id),
            block_type=block.block_type,
            text=(block.content_text or "").strip() or None,
            illustration_url=(
                f"/api/reading-room/publications/{snapshot.metadata.asset_id}/illustrations/{block.illustration_file_id}"
                if block.block_type == "illustration" and block.illustration_file_id
                else None
            ),
        )
        for block in sorted(snapshot.blocks, key=lambda item: (item.ordinal, str(item.id)))
    ]


def _require_locator(snapshot: PublicationSnapshot, locator: str) -> str:
    if not any(block.locator == locator for block in snapshot.blocks):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Reader locator is invalid")
    return locator


def _reader_preferences(position: ReaderPosition | None) -> dict[str, object]:
    defaults: dict[str, object] = {
        "theme": "sepia",
        "font_family": "serif",
        "font_size": 18,
    }
    if position is None:
        return defaults
    return {**defaults, **position.preferences}


def _chapters(snapshot: PublicationSnapshot) -> list[ReadingRoomChapter]:
    chapters: list[ReadingRoomChapter] = []
    for block in sorted(snapshot.blocks, key=lambda item: (item.ordinal, str(item.id))):
        if block.block_type not in {"part", "chapter", "heading"}:
            continue
        title = (block.content_text or "").strip()
        if not title:
            continue
        raw_level = block.metadata.get("level", 1 if block.block_type == "chapter" else 2)
        try:
            level = min(6, max(1, int(raw_level)))
        except (TypeError, ValueError):
            level = 1
        chapters.append(ReadingRoomChapter(locator=block.locator, title=title, level=level))
    return chapters


def _progress(snapshot: PublicationSnapshot, position: ReaderPosition | None) -> ReadingProgress | None:
    if position is None:
        return None
    ordered = sorted(snapshot.blocks, key=lambda item: (item.ordinal, str(item.id)))
    index = next((number for number, block in enumerate(ordered) if block.locator == position.locator), 0)
    percent = (
        100
        if position.completed
        else min(99, round(index * 100 / max(1, len(ordered) - 1)))
    )
    return ReadingProgress(
        locator=position.locator,
        character_offset=position.character_offset,
        completed=position.completed,
        percent=min(100, max(0, percent)),
    )


def _catalogue_document(
    asset: CataloguedAsset,
    snapshot: PublicationSnapshot,
    position: ReaderPosition | None,
) -> ReadingRoomPublication:
    metadata = snapshot.metadata.effective
    chapters = _chapters(snapshot)
    has_cover = any(item.role == "front_cover" for item in snapshot.files)
    return ReadingRoomPublication(
        id=asset.id,
        title=_text(metadata, "title") or asset.display_title,
        author=_text(metadata, "author") or "Unknown author",
        publication_type=snapshot.metadata.publication_type,
        edition=_text(metadata, "edition"),
        language=snapshot.metadata.language,
        description=_text(metadata, "description"),
        publisher=_text(metadata, "publisher"),
        isbn=_text(metadata, "isbn"),
        publication_details=_text(metadata, "publication_details"),
        cover_url=f"/api/reading-room/publications/{asset.id}/cover" if has_cover else None,
        chapter_count=len(chapters),
        progress=_progress(snapshot, position),
    )


@router.get("/publications", response_model=list[ReadingRoomPublication])
def list_reading_room_publications(
    response: Response,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
) -> list[ReadingRoomPublication]:
    response.headers["Cache-Control"] = "private, no-store"
    owned = {
        asset.id: asset
        for asset in catalogue.list_visible_catalogued_assets(username)
        if asset.asset_type == "Library"
    }
    results = []
    for snapshot in publications.list_publications():
        asset = owned.get(snapshot.metadata.asset_id)
        if asset is None or snapshot.metadata.extraction_state != "approved":
            continue
        results.append(
            _catalogue_document(
                asset,
                snapshot,
                publications.get_position(username, asset.id),
            )
        )
    return sorted(results, key=lambda item: (item.author.casefold(), item.title.casefold(), str(item.id)))


@router.get("/search", response_model=list[ReadingRoomSearchResult])
def search_reading_room(
    response: Response,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
    q: str = Query(min_length=2, max_length=160),
    limit: int = Query(default=30, ge=1, le=50),
) -> list[ReadingRoomSearchResult]:
    response.headers["Cache-Control"] = "private, no-store"
    owned = {
        asset.id: asset
        for asset in catalogue.list_visible_catalogued_assets(username)
        if asset.asset_type == "Library"
    }
    results: list[ReadingRoomSearchResult] = []
    for hit in publications.search_publications(q, set(owned), limit):
        asset = owned.get(hit.asset_id)
        snapshot = publications.get_publication(hit.asset_id)
        if asset is None or snapshot is None or snapshot.metadata.extraction_state != "approved" or snapshot.metadata.reading_mode != "reflowable":
            continue
        metadata = snapshot.metadata.effective
        results.append(ReadingRoomSearchResult(
            publication_id=asset.id,
            title=_text(metadata, "title") or asset.display_title,
            author=_text(metadata, "author") or "Unknown author",
            language=snapshot.metadata.language,
            locator=hit.locator,
            block_type=hit.block_type,
            snippet=hit.text,
            rank=hit.rank,
        ))
    return results


@router.get("/publications/{asset_id}", response_model=ReadingRoomPublicationDetail)
def get_reading_room_publication(
    asset_id: UUID,
    response: Response,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
) -> ReadingRoomPublicationDetail:
    response.headers["Cache-Control"] = "private, no-store"
    asset, snapshot = _approved_visible_publication(asset_id, username, catalogue, publications)
    summary = _catalogue_document(asset, snapshot, publications.get_position(username, asset.id))
    return ReadingRoomPublicationDetail(**summary.model_dump(), chapters=_chapters(snapshot))


@router.get("/publications/{asset_id}/reader", response_model=ReaderDocument)
def get_reader_document(
    asset_id: UUID,
    response: Response,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
) -> ReaderDocument:
    response.headers["Cache-Control"] = "private, no-store"
    asset, snapshot = _approved_visible_publication(asset_id, username, catalogue, publications)
    position = publications.get_position(username, asset_id)
    metadata = snapshot.metadata.effective
    return ReaderDocument(
        id=asset.id,
        title=_text(metadata, "title") or asset.display_title,
        author=_text(metadata, "author") or "Unknown author",
        language=snapshot.metadata.language,
        content_version=snapshot.metadata.content_version,
        blocks=_reader_blocks(snapshot),
        chapters=_chapters(snapshot),
        position=_progress(snapshot, position),
        preferences=_reader_preferences(position),
        bookmarks=[
            _reader_bookmark(bookmark)
            for bookmark in publications.list_bookmarks(username, asset_id)
        ],
    )


@router.put("/publications/{asset_id}/position", response_model=ReadingProgress)
def save_reader_position(
    asset_id: UUID,
    update: ReaderPositionUpdate,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
) -> ReadingProgress:
    _asset, snapshot = _approved_visible_publication(asset_id, username, catalogue, publications)
    locator = _require_locator(snapshot, update.locator)
    if update.character_offset < 0 or update.character_offset > 1_000_000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Reader offset is invalid")
    if update.font_size < 14 or update.font_size > 32:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Reader font size is invalid")
    saved = publications.save_position(
        ReaderPosition(
            username,
            asset_id,
            locator,
            update.character_offset,
            update.completed,
            {
                "theme": update.theme,
                "font_family": update.font_family,
                "font_size": update.font_size,
            },
        )
    )
    progress = _progress(snapshot, saved)
    assert progress is not None
    return progress


@router.post(
    "/publications/{asset_id}/bookmarks",
    response_model=ReaderBookmarkDocument,
    status_code=status.HTTP_201_CREATED,
)
def add_reader_bookmark(
    asset_id: UUID,
    create: ReaderBookmarkCreate,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
) -> ReaderBookmarkDocument:
    _asset, snapshot = _approved_visible_publication(asset_id, username, catalogue, publications)
    locator = _require_locator(snapshot, create.locator)
    if create.character_offset < 0 or create.character_offset > 1_000_000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bookmark offset is invalid")
    label = create.label.strip() if create.label else None
    if label and len(label) > 240:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Bookmark label is too long")
    if len(publications.list_bookmarks(username, asset_id)) >= MAX_BOOKMARKS_PER_PUBLICATION:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Publication bookmark limit reached")
    bookmark = publications.add_bookmark(
        ReaderBookmark(uuid4(), username, asset_id, locator, create.character_offset, label)
    )
    return _reader_bookmark(bookmark)


@router.delete(
    "/publications/{asset_id}/bookmarks/{bookmark_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_reader_bookmark(
    asset_id: UUID,
    bookmark_id: UUID,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
) -> None:
    _approved_visible_publication(asset_id, username, catalogue, publications)
    if not publications.delete_bookmark(username, asset_id, bookmark_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bookmark not found")


@router.get("/publications/{asset_id}/cover", response_class=FileResponse)
def get_reading_room_cover(
    asset_id: UUID,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
    destination_paths: dict[str, Path] = Depends(get_destination_paths),
) -> FileResponse:
    _asset, snapshot = _approved_visible_publication(asset_id, username, catalogue, publications)
    cover = next((item for item in snapshot.files if item.role == "front_cover"), None)
    library_root = destination_paths.get("Library")
    if cover is None or library_root is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cover not found")
    if cover.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cover not found")
    vault_path = PurePosixPath(cover.vault_path)
    prefix = PurePosixPath("/vault/Library")
    try:
        relative = vault_path.relative_to(prefix)
        path = require_file_within_root(library_root.joinpath(*relative.parts), library_root)
    except (OSError, ValueError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cover not found") from None
    if sha256_file(path) != cover.sha256:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cover verification failed")
    return FileResponse(
        path,
        media_type=cover.mime_type,
        filename=None,
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get(
    "/publications/{asset_id}/illustrations/{file_id}",
    response_class=FileResponse,
)
def get_reading_room_illustration(
    asset_id: UUID,
    file_id: UUID,
    username: AuthenticatedUsername,
    catalogue: CatalogueStore,
    publications: PublicationStore,
    destination_paths: dict[str, Path] = Depends(get_destination_paths),
) -> FileResponse:
    _asset, snapshot = _approved_visible_publication(asset_id, username, catalogue, publications)
    illustration = next(
        (
            item
            for item in snapshot.files
            if item.id == file_id and item.role == "illustration"
        ),
        None,
    )
    library_root = destination_paths.get("Library")
    if illustration is None or library_root is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Illustration not found")
    if illustration.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Illustration not found")
    try:
        relative = PurePosixPath(illustration.vault_path).relative_to(
            PurePosixPath("/vault/Library")
        )
        path = require_file_within_root(
            library_root.joinpath(*relative.parts), library_root
        )
    except (OSError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Illustration not found"
        ) from None
    if sha256_file(path) != illustration.sha256:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Illustration verification failed",
        )
    return FileResponse(
        path,
        media_type=illustration.mime_type,
        filename=None,
        headers={"Cache-Control": "private, max-age=3600"},
    )
