from dataclasses import dataclass
from datetime import date, datetime, timezone
import base64
import binascii
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.auth import AuthenticatedAdministrator, AuthenticatedUsername, SESSION_COOKIE_NAME, get_authentication_store
from app.auth_store import AuthenticationStore
from app.config import get_database_conninfo
from app.share_grants import PostgresShareGrantStore
from app.gallery_intelligence import (
    get_gallery_intelligence_store,
    gallery_analysis_catchup_candidates,
    queue_gallery_analysis_catchup,
    queue_published_gallery_assets,
)
from app.gallery_florence import get_gallery_florence_store
from app.gallery_people import FaceDetection, VaultPerson, get_gallery_people_store
from app.gallery_taxonomy import GALLERY_TERM_NAMES, gallery_taxonomy_terms
from app.gallery_thumbnails import gallery_thumbnail, get_gallery_thumbnail_cache_path
from app.gallery_custom_tags import CustomTag, get_gallery_custom_tag_store
from app.vault_master import (
    CataloguedAsset,
    VaultMasterStore,
    asset_is_editable_by,
    get_vault_master_store,
)
from app.vault_master_ingestion_ai import get_ingestion_ai_store, render_gallery_pdf_preview
from app.storage_placement import resolve_metadata_placement


router = APIRouter(prefix="/api/gallery", tags=["gallery"])
@dataclass(frozen=True)
class GalleryImage:
    id: str
    name: str
    path: Path
    size: int
    added_at: datetime
    vault_path: str | None = None


class GalleryImageSummary(BaseModel):
    id: str
    asset_id: UUID | None = None
    name: str
    size: int
    added_at: datetime
    captured_on: date | None
    captured_at: str | None = None
    date_source: str
    location: str | None
    display_title: str | None
    description: str | None = None
    thumbnail_url: str
    media_type: str
    content_type: str
    photo_display: bool
    warning: str | None = None
    owner_display_name: str | None = None


GALLERY_PAGE_SIZE = 60


class GalleryPage(BaseModel):
    items: list[GalleryImageSummary]
    next_cursor: str | None
    previous_cursor: str | None = None
    seek_anchor_id: str | None = None


class GalleryImageDetails(GalleryImageSummary):
    can_edit: bool
    asset_id: UUID | None = None
    lifecycle_state: Literal["active", "hidden"] | None = None
    vault_path: str | None = None
    mime_type: str | None = None
    sha256: str | None = None
    metadata_provenance: dict[str, str] | None = None
    image_url: str
    previous_id: str | None
    next_id: str | None
    intelligence: list["GalleryIntelligenceTerm"] = []
    intelligence_provenance: list["GalleryIntelligenceOwnerTerm"] | None = None
    people: list["GalleryAssetPerson"] | None = None
    origin_people: list["GalleryPerson"] | None = None
    local_annotation: "GalleryLocalAnnotation | None" = None
    unknown_people_count: int | None = None
    unresolved_person_presence: bool | None = None
    face_detections: list["GalleryFaceDetection"] | None = None
    custom_tags: list["GalleryCustomTag"] = []


class GalleryIntelligenceTerm(BaseModel):
    namespace: Literal["photo_type", "content_tag"]
    slug: str
    display_name: str


class GalleryCustomTag(BaseModel):
    id: UUID
    slug: str
    display_name: str


class GalleryCustomTagCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=64)


class GallerySharedPreference(BaseModel):
    include_shared_photos: bool = False


class GalleryCollectionPreference(BaseModel):
    included: bool = False


class GallerySharedCollectionPreference(GalleryCollectionPreference):
    collection_id: UUID
    name: str
    owner_display_name: str


class GalleryIntelligenceOwnerTerm(GalleryIntelligenceTerm):
    source: str


class GalleryIntelligenceDecision(BaseModel):
    namespace: Literal["photo_type", "content_tag"]
    slug: str
    decision: Literal["include", "exclude"]


class GalleryIntelligenceJobResponse(BaseModel):
    id: UUID
    status: str
    error: str | None = None


class GalleryIntelligenceJobStatusResponse(BaseModel):
    job: GalleryIntelligenceJobResponse | None


class GalleryIntelligenceBulkRunResponse(BaseModel):
    id: UUID
    total: int
    completed: int
    processing: int
    queued: int
    failed: int


class GalleryIntelligenceBackfillResponse(BaseModel):
    queued: int
    limit: int
    reanalyse: int
    run: GalleryIntelligenceBulkRunResponse | None = None


class GalleryIntelligenceBackfillStatus(BaseModel):
    eligible_count: int
    run: GalleryIntelligenceBulkRunResponse | None = None


class GalleryPerson(BaseModel):
    id: UUID
    display_name: str
    active: bool


class GalleryAssetPerson(GalleryPerson):
    source: str


class GalleryLocalAnnotation(BaseModel):
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    people: list[GalleryPerson] = Field(default_factory=list)


class GalleryLocalAnnotationEdit(BaseModel):
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    person_ids: list[UUID] = Field(default_factory=list)


class GalleryFaceDetection(BaseModel):
    id: UUID
    bounding_box: dict[str, float]
    person_id: UUID | None = None
    person_name: str | None = None
    user_confirmed: bool = False


class GalleryPersonCreate(BaseModel):
    display_name: str


class GalleryPersonUpdate(BaseModel):
    display_name: str | None = None
    active: bool | None = None


class GalleryAssetPersonDecision(BaseModel):
    person_id: UUID
    decision: Literal["include", "exclude"]
    face_detection_id: UUID | None = None


def get_gallery_path() -> Path:
    return Path(os.getenv("PV_GALLERY_PATH", "/media/gallery"))


def require_gallery_path(
    gallery_path: Path = Depends(get_gallery_path),
) -> Path:
    try:
        resolved_path = gallery_path.resolve(strict=True)
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gallery storage is unavailable",
        ) from error

    if not resolved_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gallery storage is unavailable",
        )

    return resolved_path


def get_image_id(relative_path: Path) -> str:
    return hashlib.sha256(
        relative_path.as_posix().encode("utf-8")
    ).hexdigest()[:20]


def scan_gallery(
    gallery_path: Path,
    store: VaultMasterStore | None = None,
    catalogued_assets: dict[str, CataloguedAsset] | None = None,
) -> list[GalleryImage]:
    images: list[GalleryImage] = []

    try:
        candidates = gallery_path.rglob("*")
        for candidate in candidates:
            if candidate.is_symlink() or not candidate.is_file():
                continue

            relative_path = candidate.relative_to(gallery_path)
            file_stat = candidate.stat()
            added_at = datetime.fromtimestamp(
                file_stat.st_mtime,
                tz=timezone.utc,
            )
            images.append(
                GalleryImage(
                    id=get_image_id(relative_path),
                    name=candidate.name,
                    path=candidate,
                    size=file_stat.st_size,
                    added_at=added_at,
                )
            )
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gallery storage is unavailable",
        ) from error

    if store is not None:
        for asset in store.list_catalogued_assets_by_vault_path_prefix("/vault/Gallery/"):
            if catalogued_assets is not None:
                catalogued_assets[asset.vault_path] = asset
            if asset.asset_type.casefold() != "gallery" or "storage_placement" not in asset.metadata:
                continue
            relative = asset.vault_path.removeprefix("/vault/Gallery/")
            parts = PurePosixPath(relative).parts
            if (not parts or any(part in {"", ".", ".."} for part in parts)
                    or PurePosixPath(*parts).as_posix() != relative):
                continue
            try:
                path = resolve_metadata_placement(asset.metadata)
                if path is None:
                    continue
                file_stat = path.stat()
            except (OSError, ValueError):
                continue
            images.append(GalleryImage(
                id=get_image_id(Path(*parts)), name=path.name, path=path,
                size=file_stat.st_size,
                added_at=datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.utc),
                vault_path=asset.vault_path,
            ))

    images.sort(key=lambda image: image.name.casefold())
    return images


