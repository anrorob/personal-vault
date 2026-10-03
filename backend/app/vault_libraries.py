from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.auth import AuthenticatedUsername
from app.gallery_custom_tags import get_gallery_custom_tag_store
from app.home_video_tags import system_terms, effective_system_tags
from app.home_videos import get_home_videos_path
from app.home_video_playback import (
    get_home_video_playback_cache_path, playback_status, playback_file,
)
from app.media_formats import (
    BROWSER_INLINE_VIDEO_EXTENSIONS,
    VIDEO_EXTENSIONS,
    VIDEO_MIME_TYPES,
)
from app.video_thumbnails import get_video_thumbnail_cache_path, video_thumbnail
from app.vault_master import (
    CataloguedAsset,
    VaultMasterStore,
    asset_is_editable_by,
    get_vault_master_store,
)
from app.gallery_intelligence import get_gallery_intelligence_store
from app.gallery_people import get_gallery_people_store
from app.video_intelligence import (
    VideoAnalysisJob,
    get_video_intelligence_store,
    reconcile_video_analysis_job,
)


router = APIRouter(prefix="/api", tags=["vault-libraries"])

IMAGE_EXTENSIONS = frozenset(
    {".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
)
AUDIO_EXTENSIONS = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}
)
DOCUMENT_EXTENSIONS = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".epub",
        ".md",
        ".odf",
        ".odg",
        ".odp",
        ".ods",
        ".odt",
        ".pdf",
        ".ppt",
        ".pptx",
        ".rtf",
        ".tex",
        ".txt",
        ".xls",
        ".xlsx",
    }
)
DOCUMENT_LIBRARY_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS
ARCHIVE_EXTENSIONS = frozenset(
    {".7z", ".bz2", ".gz", ".rar", ".tar", ".tgz", ".xz", ".zip"}
)
SOFTWARE_EXTENSIONS = frozenset(
    {".apk", ".deb", ".dmg", ".exe", ".iso", ".msi", ".pkg", ".rpm"}
)
SAFE_INLINE_EXTENSIONS = (
    IMAGE_EXTENSIONS
    | AUDIO_EXTENSIONS
    | BROWSER_INLINE_VIDEO_EXTENSIONS
    | frozenset({".csv", ".md", ".pdf", ".txt"})
)

LibraryKind = Literal[
    "video",
    "image",
    "audio",
    "pdf",
    "document",
    "archive",
    "software",
    "other",
]


@dataclass(frozen=True)
class VaultLibraryFile:
    id: str
    name: str
    relative_path: Path
    path: Path
    size: int
    modified_at: datetime
    kind: LibraryKind


class VaultLibraryFileSummary(BaseModel):
    id: str
    name: str
    directory: str | None
    size: int
    modified_at: datetime
    kind: LibraryKind
    opens_inline: bool
    open_url: str
    asset_id: UUID | None = None
    can_edit: bool = False
    thumbnail_url: str | None = None
    display_title: str | None = None
    captured_on: date | None = None
    location: str | None = None
    metadata_provenance: dict[str, str] = Field(default_factory=dict)


class PersonalVideoAnalysisJobResponse(BaseModel):
    id: UUID
    status: str
    requested_reanalysis: bool
    total_frames: int
    frames_completed: int
    frames_failed: int
    warning: str | None = None
    error: str | None = None
    task_version: str
    sampling_version: str


class PersonalVideoDetails(BaseModel):
    file_id: str
    asset_id: UUID
    name: str
    display_title: str | None
    analysis: PersonalVideoAnalysisJobResponse | None = None
    narrative: str | None = None
    people: list[dict[str, object]] = Field(default_factory=list)
    content_tags: list[dict[str, object]] = Field(default_factory=list)
    captured_on: date | None = None
    warnings: list[str] = Field(default_factory=list)
    narrative_source: Literal["user", "ken_adjusted", "ken_generated", "vault_master", "none"] = "none"


class PersonalVideoAnalysisRequest(BaseModel):
    asset_id: UUID
    reanalyse: bool = False


class PersonalVideoNarrativeEdit(BaseModel):
    narrative: str | None = Field(default=None, max_length=4_000)


class PersonalVideoPersonDecision(BaseModel):
    person_id: UUID
    decision: Literal["include", "exclude"]


