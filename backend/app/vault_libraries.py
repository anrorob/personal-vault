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

VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".webm"}
)
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
    | VIDEO_EXTENSIONS
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
    asset_id: UUID
    name: str
    display_title: str | None
    analysis: PersonalVideoAnalysisJobResponse | None = None
    narrative: str | None = None
    people: list[dict[str, object]] = Field(default_factory=list)
    content_tags: list[dict[str, object]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    narrative_source: Literal["user", "vault_master", "none"] = "none"


class PersonalVideoAnalysisRequest(BaseModel):
    asset_id: UUID
    reanalyse: bool = False


class PersonalVideoNarrativeEdit(BaseModel):
    narrative: str | None = Field(default=None, max_length=4_000)


class PersonalVideoPersonDecision(BaseModel):
    person_id: UUID
    decision: Literal["include", "exclude"]


class PersonalVideoTagDecision(BaseModel):
    namespace: Literal["content_tag"]
    slug: str
    decision: Literal["include", "exclude"]


def get_personal_videos_path() -> Path:
    return Path(os.getenv("PV_PERSONAL_VIDEOS_PATH", "/media/personal-videos"))


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
        name=entry.name,
        directory=None if parent == "." else parent,
        size=entry.size,
        modified_at=entry.modified_at,
        kind=entry.kind,
        opens_inline=(
            entry.path.suffix.casefold() in SAFE_INLINE_EXTENSIONS
        ),
        open_url=f"/api/{api_path}/{entry.id}/content",
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
        mimetypes.guess_type(entry.name)[0]
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


@router.get(
    "/personal-videos",
    response_model=list[VaultLibraryFileSummary],
)
def list_personal_videos(
    response: Response,
    username: AuthenticatedUsername,
    library_path: Path = Depends(get_personal_videos_path),
    vault_master_store: VaultMasterStore = Depends(
        get_vault_master_store
    ),
) -> list[VaultLibraryFileSummary]:
    _private_listing_headers(response)
    resolved_path = _require_library_path(
        library_path,
        "Personal Videos",
    )
    entries = scan_vault_library(
        resolved_path,
        allowed_extensions=VIDEO_EXTENSIONS,
    )
    return [
        _to_summary(
            entry,
            "personal-videos",
            asset,
            include_metadata_provenance=asset_is_editable_by(
                asset, username
            ),
        )
        for entry, asset in _catalogued_entries(
            entries=entries,
            library_path=resolved_path,
            vault_root="/vault/Home Videos",
            store=vault_master_store,
            username=username,
        )
    ]


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
    if not vault_master_store.get_visible_catalogued_assets(
        [vault_path], username
    ):
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
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video was not found")
    video_store = get_video_intelligence_store()
    reconciliation = video_store.latest_reconciliation(asset.id)
    user_narrative = next(
        (
            value for key in ("video_narrative", "narrative")
            if isinstance((value := asset.user_overrides.get(key)), str) and value.strip()
        ),
        None,
    )
    terms = get_gallery_intelligence_store().effective(asset.id)
    content_tags = [
        {
            "namespace": value["namespace"] if isinstance(value, dict) else value.namespace,
            "slug": value["slug"] if isinstance(value, dict) else value.slug,
            "display_name": value["display_name"] if isinstance(value, dict) else value.display_name,
        }
        for value in terms
        if (value["namespace"] if isinstance(value, dict) else value.namespace) == "content_tag"
    ]
    people = [
        {"id": str(value.person_id), "display_name": value.display_name, "source": value.source}
        for value in get_gallery_people_store().effective_people(asset.id, username)
    ]
    narrative_source: Literal["user", "vault_master", "none"] = (
        "user" if user_narrative else "vault_master" if reconciliation and reconciliation.generated_narrative else "none"
    )
    return PersonalVideoDetails(
        asset_id=asset.id,
        name=entry.name,
        display_title=asset.display_title,
        analysis=_video_job_response(video_store.latest_job(asset.id)),
        narrative=user_narrative or (reconciliation.generated_narrative if reconciliation else None),
        people=people,
        content_tags=content_tags,
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
    asset = next(
        (
            candidate
            for candidate in vault_master_store.list_owned_catalogued_assets(
                username
            )
            if candidate.id == request.asset_id
        ),
        None,
    )
    if (
        asset is None
        or asset.asset_type != "Home Videos"
        or not asset.vault_path.startswith("/vault/Home Videos/")
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video was not found")
    if asset.owner_user_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Video owner is unavailable")
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
    vault_master_store.update_catalogued_asset_metadata(asset.id, {"video_narrative": narrative}, username)
    return {"asset_id": str(asset.id), "narrative": narrative, "narrative_source": "user" if narrative else "vault_master"}


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
    try:
        intelligence_store.decide(asset.id, request.namespace, request.slug, request.decision, username)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return [
        value if isinstance(value, dict) else {
            "namespace": value.namespace, "slug": value.slug,
            "display_name": value.display_name, "source": value.source,
        }
        for value in intelligence_store.effective(asset.id)
        if (value["namespace"] if isinstance(value, dict) else value.namespace) == "content_tag"
    ]


@router.get("/personal-videos/intelligence/terms", response_model=list[dict[str, object]])
def list_personal_video_intelligence_terms(
    username: AuthenticatedUsername,
) -> list[dict[str, object]]:
    """Controlled shared vocabulary; V4 exposes only Content tags."""
    return [
        value for value in get_gallery_intelligence_store().list_terms()
        if value["namespace"] == "content_tag"
    ]


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
    asset = next(
        (
            candidate
            for candidate in vault_master_store.list_owned_catalogued_assets(username)
            if candidate.id == request.asset_id
            and candidate.asset_type == "Home Videos"
            and candidate.vault_path.startswith("/vault/Home Videos/")
        ),
        None,
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Video was not found")
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
    ]


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
    if not vault_master_store.get_visible_catalogued_assets(
        [vault_path], username
    ):
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
    ]


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
    resolved_path = _require_library_path(library_path, "Archives")
    entry = _find_file(resolved_path, file_id)
    vault_path = (
        "/vault/Archives/"
        f"{entry.path.relative_to(resolved_path).as_posix()}"
    )
    if not vault_master_store.get_visible_catalogued_assets(
        [vault_path], username
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Archive file is not catalogued by Vault Master",
        )
    return _serve_file(
        path=resolved_path,
        label="Archives",
        file_id=file_id,
    )