def find_gallery_image(
    gallery_path: Path,
    image_id: str,
    store: VaultMasterStore | None = None,
) -> tuple[list[GalleryImage], int]:
    images = scan_gallery(gallery_path, store)

    for index, image in enumerate(images):
        if image.id == image_id:
            return images, index

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Photo was not found",
    )


def find_gallery_content_image(
    gallery_path: Path,
    image_id: str,
    store: VaultMasterStore,
) -> GalleryImage:
    """Resolve one image without loading every catalogue record and placement."""
    matches: list[GalleryImage] = []
    try:
        for candidate in gallery_path.rglob("*"):
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative_path = candidate.relative_to(gallery_path)
            file_stat = candidate.stat()
            if get_image_id(relative_path) == image_id:
                matches.append(GalleryImage(
                    id=image_id, name=candidate.name, path=candidate,
                    size=file_stat.st_size,
                    added_at=datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.utc),
                ))
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Gallery storage is unavailable",
        ) from error

    for entry in store.list_catalogued_placement_candidates_by_vault_path_prefix(
        "/vault/Gallery/"
    ):
        if entry.asset_type.casefold() != "gallery" or not entry.has_storage_placement:
            continue
        relative = entry.vault_path.removeprefix("/vault/Gallery/")
        parts = PurePosixPath(relative).parts
        if (not parts or any(part in {"", ".", ".."} for part in parts)
                or PurePosixPath(*parts).as_posix() != relative):
            continue
        if get_image_id(Path(*parts)) != image_id:
            continue
        try:
            path = resolve_metadata_placement(
                {"storage_placement": entry.storage_placement}
            )
            if path is None:
                continue
            file_stat = path.stat()
        except (OSError, ValueError):
            continue
        matches.append(GalleryImage(
            id=image_id, name=path.name, path=path,
            size=file_stat.st_size,
            added_at=datetime.fromtimestamp(file_stat.st_mtime, tz=timezone.utc),
            vault_path=entry.vault_path,
        ))

    if not matches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Photo was not found",
        )
    return min(matches, key=lambda image: image.name.casefold())


def to_summary(
    image: GalleryImage,
    asset: CataloguedAsset,
    include_asset_id: bool = False,
    owner_display_name: str | None = None,
) -> GalleryImageSummary:
    evidence = asset.effective_metadata.get("ingestion_evidence")
    content_type = (
        str(evidence.get("content_type"))
        if isinstance(evidence, dict) and isinstance(evidence.get("content_type"), str)
        else "unknown"
    )
    mime_type = asset.mime_type
    # Placement in the permanent Gallery is the owner's confirmation that the
    # item should be presented as a photograph, including scan-backed PDFs.
    photo_display = True
    captured_at = asset.effective_metadata.get("captured_at")
    # A corrected canonical date is authoritative. Do not pair it with a raw
    # timestamp whose date would contradict the owner's effective correction.
    if (
        not isinstance(captured_at, str)
        or asset.captured_on is None
        or not captured_at.startswith(asset.captured_on.isoformat())
    ):
        captured_at = None
    description = asset.effective_metadata.get("description")
    return GalleryImageSummary(
        id=image.id,
        asset_id=asset.id if include_asset_id else None,
        name=image.name,
        size=image.size,
        added_at=image.added_at,
        captured_on=asset.captured_on,
        captured_at=captured_at,
        date_source=asset.metadata_provenance.get(
            "captured_on",
            "unavailable",
        ),
        location=asset.location,
        display_title=asset.display_title,
        description=description if isinstance(description, str) and description.strip() else None,
        thumbnail_url=f"/api/gallery/assets/{asset.id}/preview",
        media_type=mime_type,
        content_type=content_type,
        photo_display=photo_display,
        warning=None,
        owner_display_name=owner_display_name,
    )


def get_gallery_metadata(
    vault_master_store: VaultMasterStore,
    gallery_path: Path,
    images: list[GalleryImage],
    username: str,
    catalogued_assets: dict[str, CataloguedAsset] | None = None,
) -> dict[str, CataloguedAsset]:
    vault_paths = {
        str(image.path): (
            image.vault_path or
            f"/vault/Gallery/{image.path.relative_to(gallery_path).as_posix()}"
        )
        for image in images
    }
    if catalogued_assets is None:
        catalogue = vault_master_store.get_visible_catalogued_assets(
            list(vault_paths.values()), username
        )
    else:
        requested_paths = set(vault_paths.values())
        loaded = {
            path: asset for path, asset in catalogued_assets.items()
            if path in requested_paths
        }
        missing = requested_paths.difference(loaded)
        catalogue = vault_master_store.filter_visible_catalogued_assets(
            loaded, username
        )
        if missing:
            catalogue.update(vault_master_store.get_visible_catalogued_assets(
                list(missing), username
            ))
    metadata: dict[str, CataloguedAsset] = {}
    for image in images:
        asset = catalogue.get(vault_paths[str(image.path)])
        if asset is not None:
            metadata[str(image.path)] = asset
    return metadata


def filter_gallery_lifecycle(
    metadata: dict[str, CataloguedAsset],
    username: AuthenticatedUsername,
    include_hidden: bool,
    hidden_authorized: bool = False,
) -> dict[str, CataloguedAsset]:
    """Keep Hidden assets out of normal Gallery presentation.

    Only the immutable owner can explicitly include their own Hidden content;
    shared recipients never inherit that management view.
    """
    return {
        path: asset
        for path, asset in metadata.items()
        if (asset.lifecycle_state == "hidden" and include_hidden and hidden_authorized and asset_is_editable_by(asset, username))
        or (asset.lifecycle_state == "active" and not include_hidden)
    }


def require_hidden_photo_authorization(request: Request, username: AuthenticatedUsername, store: AuthenticationStore) -> None:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token or not store.has_hidden_photos_authorization(token, username.user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Hidden Photos passkey re-authentication is required")


def deny_hidden_photo_without_authorization(asset: CataloguedAsset, request: Request, username: AuthenticatedUsername, store: AuthenticationStore) -> None:
    if asset.lifecycle_state != "hidden":
        return
    if not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo was not found")
    require_hidden_photo_authorization(request, username, store)


GallerySortOrder = Literal["newest", "oldest"]


def get_catalogued_images(
    images: list[GalleryImage],
    metadata: dict[str, CataloguedAsset],
    sort_order: GallerySortOrder,
) -> list[GalleryImage]:
    """Order Gallery records by their published canonical capture date.

    Undated assets remain visible after dated assets and use their Vault path
    as a deterministic fallback. The same order drives both the Gallery grid
    and next/previous navigation in the image viewer.
    """

    catalogued_images = [
        image for image in images if str(image.path) in metadata
    ]

    def order_key(image: GalleryImage) -> tuple[bool, int, str, str]:
        captured_on = metadata[str(image.path)].captured_on
        if captured_on is None:
            return (True, 0, image.name.casefold(), str(image.path))

        ordinal = captured_on.toordinal()
        return (
            False,
            -ordinal if sort_order == "newest" else ordinal,
            image.name.casefold(),
            str(image.path),
        )

    return sorted(catalogued_images, key=order_key)


def filter_gallery_dates(metadata: dict[str, CataloguedAsset], date_from: date | None, date_to: date | None) -> dict[str, CataloguedAsset]:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="Date range start must not follow its end")
    if date_from is None and date_to is None:
        return metadata
    return {path: asset for path, asset in metadata.items() if asset.captured_on is not None
            and (date_from is None or asset.captured_on >= date_from)
            and (date_to is None or asset.captured_on <= date_to)}


