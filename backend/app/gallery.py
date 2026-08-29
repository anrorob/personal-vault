from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from app.auth import AuthenticatedAdministrator, AuthenticatedUsername
from app.config import get_database_conninfo
from app.share_grants import PostgresShareGrantStore
from app.gallery_intelligence import (
    get_gallery_intelligence_store,
    gallery_analysis_catchup_candidates,
    queue_gallery_analysis_catchup,
    queue_published_gallery_assets,
)
from app.gallery_people import FaceDetection, VaultPerson, get_gallery_people_store
from app.vault_master import (
    CataloguedAsset,
    VaultMasterStore,
    asset_is_editable_by,
    get_vault_master_store,
)
from app.vault_master_ingestion_ai import render_gallery_pdf_preview


router = APIRouter(prefix="/api/gallery", tags=["gallery"])
SUPPORTED_IMAGE_EXTENSIONS = {
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".webp",
}
@dataclass(frozen=True)
class GalleryImage:
    id: str
    name: str
    path: Path
    size: int
    added_at: datetime


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


class GalleryIntelligenceTerm(BaseModel):
    namespace: Literal["photo_type", "content_tag"]
    slug: str
    display_name: str


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


def scan_gallery(gallery_path: Path) -> list[GalleryImage]:
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

    images.sort(key=lambda image: image.name.casefold())
    return images


def find_gallery_image(
    gallery_path: Path,
    image_id: str,
) -> tuple[list[GalleryImage], int]:
    images = scan_gallery(gallery_path)

    for index, image in enumerate(images):
        if image.id == image_id:
            return images, index

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail="Photo was not found",
    )


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
        thumbnail_url=f"/api/gallery/{image.id}/preview",
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
) -> dict[str, CataloguedAsset]:
    vault_paths = {
        str(image.path): (
            "/vault/Gallery/"
            f"{image.path.relative_to(gallery_path).as_posix()}"
        )
        for image in images
    }
    catalogue = vault_master_store.get_visible_catalogued_assets(
        list(vault_paths.values()), username
    )
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
) -> dict[str, CataloguedAsset]:
    """Keep Hidden assets out of normal Gallery presentation.

    Only the immutable owner can explicitly include their own Hidden content;
    shared recipients never inherit that management view.
    """
    return {
        path: asset
        for path, asset in metadata.items()
        if asset.lifecycle_state == "active"
        or (include_hidden and asset.lifecycle_state == "hidden" and asset_is_editable_by(asset, username))
    }


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


def intelligence_terms_for_asset(store, asset_id: UUID) -> list[GalleryIntelligenceTerm]:
    return [
        GalleryIntelligenceTerm(
            namespace=str(term.namespace if hasattr(term, "namespace") else term["namespace"]),
            slug=str(term.slug if hasattr(term, "slug") else term["slug"]),
            display_name=str(term.display_name if hasattr(term, "display_name") else term["display_name"]),
        )
        for term in store.effective(asset_id)
    ]


def intelligence_owner_terms_for_asset(store, asset_id: UUID) -> list[GalleryIntelligenceOwnerTerm]:
    values: list[GalleryIntelligenceOwnerTerm] = []
    for term in store.effective(asset_id):
        source = term.source if hasattr(term, "source") else term.get("effective_source", term.get("source", "vault_master"))
        values.append(
            GalleryIntelligenceOwnerTerm(
                namespace=str(term.namespace if hasattr(term, "namespace") else term["namespace"]),
                slug=str(term.slug if hasattr(term, "slug") else term["slug"]),
                display_name=str(term.display_name if hasattr(term, "display_name") else term["display_name"]),
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
    return [_gallery_person(person) for person in people_store.list_people(getattr(username, "user_id", username))]


@router.post("/people", response_model=GalleryPerson, status_code=status.HTTP_201_CREATED)
def create_gallery_person(request: GalleryPersonCreate, username: AuthenticatedUsername, people_store=Depends(get_gallery_people_store)) -> GalleryPerson:
    if not request.display_name.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Display name is required")
    return _gallery_person(people_store.create_person(username, request.display_name, getattr(username, "user_id", None)))


@router.get("/people/{person_id}", response_model=GalleryPerson)
def get_gallery_person(person_id: UUID, username: AuthenticatedUsername, people_store=Depends(get_gallery_people_store)) -> GalleryPerson:
    person = people_store.get_person(person_id, username)
    if person is None: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person was not found")
    return _gallery_person(person)


@router.patch("/people/{person_id}", response_model=GalleryPerson)
def update_gallery_person(person_id: UUID, request: GalleryPersonUpdate, username: AuthenticatedUsername, people_store=Depends(get_gallery_people_store)) -> GalleryPerson:
    person = people_store.update_person(person_id, getattr(username, "user_id", username), request.display_name, request.active)
    if person is None: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Person was not found")
    return _gallery_person(person)


@router.get("/people/assets/{asset_id}", response_model=list[GalleryAssetPerson])
def list_gallery_asset_people(asset_id: UUID, username: AuthenticatedUsername, vault_master_store: VaultMasterStore = Depends(get_vault_master_store), people_store=Depends(get_gallery_people_store)) -> list[GalleryAssetPerson]:
    _owner_gallery_asset(asset_id, username, vault_master_store)
    return [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset_id, getattr(username, "user_id", username))]


@router.get("/people/assets/{asset_id}/faces", response_model=list[GalleryFaceDetection])
def list_gallery_asset_faces(asset_id: UUID, username: AuthenticatedUsername, vault_master_store: VaultMasterStore = Depends(get_vault_master_store), intelligence_store=Depends(get_gallery_intelligence_store), people_store=Depends(get_gallery_people_store)) -> list[GalleryFaceDetection]:
    _owner_gallery_asset(asset_id, username, vault_master_store)
    job = intelligence_store.latest_successful_people_job(asset_id)
    return [
        _gallery_face_detection(face)
        for face in people_store.face_detections_for_asset(
            asset_id, getattr(username, "user_id", username), job.id if job else None, job.started_at if job else None
        )
    ]


@router.patch("/people/assets/{asset_id}", response_model=list[GalleryAssetPerson])
def decide_gallery_asset_person(asset_id: UUID, request: GalleryAssetPersonDecision, username: AuthenticatedUsername, vault_master_store: VaultMasterStore = Depends(get_vault_master_store), people_store=Depends(get_gallery_people_store)) -> list[GalleryAssetPerson]:
    _owner_gallery_asset(asset_id, username, vault_master_store)
    try:
        people_store.decide(asset_id, request.person_id, request.decision, getattr(username, "user_id", username))
    except ValueError as error: raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset_id, getattr(username, "user_id", username))]


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
        people_store.identify_face(asset_id, request.face_detection_id, request.person_id, getattr(username, "user_id", username))
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset_id, getattr(username, "user_id", username))]


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
            asset_id, getattr(username, "user_id", username), job.id if job else None, job.started_at if job else None
        )
    }
    if face_id not in effective_face_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Face evidence is not part of the current People analysis")
    try:
        people_store.identify_face(asset_id, face_id, request.person_id, getattr(username, "user_id", username))
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset_id, getattr(username, "user_id", username))]


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
        people_store.clear_face_identity(asset_id, face_id, getattr(username, "user_id", username))
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