class PersonalVideoCustomTag(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


class PersonalVideoTagDecision(BaseModel):
    namespace: Literal["content_tag"]
    slug: str
    decision: Literal["include", "exclude"]


def get_personal_videos_path() -> Path:
    """Deprecated dependency name retained for route/test compatibility."""
    return get_home_videos_path()


def get_documents_path() -> Path:
    return Path(os.getenv("PV_DOCUMENTS_PATH", "/media/documents"))


def get_archives_path() -> Path:
    return Path(os.getenv("PV_ARCHIVES_PATH", "/media/archives"))


def _require_library_path(path: Path, label: str) -> Path:
    try:
        resolved_path = path.resolve(strict=True)
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{label} storage is unavailable",
        ) from error

    if not resolved_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"{label} storage is unavailable",
        )

    return resolved_path


def _file_kind(path: Path) -> LibraryKind:
    extension = path.suffix.casefold()
    if extension in VIDEO_EXTENSIONS:
        return "video"
    if extension in IMAGE_EXTENSIONS:
        return "image"
    if extension in AUDIO_EXTENSIONS:
        return "audio"
    if extension == ".pdf":
        return "pdf"
    if extension in DOCUMENT_EXTENSIONS:
        return "document"
    if extension in ARCHIVE_EXTENSIONS:
        return "archive"
    if extension in SOFTWARE_EXTENSIONS:
        return "software"
    return "other"


def _file_id(relative_path: Path) -> str:
    return hashlib.sha256(
        relative_path.as_posix().encode("utf-8")
    ).hexdigest()[:20]


def scan_vault_library(
    library_path: Path,
    *,
    allowed_extensions: frozenset[str] | None = None,
) -> list[VaultLibraryFile]:
    files: list[VaultLibraryFile] = []

    try:
        for candidate in library_path.rglob("*"):
            if (
                candidate.is_symlink()
                or not candidate.is_file()
                or (
                    allowed_extensions is not None
                    and candidate.suffix.casefold() not in allowed_extensions
                )
            ):
                continue

            relative_path = candidate.relative_to(library_path)
            file_stat = candidate.stat()
            files.append(
                VaultLibraryFile(
                    id=_file_id(relative_path),
                    name=candidate.name,
                    relative_path=relative_path,
                    path=candidate,
                    size=file_stat.st_size,
                    modified_at=datetime.fromtimestamp(
                        file_stat.st_mtime,
                        tz=timezone.utc,
                    ),
                    kind=_file_kind(candidate),
                )
            )
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Library storage is unavailable",
        ) from error

    files.sort(
        key=lambda entry: (
            entry.relative_path.parent.as_posix().casefold(),
            entry.name.casefold(),
        )
    )
    return files


def _find_file(
    library_path: Path,
    file_id: str,
    *,
    allowed_extensions: frozenset[str] | None = None,
) -> VaultLibraryFile:
    for entry in scan_vault_library(
        library_path,
        allowed_extensions=allowed_extensions,
    ):
        if entry.id == file_id:
            return entry

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="File was not found",
    )


def _to_summary(
    entry: VaultLibraryFile,
    api_path: str,
    asset: CataloguedAsset | None = None,
    *,
    include_metadata_provenance: bool = True,
) -> VaultLibraryFileSummary:
    parent = entry.relative_path.parent.as_posix()
    return VaultLibraryFileSummary(
        id=entry.id,
        asset_id=asset.id if asset else None,
        can_edit=bool(asset and include_metadata_provenance),
        name=entry.name,
        directory=None if parent == "." else parent,
        size=entry.size,
        modified_at=entry.modified_at,
        kind=entry.kind,
        opens_inline=(
            entry.path.suffix.casefold() in SAFE_INLINE_EXTENSIONS
        ),
        open_url=f"/api/{api_path}/{entry.id}/content",
        thumbnail_url=(
            f"/api/personal-videos/{entry.id}/thumbnail"
            if api_path == "personal-videos" else None
        ),
        display_title=asset.display_title if asset else None,
        captured_on=asset.captured_on if asset else None,
        location=asset.location if asset else None,
        metadata_provenance=(
            asset.metadata_provenance
            if asset and include_metadata_provenance
            else {}
        ),
    )