def intelligence_terms_for_asset(store, asset_id: UUID) -> list[GalleryIntelligenceTerm]:
    return [
        GalleryIntelligenceTerm(
            namespace=str(term.namespace if hasattr(term, "namespace") else term["namespace"]),
            slug=str(term.slug if hasattr(term, "slug") else term["slug"]),
            display_name=GALLERY_TERM_NAMES[(term.namespace, term.slug) if hasattr(term, "namespace") else (term["namespace"], term["slug"])],
        )
        for term in store.effective(asset_id)
        if ((term.namespace, term.slug) if hasattr(term, "namespace") else (term["namespace"], term["slug"])) in GALLERY_TERM_NAMES
    ]


def intelligence_owner_terms_for_asset(store, asset_id: UUID) -> list[GalleryIntelligenceOwnerTerm]:
    values: list[GalleryIntelligenceOwnerTerm] = []
    for term in store.effective(asset_id):
        if ((term.namespace, term.slug) if hasattr(term, "namespace") else (term["namespace"], term["slug"])) not in GALLERY_TERM_NAMES:
            continue
        source = term.source if hasattr(term, "source") else term.get("effective_source", term.get("source", "vault_master"))
        values.append(
            GalleryIntelligenceOwnerTerm(
                namespace=str(term.namespace if hasattr(term, "namespace") else term["namespace"]),
                slug=str(term.slug if hasattr(term, "slug") else term["slug"]),
                display_name=GALLERY_TERM_NAMES[(term.namespace, term.slug) if hasattr(term, "namespace") else (term["namespace"], term["slug"])],
                source=str(source),
            )
        )
    return values


def filter_catalogued_images_by_intelligence(
    images: list[GalleryImage],
    metadata: dict[str, CataloguedAsset],
    intelligence_store,
    photo_types: tuple[str, ...],
    content_tags: tuple[str, ...],
) -> tuple[list[GalleryImage], dict[str, CataloguedAsset]]:
    if not photo_types and not content_tags:
        return images, metadata
    if any(("photo_type", slug) not in GALLERY_TERM_NAMES for slug in photo_types) or any(("content_tag", slug) not in GALLERY_TERM_NAMES for slug in content_tags):
        return [], {}
    matching_ids = intelligence_store.matching_asset_ids(photo_types, content_tags)
    filtered_metadata = {
        path: asset for path, asset in metadata.items() if asset.id in matching_ids
    }
    return [image for image in images if str(image.path) in filtered_metadata], filtered_metadata


def filter_catalogued_images_by_people(images, metadata, people_store, people: tuple[UUID, ...], owner: UUID):
    if not people:
        return images, metadata
    matching_ids = people_store.matching_asset_ids(people, owner)
    filtered_metadata = {path: asset for path, asset in metadata.items() if asset.id in matching_ids}
    return [image for image in images if str(image.path) in filtered_metadata], filtered_metadata


def included_gallery_assets(username: AuthenticatedUsername) -> dict[UUID, str]:
    """Fail closed for shared cards while retaining an owner's own Gallery."""
    try:
        return PostgresShareGrantStore(get_database_conninfo()).included_gallery_assets(username.user_id)
    except Exception:
        # The Gallery may retain an owner's own catalogue view during a database
        # outage, but no shared card is ever inferred from a failed evaluator.
        return {}


def restrict_to_included_gallery_assets(
    metadata: dict[str, CataloguedAsset],
    username: AuthenticatedUsername,
    included_shared: dict[UUID, str],
) -> dict[str, CataloguedAsset]:
    return {
        path: asset
        for path, asset in metadata.items()
        if asset_is_editable_by(asset, username) or asset.id in included_shared
    }


def _shared_gallery_annotation(
    asset: CataloguedAsset, username: AuthenticatedUsername
) -> GalleryLocalAnnotation | None:
    """Read the separate recipient layer only through active share authority."""
    if asset_is_editable_by(asset, username):
        return None
    try:
        value = PostgresShareGrantStore(get_database_conninfo()).get_local_gallery_annotation(
            asset.id, username.user_id
        )
    except Exception:
        return None
    if value is None:
        return None
    return GalleryLocalAnnotation(
        note=value.get("note") if isinstance(value.get("note"), str) else None,
        tags=[str(tag) for tag in value.get("tags", []) if isinstance(tag, str)],
        people=[_local_gallery_person(person) for person in value.get("people", []) if isinstance(person, dict)],
    )


def _owner_gallery_asset(asset_id: UUID, username: str, store: VaultMasterStore) -> CataloguedAsset:
    asset = store.get_catalogued_asset_by_id(asset_id)
    if asset is None or asset.asset_type.casefold() != "gallery" or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo was not found")
    return asset


def _gallery_person(person: VaultPerson) -> GalleryPerson:
    return GalleryPerson(id=person.id, display_name=person.display_name, active=person.active)


def _local_gallery_person(value: dict[str, object]) -> GalleryPerson:
    return GalleryPerson(
        id=UUID(str(value["id"])),
        display_name=str(value["display_name"]),
        active=True,
    )


def _gallery_face_detection(face: FaceDetection) -> GalleryFaceDetection:
    return GalleryFaceDetection(
        id=face.id,
        bounding_box=face.bounding_box,
        person_id=face.person_id,
        person_name=face.person_name,
        user_confirmed=face.user_confirmed,
    )


@router.get("/people", response_model=list[GalleryPerson])
def list_gallery_people(username: AuthenticatedUsername, people_store=Depends(get_gallery_people_store)) -> list[GalleryPerson]:
    return [_gallery_person(person) for person in people_store.list_people(username.user_id)]


@router.post("/people", response_model=GalleryPerson, status_code=status.HTTP_201_CREATED)
def create_gallery_person(request: GalleryPersonCreate, username: AuthenticatedUsername, people_store=Depends(get_gallery_people_store)) -> GalleryPerson:
    if not request.display_name.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Display name is required")
    return _gallery_person(people_store.create_person(username, request.display_name, username.user_id))


@router.get("/people/{person_id}", response_model=GalleryPerson)
def get_gallery_person(person_id: UUID, username: AuthenticatedUsername, people_store=Depends(get_gallery_people_store)) -> GalleryPerson:
    person = people_store.get_person(person_id, username)
    if person is None: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person was not found")
    return _gallery_person(person)


@router.patch("/people/{person_id}", response_model=GalleryPerson)
def update_gallery_person(person_id: UUID, request: GalleryPersonUpdate, username: AuthenticatedUsername, people_store=Depends(get_gallery_people_store)) -> GalleryPerson:
    person = people_store.update_person(person_id, username.user_id, request.display_name, request.active)
    if person is None: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person was not found")
    return _gallery_person(person)