@router.get("", response_model=list[GalleryImageSummary])
def list_gallery_images(
    response: Response,
    username: AuthenticatedUsername,
    sort: GallerySortOrder = Query("newest"),
    photo_type: list[str] = Query(default=[]),
    content_tag: list[str] = Query(default=[]),
    person: list[UUID] = Query(default=[]),
    include_hidden: bool = Query(False),
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
    people_store=Depends(get_gallery_people_store),
) -> list[GalleryImageSummary]:
    response.headers["Cache-Control"] = "private, no-store"
    images = scan_gallery(gallery_path)
    metadata = get_gallery_metadata(
        vault_master_store,
        gallery_path,
        images,
        username,
    )
    metadata = filter_gallery_lifecycle(metadata, username, include_hidden)
    # A recipient's blended timeline is an authoritative, request-time grant
    # evaluation; own cards remain present and shared cards require an opted-in
    # direct or collection access path.
    included_shared = included_gallery_assets(username)
    metadata = restrict_to_included_gallery_assets(metadata, username, included_shared)
    images, metadata = filter_catalogued_images_by_intelligence(
        images, metadata, intelligence_store, tuple(photo_type), tuple(content_tag)
    )
    images, metadata = filter_catalogued_images_by_people(images, metadata, people_store, tuple(person), getattr(username, "user_id", username))
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
    return [GalleryIntelligenceTerm(**term) for term in intelligence_store.list_terms()]


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
    images, index = find_gallery_image(gallery_path, image_id)
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
    username: AuthenticatedUsername,
    sort: GallerySortOrder = Query("newest"),
    photo_type: list[str] = Query(default=[]),
    content_tag: list[str] = Query(default=[]),
    person: list[UUID] = Query(default=[]),
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
    intelligence_store=Depends(get_gallery_intelligence_store),
    people_store=Depends(get_gallery_people_store),
) -> JSONResponse:
    images = scan_gallery(gallery_path)
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
    )
    included_shared = included_gallery_assets(username)
    metadata = restrict_to_included_gallery_assets(metadata, username, included_shared)
    images, metadata = filter_catalogued_images_by_intelligence(
        images, metadata, intelligence_store, tuple(photo_type), tuple(content_tag)
    )
    images, metadata = filter_catalogued_images_by_people(images, metadata, people_store, tuple(person), getattr(username, "user_id", username))
    catalogued_images = get_catalogued_images(images, metadata, sort)
    index = next(
        (
            index
            for index, image in enumerate(catalogued_images)
            if image.id == image_id
        ),
        None,
    )
    if index is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Photo is not catalogued by Vault Master",
        )
    image = catalogued_images[index]
    asset = metadata[str(image.path)]
    can_edit = asset_is_editable_by(asset, username)
    summary = to_summary(image, asset, can_edit, included_shared.get(asset.id))
    effective_terms = intelligence_terms_for_asset(intelligence_store, asset.id)
    effective_people = [GalleryAssetPerson(id=value.person_id, display_name=value.display_name, active=True, source=value.source) for value in people_store.effective_people(asset.id, getattr(username, "user_id", username))] if can_edit else None
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
        getattr(username, "user_id", username),
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
    images, index = find_gallery_image(gallery_path, image_id)
    image = images[index]
    metadata = get_gallery_metadata(vault_master_store, gallery_path, [image], username)
    asset = metadata.get(str(image.path))
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Photo was not found")
    if not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner may edit Gallery Intelligence metadata")
    intelligence_store.decide(asset.id, request.namespace, request.slug, request.decision, username)
    return intelligence_terms_for_asset(intelligence_store, asset.id)


@router.get("/{image_id}/content", response_class=FileResponse)
def get_gallery_image_content(
    image_id: str,
    username: AuthenticatedUsername,
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> FileResponse:
    images, index = find_gallery_image(gallery_path, image_id)
    image = images[index]
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
    username: AuthenticatedUsername,
    gallery_path: Path = Depends(require_gallery_path),
    vault_master_store: VaultMasterStore = Depends(get_vault_master_store),
) -> FileResponse | Response:
    """Serve a private native image, or a bounded Vault Master PDF preview."""
    images, index = find_gallery_image(gallery_path, image_id)
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