def _catalogued_entries(
    *,
    entries: list[VaultLibraryFile],
    library_path: Path,
    vault_root: str,
    store: VaultMasterStore,
    username: str | None = None,
) -> list[tuple[VaultLibraryFile, CataloguedAsset]]:
    vault_paths = {
        str(entry.path): (
            f"{vault_root.rstrip('/')}/"
            f"{entry.path.relative_to(library_path).as_posix()}"
        )
        for entry in entries
    }
    catalogue = (
        store.get_visible_catalogued_assets(
            list(vault_paths.values()), username
        )
        if username is not None
        else store.get_catalogued_assets(list(vault_paths.values()))
    )
    return [
        (entry, catalogue[vault_paths[str(entry.path)]])
        for entry in entries
        if vault_paths[str(entry.path)] in catalogue
    ]


def _managed_section_entries(section: str, store: VaultMasterStore, username: str):
    """Catalogue-first access to commissioned placements, without path aliases."""
    from app.storage_placement import resolve_metadata_placement
    for candidate in store.list_catalogued_assets_by_vault_path_prefix(f"/vault/{section}/"):
        asset = store.get_visible_catalogued_asset_by_id(candidate.id, username)
        if asset is None or asset.asset_type != section or asset.lifecycle_state != "active" or "storage_placement" not in asset.metadata:
            continue
        if section == "Documents" and Path(asset.filename).suffix.casefold() not in DOCUMENT_LIBRARY_EXTENSIONS:
            continue
        try:
            path = resolve_metadata_placement(asset.metadata)
            if path is None:
                continue
            stat = path.stat()
        except (OSError, ValueError):
            continue  # Never fall back to a stale legacy copy of managed content.
        yield VaultLibraryFile(id=str(asset.id), name=asset.filename,
            relative_path=Path(asset.vault_path.removeprefix(f"/vault/{section}/")),
            path=path, size=stat.st_size,
            modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc), kind=_file_kind(path)), asset