@router.get("/people/assets/{asset_id}", response_model=list[GalleryAssetPerson])
def list_gallery_asset_people(asset_id: UUID, username: AuthenticatedUsername, vault_master_store: VaultMasterStore = Depends(get_vault_master_store), people_store=Depends(get_gallery_people_store)) -> list[GalleryAssetPerson]:
    _owner_gallery_asset(asset_id, username, vault_master_store)
    return [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset_id, username.user_id)]


@router.get("/people/assets/{asset_id}/faces", response_model=list[GalleryFaceDetection])
def list_gallery_asset_faces(asset_id: UUID, username: AuthenticatedUsername, vault_master_store: VaultMasterStore = Depends(get_vault_master_store), intelligence_store=Depends(get_gallery_intelligence_store), people_store=Depends(get_gallery_people_store)) -> list[GalleryFaceDetection]:
    _owner_gallery_asset(asset_id, username, vault_master_store)
    job = intelligence_store.latest_successful_people_job(asset_id)
    return [
        _gallery_face_detection(face)
        for face in people_store.face_detections_for_asset(
            asset_id, username.user_id, job.id if job else None, job.started_at if job else None
        )
    ]


@router.patch("/people/assets/{asset_id}", response_model=list[GalleryAssetPerson])
def decide_gallery_asset_person(asset_id: UUID, request: GalleryAssetPersonDecision, username: AuthenticatedUsername, vault_master_store: VaultMasterStore = Depends(get_vault_master_store), people_store=Depends(get_gallery_people_store)) -> list[GalleryAssetPerson]:
    _owner_gallery_asset(asset_id, username, vault_master_store)
    try:
        people_store.decide(asset_id, request.person_id, request.decision, username.user_id)
    except ValueError as error: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset_id, username.user_id)]


@router.post("/people/assets/{asset_id}/identify", response_model=list[GalleryAssetPerson])
def identify_gallery_asset_unknown_person(
    asset_id: UUID,
    request: GalleryAssetPersonDecision,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    people_store=Depends(get_gallery_people_store),
) -> list[GalleryAssetPerson]:
    """Confirm one existing Unknown face as a Person and retain it as reference evidence."""
    _owner_gallery_asset(asset_id, username, vault_master_store)
    if request.decision != "include":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Unknown faces can only be identified")
    if request.face_detection_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Select a specific detected face")
    try:
        people_store.identify_face(asset_id, request.face_detection_id, request.person_id, username.user_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset_id, username.user_id)]


@router.post("/people/assets/{asset_id}/faces/{face_id}/identify", response_model=list[GalleryAssetPerson])
def identify_gallery_face(
    asset_id: UUID,
    face_id: UUID,
    request: GalleryAssetPersonDecision,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
    people_store=Depends(get_gallery_people_store),
) -> list[GalleryAssetPerson]:
    """Authoritatively identify one selected detected face; photo-level additions stay separate."""
    _owner_gallery_asset(asset_id, username, vault_master_store)
    if request.decision != "include":
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A face identity must identify a Person")
    job = intelligence_store.latest_successful_people_job(asset_id)
    effective_face_ids = {
        face.id
        for face in people_store.face_detections_for_asset(
            asset_id, username.user_id, job.id if job else None, job.started_at if job else None
        )
    }
    if face_id not in effective_face_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Face evidence is not part of the current People analysis")
    try:
        people_store.identify_face(asset_id, face_id, request.person_id, username.user_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset_id, username.user_id)]


@router.delete("/people/assets/{asset_id}/faces/{face_id}/identity", status_code=status.HTTP_204_NO_CONTENT)
def clear_gallery_face_identity(
    asset_id: UUID,
    face_id: UUID,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    people_store=Depends(get_gallery_people_store),
) -> Response:
    _owner_gallery_asset(asset_id, username, vault_master_store)
    try:
        people_store.clear_face_identity(asset_id, face_id, username.user_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/shared-preference", response_model=GallerySharedPreference)
def get_gallery_shared_preference(username: AuthenticatedUsername) -> GallerySharedPreference:
    return GallerySharedPreference(include_shared_photos=PostgresShareGrantStore(get_database_conninfo()).gallery_shared_preference(username.user_id))


@router.put("/shared-preference", response_model=GallerySharedPreference)
def set_gallery_shared_preference(request: GallerySharedPreference, username: AuthenticatedUsername) -> GallerySharedPreference:
    return GallerySharedPreference(include_shared_photos=PostgresShareGrantStore(get_database_conninfo()).set_gallery_shared_preference(username.user_id, request.include_shared_photos))


@router.put("/shared-collections/{collection_id}/inclusion", response_model=GalleryCollectionPreference)
def set_gallery_collection_inclusion(collection_id: UUID, request: GalleryCollectionPreference, username: AuthenticatedUsername) -> GalleryCollectionPreference:
    try:
        included = PostgresShareGrantStore(get_database_conninfo()).set_gallery_collection_preference(username.user_id, collection_id, request.included)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return GalleryCollectionPreference(included=included)


@router.get("/shared-collections", response_model=list[GallerySharedCollectionPreference])
def list_gallery_shared_collections(username: AuthenticatedUsername) -> list[GallerySharedCollectionPreference]:
    try:
        collections = PostgresShareGrantStore(get_database_conninfo()).list_gallery_shared_collections(username.user_id)
    except Exception:
        return []
    return [GallerySharedCollectionPreference(**collection.__dict__) for collection in collections]


def _gallery_cursor(value: str | None) -> tuple[date | None, str, str] | None:
    if value is None:
        return None
    try:
        if len(value) > 2048:
            raise ValueError()
        decoded = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
        if (not isinstance(decoded, list) or len(decoded) != 3
                or decoded[0] is not None and not isinstance(decoded[0], str)
                or not isinstance(decoded[1], str) or not isinstance(decoded[2], str)
                or len(decoded[1]) > 512 or len(decoded[2]) > 1024
                or not decoded[2].startswith("/vault/Gallery/")):
            raise ValueError()
        return (date.fromisoformat(decoded[0]) if decoded[0] is not None else None,
                decoded[1], decoded[2])
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error) as error:
        raise HTTPException(status_code=422, detail="Invalid Gallery cursor") from error


def _encode_gallery_cursor(asset: CataloguedAsset) -> str:
    return _encode_gallery_position(asset.captured_on, asset.filename.casefold(), asset.vault_path)


def _encode_gallery_position(captured: date | None, name: str, path: str) -> str:
    payload = [captured.isoformat() if captured else None, name, path]
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def _gallery_image_from_asset(asset: CataloguedAsset, gallery_path: Path) -> GalleryImage | None:
    prefix = "/vault/Gallery/"
    if not asset.vault_path.startswith(prefix):
        return None
    relative = asset.vault_path[len(prefix):]
    parts = PurePosixPath(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts) or PurePosixPath(*parts).as_posix() != relative:
        return None
    try:
        if "storage_placement" in asset.metadata:
            path = resolve_metadata_placement(asset.metadata)
            if path is None:
                return None
        else:
            raw_path = gallery_path / Path(*parts)
            if any((gallery_path / Path(*parts[:index])).is_symlink()
                   for index in range(1, len(parts) + 1)):
                return None
            path = raw_path.resolve(strict=True)
            path.relative_to(gallery_path)
            if not path.is_file():
                return None
        stat = path.stat()
    except (OSError, ValueError):
        return None
    return GalleryImage(
        id=get_image_id(Path(*parts)), name=path.name, path=path,
        size=stat.st_size, added_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        vault_path=asset.vault_path,
    )


@router.get("/chronology")
@router.get("/pages", response_model=GalleryPage)
def list_gallery_page(
    response: Response,
    request: Request,
    username: AuthenticatedUsername,
    sort: GallerySortOrder = Query("newest"),
    cursor: str | None = None,
    start: str | None = None,
    before: str | None = None,
    anchor_asset_id: UUID | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    photo_type: list[str] = Query(default=[]),
    content_tag: list[str] = Query(default=[]),
    person: list[UUID] = Query(default=[]),
    private_tag: list[UUID] = Query(default=[]),
    include_hidden: bool = Query(False),
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    auth_store: AuthenticationStore = Depends(get_authentication_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
    people_store=Depends(get_gallery_people_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store),
) -> GalleryPage | list[dict]:
    """Return one bounded, access-filtered page without scanning Gallery files."""
    response.headers["Cache-Control"] = "private, no-store"
    chronology = request.url.path.endswith("/chronology")
    if sum(value is not None for value in (cursor, start, before, anchor_asset_id)) > 1:
        raise HTTPException(status_code=422, detail="Choose one Gallery position")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="Date range start must not follow its end")
    if include_hidden:
        require_hidden_photo_authorization(request, username, auth_store)
    shared = {} if include_hidden else included_gallery_assets(username)
    matched: set[UUID] | None = None
    if photo_type or content_tag:
        if (any(("photo_type", slug) not in GALLERY_TERM_NAMES for slug in photo_type)
                or any(("content_tag", slug) not in GALLERY_TERM_NAMES for slug in content_tag)):
            return [] if chronology else GalleryPage(items=[], next_cursor=None)
        matched = set(intelligence_store.matching_asset_ids(tuple(photo_type), tuple(content_tag)))
    if person:
        people_ids = set(people_store.matching_asset_ids(tuple(person), username.user_id))
        matched = people_ids if matched is None else matched & people_ids
    if private_tag:
        private_ids = set(custom_tag_store.matching_asset_ids(username.user_id, tuple(private_tag)))
        matched = private_ids if matched is None else matched & private_ids
    scope = (username.user_id, list(shared), "hidden" if include_hidden else "active",
             sort, date_from, date_to, list(matched) if matched is not None else None)
    if chronology:
        return [{"year": captured.year if captured else None,
                 "month": captured.month if captured else None, "count": count,
                 "start": _encode_gallery_position(captured, name, path)}
                for captured, name, path, count in vault_master_store.gallery_chronology(*scope)]
    position = _gallery_cursor(next((value for value in (cursor, start, before) if value is not None), None))
    seek_anchor_id = None
    if anchor_asset_id is not None:
        anchor = vault_master_store.get_visible_catalogued_asset_by_id(anchor_asset_id, username)
        if (anchor is None or anchor.asset_type.casefold() != "gallery"
                or anchor.lifecycle_state != scope[2]
                or not (asset_is_editable_by(anchor, username) or anchor.id in shared)
                or matched is not None and anchor.id not in matched
                or date_from and (anchor.captured_on is None or anchor.captured_on < date_from)
                or date_to and (anchor.captured_on is None or anchor.captured_on > date_to)):
            raise HTTPException(status_code=404, detail="Gallery position is no longer available")
        position = _gallery_cursor(_encode_gallery_cursor(anchor))
        start = "anchor"
    if start is not None and position is not None:
        seek_anchor_id = get_image_id(Path(position[2][len("/vault/Gallery/"):]))
        preceding = vault_master_store.gallery_page_paths(*scope, position, 12, reverse=True)
        # Include a few preceding rows so the selected photo can retain its
        # viewport offset, rather than always landing at the beginning.
        if preceding:
            prior = vault_master_store.get_visible_catalogued_assets([preceding[-1]], username)
            first = prior.get(preceding[-1])
            if first is not None:
                position = _gallery_cursor(_encode_gallery_cursor(first))
    paths = vault_master_store.gallery_page_paths(
        *scope, position, GALLERY_PAGE_SIZE + 1, inclusive=start is not None, reverse=before is not None,
    )
    page_paths = paths[:GALLERY_PAGE_SIZE]
    if before is not None:
        page_paths.reverse()
    visible = vault_master_store.get_visible_catalogued_assets(page_paths, username)
    cards: list[GalleryImageSummary] = []
    for vault_path in page_paths:
        asset = visible.get(vault_path)
        if asset is None or asset.asset_type.casefold() != "gallery":
            continue
        if asset.lifecycle_state != ("hidden" if include_hidden else "active"):
            continue
        if not (asset_is_editable_by(asset, username) or asset.id in shared):
            continue
        image = _gallery_image_from_asset(asset, gallery_path)
        if image is None:
            continue
        cards.append(to_summary(image, asset, asset_is_editable_by(asset, username), shared.get(asset.id)))
    last = visible.get(page_paths[-1]) if page_paths else None
    first = visible.get(page_paths[0]) if page_paths else None
    has_previous = position is not None and first is not None and bool(vault_master_store.gallery_page_paths(
        *scope, _gallery_cursor(_encode_gallery_cursor(first)), 1, reverse=True,
    ))
    return GalleryPage(
        items=cards,
        next_cursor=_encode_gallery_cursor(last) if (before is not None or len(paths) > GALLERY_PAGE_SIZE) and last is not None else None,
        previous_cursor=_encode_gallery_cursor(first) if has_previous else None,
        seek_anchor_id=seek_anchor_id,
    )