def _managed_section_content(section: str, file_id: str, store: VaultMasterStore, username: str):
    for entry, asset in _managed_section_entries(section, store, username):
        if entry.id == file_id:
            return FileResponse(entry.path, media_type=asset.mime_type, filename=asset.filename,
                content_disposition_type="inline" if entry.path.suffix.casefold() in SAFE_INLINE_EXTENSIONS else "attachment",
                headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
    # UUIDs are canonical managed ids; never resolve them as old path hashes.
    try:
        UUID(file_id)
    except ValueError:
        return None
    raise HTTPException(404, "File was not found")


def _list_library(
    *,
    path: Path,
    label: str,
    api_path: str,
    allowed_extensions: frozenset[str] | None = None,
) -> list[VaultLibraryFileSummary]:
    resolved_path = _require_library_path(path, label)
    return [
        _to_summary(entry, api_path)
        for entry in scan_vault_library(
            resolved_path,
            allowed_extensions=allowed_extensions,
        )
    ]


def _serve_file(
    *,
    path: Path,
    label: str,
    file_id: str,
    allowed_extensions: frozenset[str] | None = None,
) -> FileResponse:
    resolved_path = _require_library_path(path, label)
    entry = _find_file(
        resolved_path,
        file_id,
        allowed_extensions=allowed_extensions,
    )
    media_type = (
        VIDEO_MIME_TYPES.get(entry.path.suffix.casefold())
        or mimetypes.guess_type(entry.name)[0]
        or "application/octet-stream"
    )
    inline = entry.path.suffix.casefold() in SAFE_INLINE_EXTENSIONS

    return FileResponse(
        path=entry.path,
        media_type=media_type,
        filename=entry.name,
        content_disposition_type="inline" if inline else "attachment",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _private_listing_headers(response: Response) -> None:
    response.headers["Cache-Control"] = "private, no-store"


def _owner_home_video_asset(asset_id: UUID, username: str, vault_master_store: VaultMasterStore) -> CataloguedAsset:
    asset = vault_master_store.get_catalogued_asset_by_id(asset_id)
    if (
        asset is None
        or asset.lifecycle_state == "deleted"
        or asset.asset_type != "Home Videos"
        or not asset.vault_path.startswith("/vault/Home Videos/")
        or not asset_is_editable_by(asset, username)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video was not found")
    return asset


def _video_job_response(
    job: VideoAnalysisJob | None,
) -> PersonalVideoAnalysisJobResponse | None:
    if job is None:
        return None
    return PersonalVideoAnalysisJobResponse(
        id=job.id,
        status=job.status,
        requested_reanalysis=job.requested_reanalysis,
        total_frames=job.total_frames,
        frames_completed=job.frames_completed,
        frames_failed=job.frames_failed,
        warning=job.warning,
        error=job.error,
        task_version=job.task_version,
        sampling_version=job.sampling_version,
    )


def _home_video_entries(username, library_path, store, *, include_hidden=False, include_shared=True):
    if include_hidden and not username.hidden_videos_authorized:
        raise HTTPException(403, "Hidden Videos passkey re-authentication is required")
    root = _require_library_path(library_path, "Home Videos")
    pairs = _catalogued_entries(entries=scan_vault_library(root, allowed_extensions=VIDEO_EXTENSIONS),
        library_path=root, vault_root="/vault/Home Videos", store=store, username=username)
    return [(entry, asset) for entry, asset in pairs
            if asset.asset_type == "Home Videos"
            and asset.lifecycle_state == ("hidden" if include_hidden else "active")
            and (include_shared and not include_hidden or asset.owner_user_id == username.user_id)]


@router.get("/personal-videos", response_model=list[VaultLibraryFileSummary])
def list_personal_videos(response: Response, username: AuthenticatedUsername,
    library_path: Path = Depends(get_personal_videos_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    people_store=Depends(get_gallery_people_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store),
    include_hidden: bool = False, include_shared: bool = True,
    person: UUID | None = None, private_tag: UUID | None = None, content_tag: str = "",
    date_from: date | None = None, date_to: date | None = None, location: str = "",
    sort: Literal["newest", "oldest", "name_asc", "name_desc"] = "newest",
) -> list[VaultLibraryFileSummary]:
    _private_listing_headers(response)
    pairs = _home_video_entries(username, library_path, vault_master_store,
        include_hidden=include_hidden, include_shared=include_shared)
    matching_tags = custom_tag_store.matching_asset_ids(username.user_id, (private_tag,)) if private_tag else None
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "The start date must be before the end date")
    intelligence = get_gallery_intelligence_store() if content_tag else None
    if content_tag and content_tag not in {term["slug"] for term in system_terms()}:
        raise HTTPException(422, "Unknown system content tag")
    selected = []
    for entry, asset in pairs:
        if content_tag and not any(term["slug"] == content_tag for term in effective_system_tags(intelligence, asset.id)):
            continue
        if matching_tags is not None and asset.id not in matching_tags:
            continue
        if person and not any(value.person_id == person for value in people_store.effective_people(asset.id, username)):
            continue
        if date_from and (asset.captured_on is None or asset.captured_on < date_from):
            continue
        if date_to and (asset.captured_on is None or asset.captured_on > date_to):
            continue
        if location.strip().casefold() not in (asset.location or "").casefold():
            continue
        selected.append((entry, asset))
    if sort.startswith("name"):
        selected.sort(key=lambda pair: ((pair[1].display_title or pair[0].name).casefold(), str(pair[1].id)), reverse=sort == "name_desc")
    else:
        selected.sort(key=lambda pair: (pair[1].captured_on or date.min, str(pair[1].id)), reverse=sort == "newest")
    return [_to_summary(entry, "personal-videos", asset,
        include_metadata_provenance=asset_is_editable_by(asset, username)) for entry, asset in selected]


@router.get("/personal-videos/filter-options")
def home_video_filter_options(response: Response, username: AuthenticatedUsername,
    library_path: Path = Depends(get_personal_videos_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    people_store=Depends(get_gallery_people_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store), include_hidden: bool = False,
    include_shared: bool = True):
    _private_listing_headers(response)
    pairs = _home_video_entries(username, library_path, vault_master_store,
        include_hidden=include_hidden, include_shared=include_shared)
    people = {}
    for _, asset in pairs:
        for value in people_store.effective_people(asset.id, username):
            people[str(value.person_id)] = {"id": str(value.person_id), "display_name": value.display_name}
    return {"content_tags": system_terms(),
            "people": sorted(people.values(), key=lambda value: value["display_name"].casefold()),
            "private_tags": [{"id": str(tag.id), "display_name": tag.display_name} for tag in custom_tag_store.list(username.user_id)],
            "locations": sorted({asset.location for _, asset in pairs if asset.location})}


@router.get(
    "/personal-videos/{file_id}/content",
    response_class=FileResponse,
)
def get_personal_video_content(
    file_id: str,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_personal_videos_path),
    vault_master_store: VaultMasterStore = Depends(
        get_vault_master_store
    ),
) -> FileResponse:
    resolved_path = _require_library_path(
        library_path,
        "Personal Videos",
    )
    entry = _find_file(
        resolved_path,
        file_id,
        allowed_extensions=VIDEO_EXTENSIONS,
    )
    vault_path = (
        "/vault/Home Videos/"
        f"{entry.path.relative_to(resolved_path).as_posix()}"
    )
    content_asset = vault_master_store.get_visible_catalogued_assets([vault_path], username).get(vault_path)
    if content_asset is None or content_asset.asset_type != "Home Videos":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Video is not catalogued by Vault Master",
        )
    return _serve_file(
        path=resolved_path,
        label="Personal Videos",
        file_id=file_id,
        allowed_extensions=VIDEO_EXTENSIONS,
    )


class HomeVideoPlaybackResponse(BaseModel):
    status: Literal["direct", "ready", "preparing", "failed"]
    playback_url: str | None = None


def _authorized_playback_video(
    file_id: str, username: str, library_path: Path, store: VaultMasterStore,
) -> tuple[VaultLibraryFile, CataloguedAsset]:
    root = _require_library_path(library_path, "Personal Videos")
    entry = _find_file(root, file_id, allowed_extensions=VIDEO_EXTENSIONS)
    vault_path = "/vault/Home Videos/" + entry.relative_path.as_posix()
    asset = store.get_visible_catalogued_assets([vault_path], username).get(vault_path)
    if asset is None or asset.asset_type != "Home Videos":
        raise HTTPException(status_code=404, detail="Video was not found")
    return entry, asset


@router.get("/personal-videos/{file_id}/playback", response_model=HomeVideoPlaybackResponse)
def get_home_video_playback(
    file_id: str,
    response: Response,
    username: AuthenticatedUsername,
    retry: bool = False,
    library_path: Path = Depends(get_personal_videos_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    cache_root: Path = Depends(get_home_video_playback_cache_path),
) -> HomeVideoPlaybackResponse:
    _private_listing_headers(response)
    entry, asset = _authorized_playback_video(file_id, username, library_path, vault_master_store)
    state = playback_status(entry.path, asset.id, asset.sha256, cache_root, retry=retry)
    return HomeVideoPlaybackResponse(
        status=state,
        playback_url=(f"/api/personal-videos/{file_id}/playback/content"
                      if state in {"direct", "ready"} else None),
    )


@router.get("/personal-videos/{file_id}/playback/content", response_class=FileResponse)
def get_home_video_playback_content(
    file_id: str,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_personal_videos_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    cache_root: Path = Depends(get_home_video_playback_cache_path),
) -> FileResponse:
    entry, asset = _authorized_playback_video(file_id, username, library_path, vault_master_store)
    media = playback_file(entry.path, asset.id, asset.sha256, cache_root)
    if media is None:
        raise HTTPException(status_code=409, detail="Playback is not ready",
                            headers={"Cache-Control": "private, no-store"})
    return FileResponse(media, media_type="video/mp4", headers={
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
    })


@router.get("/personal-videos/{file_id}/thumbnail", response_class=FileResponse)
def get_personal_video_thumbnail(
    file_id: str,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_personal_videos_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    cache_root: Path = Depends(get_video_thumbnail_cache_path),
) -> FileResponse:
    resolved_path = _require_library_path(library_path, "Personal Videos")
    entry = _find_file(resolved_path, file_id, allowed_extensions=VIDEO_EXTENSIONS)
    vault_path = "/vault/Home Videos/" + entry.relative_path.as_posix()
    asset = vault_master_store.get_visible_catalogued_assets(
        [vault_path], username
    ).get(vault_path)
    if asset is None or asset.asset_type != "Home Videos":
        raise HTTPException(status_code=404, detail="Video was not found")
    thumbnail = video_thumbnail(entry.path, asset.id, cache_root)
    if thumbnail is None:
        raise HTTPException(
            status_code=503, detail="Video thumbnail is unavailable",
            headers={"Cache-Control": "private, no-store"},
        )
    return FileResponse(thumbnail, media_type="image/jpeg", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
    })


@router.get(
    "/personal-videos/{file_id}/details",
    response_model=PersonalVideoDetails,
)
def get_personal_video_details(
    file_id: str,
    response: Response,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_personal_videos_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> PersonalVideoDetails:
    """Owner-only intelligence state keyed by the canonical asset UUID."""
    _private_listing_headers(response)
    resolved_path = _require_library_path(library_path, "Personal Videos")
    entry = _find_file(
        resolved_path, file_id, allowed_extensions=VIDEO_EXTENSIONS
    )
    vault_path = "/vault/Home Videos/" + entry.path.relative_to(
        resolved_path
    ).as_posix()
    asset = vault_master_store.get_catalogued_assets([vault_path]).get(
        vault_path
    )
    if asset is None or asset.asset_type != "Home Videos" or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video was not found")
    video_store = get_video_intelligence_store()
    reconciliation = video_store.latest_reconciliation(asset.id)
    from app.home_video_ken import description_for
    narrative, narrative_source = description_for(
        asset, reconciliation.generated_narrative if reconciliation else None)
    content_tags = effective_system_tags(get_gallery_intelligence_store(), asset.id)
    people = [
        {"id": str(value.person_id), "display_name": value.display_name, "source": value.source}
        for value in get_gallery_people_store().effective_people(asset.id, username)
    ]
    return PersonalVideoDetails(
        file_id=file_id,
        asset_id=asset.id,
        name=entry.name,
        display_title=asset.display_title,
        analysis=_video_job_response(video_store.latest_job(asset.id)),
        narrative=narrative,
        people=people,
        content_tags=content_tags,
        captured_on=asset.captured_on,
        warnings=list(reconciliation.warnings) if reconciliation else [],
        narrative_source=narrative_source,
    )


@router.post(
    "/personal-videos/intelligence/jobs",
    response_model=PersonalVideoAnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def queue_personal_video_analysis(
    request: PersonalVideoAnalysisRequest,
    response: Response,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> PersonalVideoAnalysisJobResponse:
    """Queue one owner-selected Home Video; V1 intentionally does not run it."""
    _private_listing_headers(response)
    asset = _owner_home_video_asset(request.asset_id, username, vault_master_store)
    job = get_video_intelligence_store().queue(
        asset.id, str(username), asset.owner_user_id, reanalyse=request.reanalyse
    )
    result = _video_job_response(job)
    assert result is not None
    return result


@router.patch("/personal-videos/intelligence/{asset_id}/narrative")
def edit_personal_video_narrative(
    asset_id: UUID,
    request: PersonalVideoNarrativeEdit,
    response: Response,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> dict[str, object]:
    """Persist an owner narrative override without replacing VM provenance."""
    _private_listing_headers(response)
    asset = _owner_home_video_asset(asset_id, username, vault_master_store)
    narrative = request.narrative.strip() if request.narrative else None
    updated = vault_master_store.update_catalogued_asset_metadata(asset.id, {"video_narrative": narrative}, username)
    if updated is None:
        raise HTTPException(404, 'Video was not found')
    from app.home_video_ken import description_for
    fallback = get_video_intelligence_store().latest_reconciliation(asset.id)
    resolved, source = description_for(updated, fallback.generated_narrative if fallback else None)
    return {"asset_id": str(asset.id), "narrative": resolved, "narrative_source": source}


@router.patch("/personal-videos/intelligence/{asset_id}/people", response_model=list[dict[str, object]])
def decide_personal_video_person(
    asset_id: UUID,
    request: PersonalVideoPersonDecision,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> list[dict[str, object]]:
    asset = _owner_home_video_asset(asset_id, username, vault_master_store)
    people_store = get_gallery_people_store()
    try:
        people_store.decide(asset.id, request.person_id, request.decision, username, source="video_presence")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return [
        {"id": str(value.person_id), "display_name": value.display_name, "source": value.source}
        for value in people_store.effective_people(asset.id, username)
    ]


@router.patch("/personal-videos/intelligence/{asset_id}/tags", response_model=list[dict[str, object]])
def decide_personal_video_tag(
    asset_id: UUID,
    request: PersonalVideoTagDecision,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> list[dict[str, object]]:
    asset = _owner_home_video_asset(asset_id, username, vault_master_store)
    intelligence_store = get_gallery_intelligence_store()
    if request.slug not in {term["slug"] for term in system_terms()}:
        raise HTTPException(422, "Unknown system content tag")
    try:
        intelligence_store.decide(asset.id, request.namespace, request.slug, request.decision, str(asset.owner_user_id))
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return effective_system_tags(intelligence_store, asset.id)


def _visible_home_video_asset(asset_id, username, store):
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if (asset is None or asset.asset_type != "Home Videos"
            or not asset.vault_path.startswith("/vault/Home Videos/")):
        raise HTTPException(404, "Video was not found")
    return asset


def _private_video_tags(tags, username, asset_id):
    return [{"id": str(tag.id), "display_name": tag.display_name, "slug": tag.slug}
            for tag in tags.for_asset(username.user_id, asset_id)]


@router.get("/personal-videos/assets/{asset_id}/private-tags")
def personal_video_private_tags(asset_id: UUID, response: Response, username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store)):
    _private_listing_headers(response)
    asset = _visible_home_video_asset(asset_id, username, vault_master_store)
    return _private_video_tags(custom_tag_store, username, asset.id)


# Keep existing callers safe: the old custom-create URL now creates only a
# current-user private annotation, never a term in the shared vocabulary.
@router.post('/personal-videos/intelligence/{asset_id}/tags', status_code=201)
@router.post('/personal-videos/assets/{asset_id}/private-tags', status_code=201)
def create_personal_video_custom_tag(asset_id: UUID, request: PersonalVideoCustomTag,
    response: Response, username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store)):
    _private_listing_headers(response)
    asset = _visible_home_video_asset(asset_id, username, vault_master_store)
    try:
        tag = custom_tag_store.create(username.user_id, request.display_name)
        if tag.id not in {value.id for value in custom_tag_store.for_asset(username.user_id, asset.id)}:
            custom_tag_store.assign(username.user_id, tag.id, asset.id)
        return {"id": str(tag.id), "slug": tag.slug, "display_name": tag.display_name}
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.put('/personal-videos/assets/{asset_id}/private-tags/{tag_id}', status_code=204)
def assign_personal_video_private_tag(asset_id: UUID, tag_id: UUID, username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store)):
    asset = _visible_home_video_asset(asset_id, username, vault_master_store)
    try:
        if tag_id not in {tag.id for tag in custom_tag_store.for_asset(username.user_id, asset.id)}:
            custom_tag_store.assign(username.user_id, tag_id, asset.id)
    except ValueError as error:
        raise HTTPException(404, "Private tag was not found") from error
    return Response(status_code=204)


@router.delete('/personal-videos/assets/{asset_id}/private-tags/{tag_id}', status_code=204)
def unassign_personal_video_private_tag(asset_id: UUID, tag_id: UUID, username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store)):
    asset = _visible_home_video_asset(asset_id, username, vault_master_store)
    custom_tag_store.unassign(username.user_id, tag_id, asset.id)
    return Response(status_code=204)


@router.get("/personal-videos/intelligence/terms", response_model=list[dict[str, object]])
def list_personal_video_intelligence_terms(username: AuthenticatedUsername):
    return system_terms()



@router.post(
    "/personal-videos/intelligence/reconcile",
    response_model=PersonalVideoAnalysisJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def reconcile_personal_video_analysis(
    request: PersonalVideoAnalysisRequest,
    response: Response,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> PersonalVideoAnalysisJobResponse:
    """Explicitly reconcile retained V2 evidence without rerunning specialists."""
    _private_listing_headers(response)
    asset = _owner_home_video_asset(request.asset_id, username, vault_master_store)
    video_store = get_video_intelligence_store()
    job = video_store.latest_job(asset.id)
    if job is None or job.status not in {"analysis_complete", "completed", "completed_with_warnings"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Video specialist analysis is not ready for reconciliation")
    reconcile_video_analysis_job(
        video_store, vault_master_store, get_gallery_people_store(),
        get_gallery_intelligence_store(), job.id, refresh=True,
    )
    result = _video_job_response(video_store.latest_job(asset.id))
    assert result is not None
    return result


@router.get(
    "/documents",
    response_model=list[VaultLibraryFileSummary],
)
def list_documents(
    response: Response,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_documents_path),
    vault_master_store: VaultMasterStore = Depends(
        get_vault_master_store
    ),
) -> list[VaultLibraryFileSummary]:
    _private_listing_headers(response)
    resolved_path = _require_library_path(library_path, "Documents")
    entries = scan_vault_library(
        resolved_path,
        allowed_extensions=DOCUMENT_LIBRARY_EXTENSIONS,
    )
    return [
        _to_summary(
            entry,
            "documents",
            asset,
            include_metadata_provenance=asset_is_editable_by(
                asset, username
            ),
        )
        for entry, asset in _catalogued_entries(
            entries=entries,
            library_path=resolved_path,
            vault_root="/vault/Documents",
            store=vault_master_store,
            username=username,
        )
        if "storage_placement" not in asset.metadata
    ] + [_to_summary(entry, "documents", asset, include_metadata_provenance=asset_is_editable_by(asset, username))
         for entry, asset in _managed_section_entries("Documents", vault_master_store, username)]


@router.get(
    "/documents/{file_id}/content",
    response_class=FileResponse,
)
def get_document_content(
    file_id: str,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_documents_path),
    vault_master_store: VaultMasterStore = Depends(
        get_vault_master_store
    ),
) -> FileResponse:
    managed = _managed_section_content("Documents", file_id, vault_master_store, username)
    if managed is not None:
        return managed
    resolved_path = _require_library_path(library_path, "Documents")
    entry = _find_file(
        resolved_path,
        file_id,
        allowed_extensions=DOCUMENT_LIBRARY_EXTENSIONS,
    )
    vault_path = (
        "/vault/Documents/"
        f"{entry.path.relative_to(resolved_path).as_posix()}"
    )
    visible = vault_master_store.get_visible_catalogued_assets([vault_path], username)
    if vault_path not in visible or "storage_placement" in visible[vault_path].metadata:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document is not catalogued by Vault Master",
        )
    return _serve_file(
        path=resolved_path,
        label="Documents",
        file_id=file_id,
        allowed_extensions=DOCUMENT_LIBRARY_EXTENSIONS,
    )


@router.get(
    "/archives",
    response_model=list[VaultLibraryFileSummary],
)
def list_archives(
    response: Response,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_archives_path),
    vault_master_store: VaultMasterStore = Depends(
        get_vault_master_store
    ),
) -> list[VaultLibraryFileSummary]:
    _private_listing_headers(response)
    resolved_path = _require_library_path(library_path, "Archives")
    entries = scan_vault_library(resolved_path)
    return [
        _to_summary(
            entry,
            "archives",
            asset,
            include_metadata_provenance=asset_is_editable_by(
                asset, username
            ),
        )
        for entry, asset in _catalogued_entries(
            entries=entries,
            library_path=resolved_path,
            vault_root="/vault/Archives",
            store=vault_master_store,
            username=username,
        )
        if "storage_placement" not in asset.metadata
    ] + [_to_summary(entry, "archives", asset, include_metadata_provenance=asset_is_editable_by(asset, username))
         for entry, asset in _managed_section_entries("Archives", vault_master_store, username)]


@router.get(
    "/archives/{file_id}/content",
    response_class=FileResponse,
)
def get_archive_content(
    file_id: str,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_archives_path),
    vault_master_store: VaultMasterStore = Depends(
        get_vault_master_store
    ),
) -> FileResponse:
    managed = _managed_section_content("Archives", file_id, vault_master_store, username)
    if managed is not None:
        return managed
    resolved_path = _require_library_path(library_path, "Archives")
    entry = _find_file(resolved_path, file_id)
    vault_path = (
        "/vault/Archives/"
        f"{entry.path.relative_to(resolved_path).as_posix()}"
    )
    visible = vault_master_store.get_visible_catalogued_assets([vault_path], username)
    if vault_path not in visible or "storage_placement" in visible[vault_path].metadata:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Archive file is not catalogued by Vault Master",
        )
    return _serve_file(
        path=resolved_path,
        label="Archives",
        file_id=file_id,
    )