@router.get("", response_model=list[GalleryImageSummary])
def list_gallery_images(
    response: Response,
    request: Request,
    username: AuthenticatedUsername,
    sort: GallerySortOrder = Query("newest"),
    date_from: date | None = None,
    date_to: date | None = None,
    photo_type: list[str] = Query(default=[]),
    content_tag: list[str] = Query(default=[]),
    person: list[UUID] = Query(default=[]),
    private_tag: list[UUID] = Query(default=[]),
    include_hidden: bool = Query(False),
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    auth_store: AuthenticationStore = Depends(get_authentication_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
    people_store=Depends(get_gallery_people_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store),
) -> list[GalleryImageSummary]:
    response.headers["Cache-Control"] = "private, no-store"
    catalogued_assets: dict[str, CataloguedAsset] = {}
    images = scan_gallery(gallery_path, vault_master_store, catalogued_assets)
    metadata = get_gallery_metadata(
        vault_master_store,
        gallery_path,
        images,
        username,
        catalogued_assets,
    )
    if include_hidden:
        require_hidden_photo_authorization(request, username, auth_store)
    metadata = filter_gallery_lifecycle(metadata, username, include_hidden, include_hidden)
    # A recipient's blended timeline is an authoritative, request-time grant
    # evaluation; own cards remain present and shared cards require an opted-in
    # direct or collection access path.
    included_shared = included_gallery_assets(username)
    metadata = restrict_to_included_gallery_assets(metadata, username, included_shared)
    images, metadata = filter_catalogued_images_by_intelligence(
        images, metadata, intelligence_store, tuple(photo_type), tuple(content_tag)
    )
    images, metadata = filter_catalogued_images_by_people(images, metadata, people_store, tuple(person), username.user_id)
    if private_tag:
        matching_ids = custom_tag_store.matching_asset_ids(username.user_id, tuple(private_tag))
        metadata = {path: asset for path, asset in metadata.items() if asset.id in matching_ids}
        images = [image for image in images if str(image.path) in metadata]
    metadata = filter_gallery_dates(metadata, date_from, date_to)
    return [
        to_summary(
            image,
            metadata[str(image.path)],
            asset_is_editable_by(metadata[str(image.path)], username),
            included_shared.get(metadata[str(image.path)].id),
        )
        for image in get_catalogued_images(images, metadata, sort)
    ]


@router.post("/intelligence/backfill")
def queue_gallery_intelligence_backfill(
    username: AuthenticatedUsername,
    limit: int = Query(50, ge=1, le=500),
    reanalyse: bool = Query(False),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
    florence_store=Depends(get_gallery_florence_store),
    ingestion_ai_store=Depends(get_ingestion_ai_store),
) -> GalleryIntelligenceBackfillResponse:
    """Owner/admin-triggered, bounded post-publication metadata backfill."""
    run_id = intelligence_store.start_bulk_run(username, username.user_id)
    queued = queue_gallery_analysis_catchup(
        intelligence_store,
        vault_master_store,
        username,
        username.user_id,
        limit,
        bulk_run_id=run_id,
    ) if not reanalyse else queue_published_gallery_assets(
        intelligence_store, vault_master_store, username, username.user_id, limit, force=True, bulk_run_id=run_id
    )
    # Florence recovery is deliberately part of this same owner action.  It
    # reads canonical published assets; it never recreates Arrival Hall work.
    from app.gallery_publication import queue_missing_gallery_florence
    florence_queued = 0
    if not reanalyse:
        for asset in vault_master_store.list_owned_catalogued_assets_by_user_id(username.user_id):
            if queued + florence_queued >= limit:
                break
            if queue_missing_gallery_florence(vault_master_store, ingestion_ai_store,
                                             florence_store, asset, username):
                florence_queued += 1
    queued += florence_queued
    if not queued:
        intelligence_store.discard_bulk_run(run_id)
        return GalleryIntelligenceBackfillResponse(queued=0, limit=limit, reanalyse=int(reanalyse))
    run = intelligence_store.latest_bulk_run(username.user_id)
    return GalleryIntelligenceBackfillResponse(
        queued=queued,
        limit=limit,
        reanalyse=int(reanalyse),
        run=GalleryIntelligenceBulkRunResponse(**run.__dict__) if run else None,
    )


@router.get("/intelligence/backfill/latest")
def get_latest_gallery_intelligence_backfill(
    username: AuthenticatedUsername,
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> dict[str, GalleryIntelligenceBulkRunResponse | None]:
    """Persisted status for the latest owner-initiated bulk Gallery run."""
    run = intelligence_store.latest_bulk_run(username.user_id)
    return {"run": GalleryIntelligenceBulkRunResponse(**run.__dict__) if run else None}


@router.get("/intelligence/backfill/status", response_model=GalleryIntelligenceBackfillStatus)
def get_gallery_intelligence_backfill_status(
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> GalleryIntelligenceBackfillStatus:
    """Return only this owner's historical Gallery catch-up status.

    Unlike the administrator-only worker health endpoint, this endpoint is a
    user capability check.  It remains available before an owner has ever
    started a bulk run.
    """
    run = intelligence_store.latest_bulk_run(username.user_id)
    candidates = gallery_analysis_catchup_candidates(
        intelligence_store, vault_master_store, username, username.user_id
    )
    return GalleryIntelligenceBackfillStatus(
        eligible_count=len(candidates),
        run=GalleryIntelligenceBulkRunResponse(**run.__dict__) if run else None,
    )


@router.post(
    "/intelligence/assets/{asset_id}/reanalyse",
    response_model=GalleryIntelligenceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def reanalyse_gallery_intelligence_asset(
    asset_id: UUID,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> GalleryIntelligenceJobResponse:
    """Queue Gallery Intelligence only for an owner-selected canonical asset.

    This deliberately queues the post-publication descriptive-metadata worker;
    it does not enter the Arrival Hall or invoke routing analysis.
    """
    asset = vault_master_store.get_catalogued_asset_by_id(asset_id)
    if asset is None or asset.asset_type.casefold() != "gallery":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo was not found")
    if not asset_is_editable_by(asset, username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner may analyse a Gallery photo",
        )
    job = intelligence_store.queue(asset.id, username, force=True)
    return GalleryIntelligenceJobResponse(id=job.id, status=job.status, error=job.error)


@router.post("/people/assets/{asset_id}/analyse", response_model=GalleryIntelligenceJobResponse, status_code=status.HTTP_202_ACCEPTED)
def analyse_gallery_people_asset(
    asset_id: UUID,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> GalleryIntelligenceJobResponse:
    """Queue People evidence only for one owner-selected Gallery asset."""
    asset = _owner_gallery_asset(asset_id, username, vault_master_store)
    job = intelligence_store.queue(asset.id, username, force=True, people_only=True)
    return GalleryIntelligenceJobResponse(id=job.id, status=job.status, error=job.error)


@router.get("/people/assets/{asset_id}/status", response_model=GalleryIntelligenceJobStatusResponse)
def get_gallery_people_asset_status(
    asset_id: UUID,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> GalleryIntelligenceJobStatusResponse:
    _owner_gallery_asset(asset_id, username, vault_master_store)
    job = intelligence_store.latest_people_job(asset_id)
    return GalleryIntelligenceJobStatusResponse(job=(GalleryIntelligenceJobResponse(id=job.id, status=job.people_status if job and job.people_status != "pending" else (job.status if job else "pending"), error=job.people_error if job else None) if job else None))


@router.get(
    "/intelligence/assets/{asset_id}/status",
    response_model=GalleryIntelligenceJobStatusResponse,
)
def get_gallery_intelligence_asset_status(
    asset_id: UUID,
    username: AuthenticatedUsername,
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> GalleryIntelligenceJobStatusResponse:
    """Return the latest persisted Gallery Intelligence job for one owner asset."""
    asset = vault_master_store.get_catalogued_asset_by_id(asset_id)
    if asset is None or asset.asset_type.casefold() != "gallery":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo was not found")
    if not asset_is_editable_by(asset, username):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner may view Gallery Intelligence analysis status",
        )
    job = intelligence_store.latest_job(asset.id)
    return GalleryIntelligenceJobStatusResponse(
        job=(GalleryIntelligenceJobResponse(id=job.id, status=job.status, error=job.error) if job else None)
    )


@router.get("/intelligence/terms", response_model=list[GalleryIntelligenceTerm])
def list_gallery_intelligence_terms(
    username: AuthenticatedUsername,
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> list[GalleryIntelligenceTerm]:
    return [GalleryIntelligenceTerm(**term) for term in gallery_taxonomy_terms()]


def _custom_tag_response(tag: CustomTag) -> GalleryCustomTag:
    return GalleryCustomTag(id=tag.id, slug=tag.slug, display_name=tag.display_name)


@router.get("/custom-tags", response_model=list[GalleryCustomTag])
def list_gallery_custom_tags(response: Response, username: AuthenticatedUsername, custom_tag_store=Depends(get_gallery_custom_tag_store)) -> list[GalleryCustomTag]:
    response.headers["Cache-Control"] = "private, no-store"
    return [_custom_tag_response(tag) for tag in custom_tag_store.list(username.user_id)]


@router.post("/custom-tags", response_model=GalleryCustomTag, status_code=status.HTTP_201_CREATED)
def create_gallery_custom_tag(request: GalleryCustomTagCreate, username: AuthenticatedUsername, custom_tag_store=Depends(get_gallery_custom_tag_store)) -> GalleryCustomTag:
    try:
        return _custom_tag_response(custom_tag_store.create(username.user_id, request.display_name))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.patch("/custom-tags/{tag_id}", response_model=GalleryCustomTag)
def rename_gallery_custom_tag(tag_id: UUID, request: GalleryCustomTagCreate, username: AuthenticatedUsername, custom_tag_store=Depends(get_gallery_custom_tag_store)) -> GalleryCustomTag:
    try: return _custom_tag_response(custom_tag_store.rename(username.user_id, tag_id, request.display_name))
    except ValueError as error: raise HTTPException(status_code=404, detail=str(error)) from error


@router.delete("/custom-tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_gallery_custom_tag(tag_id: UUID, username: AuthenticatedUsername, custom_tag_store=Depends(get_gallery_custom_tag_store)) -> Response:
    try: custom_tag_store.delete(username.user_id, tag_id)
    except ValueError as error: raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/{image_id}/custom-tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def assign_gallery_custom_tag(image_id: str, tag_id: UUID, username: AuthenticatedUsername, gallery_path: Path = Depends(require_gallery_path), vault_master_store: VaultMasterStore = Depends(get_vault_master_store), custom_tag_store=Depends(get_gallery_custom_tag_store)) -> Response:
    images, index = find_gallery_image(gallery_path, image_id, vault_master_store)
    image = images[index]
    metadata = get_gallery_metadata(vault_master_store, gallery_path, [image], username)
    asset = metadata.get(str(image.path))
    if asset is None or asset.lifecycle_state != "active" or (not asset_is_editable_by(asset, username) and asset.id not in included_gallery_assets(username)):
        raise HTTPException(status_code=404, detail="Photo was not found")
    try:
        custom_tag_store.assign(username.user_id, tag_id, asset.id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/{image_id}/custom-tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def unassign_gallery_custom_tag(image_id: str, tag_id: UUID, username: AuthenticatedUsername, gallery_path: Path = Depends(require_gallery_path), vault_master_store: VaultMasterStore = Depends(get_vault_master_store), custom_tag_store=Depends(get_gallery_custom_tag_store)) -> Response:
    images, index = find_gallery_image(gallery_path, image_id, vault_master_store); image = images[index]
    asset = get_gallery_metadata(vault_master_store, gallery_path, [image], username).get(str(image.path))
    if asset is None or asset.lifecycle_state != "active" or (not asset_is_editable_by(asset, username) and asset.id not in included_gallery_assets(username)):
        raise HTTPException(status_code=404, detail="Photo was not found")
    custom_tag_store.unassign(username.user_id, tag_id, asset.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/intelligence/status")
def get_gallery_intelligence_status(
    username: AuthenticatedAdministrator,
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> dict[str, dict[str, int]]:
    return {"jobs": intelligence_store.job_counts()}


@router.put("/{image_id}/local-annotation", response_model=GalleryLocalAnnotation)
def set_gallery_local_annotation(
    image_id: str,
    request: GalleryLocalAnnotationEdit,
    username: AuthenticatedUsername,
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> GalleryLocalAnnotation:
    """Save only a recipient's separate local view of an active shared photo."""
    images, index = find_gallery_image(gallery_path, image_id, vault_master_store)
    image = images[index]
    metadata = get_gallery_metadata(vault_master_store, gallery_path, [image], username)
    asset = metadata.get(str(image.path))
    if asset is None or asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Shared photo was not found")
    try:
        value = PostgresShareGrantStore(get_database_conninfo()).set_local_gallery_annotation(
            asset.id,
            username.user_id,
            note=request.note,
            tags=request.tags,
            person_ids=request.person_ids,
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return GalleryLocalAnnotation(
        note=value.get("note") if isinstance(value.get("note"), str) else None,
        tags=[str(tag) for tag in value.get("tags", []) if isinstance(tag, str)],
        people=[_local_gallery_person(person) for person in value.get("people", []) if isinstance(person, dict)],
    )


@router.get(
    "/{image_id}",
    response_model=GalleryImageDetails,
)
def get_gallery_image(
    image_id: str,
    request: Request,
    username: AuthenticatedUsername,
    sort: GallerySortOrder = Query("newest"),
    date_from: date | None = None,
    date_to: date | None = None,
    photo_type: list[str] = Query(default=[]),
    content_tag: list[str] = Query(default=[]),
    person: list[UUID] = Query(default=[]),
    private_tag: list[UUID] = Query(default=[]),
    include_hidden: bool = Query(False),
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    auth_store: AuthenticationStore = Depends(get_authentication_store),
    custom_tag_store=Depends(get_gallery_custom_tag_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
    people_store=Depends(get_gallery_people_store),
) -> JSONResponse:
    catalogued_assets: dict[str, CataloguedAsset] = {}
    images = scan_gallery(gallery_path, vault_master_store, catalogued_assets)
    if not any(image.id == image_id for image in images):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Photo was not found",
        )
    metadata = get_gallery_metadata(
        vault_master_store,
        gallery_path,
        images,
        username,
        catalogued_assets,
    )
    requested_image = next((image for image in images if image.id == image_id), None)
    requested_asset = metadata.get(str(requested_image.path)) if requested_image is not None else None
    if requested_asset is not None:
        deny_hidden_photo_without_authorization(
            requested_asset, request, username, auth_store
        )
    if include_hidden:
        require_hidden_photo_authorization(request, username, auth_store)
    metadata = filter_gallery_lifecycle(metadata, username, include_hidden, include_hidden)
    included_shared = included_gallery_assets(username)
    metadata = restrict_to_included_gallery_assets(metadata, username, included_shared)
    images, metadata = filter_catalogued_images_by_intelligence(
        images, metadata, intelligence_store, tuple(photo_type), tuple(content_tag)
    )
    images, metadata = filter_catalogued_images_by_people(images, metadata, people_store, tuple(person), username.user_id)
    if private_tag:
        matching_ids = custom_tag_store.matching_asset_ids(username.user_id, tuple(private_tag))
        metadata = {path: asset for path, asset in metadata.items() if asset.id in matching_ids}
        images = [image for image in images if str(image.path) in metadata]
    metadata = filter_gallery_dates(metadata, date_from, date_to)
    catalogued_images = get_catalogued_images(images, metadata, sort)
    index = next(
        (
            index
            for index, image in enumerate(catalogued_images)
            if image.id == image_id
        ),
        None,
    )
    # An already-authorized direct hidden-photo view remains a valid detail
    # request.  It is deliberately not admitted to the normal navigation
    # sequence; expose it without adjacent IDs instead.
    if (
        index is None
        and not include_hidden
        and not private_tag
        and date_from is None and date_to is None
        and requested_image is not None
        and requested_asset is not None
        and requested_asset.lifecycle_state == "hidden"
        and asset_is_editable_by(requested_asset, username)
    ):
        catalogued_images = [requested_image]
        metadata = {str(requested_image.path): requested_asset}
        index = 0
    if index is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Photo is not catalogued by Vault Master",
        )
    image = catalogued_images[index]
    asset = metadata[str(image.path)]
    deny_hidden_photo_without_authorization(asset, request, username, auth_store)
    can_edit = asset_is_editable_by(asset, username)
    summary = to_summary(image, asset, can_edit, included_shared.get(asset.id))
    effective_terms = intelligence_terms_for_asset(intelligence_store, asset.id)
    effective_people = [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset.id, username.user_id)] if can_edit else None
    local_annotation = _shared_gallery_annotation(asset, username)
    origin_people = (
        [
            GalleryPerson(id=value.person_id, display_name=value.display_name, active=True)
            for value in people_store.effective_people(asset.id, asset.owner_user_id)
        ]
        if not can_edit and local_annotation is not None and asset.owner_user_id is not None
        else None
    )
    people_job = intelligence_store.latest_successful_people_job(asset.id) if can_edit else None
    current_faces = people_store.face_detections_for_asset(
        asset.id,
        username.user_id,
        people_job.id if people_job else None,
        people_job.started_at if people_job else None,
    ) if can_edit else []
    unknown_people_count = len([face for face in current_faces if face.person_id is None]) if can_edit else None
    reconciliation = intelligence_store.reconciliation(asset.id) if can_edit else None
    if isinstance(reconciliation, dict):
        unresolved_person_presence = bool(reconciliation.get("unresolved_person_presence"))
    elif reconciliation is not None:
        unresolved_person_presence = bool(getattr(reconciliation, "unresolved_person_presence", False))
    else:
        unresolved_person_presence = None
    face_detections = [_gallery_face_detection(face) for face in current_faces] if can_edit else None

    owner_only_fields = (
        {
            "lifecycle_state": asset.lifecycle_state,
            "vault_path": asset.vault_path,
            "mime_type": asset.mime_type,
            "sha256": asset.sha256,
            "metadata_provenance": asset.metadata_provenance,
            "intelligence_provenance": [
                term.model_dump()
                for term in intelligence_owner_terms_for_asset(intelligence_store, asset.id)
            ],
        }
        if can_edit
        else {}
    )

    details = GalleryImageDetails(
        **summary.model_dump(),
        **owner_only_fields,
        intelligence=[term.model_dump() for term in effective_terms],
        custom_tags=[_custom_tag_response(tag).model_dump() for tag in custom_tag_store.for_asset(username.user_id, asset.id)],
        people=effective_people,
        origin_people=origin_people,
        local_annotation=local_annotation,
        unknown_people_count=unknown_people_count,
        unresolved_person_presence=unresolved_person_presence,
        face_detections=face_detections,
        can_edit=can_edit,
        image_url=f"/api/gallery/{image.id}/content",
        previous_id=(
            catalogued_images[index - 1].id if index > 0 else None
        ),
        next_id=(
            catalogued_images[index + 1].id
            if index + 1 < len(catalogued_images)
            else None
        ),
    )
    payload = details.model_dump(mode="json", exclude_none=True)
    # Navigation is part of the public Gallery contract, including the null
    # boundary values.  Owner-only file facts remain omitted for shared users.
    payload["previous_id"] = details.previous_id
    payload["next_id"] = details.next_id
    payload["captured_on"] = summary.model_dump(mode="json")["captured_on"]
    return JSONResponse(
        content=payload,
        headers={"Cache-Control": "private, no-store"},
    )


@router.patch("/{image_id}/intelligence", response_model=list[GalleryIntelligenceTerm])
def decide_gallery_intelligence_term(
    image_id: str,
    request: GalleryIntelligenceDecision,
    username: AuthenticatedUsername,
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
) -> list[GalleryIntelligenceTerm]:
    images, index = find_gallery_image(gallery_path, image_id, vault_master_store)
    image = images[index]
    metadata = get_gallery_metadata(vault_master_store, gallery_path, [image], username)
    asset = metadata.get(str(image.path))
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo was not found")
    if not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner may edit Gallery Intelligence metadata")
    if (request.namespace, request.slug) not in GALLERY_TERM_NAMES:
        raise HTTPException(status_code=422, detail="Unknown Gallery Intelligence term")
    intelligence_store.decide(asset.id, request.namespace, request.slug, request.decision, username)
    return intelligence_terms_for_asset(intelligence_store, asset.id)


@router.get("/assets/{asset_id}/preview", response_model=None)
def get_gallery_asset_thumbnail(
    asset_id: UUID,
    request: Request,
    username: AuthenticatedUsername,
    gallery_path: Path = Depends(require_gallery_path),
    cache_root: Path = Depends(get_gallery_thumbnail_cache_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    auth_store: AuthenticationStore = Depends(get_authentication_store),
) -> FileResponse | Response:
    """Revalidate access on every request before using a private derivative."""
    asset = vault_master_store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or asset.asset_type.casefold() != "gallery":
        raise HTTPException(status_code=404, detail="Photo was not found")
    deny_hidden_photo_without_authorization(asset, request, username, auth_store)
    if not (asset_is_editable_by(asset, username) or
            (asset.lifecycle_state == "active" and asset.id in included_gallery_assets(username))):
        raise HTTPException(status_code=404, detail="Photo was not found")
    image = _gallery_image_from_asset(asset, gallery_path)
    if image is None:
        raise HTTPException(status_code=404, detail="Photo was not found")
    result = gallery_thumbnail(image.path, asset, cache_root)
    if result is None:
        raise HTTPException(status_code=503, detail="Photo preview is unavailable")
    path, key = result
    headers = {
        "Cache-Control": "private, no-cache, max-age=0, must-revalidate",
        "ETag": f'"{key}"',
        "X-Content-Type-Options": "nosniff",
    }
    if request.headers.get("if-none-match") == headers["ETag"]:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="image/jpeg", headers=headers)


@router.get("/{image_id}/content", response_class=FileResponse)
def get_gallery_image_content(
    image_id: str,
    request: Request,
    username: AuthenticatedUsername,
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    auth_store: AuthenticationStore = Depends(get_authentication_store),
) -> FileResponse:
    image = find_gallery_content_image(gallery_path, image_id, vault_master_store)
    visible_metadata = get_gallery_metadata(
        vault_master_store,
        gallery_path,
        [image],
        username,
    )
    asset = visible_metadata.get(str(image.path))
    if asset is None or (
        not asset_is_editable_by(asset, username)
        and asset.id not in included_gallery_assets(username)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Photo was not found",
        )
    deny_hidden_photo_without_authorization(asset, request, username, auth_store)
    media_type = (
        mimetypes.guess_type(image.name)[0]
        or "application/octet-stream"
    )

    return FileResponse(
        path=image.path,
        media_type=media_type,
        filename=image.name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{image_id}/preview", response_model=None)
def get_gallery_image_preview(
    image_id: str,
    request: Request,
    username: AuthenticatedUsername,
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    auth_store: AuthenticationStore = Depends(get_authentication_store),
) -> FileResponse | Response:
    """Serve a private native image, or a bounded Vault Master PDF preview."""
    images, index = find_gallery_image(gallery_path, image_id, vault_master_store)
    image = images[index]
    visible_metadata = get_gallery_metadata(
        vault_master_store, gallery_path, [image], username
    )
    asset = visible_metadata.get(str(image.path))
    if asset is None or (
        not asset_is_editable_by(asset, username)
        and asset.id not in included_gallery_assets(username)
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Photo was not found",
        )
    deny_hidden_photo_without_authorization(asset, request, username, auth_store)
    if asset.mime_type.startswith("image/"):
        return FileResponse(
            path=image.path,
            media_type=asset.mime_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )
    if asset.mime_type != "application/pdf":
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="This file type has no image preview",
        )
    try:
        preview = render_gallery_pdf_preview(image.path)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The PDF preview is unavailable",
        ) from error
    return Response(
        content=preview,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
