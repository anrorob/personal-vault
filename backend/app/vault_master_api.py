from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
import os
import psycopg
import hashlib
from pathlib import Path, PurePosixPath
import shutil
from urllib.error import HTTPError, URLError
from urllib.request import Request as UrlRequest, urlopen
from typing import Annotated
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal

from app.auth import AuthenticatedAdministrator, ElevatedVaultControlAdministrator, AuthenticatedUsername, get_authentication_store
from app.auth_store import AuthenticationStore
from app.config import get_database_conninfo, get_metadata_storage_root
from app.share_grants import (
    ACTIVE_GRANT_STATE,
    LOCAL_ALL_TARGET,
    LOCAL_USER_TARGET,
    PENDING_GRANT_STATE,
    PostgresShareGrantStore,
)
from app.federation import FEDERATION_PROTOCOL_VERSION, FederationStore, FederatedDownloadOperation, sign_envelope
from app.federated_download_executor import FederatedDownloadPromotionRequest, queue_signed_request
from app.incoming import get_incoming_path
from app.arrival_managed_publisher import reissue_item as reissue_arrival_theatre_item
from app.theatre_movie_rename import queue_movie_rename
from app.reading_room_intake import (
    CORRECTION_AUTHOR_KEY,
    CORRECTION_TITLE_KEY,
    PublicationBundle,
    build_publication_bundles,
    parse_publication_filename,
)
from app.reading_room_storage import (
    PublicationStorageFile,
    publication_directory,
    publication_role_path,
    safely_publish_publication_directory,
)
from app.vault_master_reading import (
    PublicationFile,
    PublicationSnapshot,
    ReadingRoomStore,
    get_reading_room_store,
)
from app.vault_master_reading_extraction import (
    MAX_FLORENCE_PAGES_PER_RUN,
    extract_publication,
    render_exact_page,
)
from app.vault_master_reading_review import (
    PublicationReview,
    PublicationReviewStore,
    caption_illustration,
    correct_page_order,
    correct_review_block,
    correct_review_metadata,
    get_publication_review_store,
    review_document,
    review_publication_issue,
    transition_review,
    write_reviewed_html,
)
from app.vault_master import (
    CHECKSUM_CHUNK_BYTES,
    INCOMING_SOURCE,
    INVENTORY_SOURCE,
    MAKEMKV_TRACK_PATTERN,
    SCREENSHOT_ARCHIVE_SUBFOLDER,
    CataloguedAsset,
    ImportItem,
    MemoryVaultMasterStore,
    VaultMasterStore,
    analyse_asset_relationship,
    asset_is_editable_by,
    canonical_relationship_type,
    canonical_movie_destination,
    enqueue_catalogue_backfill,
    get_inventory_paths,
    get_vault_master_store,
    has_hard_coded_screenshot_marker,
    is_theatre_category,
    movie_publication_set_has_consistent_audience,
    movie_publication_set_destination,
    movie_publication_set_is_ready,
    tv_publication_set_destination,
    tv_publication_set_has_consistent_audience,
    tv_publication_set_is_ready,
    enqueue_root,
    require_file_within_root,
    safely_remove_rejected_arrival_item,
    safely_remove_exact_duplicate,
    sha256_file,
)
from app.vault_master_sidecars import (
    canonical_sidecar_path,
    compare_sidecar_recovery,
    read_restorable_sidecar,
)
from app.vault_master_ai import (
    AiStore,
    get_ai_store,
    request_florence_ocr,
)
from app.vault_master_ingestion_ai import (
    IngestionAiStore,
    get_ingestion_ai_store,
    render_gallery_pdf_preview,
    routing_features,
    routing_signature,
)
from app.tv_shows import parse_reviewed_episode
from app.tv_disc_resolver import discover_tv_disc_batches, resolve_tv_disc_batch
from app.tv_resolver_publication import PostgresTvResolverStore
from app.vault_master_autopilot import (
    AUTOPILOT_MAX_FAILURE_PERCENT,
    AUTOPILOT_MAX_FAILURES,
    AUTOPILOT_MAX_ITEMS,
    AutopilotStore,
    audit_recent_gallery_screenshots,
    get_autopilot_store,
    process_autopilot_batch,
)


router = APIRouter(prefix="/api/vault-master", tags=["vault-master"])
VaultMasterStoreDependency = Annotated[
    VaultMasterStore,
    Depends(get_vault_master_store),
]
AuthenticationStoreDependency = Annotated[
    AuthenticationStore,
    Depends(get_authentication_store),
]
AiStoreDependency = Annotated[AiStore, Depends(get_ai_store)]
IngestionAiStoreDependency = Annotated[
    IngestionAiStore,
    Depends(get_ingestion_ai_store),
]
AutopilotStoreDependency = Annotated[
    AutopilotStore,
    Depends(get_autopilot_store),
]
PublicationReviewStoreDependency = Annotated[
    PublicationReviewStore,
    Depends(get_publication_review_store),
]
ReadingRoomStoreDependency = Annotated[
    ReadingRoomStore,
    Depends(get_reading_room_store),
]


class AiJobResult(BaseModel):
    id: UUID
    asset_id: UUID
    requested_by: str
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AiSuggestionResult(BaseModel):
    id: UUID
    job_id: UUID
    asset_id: UUID
    suggestion_type: str
    raw_value: str
    reviewed_value: str | None
    confidence: float | None
    model_id: str
    model_revision: str
    task_version: str
    processing_ms: int
    status: str
    requested_by: str
    reviewed_by: str | None
    created_at: datetime
    reviewed_at: datetime | None


class VisualDescriptionEvidenceResult(BaseModel):
    caption: str
    confidence: float
    model_id: str
    model_revision: str
    task_version: str
    created_at: datetime


class AiEvidenceResult(BaseModel):
    jobs: list[AiJobResult]
    suggestions: list[AiSuggestionResult]
    visual_description: VisualDescriptionEvidenceResult | None = None
    publication_rule: str = "owner_review_evidence_only"


class AiSuggestionReview(BaseModel):
    status: Literal["accepted", "rejected", "deferred"]
    value: str | None = Field(default=None, max_length=100000)


class IngestionAiJobResult(BaseModel):
    id: UUID
    item_id: UUID
    requested_by: str
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class IngestionAiEvidenceResult(BaseModel):
    id: UUID
    job_id: UUID
    item_id: UUID
    content_type: str
    caption: str
    ocr_text: str
    confidence: float
    reasons: tuple[str, ...]
    model_id: str
    model_revision: str
    task_version: str
    processing_ms: int
    recommended_destination: str | None
    decision_score: int
    routing_band: str
    confidence_components: dict[str, float]
    conflicts: tuple[str, ...]
    automatic_disqualifiers: tuple[str, ...]
    decision_model_version: str
    requested_by: str
    created_at: datetime


class IngestionAiItemEvidence(BaseModel):
    item_id: UUID
    jobs: list[IngestionAiJobResult]
    evidence: list[IngestionAiEvidenceResult]


class IngestionAiEvidenceListing(BaseModel):
    items: list[IngestionAiItemEvidence]
    publication_rule: str = "private_review_evidence_only"


class IngestionAnalysisBatchRequest(BaseModel):
    item_ids: list[UUID] = Field(min_length=1, max_length=500)


class IngestionAnalysisGroup(BaseModel):
    destination: str | None
    content_type: str
    routing_band: str
    explanation: str
    item_ids: list[UUID]
    count: int


class IngestionAnalysisBatchResult(BaseModel):
    id: UUID
    requested_by: str
    status: str
    total_items: int
    queued_items: int
    processing_items: int
    completed_items: int
    failed_items: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    groups: list[IngestionAnalysisGroup] = Field(default_factory=list)


class IngestionAnalysisBatchListing(BaseModel):
    batches: list[IngestionAnalysisBatchResult]


class IngestionReviewBatchRequest(BaseModel):
    action: Literal["approve", "reject", "move"]
    item_ids: list[UUID] = Field(min_length=1, max_length=500)


class RoutingMemoryRuleResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    feature_signature: str
    features: dict[str, str]
    destination: str
    example_count: int
    contradiction_count: int
    confidence: float
    maturity: str
    status: str
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime
    affected_item_ids: list[UUID] = Field(default_factory=list)


class RoutingMemoryListing(BaseModel):
    rules: list[RoutingMemoryRuleResult]


class RoutingMemoryUpdate(BaseModel):
    action: Literal["enable", "disable", "reset", "edit"]
    destination: Literal[
        "Gallery", "Home Videos", "Music", "Movies", "TV Shows", "Documents", "Archives", "Ledger"
    ] | None = None


class AutopilotPolicyUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_type: Literal[
        "personal_photo", "receipt", "financial_document", "general_document", "artwork"
    ] = "personal_photo"
    destination: Literal["Gallery", "Documents", "Ledger", "Archives"] = "Gallery"
    threshold: int = Field(default=80, ge=80, le=100)
    max_items: int = Field(default=50, ge=1, le=AUTOPILOT_MAX_ITEMS)
    max_failures: int = Field(default=2, ge=1, le=AUTOPILOT_MAX_FAILURES)
    max_failure_percent: int = Field(
        default=5, ge=1, le=AUTOPILOT_MAX_FAILURE_PERCENT
    )


class AutopilotPolicyStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["enabled", "paused", "disabled"]


class RejectedArrivalRemovalConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation: Literal["REMOVE FROM ARRIVAL HALL"]


class AutopilotPolicyResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
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


class AutopilotRunResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)
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


class AutopilotListing(BaseModel):
    policies: list[AutopilotPolicyResult]
    runs: list[AutopilotRunResult]


class GalleryScreenshotSuspectResult(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    item_id: UUID
    run_id: UUID
    filename: str
    vault_path: str
    sha256: str
    reasons: tuple[str, ...]
    moved_at: datetime


class GalleryScreenshotAuditResult(BaseModel):
    suspects: list[GalleryScreenshotSuspectResult]
    action: Literal["report_only"] = "report_only"


class VaultMasterItem(BaseModel):
    id: UUID
    batch_id: UUID
    source_kind: str
    source_path: str
    relative_path: str
    filename: str
    size_bytes: int
    mime_type: str
    modified_at: datetime
    sha256: str
    state: str
    duplicate_of_id: UUID | None
    proposed_category: str | None
    proposed_destination: str | None
    proposal_reason: str | None
    proposal_confidence: str | None
    metadata: dict[str, object]
    metadata_overrides: dict[str, object]
    publication_audience: Literal["vault-wide", "private"] | None = None


class VaultMasterListing(BaseModel):
    items: list[VaultMasterItem]


class ScanResult(BaseModel):
    batch_ids: list[UUID]
    status: str
    reused_active_batches: int = 0


class VaultMasterJob(BaseModel):
    id: UUID
    source_kind: str
    status: str
    item_count: int = 0
    error: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None


class VaultMasterJobListing(BaseModel):
    jobs: list[VaultMasterJob]


class VaultMasterActivityEntry(BaseModel):
    id: UUID
    batch_id: UUID | None
    item_id: UUID | None
    source_kind: str | None
    filename: str | None
    action: str
    username: str | None
    detail: str
    succeeded: bool
    created_at: datetime


class VaultMasterActivityListing(BaseModel):
    events: list[VaultMasterActivityEntry]


class SidecarReconciliationResult(BaseModel):
    checked: int
    current: int
    repaired: int
    failed: int


class SidecarRecoveryCandidateResult(BaseModel):
    sidecar_name: str
    status: str
    detail: str
    asset_id: UUID | None
    display_title: str | None
    vault_path: str | None
    filename: str | None


class SidecarRecoveryAssessmentResult(BaseModel):
    discovered: int
    valid: int
    invalid: int
    unsupported: int
    current: int
    hidden: int
    recoverable: int
    intentionally_deleted: int
    media_missing: int
    restorable: int
    conflicting: int
    path_conflicts: int
    candidates: list[SidecarRecoveryCandidateResult]


class PublicationBundleResult(BaseModel):
    key: str
    author: str
    title: str
    source_item_ids: tuple[UUID, ...]
    front_cover_item_ids: tuple[UUID, ...]
    back_cover_item_ids: tuple[UUID, ...]
    issues: tuple[str, ...]
    review_status: Literal["review_required"]


class PublicationBundleListing(BaseModel):
    bundles: list[PublicationBundleResult]
    publication_rule: str = "owner_review_required"


class PublicationIdentityCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    author: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=240)

    @field_validator("author", "title")
    @classmethod
    def validate_identity_text(cls, value: str) -> str:
        return " ".join(value.split())


class PublicationExtractionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_florence_pages: int = Field(default=1, ge=0, le=MAX_FLORENCE_PAGES_PER_RUN)


class PublicationExtractionResult(BaseModel):
    source_item_id: UUID
    page_count: int
    completed_pages: int
    pending_pages: tuple[int, ...]
    review: dict[str, object] | None


class PublicationMetadataCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    values: dict[str, str] = Field(default_factory=dict)
    language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$")
    publication_type: Literal["book", "magazine", "comic", "journal", "other"] | None = None


class PublicationBlockCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=100000)
    block_type: Literal["part", "chapter", "heading", "paragraph", "footnote", "caption", "table", "other"] | None = None


class PublicationPageCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_order: tuple[int, ...]
    rotations: dict[int, Literal[0, 90, 180, 270]] = Field(default_factory=dict)


class PublicationIssueReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["accepted", "resolved", "rejected"]


class PublicationCaptionCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    caption: str = Field(min_length=1, max_length=1000)


class PublicationReviewAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["retry", "defer", "reject", "publish"]


class VaultAsset(BaseModel):
    id: UUID
    asset_type: str
    display_title: str
    captured_on: date | None
    location: str | None
    vault_path: str | None = None
    filename: str | None = None
    size_bytes: int | None = None
    mime_type: str | None = None
    sha256: str | None = None
    metadata: dict[str, object] | None = None
    metadata_provenance: dict[str, str] | None = None
    origin_vault_id: UUID | None = None
    lifecycle_state: Literal["active", "hidden"] | None = None


class VaultAssetSearchResult(BaseModel):
    assets: list[VaultAsset]


class RelationshipAffectedFile(BaseModel):
    asset_id: UUID
    vault_path: str
    filename: str
    size_bytes: int
    mime_type: str
    sha256: str


class AssetRelationshipCandidate(BaseModel):
    classification: Literal[
        "exact_duplicate",
        "probable_duplicate",
        "alternate_version",
        "related_file",
    ]
    confidence: Literal["certain", "high", "medium", "low"]
    evidence: list[str]
    affected_files: list[RelationshipAffectedFile]


class AssetRelationshipCandidateListing(BaseModel):
    candidates: list[AssetRelationshipCandidate]


class CanonicalAssetRelationship(BaseModel):
    relationship_type: Literal["duplicate", "alternate_version", "related_file"]
    confidence: Literal["certain", "high", "medium", "low"]
    evidence: list[str]
    created_by: str
    created_at: datetime
    affected_files: list[RelationshipAffectedFile]


class CanonicalAssetRelationshipListing(BaseModel):
    relationships: list[CanonicalAssetRelationship]


class AssetRelationshipReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_asset_id: UUID


class VaultAssetAccessPolicy(BaseModel):
    owner_username: str
    visibility: Literal["private", "shared"]
    shared_with: list[str]


class VaultAssetAccessPolicyEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visibility: Literal["private", "shared"]
    shared_with: list[str] = Field(default_factory=list, max_length=25)

    @field_validator("shared_with")
    @classmethod
    def validate_shared_with(cls, value: list[str]) -> list[str]:
        cleaned = [username.strip() for username in value]
        if any(not username for username in cleaned):
            raise ValueError("Shared usernames cannot be blank")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Shared usernames must be unique")
        return cleaned


class LocalShareRecipient(BaseModel):
    user_id: UUID
    display_name: str
    avatar_label: str


class LocalAssetSharingState(BaseModel):
    owner_username: str
    mode: Literal["private", "everyone", "specific"]
    recipients: list[LocalShareRecipient]
    eligible_users: list[LocalShareRecipient]
    pending: bool = False


class LocalAssetSharingEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["private", "everyone", "specific"]
    recipient_user_ids: list[UUID] = Field(default_factory=list, max_length=25)
    share_mode: Literal["quick", "standard"] = "quick"

    @field_validator("recipient_user_ids")
    @classmethod
    def validate_recipient_user_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Share recipients must be unique")
        return value


class BulkLocalAssetSharingEdit(LocalAssetSharingEdit):
    asset_ids: list[UUID] = Field(min_length=1, max_length=100)

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("Selected assets must be unique")
        return value


class BulkLocalAssetSharingResult(BaseModel):
    asset_ids: list[UUID]


class OutgoingShareGrant(BaseModel):
    asset_id: UUID
    asset_title: str
    preview_url: str | None = None
    target_type: Literal["local_all", "local_user"]
    recipient: LocalShareRecipient | None = None


class OutgoingCollectionMember(BaseModel):
    asset_id: UUID
    asset_title: str


class SharedCollectionResponse(BaseModel):
    collection_id: UUID
    name: str
    description: str | None
    owner_display_name: str
    member_count: int
    is_federated: bool = False
    origin_vault_id: UUID | None = None
    origin_collection_id: UUID | None = None
    state: str = "active"


class OutgoingShareOperation(BaseModel):
    operation_id: UUID
    share_mode: Literal["quick", "standard"]
    state: Literal["pending", "active", "revoked"]
    created_at: datetime
    release_at: datetime | None
    activated_at: datetime | None
    revoked_at: datetime | None
    grants: list[OutgoingShareGrant]
    subject_type: Literal["assets", "collection"] = "assets"
    collection: SharedCollectionResponse | None = None
    collection_members: list[OutgoingCollectionMember] = Field(default_factory=list)


class OutgoingShareListing(BaseModel):
    operations: list[OutgoingShareOperation]


class CommonsSharedAsset(BaseModel):
    asset_id: UUID
    asset_type: str
    display_title: str
    captured_on: date | None
    owner_display_name: str
    preview_url: str | None = None
    origin_vault_id: UUID | None = None
    origin_asset_id: UUID | None = None
    is_federated: bool = False
    origin_metadata: dict[str, object] | None = None
    metadata_revision: int | None = None
    content_url: str | None = None
    cache_state: str | None = None
    download_allowed: bool = False


class CommonsSharedListing(BaseModel):
    assets: list[CommonsSharedAsset]


class FederationPeerCreate(BaseModel):
    remote_vault_id: UUID
    display_label: str = Field(min_length=1, max_length=160)
    endpoint: str = Field(min_length=8, max_length=1000)
    pairing_key: str = Field(min_length=32, max_length=512)


class FederationPeerResponse(BaseModel):
    remote_vault_id: UUID
    display_label: str
    trust_state: str


class FederationDiagnosticsResponse(BaseModel):
    pending_deliveries: int
    failed_deliveries: int
    stuck_deliveries: int
    incomplete_cache: int
    invalidated_cache: int
    open_downloads: int


class FederationAuditRecord(BaseModel):
    event_type: str
    origin_vault_id: UUID | None = None
    target_vault_id: UUID | None = None
    federation_share_id: UUID | None = None
    detail: str
    created_at: datetime


class FederatedShareCreate(BaseModel):
    asset_ids: list[UUID] = Field(min_length=1, max_length=100)
    target_vault_id: UUID
    share_mode: Literal["quick", "standard"] = "quick"


class FederatedShareResult(BaseModel):
    federation_share_ids: list[UUID]


class FederatedCollectionShareCreate(BaseModel):
    target_vault_id: UUID
    share_mode: Literal["quick", "standard"] = "quick"


class FederatedCollectionShareResult(BaseModel):
    federation_collection_share_id: UUID


class FederatedOutgoingShare(BaseModel):
    federation_share_id: UUID
    origin_asset_id: UUID
    target_vault_id: UUID
    target_label: str
    display_title: str
    asset_type: str
    share_mode: str
    state: str
    release_at: datetime | None
    preview_url: str | None = None
    download_allowed: bool = False


class FederatedOutgoingCollection(BaseModel):
    federation_collection_share_id: UUID
    origin_collection_id: UUID
    target_vault_id: UUID
    target_label: str
    name: str
    member_count: int
    share_mode: str
    state: str
    release_at: datetime | None


class FederatedDownloadPermissionEdit(BaseModel):
    """Owner-controlled, per-federated-share permission for Stage 9 copies."""

    download_allowed: bool


class FederatedDownloadRequest(BaseModel):
    """The client supplies only a replay-safe idempotency identity."""

    idempotency_key: UUID


class FederatedDownloadOperationResponse(BaseModel):
    operation_id: UUID
    local_asset_id: UUID | None
    state: str


class IncomingFederatedShareResponse(BaseModel):
    incoming_share_id: UUID
    origin_vault_id: UUID
    origin_asset_id: UUID
    origin_share_id: UUID
    owner_label: str
    asset_type: str
    display_title: str
    captured_on: date | None
    state: str
    preview_url: str | None = None
    download_allowed: bool = False
    origin_metadata: dict[str, object] | None = None
    metadata_revision: int | None = None


class IncomingFederatedShareListing(BaseModel):
    shares: list[IncomingFederatedShareResponse]


class IncomingFederatedCollectionResponse(BaseModel):
    incoming_collection_id: UUID
    origin_vault_id: UUID
    origin_collection_id: UUID
    origin_collection_share_id: UUID
    owner_label: str
    name: str
    description: str | None = None
    category: str
    state: str
    lifecycle_revision: int
    membership_revision: int
    member_count: int


class IncomingFederatedCollectionListing(BaseModel):
    collections: list[IncomingFederatedCollectionResponse]


class IncomingFederatedDistributionEdit(BaseModel):
    mode: Literal["everyone", "specific"]
    recipient_user_ids: list[UUID] = Field(default_factory=list, max_length=100)


class FederatedLocalAnnotationEdit(BaseModel):
    note: str | None = Field(default=None, max_length=2000)
    alias: str | None = Field(default=None, max_length=500)
    tags: list[str] = Field(default_factory=list, max_length=64)


class SharedCollectionListing(BaseModel):
    collections: list[SharedCollectionResponse]


class SharedCollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    asset_ids: list[UUID] = Field(min_length=2, max_length=250)

    @field_validator("asset_ids")
    @classmethod
    def validate_asset_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(value) != len(set(value)):
            raise ValueError("Collection assets must be unique")
        return value


class SharedCollectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=1000)


class SharedCollectionMembersEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_ids: list[UUID] = Field(min_length=1, max_length=250)
    confirm_live_share: bool = False


class SharedCollectionShareEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["everyone", "specific"]
    recipient_user_ids: list[UUID] = Field(default_factory=list, max_length=25)
    share_mode: Literal["quick", "standard"] = "quick"


class VaultAssetHistoryEntry(BaseModel):
    id: UUID
    asset_id: UUID
    action: str
    username: str
    previous_values: dict[str, str | None]
    current_values: dict[str, str | None]
    created_at: datetime


class VaultAssetHistory(BaseModel):
    entries: list[VaultAssetHistoryEntry]


class QuarantineReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("The review reason cannot be blank")
        return cleaned


class QuarantinePreflight(BaseModel):
    """A non-destructive check of an owner's requested quarantine action."""

    ready: bool
    source_path: str
    proposed_quarantine_path: str | None = None
    checksum_verified: bool = False
    reason: str | None = None


class PermanentDeletionPreflight(BaseModel):
    """A non-destructive permanent-deletion eligibility assessment."""

    ready: bool
    source_path: str
    proposed_permanent_deletion_path: str | None = None
    checksum_verified: bool = False
    quarantined_at: datetime | None = None
    eligible_at: datetime | None = None
    reason: str | None = None


class QuarantineConfirmation(BaseModel):
    """An explicit owner confirmation for a recoverable Quarantine move."""

    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


class FolderMoveRequest(BaseModel):
    """A category-scoped, existing-folder-only relocation request."""

    model_config = ConfigDict(extra="forbid")

    category: Literal["Gallery", "Home Videos", "Documents", "Archives", "Music"]
    destination_folder: str = Field(default="", max_length=500)
    confirm: Literal[True] | None = None

    @field_validator("destination_folder")
    @classmethod
    def validate_destination_folder(cls, value: str) -> str:
        path = PurePosixPath(value.strip().replace("\\", "/"))
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            if value.strip() not in {"", "."}:
                raise ValueError("The destination folder is invalid")
        return "" if value.strip() in {"", "."} else str(path)


class FolderMovePreflight(BaseModel):
    ready: bool
    source_path: str
    destination_path: str | None = None
    checksum_verified: bool = False
    existing_destinations: list[str] = []
    reason: str | None = None


class BinRestorePreflight(BaseModel):
    ready: bool
    source_path: str
    destination_path: str | None = None
    checksum_verified: bool = False
    reason: str | None = None


class PermanentDeletionReviewRequest(BaseModel):
    """A reasoned, non-destructive request for permanent-deletion review."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A permanent-deletion reason is required")
        return cleaned


class PermanentDeletionConfirmation(BaseModel):
    """A second explicit authorization without deletion execution."""

    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


class PermanentDeletionExecution(BaseModel):
    """A final explicit instruction to execute confirmed deletion."""

    model_config = ConfigDict(extra="forbid")

    execute: Literal[True]


ApprovedCategory = Literal[
    "Gallery",
    "Home Videos",
    "Movies",
    "TV Shows",
    "Documents",
    "Archives",
    "Music",
]


class ProposalEdit(BaseModel):
    category: ApprovedCategory
    publication_audience: Literal["vault-wide", "private"] | None = None


class TheatreMovieRenameEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=240)
    release_year: int = Field(ge=1000, le=9999)
    confirm: Literal[True]

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("A reviewed movie title is required")
        return cleaned


class MetadataOverrideEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_title: str | None = Field(default=None, max_length=240)
    captured_on: date | None = None
    captured_at: datetime | None = None
    location: str | None = Field(default=None, max_length=240)
    artist: str | None = Field(default=None, max_length=240)
    album: str | None = Field(default=None, max_length=240)
    album_artist: str | None = Field(default=None, max_length=240)
    track_number: int | None = Field(default=None, ge=1)
    disc_number: int | None = Field(default=None, ge=1)
    release_year: int | None = Field(default=None, ge=1000, le=9999)

    @model_validator(mode="after")
    def validate_capture_values(self) -> "MetadataOverrideEdit":
        if (
            self.captured_at is not None
            and self.captured_on is not None
            and self.captured_at.date() != self.captured_on
        ):
            raise ValueError("Capture date must match the capture timestamp")
        return self

    @field_validator(
        "display_title",
        "location",
        "artist",
        "album",
        "album_artist",
    )
    @classmethod
    def validate_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("Metadata text cannot be blank")
        return cleaned

    @field_validator("release_year")
    @classmethod
    def validate_release_year(cls, value: int | None) -> int | None:
        if value is not None and value > date.today().year:
            raise ValueError("The release year cannot be in the future")
        return value

    @field_validator("captured_on")
    @classmethod
    def validate_captured_on(cls, value: date | None) -> date | None:
        if value is not None and value > date.today():
            raise ValueError("The date cannot be in the future")
        return value


class ItemSelection(BaseModel):
    item_ids: list[UUID] = Field(max_length=500)


class BulkActionResult(BaseModel):
    items: list[VaultMasterItem]


class IngestionReviewBatchResult(BaseModel):
    batch_id: UUID
    action: str
    outcomes: dict[str, str]
    items: list[VaultMasterItem]


class SidecarRestoreConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: Literal[True]


def get_destination_paths() -> dict[str, Path]:
    return {
        "Gallery": Path(os.getenv("PV_GALLERY_PATH", "/media/gallery")),
        "Home Videos": Path(
            os.getenv("PV_PERSONAL_VIDEOS_PATH", "/media/personal-videos")
        ),
        "Movies": Path(os.getenv("PV_MOVIES_PATH", "/media/movies")),
        "Documents": Path(
            os.getenv("PV_DOCUMENTS_PATH", "/media/documents")
        ),
        "Archives": Path(os.getenv("PV_ARCHIVES_PATH", "/media/archives")),
        "Music": Path(os.getenv("PV_MUSIC_PATH", "/media/music")),
        "Library": Path(os.getenv("PV_LIBRARY_PATH", "/media/library")),
    }


def get_catalogue_preview_roots() -> dict[str, Path]:
    return {
        "/vault/Theatre/Movies": Path(
            os.getenv("PV_MOVIES_PATH", "/media/movies")
        ),
        "/vault/Gallery": Path(
            os.getenv("PV_GALLERY_PATH", "/media/gallery")
        ),
        "/vault/Home Videos": Path(
            os.getenv(
                "PV_PERSONAL_VIDEOS_PATH",
                "/media/personal-videos",
            )
        ),
        "/vault/Documents": Path(
            os.getenv("PV_DOCUMENTS_PATH", "/media/documents")
        ),
        "/vault/Archives": Path(
            os.getenv("PV_ARCHIVES_PATH", "/media/archives")
        ),
        "/vault/Music": Path(os.getenv("PV_MUSIC_PATH", "/media/music")),
        "/vault/Library": Path(
            os.getenv("PV_LIBRARY_PATH", "/media/library")
        ),
        "/vault/Quarantine": get_quarantine_root(),
    }


def get_quarantine_root() -> Path:
    """Return the dedicated, recoverable Vault Master quarantine mount."""
    return Path(os.getenv("PV_QUARANTINE_PATH", "/vault/Quarantine"))


def get_quarantine_retention_days() -> int:
    """Return the configured minimum delay before permanent deletion."""
    raw_value = os.getenv("PV_QUARANTINE_RETENTION_DAYS", "30")
    try:
        retention_days = int(raw_value)
    except ValueError as error:
        raise ValueError("PV_QUARANTINE_RETENTION_DAYS must be a whole number") from error
    if retention_days < 1:
        raise ValueError("PV_QUARANTINE_RETENTION_DAYS must be at least 1")
    return retention_days


def _category_vault_root(category: str) -> str:
    return f"/vault/{category}"


def list_existing_move_destinations(category: str, preview_roots: dict[str, Path]) -> list[str]:
    """Return only real directories below a configured category root."""
    vault_root = _category_vault_root(category)
    root = preview_roots.get(vault_root)
    if root is None:
        return []
    try:
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            return []
        folders = [resolved_root, *sorted(path for path in resolved_root.rglob("*") if path.is_dir() and not path.is_symlink())]
        return [vault_root + ("/" + str(path.relative_to(resolved_root)).replace("\\", "/") if path != resolved_root else "") for path in folders if path.resolve().is_relative_to(resolved_root)]
    except OSError:
        return []


def preflight_catalogued_asset_folder_move(
    asset: CataloguedAsset,
    request: FolderMoveRequest,
    preview_roots: dict[str, Path],
) -> FolderMovePreflight:
    destinations = list_existing_move_destinations(request.category, preview_roots)
    category_root = _category_vault_root(request.category)
    destination_directory = category_root + (f"/{request.destination_folder}" if request.destination_folder else "")
    if destination_directory not in destinations:
        return FolderMovePreflight(ready=False, source_path=asset.vault_path, existing_destinations=destinations, reason="The destination must be an existing folder in the selected Vault category")
    root = preview_roots.get(category_root)
    if root is None:
        return FolderMovePreflight(ready=False, source_path=asset.vault_path, existing_destinations=destinations, reason="The selected Vault category is unavailable")
    try:
        source = resolve_catalogued_asset_path(asset, preview_roots)
        if sha256_file(source) != asset.sha256:
            raise ValueError("The file checksum no longer matches the catalogue")
        resolved_root = root.resolve(strict=True)
        folder = (resolved_root / request.destination_folder).resolve(strict=True)
        if not folder.is_relative_to(resolved_root) or not folder.is_dir() or folder.is_symlink():
            raise ValueError("The destination must be an existing folder in the selected Vault category")
        destination = folder / asset.filename
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("A file already exists at the requested destination")
    except (OSError, ValueError) as error:
        return FolderMovePreflight(ready=False, source_path=asset.vault_path, existing_destinations=destinations, reason=str(error))
    return FolderMovePreflight(ready=True, source_path=asset.vault_path, destination_path=f"{destination_directory}/{asset.filename}", checksum_verified=True, existing_destinations=destinations)


def copy_catalogued_asset_to_existing_folder(
    asset: CataloguedAsset, request: FolderMoveRequest, preview_roots: dict[str, Path]
) -> tuple[Path, Path, str]:
    preflight = preflight_catalogued_asset_folder_move(asset, request, preview_roots)
    if not preflight.ready or preflight.destination_path is None:
        raise ValueError(preflight.reason or "The move is not ready")
    source = resolve_catalogued_asset_path(asset, preview_roots)
    folder = preview_roots[_category_vault_root(request.category)].resolve(strict=True) / request.destination_folder
    destination = folder / asset.filename
    temporary = folder / f".vault-master-move-{asset.id.hex}-{uuid4().hex}.part"
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file, CHECKSUM_CHUNK_BYTES)
            output_file.flush()
            os.fsync(output_file.fileno())
        shutil.copystat(source, temporary, follow_symlinks=False)
        if sha256_file(source) != asset.sha256 or sha256_file(temporary) != asset.sha256:
            raise ValueError("Checksum verification failed while copying the file")
        os.link(temporary, destination)
        temporary.unlink()
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise
    return source, destination, preflight.destination_path


def preflight_catalogued_asset_bin_restore(asset: CataloguedAsset, preview_roots: dict[str, Path], quarantine_root: Path) -> BinRestorePreflight:
    """Restore only to the original path preserved under the recoverable Bin."""
    prefix = "/vault/Quarantine/"
    if not asset.vault_path.startswith(prefix):
        return BinRestorePreflight(ready=False, source_path=asset.vault_path, reason="Only a Bin item can be restored")
    original_path = "/vault/" + asset.vault_path.removeprefix(prefix)
    matched = next(((root, path) for root, path in preview_roots.items() if root != "/vault/Quarantine" and (original_path == root or original_path.startswith(root + "/"))), None)
    if matched is None:
        return BinRestorePreflight(ready=False, source_path=asset.vault_path, reason="The recorded original Vault path is unavailable")
    vault_root, filesystem_root = matched
    try:
        source = require_file_within_root(quarantine_root.resolve(strict=True) / asset.vault_path.removeprefix(prefix), quarantine_root.resolve(strict=True))
        destination = (filesystem_root.resolve(strict=True) / original_path.removeprefix(vault_root).lstrip("/")).resolve(strict=False)
        if not destination.parent.is_relative_to(filesystem_root.resolve(strict=True)) or not destination.parent.is_dir():
            raise ValueError("The recorded original folder no longer exists")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("A file already exists at the recorded original path")
        if sha256_file(source) != asset.sha256:
            raise ValueError("The Bin file checksum no longer matches the catalogue")
    except (OSError, ValueError) as error:
        return BinRestorePreflight(ready=False, source_path=asset.vault_path, reason=str(error))
    return BinRestorePreflight(ready=True, source_path=asset.vault_path, destination_path=original_path, checksum_verified=True)


def copy_catalogued_asset_from_bin(asset: CataloguedAsset, preview_roots: dict[str, Path], quarantine_root: Path) -> tuple[Path, Path, str]:
    preflight = preflight_catalogued_asset_bin_restore(asset, preview_roots, quarantine_root)
    if not preflight.ready or preflight.destination_path is None:
        raise ValueError(preflight.reason or "The Bin restore is not ready")
    source = quarantine_root.resolve(strict=True) / asset.vault_path.removeprefix("/vault/Quarantine/")
    destination = preview_roots[next(root for root in preview_roots if preflight.destination_path == root or preflight.destination_path.startswith(root + "/"))].resolve(strict=True) / preflight.destination_path.removeprefix(next(root for root in preview_roots if preflight.destination_path == root or preflight.destination_path.startswith(root + "/"))).lstrip("/")
    temporary = destination.parent / f".vault-master-bin-restore-{asset.id.hex}-{uuid4().hex}.part"
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file, CHECKSUM_CHUNK_BYTES); output_file.flush(); os.fsync(output_file.fileno())
        if sha256_file(source) != asset.sha256 or sha256_file(temporary) != asset.sha256:
            raise ValueError("Checksum verification failed while restoring the Bin file")
        os.link(temporary, destination); temporary.unlink()
    except Exception:
        if temporary.exists() and not temporary.is_symlink(): temporary.unlink()
        raise
    return source, destination, preflight.destination_path


def preflight_catalogued_asset_quarantine(
    asset: CataloguedAsset,
    preview_roots: dict[str, Path],
    quarantine_root: Path,
) -> QuarantinePreflight:
    """Validate a possible quarantine move without creating, moving or deleting.

    Quarantine preserves the Vault-relative library path below its own dedicated
    root.  The later execution step will repeat every one of these checks before
    it changes either filesystem location.
    """
    matched = next(
        (
            (vault_root, filesystem_root)
            for vault_root, filesystem_root in preview_roots.items()
            if asset.vault_path == vault_root
            or asset.vault_path.startswith(f"{vault_root}/")
        ),
        None,
    )
    if matched is None:
        return QuarantinePreflight(
            ready=False,
            source_path=asset.vault_path,
            reason="The asset is outside a configured Vault library",
        )

    vault_root, filesystem_root = matched
    if vault_root == "/vault/Quarantine":
        return QuarantinePreflight(
            ready=False,
            source_path=asset.vault_path,
            reason="The asset is already in Quarantine.",
        )
    if vault_root == "/vault/Theatre/Movies":
        return QuarantinePreflight(
            ready=False,
            source_path=asset.vault_path,
            reason="The Theatre Movies library is read-only and cannot be quarantined",
        )
    try:
        source = require_file_within_root(
            filesystem_root
            / asset.vault_path.removeprefix(vault_root).lstrip("/"),
            filesystem_root,
        )
        resolved_quarantine_root = quarantine_root.resolve(strict=True)
        if not resolved_quarantine_root.is_dir():
            raise ValueError("The configured Quarantine path is not a directory")
        if sha256_file(source) != asset.sha256:
            raise ValueError("The file checksum no longer matches the catalogue")

        vault_relative = asset.vault_path.removeprefix("/vault/")
        relative_parts = vault_relative.split("/")
        if not vault_relative or any(
            not part or part in {".", ".."} for part in relative_parts
        ):
            raise ValueError("The catalogue path is not safe for Quarantine")
        destination = resolved_quarantine_root.joinpath(*relative_parts)
        if not destination.is_relative_to(resolved_quarantine_root):
            raise ValueError("The Quarantine destination is outside its root")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("A Quarantine file already exists at this path")
    except (OSError, ValueError) as error:
        return QuarantinePreflight(
            ready=False,
            source_path=asset.vault_path,
            reason=str(error),
        )

    return QuarantinePreflight(
        ready=True,
        source_path=asset.vault_path,
        proposed_quarantine_path=f"/vault/Quarantine/{vault_relative}",
        checksum_verified=True,
    )


def preflight_catalogued_asset_permanent_deletion(
    asset: CataloguedAsset,
    quarantine_root: Path,
    asset_history: list[dict[str, object]],
    retention_days: int,
    preview_roots: dict[str, Path] | None = None,
    assessed_at: datetime | None = None,
) -> PermanentDeletionPreflight:
    """Assess permanent-deletion eligibility without modifying files or records."""
    quarantine_vault_root = "/vault/Quarantine"
    if not asset.vault_path.startswith(f"{quarantine_vault_root}/"):
        if preview_roots is None:
            return PermanentDeletionPreflight(
                ready=False,
                source_path=asset.vault_path,
                reason="The canonical storage path is unavailable for permanent deletion",
            )
        try:
            source = resolve_catalogued_asset_path(asset, preview_roots)
            if source.stat().st_size != asset.size_bytes or sha256_file(source) != asset.sha256:
                raise ValueError("The file checksum no longer matches the catalogue")
        except (OSError, ValueError) as error:
            return PermanentDeletionPreflight(
                ready=False,
                source_path=asset.vault_path,
                reason=str(error),
            )
        return PermanentDeletionPreflight(
            ready=True,
            source_path=asset.vault_path,
            proposed_permanent_deletion_path=asset.vault_path,
            checksum_verified=True,
            eligible_at=assessed_at or datetime.now(timezone.utc),
        )
    if asset.vault_path == quarantine_vault_root:
        return PermanentDeletionPreflight(
            ready=False,
            source_path=asset.vault_path,
            reason=(
                "The Quarantine root is not a file that can be permanently deleted"
            ),
        )

    quarantine_entry = next(
        (entry for entry in asset_history if entry.get("action") == "quarantined"),
        None,
    )
    quarantined_at = (
        quarantine_entry.get("created_at") if quarantine_entry is not None else None
    )
    if not isinstance(quarantined_at, datetime) or quarantined_at.tzinfo is None:
        return PermanentDeletionPreflight(
            ready=False,
            source_path=asset.vault_path,
            reason="The asset has no verified Quarantine timestamp",
        )
    eligible_at = quarantined_at + timedelta(days=retention_days)
    now = assessed_at or datetime.now(timezone.utc)
    if now < eligible_at:
        return PermanentDeletionPreflight(
            ready=False,
            source_path=asset.vault_path,
            quarantined_at=quarantined_at,
            eligible_at=eligible_at,
            reason=(
                f"The {retention_days}-day Quarantine retention period has not elapsed"
            ),
        )

    vault_relative = asset.vault_path.removeprefix(f"{quarantine_vault_root}/")
    relative_parts = vault_relative.split("/")
    if not vault_relative or any(
        not part or part in {".", ".."} for part in relative_parts
    ):
        return PermanentDeletionPreflight(
            ready=False,
            source_path=asset.vault_path,
            quarantined_at=quarantined_at,
            eligible_at=eligible_at,
            reason="The Quarantine path is invalid",
        )

    try:
        resolved_quarantine_root = quarantine_root.resolve(strict=True)
        if not resolved_quarantine_root.is_dir():
            raise ValueError("The configured Quarantine path is not a directory")
        source = require_file_within_root(
            resolved_quarantine_root.joinpath(*relative_parts),
            resolved_quarantine_root,
        )
        if sha256_file(source) != asset.sha256:
            raise ValueError("The file checksum no longer matches the catalogue")
    except (OSError, ValueError) as error:
        return PermanentDeletionPreflight(
            ready=False,
            source_path=asset.vault_path,
            quarantined_at=quarantined_at,
            eligible_at=eligible_at,
            reason=str(error),
        )

    return PermanentDeletionPreflight(
        ready=True,
        source_path=asset.vault_path,
        proposed_permanent_deletion_path=asset.vault_path,
        checksum_verified=True,
        quarantined_at=quarantined_at,
        eligible_at=eligible_at,
    )


def copy_catalogued_asset_to_quarantine(
    asset: CataloguedAsset,
    preview_roots: dict[str, Path],
    quarantine_root: Path,
) -> tuple[Path, Path, str]:
    """Create a checksum-verified Quarantine copy without overwriting.

    The catalogue and source stay unchanged until this copy has been fully
    written and verified.  The caller is responsible for the later catalogue
    update and original removal.
    """
    preflight = preflight_catalogued_asset_quarantine(
        asset,
        preview_roots,
        quarantine_root,
    )
    if not preflight.ready or preflight.proposed_quarantine_path is None:
        raise ValueError(preflight.reason or "The Quarantine move is not ready")

    matched = next(
        (
            (vault_root, filesystem_root)
            for vault_root, filesystem_root in preview_roots.items()
            if asset.vault_path == vault_root
            or asset.vault_path.startswith(f"{vault_root}/")
        ),
        None,
    )
    if matched is None:
        raise ValueError("The asset is outside a configured Vault library")
    vault_root, filesystem_root = matched
    source = require_file_within_root(
        filesystem_root / asset.vault_path.removeprefix(vault_root).lstrip("/"),
        filesystem_root,
    )
    resolved_quarantine_root = quarantine_root.resolve(strict=True)
    vault_relative = asset.vault_path.removeprefix("/vault/")
    destination = resolved_quarantine_root.joinpath(*vault_relative.split("/"))

    parent = resolved_quarantine_root
    for part in destination.relative_to(resolved_quarantine_root).parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise ValueError("The Quarantine destination contains a symbolic link")
        parent.mkdir(exist_ok=True)
        if not parent.is_dir():
            raise ValueError("The Quarantine destination parent is not a directory")

    if destination.exists() or destination.is_symlink():
        raise FileExistsError("A Quarantine file already exists at this path")
    temporary = destination.parent / (
        f".vault-master-quarantine-{asset.id.hex}-{uuid4().hex}.part"
    )
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file, CHECKSUM_CHUNK_BYTES)
            output_file.flush()
            os.fsync(output_file.fileno())
        shutil.copystat(source, temporary, follow_symlinks=False)
        if sha256_file(source) != asset.sha256:
            raise ValueError("The source checksum changed while copying to Quarantine")
        if sha256_file(temporary) != asset.sha256:
            raise ValueError("The Quarantine copy checksum does not match the catalogue")
        os.link(temporary, destination)
        temporary.unlink()
    except Exception:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()
        raise
    return source, destination, preflight.proposed_quarantine_path


def resolve_catalogued_asset_path(
    asset: CataloguedAsset,
    preview_roots: dict[str, Path],
) -> Path:
    # Multi-slot files carry an authoritative placement.  Legacy files keep
    # their established logical-root resolution until safely backfilled.
    from app.storage_placement import resolve_metadata_placement

    placement_path = resolve_metadata_placement(asset.effective_metadata)
    if placement_path is not None:
        return placement_path
    matched = next(
        (
            (vault_root, filesystem_root)
            for vault_root, filesystem_root in preview_roots.items()
            if asset.vault_path == vault_root
            or asset.vault_path.startswith(f"{vault_root}/")
        ),
        None,
    )
    if matched is None:
        raise ValueError("The asset is outside a configured Vault library")
    vault_root, filesystem_root = matched
    relative_path = asset.vault_path.removeprefix(vault_root).lstrip("/")
    resolved_root = filesystem_root.resolve(strict=True)
    resolved_file = (resolved_root / relative_path).resolve(strict=True)
    if not resolved_file.is_relative_to(resolved_root) or not resolved_file.is_file():
        raise ValueError("The catalogued file is unavailable")
    return resolved_file


def resolve_owned_artwork_path(
    asset: CataloguedAsset,
    kind: Literal["poster", "backdrop", "primary"],
    storage_root: Path,
) -> tuple[Path, str]:
    artwork = asset.imported_metadata.get("artwork")
    owned = artwork.get("owned") if isinstance(artwork, dict) else None
    record = owned.get(kind) if isinstance(owned, dict) else None
    if not isinstance(record, dict):
        raise ValueError("The asset has no retained artwork of this type")

    expected_key = f"artwork/{asset.id}/{kind}"
    if record.get("storage_key") != expected_key:
        raise ValueError("The retained artwork key is invalid")
    mime_type = record.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        raise ValueError("The retained artwork content type is invalid")

    resolved_root = storage_root.resolve(strict=True)
    resolved_file = (resolved_root / expected_key).resolve(strict=True)
    if (
        not resolved_file.is_relative_to(resolved_root)
        or not resolved_file.is_file()
    ):
        raise ValueError("The retained artwork file is unavailable")
    return resolved_file, mime_type


def resolve_owned_person_image_path(
    asset: CataloguedAsset,
    portrait_id: str,
    storage_root: Path,
) -> tuple[Path, str]:
    people = asset.imported_metadata.get("people")
    if not isinstance(people, list):
        raise ValueError("The asset has no retained person portraits")

    record: dict[str, object] | None = None
    for person in people:
        if not isinstance(person, dict):
            continue
        provider_item_id = person.get("provider_item_id")
        expected_id = (
            hashlib.sha256(provider_item_id.encode("utf-8")).hexdigest()[:16]
            if isinstance(provider_item_id, str)
            else None
        )
        candidate = person.get("owned_image")
        if expected_id == portrait_id and isinstance(candidate, dict):
            record = candidate
            break
    if record is None:
        raise ValueError("The asset has no retained person portrait")

    expected_key = f"artwork/{asset.id}/people/{portrait_id}"
    if record.get("storage_key") != expected_key:
        raise ValueError("The retained person portrait key is invalid")
    mime_type = record.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        raise ValueError("The retained person portrait type is invalid")

    resolved_root = storage_root.resolve(strict=True)
    resolved_file = (resolved_root / expected_key).resolve(strict=True)
    if (
        not resolved_file.is_relative_to(resolved_root)
        or not resolved_file.is_file()
    ):
        raise ValueError("The retained person portrait is unavailable")
    return resolved_file, mime_type


def resolve_owned_feature_image_path(
    asset: CataloguedAsset,
    feature_id: str,
    storage_root: Path,
) -> tuple[Path, str]:
    record: dict[str, object] | None = None
    for field in ("extras", "trailers"):
        entries = asset.imported_metadata.get(field)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            provider_item_id = entry.get("provider_item_id")
            expected_id = (
                hashlib.sha256(
                    provider_item_id.encode("utf-8")
                ).hexdigest()[:16]
                if isinstance(provider_item_id, str)
                else None
            )
            candidate = entry.get("owned_image")
            if expected_id == feature_id and isinstance(candidate, dict):
                record = candidate
                break
    if record is None:
        raise ValueError("The asset has no retained feature thumbnail")
    expected_key = f"artwork/{asset.id}/features/{feature_id}"
    if record.get("storage_key") != expected_key:
        raise ValueError("The retained feature thumbnail key is invalid")
    mime_type = record.get("mime_type")
    if not isinstance(mime_type, str) or not mime_type.startswith("image/"):
        raise ValueError("The retained feature thumbnail type is invalid")
    resolved_root = storage_root.resolve(strict=True)
    resolved_file = (resolved_root / expected_key).resolve(strict=True)
    if (
        not resolved_file.is_relative_to(resolved_root)
        or not resolved_file.is_file()
    ):
        raise ValueError("The retained feature thumbnail is unavailable")
    return resolved_file, mime_type


def to_api_item(item: ImportItem) -> VaultMasterItem:
    return VaultMasterItem(**item.__dict__)


def to_api_asset(asset: CataloguedAsset, username: str) -> VaultAsset:
    if asset_is_editable_by(asset, username):
        return VaultAsset(**asset.__dict__)
    # A shared family member receives only Vault Master's deliberately
    # published, basic view. Raw paths, checksums, technical file facts,
    # extraction evidence, and correction provenance remain owner-only.
    return VaultAsset(
        id=asset.id,
        asset_type=asset.asset_type,
        display_title=asset.display_title,
        captured_on=asset.captured_on,
        location=asset.location,
    )


@router.get(
    "/assets/search",
    response_model=VaultAssetSearchResult,
    response_model_exclude_none=True,
)
def search_vault_assets(
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    query: str = Query(min_length=1, max_length=240),
    limit: int = Query(default=50, ge=1, le=100),
) -> VaultAssetSearchResult:
    response.headers["Cache-Control"] = "private, no-store"
    return VaultAssetSearchResult(
        assets=[
            to_api_asset(asset, username)
            for asset in store.search_visible_catalogued_assets(
                query,
                username,
                limit,
            )
        ]
    )


@router.get(
    "/assets/{asset_id}/relationships/analysis",
    response_model=AssetRelationshipCandidateListing,
)
def analyse_catalogued_asset_relationships(
    asset_id: UUID,
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> AssetRelationshipCandidateListing:
    """Return owner-only, non-persistent relationship evidence."""
    selected = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if selected is None or not asset_is_editable_by(selected, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    candidates: list[AssetRelationshipCandidate] = []
    for other in store.list_owned_catalogued_assets(username):
        if other.id == selected.id or other.asset_type != selected.asset_type:
            continue
        analysis = analyse_asset_relationship(selected, other)
        if analysis.classification == "none":
            continue
        candidates.append(
            AssetRelationshipCandidate(
                classification=analysis.classification,
                confidence=analysis.confidence,
                evidence=list(analysis.evidence),
                affected_files=[
                    RelationshipAffectedFile(
                        asset_id=asset.id,
                        vault_path=asset.vault_path,
                        filename=asset.filename,
                        size_bytes=asset.size_bytes,
                        mime_type=asset.mime_type,
                        sha256=asset.sha256,
                    )
                    for asset in (selected, other)
                ],
            )
        )
    priority = {
        "exact_duplicate": 0,
        "probable_duplicate": 1,
        "alternate_version": 2,
        "related_file": 3,
    }
    candidates.sort(
        key=lambda candidate: (
            priority[candidate.classification],
            candidate.affected_files[1].filename.casefold(),
            str(candidate.affected_files[1].asset_id),
        )
    )
    response.headers["Cache-Control"] = "private, no-store"
    return AssetRelationshipCandidateListing(candidates=candidates)


@router.post(
    "/assets/{asset_id}/relationships/review",
    response_model=VaultAssetHistoryEntry,
    status_code=status.HTTP_201_CREATED,
)
def request_asset_relationship_review(
    asset_id: UUID,
    request: AssetRelationshipReviewRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAssetHistoryEntry:
    selected = store.get_visible_catalogued_asset_by_id(asset_id, username)
    candidate = store.get_visible_catalogued_asset_by_id(
        request.candidate_asset_id, username
    )
    if (
        selected is None
        or candidate is None
        or not asset_is_editable_by(selected, username)
        or not asset_is_editable_by(candidate, username)
        or selected.asset_type != candidate.asset_type
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        analysis = analyse_asset_relationship(selected, candidate)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    if analysis.classification == "none":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The selected assets have no deterministic relationship evidence",
        )
    entry = store.request_asset_relationship_review(
        selected.id,
        candidate.id,
        analysis.classification,
        analysis.confidence,
        analysis.evidence,
        username,
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A relationship review already exists for these assets",
        )
    return VaultAssetHistoryEntry(**entry)


@router.get(
    "/assets/{asset_id}/relationships",
    response_model=CanonicalAssetRelationshipListing,
)
def list_asset_relationships(
    asset_id: UUID,
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> CanonicalAssetRelationshipListing:
    selected = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if selected is None or not asset_is_editable_by(selected, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    results = []
    for relationship in store.list_catalogued_asset_relationships(asset_id):
        other_id = (
            relationship.second_asset_id
            if relationship.first_asset_id == asset_id
            else relationship.first_asset_id
        )
        other = store.get_visible_catalogued_asset_by_id(other_id, username)
        if other is None or not asset_is_editable_by(other, username):
            continue
        results.append(
            CanonicalAssetRelationship(
                relationship_type=relationship.relationship_type,
                confidence=relationship.confidence,
                evidence=list(relationship.evidence),
                created_by=relationship.created_by,
                created_at=relationship.created_at,
                affected_files=[
                    RelationshipAffectedFile(
                        asset_id=asset.id,
                        vault_path=asset.vault_path,
                        filename=asset.filename,
                        size_bytes=asset.size_bytes,
                        mime_type=asset.mime_type,
                        sha256=asset.sha256,
                    )
                    for asset in (selected, other)
                ],
            )
        )
    response.headers["Cache-Control"] = "private, no-store"
    return CanonicalAssetRelationshipListing(relationships=results)


@router.post(
    "/assets/{asset_id}/relationships/review/retain",
    response_model=VaultAssetHistoryEntry,
)
def retain_separate_asset_relationship_review(
    asset_id: UUID,
    request: AssetRelationshipReviewRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAssetHistoryEntry:
    selected = store.get_visible_catalogued_asset_by_id(asset_id, username)
    candidate = store.get_visible_catalogued_asset_by_id(
        request.candidate_asset_id, username
    )
    if (
        selected is None
        or candidate is None
        or not asset_is_editable_by(selected, username)
        or not asset_is_editable_by(candidate, username)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    entry = store.retain_separate_asset_relationship_review(
        selected.id, candidate.id, username
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending relationship review exists for these assets",
        )
    return VaultAssetHistoryEntry(**entry)


@router.post(
    "/assets/{asset_id}/relationships/review/link",
    response_model=VaultAssetHistoryEntry,
)
def approve_asset_relationship_review(
    asset_id: UUID,
    request: AssetRelationshipReviewRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAssetHistoryEntry:
    selected = store.get_visible_catalogued_asset_by_id(asset_id, username)
    candidate = store.get_visible_catalogued_asset_by_id(
        request.candidate_asset_id, username
    )
    if (
        selected is None
        or candidate is None
        or not asset_is_editable_by(selected, username)
        or not asset_is_editable_by(candidate, username)
        or selected.asset_type != candidate.asset_type
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    analysis = analyse_asset_relationship(selected, candidate)
    try:
        relationship_type = canonical_relationship_type(analysis.classification)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)
        ) from error
    entry = store.approve_asset_relationship_review(
        selected.id,
        candidate.id,
        relationship_type,
        analysis.confidence,
        analysis.evidence,
        username,
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending unlinked relationship review exists for these assets",
        )
    return VaultAssetHistoryEntry(**entry)


@router.patch("/assets/{asset_id}/metadata", response_model=VaultAsset)
def edit_catalogued_asset_metadata(
    asset_id: UUID,
    edit: MetadataOverrideEdit,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAsset:
    if not edit.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one editable metadata field is required",
        )
    changes = {
        name: value.isoformat() if isinstance(value, date) else value
        for name, value in edit.model_dump(exclude_unset=True).items()
    }
    # A user-entered timestamp is authoritative for its date as well. Keep the
    # canonical date/timestamp pair coherent even when the caller supplied
    # only the timestamp.
    if edit.captured_at is not None and "captured_on" not in edit.model_fields_set:
        changes["captured_on"] = edit.captured_at.date().isoformat()
    existing = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if existing is None or not asset_is_editable_by(existing, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    asset = store.update_catalogued_asset_metadata(
        asset_id,
        changes,
        username,
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return to_api_asset(asset, username)


@router.post("/assets/{asset_id}/theatre-movie-rename", status_code=status.HTTP_202_ACCEPTED)
def queue_theatre_movie_rename(
    asset_id: UUID,
    edit: TheatreMovieRenameEdit,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> dict[str, str]:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if (
        asset is None
        or asset.asset_type not in {"Movie", "Movies"}
        or asset.owner_user_id is None
        or not asset_is_editable_by(asset, username)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    snapshot = store.theatre_movie_rename_snapshot(asset.id, asset.owner_user_id)
    if snapshot is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The authoritative Theatre placement is unavailable",
        )
    try:
        destination = canonical_movie_destination(
            edit.title, edit.release_year, Path(asset.filename).suffix
        )
        request = queue_movie_rename(
            snapshot, destination, edit.title, edit.release_year
        )
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    return {
        "status": "queued",
        "request_id": str(request.request_id),
        "destination": request.destination_logical_path,
    }


@router.post(
    "/assets/{asset_id}/ai/ocr",
    response_model=AiJobResult,
    status_code=status.HTTP_202_ACCEPTED,
)
def queue_asset_ocr(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: AiStoreDependency,
) -> AiJobResult:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if (
        asset is None
        or not asset_is_editable_by(asset, username)
        or asset.asset_type != "Gallery"
        or not asset.mime_type.startswith("image/")
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return AiJobResult.model_validate(
        ai_store.queue_ocr(asset_id, username, getattr(username, "user_id", None)),
        from_attributes=True,
    )


@router.post(
    "/items/{item_id}/ai/analyse",
    response_model=IngestionAiJobResult,
    status_code=status.HTTP_202_ACCEPTED,
)
def queue_ingestion_image_analysis(
    item_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
    authentication: AuthenticationStoreDependency,
) -> IngestionAiJobResult:
    item = store.get_item(item_id)
    account = authentication.get_account(username)
    if (
        account is None
        or item is None
        or item.owner_user_id is None
        or item.owner_user_id != account.user_id
        or item.source_kind != INCOMING_SOURCE
        or not (
            item.mime_type.startswith("image/")
            or item.mime_type == "application/pdf"
        )
        or item.state not in {"inventoried", "needs_review"}
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return IngestionAiJobResult.model_validate(
        ai_store.queue_analysis(item_id, username, account.user_id),
        from_attributes=True,
    )


@router.get("/items/ai", response_model=IngestionAiEvidenceListing)
def list_ingestion_ai_evidence(
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
    authentication: AuthenticationStoreDependency,
    response: Response,
) -> IngestionAiEvidenceListing:
    response.headers["Cache-Control"] = "private, no-store"
    account = authentication.get_account(username)
    if account is None:
        return IngestionAiEvidenceListing(items=[])
    visible_items = {
        item.id: item
        for item in store.list_items()
        if item.source_kind == INCOMING_SOURCE and item.owner_user_id == account.user_id
    }
    evidence = [entry for entry in ai_store.list_all_evidence() if entry.item_id in visible_items]
    jobs = [job for job in ai_store.list_all_jobs() if job.item_id in visible_items]
    evidence_by_item: dict[UUID, list[object]] = {}
    for item in evidence:
        evidence_by_item.setdefault(item.item_id, []).append(item)
    jobs_by_item: dict[UUID, list[object]] = {}
    for job in jobs:
        jobs_by_item.setdefault(job.item_id, []).append(job)
    item_ids = set(visible_items).intersection(evidence_by_item.keys() | jobs_by_item.keys())
    return IngestionAiEvidenceListing(
        items=[
            IngestionAiItemEvidence(
                item_id=item_id,
                jobs=[
                    IngestionAiJobResult.model_validate(job, from_attributes=True)
                    for job in jobs_by_item.get(item_id, [])
                ],
                evidence=[
                    IngestionAiEvidenceResult.model_validate(item, from_attributes=True)
                    for item in evidence_by_item.get(item_id, [])
                ],
            )
            for item_id in sorted(item_ids, key=str)
        ]
    )


def _analysis_batch_result(
    batch: object,
    username: str,
    ai_store: IngestionAiStore,
) -> IngestionAnalysisBatchResult:
    item_ids = set(ai_store.list_analysis_batch_item_ids(batch.id, username))
    latest = {}
    for evidence in ai_store.list_user_evidence(getattr(username, "user_id", username)):
        if evidence.item_id in item_ids and evidence.item_id not in latest:
            latest[evidence.item_id] = evidence
    grouped: dict[tuple[str | None, str, str, str], list[UUID]] = {}
    for item_id, evidence in latest.items():
        explanation = (
            evidence.reasons[0]
            if evidence.reasons
            else "No reliable classification explanation was available"
        )
        key = (
            evidence.recommended_destination,
            evidence.content_type,
            evidence.routing_band,
            explanation,
        )
        grouped.setdefault(key, []).append(item_id)
    return IngestionAnalysisBatchResult(
        **batch.__dict__,
        groups=[
            IngestionAnalysisGroup(
                destination=key[0],
                content_type=key[1],
                routing_band=key[2],
                explanation=key[3],
                item_ids=sorted(ids, key=str),
                count=len(ids),
            )
            for key, ids in sorted(
                grouped.items(), key=lambda entry: (str(entry[0]), str(entry[1]))
            )
        ],
    )


@router.post(
    "/items/ai/batches",
    response_model=IngestionAnalysisBatchResult,
    status_code=status.HTTP_202_ACCEPTED,
)
def create_ingestion_analysis_batch(
    selection: IngestionAnalysisBatchRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
) -> IngestionAnalysisBatchResult:
    unique_ids = tuple(dict.fromkeys(selection.item_ids))
    eligible = []
    for item_id in unique_ids:
        item = store.get_item(item_id)
        if (
            item is None
            or item.source_kind != INCOMING_SOURCE
            or item.owner_user_id != getattr(username, "user_id", None)
            or not item.mime_type.startswith("image/")
            or item.state not in {"inventoried", "needs_review"}
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Every selected file must be a reviewable Arrival Hall image",
            )
        eligible.append(item_id)
        jobs = ai_store.list_jobs(item_id, getattr(username, "user_id", username))
        if jobs and jobs[0].status in {"queued", "processing"}:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A selected image already has active analysis",
            )
    batch = ai_store.create_analysis_batch(
        tuple(eligible), username, getattr(username, "user_id", None)
    )
    return _analysis_batch_result(batch, username, ai_store)


@router.get(
    "/items/ai/batches",
    response_model=IngestionAnalysisBatchListing,
)
def list_ingestion_analysis_batches(
    username: AuthenticatedUsername,
    ai_store: IngestionAiStoreDependency,
    response: Response,
) -> IngestionAnalysisBatchListing:
    response.headers["Cache-Control"] = "private, no-store"
    return IngestionAnalysisBatchListing(
        batches=[
            _analysis_batch_result(batch, username, ai_store)
            for batch in ai_store.list_analysis_batches(username)
        ]
    )


@router.post(
    "/items/ai/batches/{batch_id}/{command}",
    response_model=IngestionAnalysisBatchResult,
)
def control_ingestion_analysis_batch(
    batch_id: UUID,
    command: Literal["pause", "resume", "retry"],
    username: AuthenticatedUsername,
    ai_store: IngestionAiStoreDependency,
) -> IngestionAnalysisBatchResult:
    batch = (
        ai_store.retry_analysis_batch(batch_id, username)
        if command == "retry"
        else ai_store.set_analysis_batch_status(
            batch_id, username, "paused" if command == "pause" else "running"
        )
    )
    if batch is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)
    return _analysis_batch_result(batch, username, ai_store)


@router.get("/assets/{asset_id}/ai", response_model=AiEvidenceResult)
def get_asset_ai_evidence(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: AiStoreDependency,
    ingestion_ai_store: IngestionAiStoreDependency,
    response: Response,
) -> AiEvidenceResult:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    source_items = [
        item
        for item in store.list_items()
        if item.source_kind == INCOMING_SOURCE
        and item.state == "moved"
        and item.owner_user_id == getattr(username, "user_id", None)
        and item.sha256 == asset.sha256
        and item.proposed_destination == asset.vault_path
    ]
    visual_evidence = [
        evidence
        for item in source_items
        for evidence in ingestion_ai_store.list_evidence(item.id, getattr(username, "user_id", username))
        if evidence.caption.strip()
    ]
    latest_visual_evidence = max(
        visual_evidence,
        key=lambda evidence: evidence.created_at,
        default=None,
    )
    return AiEvidenceResult(
        jobs=[
            AiJobResult.model_validate(job, from_attributes=True)
            for job in ai_store.list_jobs(asset_id, getattr(username, "user_id", username))
        ],
        suggestions=[
            AiSuggestionResult.model_validate(item, from_attributes=True)
            for item in ai_store.list_suggestions(asset_id, getattr(username, "user_id", username))
        ],
        visual_description=(
            VisualDescriptionEvidenceResult.model_validate(
                latest_visual_evidence,
                from_attributes=True,
            )
            if latest_visual_evidence is not None
            else None
        ),
    )


@router.post(
    "/assets/{asset_id}/ai/suggestions/{suggestion_id}/review",
    response_model=AiSuggestionResult,
)
def review_asset_ai_suggestion(
    asset_id: UUID,
    suggestion_id: UUID,
    review: AiSuggestionReview,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: AiStoreDependency,
) -> AiSuggestionResult:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if review.status != "accepted" and review.value is not None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Only an accepted suggestion can contain an edited value",
        )
    pending = next(
        (
            item
            for item in ai_store.list_suggestions(asset_id, getattr(username, "user_id", username))
            if item.id == suggestion_id and item.status == "pending"
        ),
        None,
    )
    if pending is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending AI suggestion exists for this asset",
        )
    reviewed = ai_store.review_suggestion(
        suggestion_id,
        username,
        getattr(username, "user_id", username),
        review.status,
        review.value.strip() if review.value is not None else None,
    )
    if reviewed is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending AI suggestion exists for this asset",
        )
    return AiSuggestionResult.model_validate(reviewed, from_attributes=True)


@router.get("/assets/{asset_id}/access", response_model=VaultAssetAccessPolicy)
def get_catalogued_asset_access(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    response: Response,
) -> VaultAssetAccessPolicy:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    return VaultAssetAccessPolicy(
        owner_username=asset.owner_username,
        visibility=asset.visibility,
        shared_with=list(asset.shared_with),
    )


@router.patch("/assets/{asset_id}/access", response_model=VaultAssetAccessPolicy)
def edit_catalogued_asset_access(
    asset_id: UUID,
    edit: VaultAssetAccessPolicyEdit,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    auth_store: AuthenticationStoreDependency,
) -> VaultAssetAccessPolicy:
    if edit.visibility == "shared" and not edit.shared_with:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A shared asset needs at least one family member",
        )
    if username in edit.shared_with:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The owner cannot be listed as a shared family member",
        )
    existing = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if existing is None or not asset_is_editable_by(existing, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    recipient_user_ids: tuple[UUID, ...] = ()
    if isinstance(store, MemoryVaultMasterStore) and edit.visibility == "shared":
        accounts = {account.username: account for account in auth_store.list_accounts() if account.active}
        recipients = [accounts.get(recipient) for recipient in edit.shared_with]
        if any(account is None for account in recipients):
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A selected person is no longer eligible")
        recipient_user_ids = tuple(account.user_id for account in recipients if account is not None)
    access_options = {}
    if isinstance(store, MemoryVaultMasterStore):
        access_options["shared_with_user_ids"] = recipient_user_ids
    asset = store.update_catalogued_asset_access(asset_id, edit.visibility, tuple(edit.shared_with), username, **access_options)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return VaultAssetAccessPolicy(
        owner_username=asset.owner_username,
        visibility=asset.visibility,
        shared_with=list(asset.shared_with),
    )


def _local_share_recipient(account: object) -> LocalShareRecipient:
    # The account protocol deliberately exposes no avatar data.  Initials are
    # a stable, local-only fallback until profile images exist.
    display_name = str(getattr(account, "display_name"))
    initials = "".join(part[:1].upper() for part in display_name.split()[:2]) or "?"
    return LocalShareRecipient(
        user_id=getattr(account, "user_id"),
        display_name=display_name,
        avatar_label=initials,
    )


def _local_sharing_state(
    asset: CataloguedAsset,
    auth_store: AuthenticationStore,
) -> tuple[Literal["private", "everyone", "specific"], list[LocalShareRecipient]]:
    accounts = {account.user_id: account for account in auth_store.list_accounts() if account.active}
    if asset.owner_user_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Asset ownership is unavailable")
    grants = PostgresShareGrantStore(get_database_conninfo()).list_outgoing_grants(asset.owner_user_id)
    active = [grant for grant in grants if grant.asset_id == asset.id and grant.state == ACTIVE_GRANT_STATE]
    if any(grant.target_type == LOCAL_ALL_TARGET for grant in active):
        return "everyone", []
    recipients = [
        _local_share_recipient(accounts[grant.target_local_user_id])
        for grant in active
        if grant.target_type == LOCAL_USER_TARGET and grant.target_local_user_id in accounts
    ]
    if recipients:
        return "specific", recipients
    return "private", []


@router.get("/assets/{asset_id}/sharing", response_model=LocalAssetSharingState)
def get_catalogued_asset_local_sharing(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    auth_store: AuthenticationStoreDependency,
    response: Response,
) -> LocalAssetSharingState:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    eligible = [
        _local_share_recipient(account)
        for account in auth_store.list_accounts()
        if account.active and account.user_id != asset.owner_user_id
    ]
    mode, recipients = _local_sharing_state(asset, auth_store)
    response.headers["Cache-Control"] = "private, no-store"
    return LocalAssetSharingState(
        owner_username=asset.owner_username,
        mode=mode,
        recipients=recipients,
        eligible_users=eligible,
    )


@router.put("/assets/{asset_id}/sharing", response_model=LocalAssetSharingState)
def set_catalogued_asset_local_sharing(
    asset_id: UUID,
    edit: LocalAssetSharingEdit,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    auth_store: AuthenticationStoreDependency,
) -> LocalAssetSharingState:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if edit.mode != "specific" and edit.recipient_user_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Only specific sharing can name recipients")
    if edit.mode == "specific" and not edit.recipient_user_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Select at least one person")
    accounts = {account.user_id: account for account in auth_store.list_accounts() if account.active}
    recipients = [accounts.get(user_id) for user_id in edit.recipient_user_ids]
    if any(account is None for account in recipients):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="A selected person is no longer eligible")
    if asset.owner_user_id in edit.recipient_user_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="The owner cannot be a recipient")
    if edit.mode == "private":
        visibility, shared_with, recipient_user_ids, local_all = "private", (), (), False
    elif edit.mode == "everyone":
        # Compatibility fields retain a current local-account snapshot; grants
        # remain the live, dynamic authorization source.
        visibility = "shared"
        shared_with = tuple(account.username for account in accounts.values() if account.user_id != asset.owner_user_id)
        recipient_user_ids = tuple(account.user_id for account in accounts.values() if account.user_id != asset.owner_user_id)
        local_all = True
    else:
        visibility = "shared"
        shared_with = tuple(account.username for account in recipients if account is not None)
        recipient_user_ids = tuple(account.user_id for account in recipients if account is not None)
        local_all = False
    access_options = {"local_all": local_all, "share_mode": edit.share_mode}
    if isinstance(store, MemoryVaultMasterStore):
        access_options["shared_with_user_ids"] = recipient_user_ids
    updated = store.update_catalogued_asset_access(asset_id, visibility, shared_with, username, **access_options)
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    mode, current_recipients = _local_sharing_state(updated, auth_store)
    eligible = [
        _local_share_recipient(account)
        for account in accounts.values()
        if account.user_id != updated.owner_user_id
    ]
    return LocalAssetSharingState(
        owner_username=updated.owner_username,
        mode=mode,
        recipients=current_recipients,
        eligible_users=eligible,
    )


@router.put("/assets/sharing/bulk", response_model=BulkLocalAssetSharingResult)
def set_catalogued_assets_local_sharing(
    edit: BulkLocalAssetSharingEdit, username: AuthenticatedUsername,
    store: VaultMasterStoreDependency, auth_store: AuthenticationStoreDependency,
) -> BulkLocalAssetSharingResult:
    """Preflight every item, then commit an all-or-nothing local share batch."""
    assets: list[CataloguedAsset] = []
    for asset_id in edit.asset_ids:
        asset = store.get_catalogued_asset_by_id(asset_id)
        if asset is None:
            raise HTTPException(status_code=422, detail={"message": "Selected asset was not found", "asset_id": str(asset_id)})
        if not asset_is_editable_by(asset, username):
            raise HTTPException(status_code=422, detail={"message": "Selected asset is not owned by the authenticated user", "asset_id": str(asset_id), "asset_title": asset.display_title or asset.filename})
        assets.append(asset)
    if edit.mode != "specific" and edit.recipient_user_ids:
        raise HTTPException(status_code=422, detail="Only specific sharing can name recipients")
    if edit.mode == "specific" and not edit.recipient_user_ids:
        raise HTTPException(status_code=422, detail="Select at least one person")
    accounts = {account.user_id: account for account in auth_store.list_accounts() if account.active}
    recipients = [accounts.get(user_id) for user_id in edit.recipient_user_ids]
    if any(account is None for account in recipients):
        raise HTTPException(status_code=422, detail="A selected person is no longer eligible")
    if any(asset.owner_user_id in edit.recipient_user_ids for asset in assets):
        raise HTTPException(status_code=422, detail="The owner cannot be a recipient")
    if edit.mode == "private":
        visibility, shared_with, recipient_user_ids, local_all = "private", (), (), False
    elif edit.mode == "everyone":
        visibility, shared_with, recipient_user_ids, local_all = "shared", tuple(account.username for account in accounts.values() if account.user_id != username.user_id), tuple(account.user_id for account in accounts.values() if account.user_id != username.user_id), True
    else:
        visibility, shared_with, recipient_user_ids, local_all = "shared", tuple(account.username for account in recipients if account is not None), tuple(account.user_id for account in recipients if account is not None), False
    access_options = {"local_all": local_all, "share_mode": edit.share_mode}
    if isinstance(store, MemoryVaultMasterStore):
        access_options["shared_with_user_ids"] = recipient_user_ids
    updated = store.update_catalogued_assets_access(edit.asset_ids, visibility, shared_with, username, **access_options)
    return BulkLocalAssetSharingResult(asset_ids=[asset.id for asset in updated])


def _outgoing_operations(username: str, auth_store: AuthenticationStore, store: VaultMasterStore) -> OutgoingShareListing:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    accounts = {account.user_id: account for account in auth_store.list_accounts() if account.active}
    operations = PostgresShareGrantStore(get_database_conninfo()).list_outgoing_operations(owner.user_id)
    response: list[OutgoingShareOperation] = []
    for operation, grants in operations:
        entries: list[OutgoingShareGrant] = []
        for grant in grants:
            # The durable grant asset id is authoritative; never echo a UI selection.
            asset = store.get_visible_catalogued_asset_by_id(grant.asset_id, username)
            if asset is None or asset.owner_user_id != owner.user_id:
                continue
            recipient = accounts.get(grant.target_local_user_id) if grant.target_local_user_id else None
            entries.append(OutgoingShareGrant(
                asset_id=grant.asset_id,
                asset_title=asset.display_title,
                preview_url=(f"/api/vault-master/assets/{asset.id}/preview" if asset.asset_type.casefold() == "gallery" else None),
                target_type=grant.target_type,
                recipient=_local_share_recipient(recipient) if recipient else None,
            ))
        if entries:
            response.append(OutgoingShareOperation(**operation.__dict__, grants=entries))
    for operation, collection, grants in PostgresShareGrantStore(get_database_conninfo()).list_outgoing_collection_operations(owner.user_id):
        entries = [
            OutgoingShareGrant(
                asset_id=collection.collection_id, asset_title=collection.name,
                target_type=grant.target_type,
                recipient=_local_share_recipient(accounts[grant.target_local_user_id]) if grant.target_local_user_id in accounts else None,
            )
            for grant in grants
        ]
        members = [
            OutgoingCollectionMember(asset_id=asset_id, asset_title=asset.display_title)
            for asset_id in PostgresShareGrantStore(get_database_conninfo()).list_collection_members(collection.collection_id, owner.user_id)
            if (asset := store.get_visible_catalogued_asset_by_id(asset_id, username)) is not None
        ]
        response.append(OutgoingShareOperation(
            **operation.__dict__, grants=entries, subject_type="collection",
            collection=_shared_collection_response(collection, auth_store), collection_members=members,
        ))
    response.sort(key=lambda operation: (operation.created_at, operation.operation_id), reverse=True)
    return OutgoingShareListing(operations=response)


COMMONS_CATEGORY_TYPES: dict[str, tuple[str, ...]] = {
    "gallery": ("gallery",),
    "theatre": ("movie", "movies"),
    "home-videos": ("personal videos", "personal video", "home videos", "home video"),
    "documents": ("documents", "archives"),
    "library": ("music", "library", "reading room"),
}


@router.get(
    "/commons/shared-with-me",
    response_model=CommonsSharedListing,
    response_model_exclude_none=True,
    response_model_exclude_defaults=True,
)
def list_commons_shared_with_me(
    username: AuthenticatedUsername,
    auth_store: AuthenticationStoreDependency,
    response: Response,
    category: Literal["gallery", "theatre", "home-videos", "documents", "library"] = "gallery",
) -> CommonsSharedListing:
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    assets = PostgresShareGrantStore(get_database_conninfo()).list_assets_shared_with_user(
        account.user_id, COMMONS_CATEGORY_TYPES[category]
    )
    local_assets=[
            CommonsSharedAsset(
                asset_id=asset.asset_id,
                asset_type=asset.asset_type,
                display_title=asset.display_title,
                captured_on=asset.captured_on,
                owner_display_name=asset.owner_display_name,
                preview_url=(
                    f"/api/vault-master/commons/shared-with-me/{asset.asset_id}/preview"
                    if asset.asset_type.casefold() == "gallery"
                    else None
                ),
            )
            for asset in assets
        ]
    # Federation is additive.  A local Commons listing must retain its proven
    # local-share behaviour while an isolated legacy/test database has not yet
    # bootstrapped the optional Stage 6 tables.  No remote item is shown on an
    # error, which remains fail-closed for federation access.
    try:
        federated_assets = FederationStore(get_database_conninfo()).list_incoming_for_user(
            account.user_id, COMMONS_CATEGORY_TYPES[category][0]
        )
    except psycopg.Error:
        federated_assets = []
    return CommonsSharedListing(assets=local_assets + [_federated_commons_asset(share) for share in federated_assets])


def _federated_commons_asset(share: object) -> CommonsSharedAsset:
    """One canonical remote-asset presentation for Commons and collection members."""
    federation = FederationStore(get_database_conninfo())
    origin_vault_id = getattr(share, "origin_vault_id")
    origin_asset_id = getattr(share, "origin_asset_id")
    cache = federation.cache_entry(origin_vault_id, origin_asset_id)
    asset_type = str(getattr(share, "asset_type"))
    incoming_share_id = getattr(share, "incoming_share_id")
    return CommonsSharedAsset(
        asset_id=incoming_share_id, asset_type=asset_type,
        display_title=getattr(share, "display_title"), captured_on=getattr(share, "captured_on"),
        owner_display_name=getattr(share, "owner_label"),
        preview_url=(f"/api/vault-master/federation/incoming/{incoming_share_id}/preview" if asset_type.casefold() == "gallery" else None),
        origin_vault_id=origin_vault_id, origin_asset_id=origin_asset_id, is_federated=True,
        origin_metadata=getattr(share, "origin_metadata"), metadata_revision=getattr(share, "metadata_revision"),
        content_url=(f"/api/vault-master/federation/incoming/{incoming_share_id}/content" if asset_type.casefold() in {"movie", "movies", "personal videos", "personal video", "home videos", "home video"} else None),
        cache_state=cache.state if cache else "remote", download_allowed=bool(getattr(share, "download_allowed", False)),
    )


def _federated_response(share: object, *, preview: bool = False) -> IncomingFederatedShareResponse:
    return IncomingFederatedShareResponse(
        incoming_share_id=getattr(share, "incoming_share_id"),
        origin_vault_id=getattr(share, "origin_vault_id"),
        origin_asset_id=getattr(share, "origin_asset_id"),
        origin_share_id=getattr(share, "origin_share_id"),
        owner_label=getattr(share, "owner_label"), asset_type=getattr(share, "asset_type"),
        display_title=getattr(share, "display_title"), captured_on=getattr(share, "captured_on"),
        state=getattr(share, "state"),
        origin_metadata=getattr(share, "origin_metadata"), metadata_revision=getattr(share, "metadata_revision"),
        preview_url=(f"/api/vault-master/federation/incoming/{getattr(share, 'incoming_share_id')}/preview" if preview and str(getattr(share, "asset_type")).casefold() == "gallery" else None),
        download_allowed=bool(getattr(share, "download_allowed", False)),
    )


@router.get("/federation/peers", response_model=list[FederationPeerResponse])
def list_federation_peers(username: AuthenticatedUsername) -> list[FederationPeerResponse]:
    del username
    return [FederationPeerResponse(remote_vault_id=peer.remote_vault_id, display_label=peer.display_label, trust_state=peer.trust_state) for peer in FederationStore(get_database_conninfo()).list_paired_vaults()]


@router.post("/federation/peers", response_model=FederationPeerResponse, status_code=status.HTTP_201_CREATED)
def pair_federation_vault(request: FederationPeerCreate, username: ElevatedVaultControlAdministrator) -> FederationPeerResponse:
    peer=FederationStore(get_database_conninfo()).pair_vault(request.remote_vault_id,request.display_label,request.endpoint,request.pairing_key)
    return FederationPeerResponse(remote_vault_id=peer.remote_vault_id,display_label=peer.display_label,trust_state=peer.trust_state)


@router.delete("/federation/peers/{remote_vault_id}", status_code=status.HTTP_204_NO_CONTENT)
def unpair_federation_vault(remote_vault_id: UUID, username: ElevatedVaultControlAdministrator) -> Response:
    """Immediately remove remote-only access; independent downloads remain local."""
    del username
    try:
        FederationStore(get_database_conninfo()).unpair_vault(remote_vault_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Paired Vault is unavailable") from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/federation/diagnostics", response_model=FederationDiagnosticsResponse)
def federation_diagnostics(username: ElevatedVaultControlAdministrator) -> FederationDiagnosticsResponse:
    del username
    return FederationDiagnosticsResponse(**FederationStore(get_database_conninfo()).diagnostics())


@router.get("/federation/audit", response_model=list[FederationAuditRecord])
def federation_audit(username: ElevatedVaultControlAdministrator, limit: int = Query(default=100, ge=1, le=200)) -> list[FederationAuditRecord]:
    del username
    return [FederationAuditRecord(**row) for row in FederationStore(get_database_conninfo()).recent_audit(limit)]


@router.post("/federation/peers/{remote_vault_id}/reconcile", status_code=status.HTTP_202_ACCEPTED)
def reconcile_federation_peer(remote_vault_id: UUID, username: ElevatedVaultControlAdministrator) -> dict[str, int]:
    """Queue only current authoritative share state for a trusted peer."""
    del username
    try:
        return {"queued": FederationStore(get_database_conninfo()).reconcile_authoritative_state(remote_vault_id)}
    except ValueError as error:
        raise HTTPException(status_code=404, detail="Paired Vault is unavailable") from error


@router.post("/federation/outgoing", response_model=FederatedShareResult, status_code=status.HTTP_201_CREATED)
def create_federated_shares(request: FederatedShareCreate, username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> FederatedShareResult:
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    try:
        ids=FederationStore(get_database_conninfo()).create_outgoing_shares(account.user_id,request.asset_ids,request.target_vault_id,request.share_mode)
    except ValueError as error:
        raise HTTPException(status_code=422,detail=str(error)) from error
    return FederatedShareResult(federation_share_ids=ids)


@router.post("/shared-collections/{collection_id}/federation", response_model=FederatedCollectionShareResult, status_code=status.HTTP_201_CREATED)
def create_federated_collection_share(collection_id: UUID, request: FederatedCollectionShareCreate, username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> FederatedCollectionShareResult:
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=404)
    try:
        share_id = FederationStore(get_database_conninfo()).create_outgoing_collection_share(account.user_id, collection_id, request.target_vault_id, request.share_mode)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return FederatedCollectionShareResult(federation_collection_share_id=share_id)


@router.post("/federation/outgoing-collections/{collection_share_id}/{action}", status_code=status.HTTP_204_NO_CONTENT)
def transition_federated_collection_outgoing(collection_share_id: UUID, action: Literal["share-now", "revoke"], username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> Response:
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=404)
    try:
        FederationStore(get_database_conninfo()).transition_outgoing_collection(collection_share_id, account.user_id, "activate" if action == "share-now" else "revoke")
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=204)


@router.get("/federation/outgoing", response_model=list[FederatedOutgoingShare])
def list_federated_outgoing(username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> list[FederatedOutgoingShare]:
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    return [FederatedOutgoingShare(**share.__dict__,preview_url=(f"/api/vault-master/assets/{share.origin_asset_id}/preview" if share.asset_type.casefold()=="gallery" else None)) for share in FederationStore(get_database_conninfo()).list_outgoing(account.user_id)]


@router.get("/federation/outgoing-collections", response_model=list[FederatedOutgoingCollection])
def list_federated_outgoing_collections(username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> list[FederatedOutgoingCollection]:
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=404)
    return [FederatedOutgoingCollection(**share.__dict__) for share in FederationStore(get_database_conninfo()).list_outgoing_collections(account.user_id)]


@router.put("/federation/outgoing/{share_id}/download-permission", status_code=status.HTTP_204_NO_CONTENT)
def set_federated_download_permission(
    share_id: UUID,
    request: FederatedDownloadPermissionEdit,
    username: AuthenticatedUsername,
    auth_store: AuthenticationStoreDependency,
) -> Response:
    """Change the origin owner's durable per-share download permission."""
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=404)
    try:
        FederationStore(get_database_conninfo()).set_download_allowed(
            share_id, account.user_id, request.download_allowed
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/federation/outgoing/{share_id}/{action}", status_code=status.HTTP_204_NO_CONTENT)
def transition_federated_outgoing(share_id: UUID, action: Literal["share-now", "revoke"], username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> Response:
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    try: FederationStore(get_database_conninfo()).transition_outgoing([share_id],account.user_id,"activate" if action=="share-now" else "revoke")
    except ValueError as error: raise HTTPException(status_code=409,detail=str(error)) from error
    return Response(status_code=204)


@router.post("/federation/events")
async def receive_federation_event(request: Request, x_pv_federation_signature: Annotated[str | None, Header()] = None) -> dict[str, object]:
    if not x_pv_federation_signature: raise HTTPException(status_code=404)
    try: envelope=await request.json()
    except ValueError as error: raise HTTPException(status_code=422,detail="Malformed federation envelope") from error
    if not isinstance(envelope,dict): raise HTTPException(status_code=422,detail="Malformed federation envelope")
    try: applied=FederationStore(get_database_conninfo()).receive_event(envelope,x_pv_federation_signature)
    except ValueError as error: raise HTTPException(status_code=404,detail="Federation event denied") from error
    return {"protocol_version":FEDERATION_PROTOCOL_VERSION,"applied":applied}


@router.get("/federation/incoming", response_model=IncomingFederatedShareListing)
def list_federated_incoming(username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency, category: str | None = Query(default=None)) -> IncomingFederatedShareListing:
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    shares=FederationStore(get_database_conninfo()).list_incoming_for_user(account.user_id,category)
    return IncomingFederatedShareListing(shares=[_federated_response(share,preview=True) for share in shares])


@router.get("/federation/incoming/admin", response_model=IncomingFederatedShareListing)
def list_federated_incoming_admin(username: ElevatedVaultControlAdministrator) -> IncomingFederatedShareListing:
    del username
    return IncomingFederatedShareListing(shares=[_federated_response(share) for share in FederationStore(get_database_conninfo()).list_incoming_admin()])


@router.get("/federation/incoming-collections", response_model=IncomingFederatedCollectionListing)
def list_federated_incoming_collections(username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> IncomingFederatedCollectionListing:
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=404)
    return IncomingFederatedCollectionListing(collections=[IncomingFederatedCollectionResponse(**collection.__dict__) for collection in FederationStore(get_database_conninfo()).list_incoming_collections_for_user(account.user_id)])


@router.get("/federation/incoming-collections/admin", response_model=IncomingFederatedCollectionListing)
def list_federated_incoming_collections_admin(username: ElevatedVaultControlAdministrator) -> IncomingFederatedCollectionListing:
    del username
    return IncomingFederatedCollectionListing(collections=[IncomingFederatedCollectionResponse(**collection.__dict__) for collection in FederationStore(get_database_conninfo()).list_incoming_collections_admin()])


@router.put("/federation/incoming-collections/{incoming_collection_id}/distribution", status_code=status.HTTP_204_NO_CONTENT)
def distribute_federated_collection(incoming_collection_id: UUID, request: IncomingFederatedDistributionEdit, username: ElevatedVaultControlAdministrator, auth_store: AuthenticationStoreDependency) -> Response:
    admin = auth_store.get_account(username)
    if admin is None:
        raise HTTPException(status_code=404)
    try:
        FederationStore(get_database_conninfo()).set_collection_distribution(incoming_collection_id, admin.user_id, request.mode == "everyone", request.recipient_user_ids)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(status_code=204)


@router.delete("/federation/incoming-collections/{incoming_collection_id}/distribution", status_code=status.HTTP_204_NO_CONTENT)
def remove_federated_collection_distribution(incoming_collection_id: UUID, username: ElevatedVaultControlAdministrator, auth_store: AuthenticationStoreDependency) -> Response:
    admin = auth_store.get_account(username)
    if admin is None:
        raise HTTPException(status_code=404)
    try:
        FederationStore(get_database_conninfo()).clear_collection_distribution(incoming_collection_id, admin.user_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return Response(status_code=204)


@router.put("/federation/incoming/{incoming_share_id}/distribution", status_code=status.HTTP_204_NO_CONTENT)
def distribute_federated_incoming(incoming_share_id: UUID, request: IncomingFederatedDistributionEdit, username: ElevatedVaultControlAdministrator, auth_store: AuthenticationStoreDependency) -> Response:
    admin=auth_store.get_account(username)
    if admin is None: raise HTTPException(status_code=404)
    try: FederationStore(get_database_conninfo()).set_distribution(incoming_share_id,admin.user_id,request.mode=='everyone',request.recipient_user_ids)
    except ValueError as error: raise HTTPException(status_code=422,detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/federation/incoming/{incoming_share_id}/distribution", status_code=status.HTTP_204_NO_CONTENT)
def remove_federated_incoming_distribution(incoming_share_id: UUID, username: ElevatedVaultControlAdministrator, auth_store: AuthenticationStoreDependency) -> Response:
    admin=auth_store.get_account(username)
    if admin is None: raise HTTPException(status_code=404)
    try: FederationStore(get_database_conninfo()).clear_distribution(incoming_share_id,admin.user_id)
    except ValueError as error: raise HTTPException(status_code=422,detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/federation/incoming/{origin_vault_id}/{origin_asset_id}/annotation", status_code=status.HTTP_204_NO_CONTENT)
def save_federated_local_annotation(origin_vault_id: UUID, origin_asset_id: UUID, request: FederatedLocalAnnotationEdit, username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> Response:
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    try: FederationStore(get_database_conninfo()).set_local_annotation(origin_vault_id,origin_asset_id,account.user_id,note=request.note,alias=request.alias,tags=request.tags)
    except ValueError as error: raise HTTPException(status_code=404,detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/federation/incoming/{origin_vault_id}/{origin_asset_id}/annotation")
def get_federated_local_annotation(origin_vault_id: UUID, origin_asset_id: UUID, username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> dict[str, object]:
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    annotation=FederationStore(get_database_conninfo()).local_annotation(origin_vault_id,origin_asset_id,account.user_id)
    if annotation is None: return {"note":None,"alias":None,"tags":[]}
    return annotation


def get_federation_cache_root() -> Path:
    """Dedicated managed-cache root; never a catalogue root or a user path."""
    return Path(os.getenv("PV_FEDERATION_CACHE_ROOT", "/vault/.cache/federation"))


def get_federated_download_staging_root() -> Path:
    """Persistent, non-canonical staging for explicit Stage 9 downloads."""
    return Path(os.getenv("PV_FEDERATED_DOWNLOAD_STAGING_ROOT", "/var/lib/personal-vault/federated-download-staging"))


def _download_response(operation: FederatedDownloadOperation) -> FederatedDownloadOperationResponse:
    return FederatedDownloadOperationResponse(
        operation_id=operation.operation_id,
        local_asset_id=operation.local_asset_id,
        state=operation.state,
    )


_FEDERATED_DOWNLOAD_DESTINATIONS: dict[str, tuple[str, str]] = {
    "gallery": ("Gallery", "/vault/Gallery"),
    "home video": ("Home Videos", "/vault/Home Videos"),
    "home videos": ("Home Videos", "/vault/Home Videos"),
    "document": ("Documents", "/vault/Documents"),
    "documents": ("Documents", "/vault/Documents"),
    "archive": ("Archives", "/vault/Archives"),
    "archives": ("Archives", "/vault/Archives"),
    "music": ("Music", "/vault/Music"),
    "library": ("Library", "/vault/Library"),
    "reading room": ("Library", "/vault/Library"),
}


def _federated_download_filename(value: object) -> str:
    filename = Path(str(value)).name
    if not filename or filename in {".", ".."} or filename != value:
        raise ValueError("Origin filename is unavailable")
    return filename


def _federated_download_destination(root: Path, filename: str, asset_id: UUID) -> Path:
    """Use the canonical root and collision-safe UUID suffix without user paths."""
    root = root.resolve()
    candidate = (root / filename).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError("Unsafe canonical destination")
    if not candidate.exists():
        return candidate
    stem, suffix = Path(filename).stem, Path(filename).suffix
    candidate = (root / f"{stem} ({asset_id}){suffix}").resolve()
    if not candidate.is_relative_to(root) or candidate.exists():
        raise FileExistsError("Canonical destination already exists")
    return candidate


def _promote_federated_download(source: Path, destination: Path, expected_sha256: str, expected_size: int) -> None:
    """Copy-then-verify promotion for writable canonical category roots."""
    if not source.is_file() or source.is_symlink() or source.stat().st_size != expected_size or sha256_file(source) != expected_sha256:
        raise ValueError("Verified staging file is unavailable")
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
    try:
        with source.open("rb") as input_file, temporary.open("xb") as output_file:
            shutil.copyfileobj(input_file, output_file, CHECKSUM_CHUNK_BYTES)
            output_file.flush(); os.fsync(output_file.fileno())
        if temporary.stat().st_size != expected_size or sha256_file(temporary) != expected_sha256:
            raise ValueError("Canonical promotion verification failed")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _federation_cache_path(root: Path, origin_vault_id: UUID, origin_asset_id: UUID) -> Path:
    path=(root / str(origin_vault_id) / f"{origin_asset_id}.cache").resolve()
    if root.resolve() not in path.parents: raise ValueError("Unsafe cache path")
    return path


@router.get("/federation/incoming/{incoming_share_id}/preview", response_model=None)
def proxy_federated_preview(incoming_share_id: UUID, username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> Response:
    """Remote-only Gallery preview; local distribution and origin share both authorize it."""
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    federation=FederationStore(get_database_conninfo())
    try: share,endpoint,pairing_key=federation.incoming_for_preview(incoming_share_id,account.user_id)
    except ValueError as error: raise HTTPException(status_code=404) from error
    timestamp=datetime.now(timezone.utc).isoformat()
    request_body={"share_id":str(share.origin_share_id),"asset_id":str(share.origin_asset_id),"requester_vault_id":str(federation.local_vault_id()),"timestamp":timestamp}
    try:
        request=UrlRequest(endpoint.rstrip('/')+f"/api/vault-master/federation/origin-preview/{share.origin_share_id}/{share.origin_asset_id}",headers={"X-PV-Federation-Signature":sign_envelope(request_body,pairing_key),"X-PV-Federation-Timestamp":timestamp,"X-PV-Requester-Vault":str(federation.local_vault_id())})
        with urlopen(request,timeout=15) as remote:
            body=remote.read(); content_type=remote.headers.get_content_type()
    except (HTTPError,URLError,OSError,ValueError): raise HTTPException(status_code=404) from None
    if not body or not content_type.startswith('image/'): raise HTTPException(status_code=404)
    return Response(content=body,media_type=content_type,headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})


@router.get("/federation/incoming/{incoming_share_id}/content", response_model=None)
def proxy_federated_content(incoming_share_id: UUID, request: Request, username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency, cache_root: Path = Depends(get_federation_cache_root)) -> StreamingResponse | FileResponse:
    """Stream remote media through the receiving Vault; never reveal origin paths."""
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    federation=FederationStore(get_database_conninfo())
    try: share,endpoint,pairing_key=federation.incoming_for_content(incoming_share_id,account.user_id)
    except ValueError as error: raise HTTPException(status_code=404) from error
    if share.asset_type.casefold() not in {'movie','movies','personal videos','personal video','home videos','home video'}: raise HTTPException(status_code=404)
    timestamp=datetime.now(timezone.utc).isoformat(); requester=str(federation.local_vault_id())
    signed={"share_id":str(share.origin_share_id),"asset_id":str(share.origin_asset_id),"requester_vault_id":requester,"timestamp":timestamp}
    headers={"X-PV-Federation-Signature":sign_envelope(signed,pairing_key),"X-PV-Federation-Timestamp":timestamp,"X-PV-Requester-Vault":requester}
    cached=_federation_cache_path(cache_root,share.origin_vault_id,share.origin_asset_id)
    entry=federation.cache_entry(share.origin_vault_id,share.origin_asset_id)
    # A cache is usable only after a live origin authorization probe.  This
    # deliberately defers offline rights until a lease policy is approved.
    if entry and entry.state=='complete' and cached.is_file():
        try:
            probe_headers={**headers,'Range':'bytes=0-0'}
            with urlopen(UrlRequest(endpoint.rstrip('/')+f"/api/vault-master/federation/origin-content/{share.origin_share_id}/{share.origin_asset_id}",headers=probe_headers),timeout=10): pass
            return FileResponse(cached,media_type=share.origin_metadata.get('media_type','application/octet-stream') if share.origin_metadata else 'application/octet-stream',headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff","Accept-Ranges":"bytes"})
        except (HTTPError,URLError,OSError,ValueError):
            raise HTTPException(status_code=404) from None
    if range_header:=request.headers.get('range'): headers['Range']=range_header
    try:
        remote=urlopen(UrlRequest(endpoint.rstrip('/')+f"/api/vault-master/federation/origin-content/{share.origin_share_id}/{share.origin_asset_id}",headers=headers),timeout=20)
    except (HTTPError,URLError,OSError,ValueError) as error: raise HTTPException(status_code=404) from error
    def chunks():
        try:
            while data:=remote.read(64*1024): yield data
        finally: remote.close()
    passthrough={key: value for key in ('Content-Length','Content-Range','Accept-Ranges') if (value:=remote.headers.get(key))}
    return StreamingResponse(chunks(),status_code=remote.status,media_type=remote.headers.get_content_type(),headers={**passthrough,"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})


@router.put("/federation/incoming/{incoming_share_id}/progress", status_code=status.HTTP_204_NO_CONTENT)
def update_federated_progress(incoming_share_id: UUID, payload: dict[str, float], username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency) -> Response:
    account=auth_store.get_account(username)
    position=payload.get('position_seconds'); duration=payload.get('duration_seconds')
    if account is None or not account.active or not isinstance(position,(int,float)) or position < 0 or (duration is not None and (not isinstance(duration,(int,float)) or duration < position)): raise HTTPException(status_code=404)
    federation=FederationStore(get_database_conninfo())
    try: share,_,_=federation.incoming_for_content(incoming_share_id,account.user_id)
    except ValueError as error: raise HTTPException(status_code=404) from error
    federation.set_progress(share,account.user_id,float(position),float(duration) if duration is not None else None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/federation/incoming/{incoming_share_id}/cache", response_model=dict[str, str])
def cache_federated_content(incoming_share_id: UUID, payload: dict[str, bool], username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency, cache_root: Path = Depends(get_federation_cache_root)) -> dict[str, str]:
    """Explicit full-file managed cache; verified before promotion and never an asset."""
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    federation=FederationStore(get_database_conninfo())
    try: share,endpoint,pairing_key=federation.incoming_for_content(incoming_share_id,account.user_id)
    except ValueError as error: raise HTTPException(status_code=404) from error
    metadata=share.origin_metadata or {}; size=metadata.get('size_bytes'); checksum=metadata.get('sha256')
    large=share.asset_type.casefold() in {'movie','movies'} or (isinstance(size,int) and size>100*1024*1024)
    if large and payload.get('confirm_large_media') is not True: raise HTTPException(status_code=409,detail='Large managed cache requires confirmation')
    if not isinstance(size,int) or size < 0 or not isinstance(checksum,str) or len(checksum)!=64: raise HTTPException(status_code=409,detail='Origin content facts are unavailable')
    target=_federation_cache_path(cache_root,share.origin_vault_id,share.origin_asset_id); target.parent.mkdir(parents=True,exist_ok=True)
    if (existing:=federation.cache_entry(share.origin_vault_id,share.origin_asset_id)) and existing.state=='complete' and target.is_file(): return {'state':'complete'}
    if not federation.begin_cache(share,size,checksum):
        raise HTTPException(status_code=409,detail='Managed cache is already being prepared')
    temporary=target.with_suffix('.part')
    timestamp=datetime.now(timezone.utc).isoformat(); requester=str(federation.local_vault_id())
    signed={"share_id":str(share.origin_share_id),"asset_id":str(share.origin_asset_id),"requester_vault_id":requester,"timestamp":timestamp}
    try:
        with urlopen(UrlRequest(endpoint.rstrip('/')+f"/api/vault-master/federation/origin-content/{share.origin_share_id}/{share.origin_asset_id}",headers={"X-PV-Federation-Signature":sign_envelope(signed,pairing_key),"X-PV-Federation-Timestamp":timestamp,"X-PV-Requester-Vault":requester}),timeout=30) as remote, temporary.open('wb') as destination:
            digest=hashlib.sha256(); received=0
            while block:=remote.read(64*1024): destination.write(block); digest.update(block); received+=len(block)
        if received!=size or digest.hexdigest()!=checksum: raise ValueError('cache verification failed')
        os.replace(temporary,target); federation.set_cache_state(share,'complete',size,checksum)
        return {'state':'complete'}
    except (HTTPError,URLError,OSError,ValueError):
        temporary.unlink(missing_ok=True); federation.set_cache_state(share,'invalidated',size,checksum); raise HTTPException(status_code=502,detail='Managed cache failed') from None


@router.post("/federation/incoming/{incoming_share_id}/download", response_model=FederatedDownloadOperationResponse, status_code=status.HTTP_202_ACCEPTED)
def begin_federated_download(
    incoming_share_id: UUID,
    request: FederatedDownloadRequest,
    username: AuthenticatedUsername,
    auth_store: AuthenticationStoreDependency,
    staging_root: Path = Depends(get_federated_download_staging_root),
    store: VaultMasterStoreDependency = None,
    destination_paths: dict[str, Path] = Depends(get_destination_paths),
) -> FederatedDownloadOperationResponse:
    """Stage verified remote bytes for an independently owned local copy.

    This endpoint deliberately never treats the managed cache as a source and
    never writes Theatre.  Theatre promotion is handed to the fixed executor
    only after the complete staging checksum has been verified.
    """
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=404)
    federation = FederationStore(get_database_conninfo())
    try:
        operation = federation.reserve_download(incoming_share_id, account.user_id, request.idempotency_key)
        if operation.state in {'verified', 'promotion_requested', 'completed'}:
            return _download_response(operation)
        share, endpoint, pairing_key = federation.incoming_for_content(incoming_share_id, account.user_id)
        if not share.download_allowed:
            raise ValueError('Download to My Vault is unavailable')
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = staging_root.resolve()
    if root.is_symlink() or not root.is_dir() or operation.staging_name is None:
        federation.set_download_operation_state(operation.operation_id, account.user_id, 'failed', failure_reason='unsafe staging root')
        raise HTTPException(status_code=409, detail='Download staging is unavailable')
    target = (root / operation.staging_name).resolve()
    if not target.is_relative_to(root):
        federation.set_download_operation_state(operation.operation_id, account.user_id, 'failed', failure_reason='unsafe staging target')
        raise HTTPException(status_code=409, detail='Download staging is unavailable')
    temporary = target.with_suffix('.transfer')
    federation.set_download_operation_state(operation.operation_id, account.user_id, 'transferring')
    timestamp = datetime.now(timezone.utc).isoformat()
    requester = str(federation.local_vault_id())
    signed = {"share_id": str(share.origin_share_id), "asset_id": str(share.origin_asset_id), "requester_vault_id": requester, "timestamp": timestamp}
    headers = {"X-PV-Federation-Signature": sign_envelope(signed, pairing_key), "X-PV-Federation-Timestamp": timestamp, "X-PV-Requester-Vault": requester}
    try:
        with urlopen(UrlRequest(endpoint.rstrip('/') + f"/api/vault-master/federation/origin-download/{share.origin_share_id}/{share.origin_asset_id}", headers=headers), timeout=30) as remote, temporary.open('xb') as output:
            digest = hashlib.sha256(); received = 0
            while block := remote.read(64 * 1024):
                output.write(block); digest.update(block); received += len(block)
            output.flush(); os.fsync(output.fileno())
        if received != operation.expected_size_bytes or digest.hexdigest() != operation.expected_sha256:
            raise ValueError('Downloaded bytes did not match origin facts')
        # Re-authorize immediately before any irreversible canonical promotion.
        # A revoke/permission withdrawal during transfer therefore fails closed.
        with urlopen(
            UrlRequest(
                endpoint.rstrip('/') + f"/api/vault-master/federation/origin-download/{share.origin_share_id}/{share.origin_asset_id}",
                headers={**headers, "Range": "bytes=0-0"},
            ),
            timeout=10,
        ) as probe:
            if probe.status not in {200, 206}:
                raise ValueError('Origin download authorization changed')
        os.replace(temporary, target)
        operation = federation.set_download_operation_state(operation.operation_id, account.user_id, 'verified')
    except (HTTPError, URLError, OSError, ValueError):
        temporary.unlink(missing_ok=True)
        operation = federation.set_download_operation_state(operation.operation_id, account.user_id, 'failed', failure_reason='transfer or checksum verification failed')
        raise HTTPException(status_code=502, detail='Download transfer failed') from None
    # The executor request is intentionally deferred until the backend has a
    # verified staging file.  The receipt processor will create the catalogue
    # asset only after the executor has independently confirmed promotion.
    if share.asset_type.casefold() in {'movie', 'movies'}:
        key_path = Path(os.getenv('PV_FEDERATED_DOWNLOAD_EXECUTOR_KEY_PATH', '/run/secrets/federated-download.key'))
        queue_root = Path(os.getenv('PV_FEDERATED_DOWNLOAD_EXECUTOR_QUEUE', '/var/lib/personal-vault-storage/federated-download-requests'))
        metadata = share.origin_metadata or {}
        filename = metadata.get('filename')
        if not isinstance(filename, str) or not filename or '/' in filename or operation.local_asset_id is None:
            federation.set_download_operation_state(operation.operation_id, account.user_id, 'failed', failure_reason='origin filename unavailable')
            raise HTTPException(status_code=409, detail='Origin filename is unavailable')
        try:
            promotion = FederatedDownloadPromotionRequest.create(operation_id=operation.operation_id, local_asset_id=operation.local_asset_id, owner_user_id=account.user_id, origin_vault_id=share.origin_vault_id, origin_asset_id=share.origin_asset_id, staging_name=operation.staging_name, filename=filename, expected_sha256=operation.expected_sha256, expected_size_bytes=operation.expected_size_bytes)
            queue_signed_request(promotion, queue_root=queue_root, key=key_path.read_bytes())
            operation = federation.set_download_operation_state(operation.operation_id, account.user_id, 'promotion_requested')
        except (OSError, ValueError):
            federation.set_download_operation_state(operation.operation_id, account.user_id, 'failed', failure_reason='Theatre promotion request failed')
            raise HTTPException(status_code=502, detail='Theatre promotion request failed') from None
    else:
        category = _FEDERATED_DOWNLOAD_DESTINATIONS.get(share.asset_type.casefold())
        metadata = share.origin_metadata or {}
        if category is None or operation.local_asset_id is None:
            federation.set_download_operation_state(operation.operation_id, account.user_id, 'failed', failure_reason='unsupported canonical destination')
            raise HTTPException(status_code=409, detail='This shared category cannot yet be downloaded')
        try:
            filename = _federated_download_filename(metadata.get('filename'))
            destination = _federated_download_destination(destination_paths[category[0]], filename, operation.local_asset_id)
            _promote_federated_download(target, destination, operation.expected_sha256, operation.expected_size_bytes)
            captured = metadata.get('captured_on')
            captured_on = date.fromisoformat(captured) if isinstance(captured, str) and captured else None
            copied_metadata = {key: value for key, value in metadata.items() if key not in {'people', 'biometric_evidence', 'face_embeddings'}}
            local_asset = CataloguedAsset(
                id=operation.local_asset_id, asset_type=share.asset_type, display_title=share.display_title,
                captured_on=captured_on, location=None, vault_path=f"{category[1]}/{destination.name}", filename=destination.name,
                size_bytes=operation.expected_size_bytes, mime_type=str(metadata.get('media_type') or 'application/octet-stream'), sha256=operation.expected_sha256,
                metadata=copied_metadata, metadata_provenance={key: 'federated-origin-snapshot' for key in copied_metadata},
                imported_metadata=copied_metadata, effective_metadata=copied_metadata, owner_username=account.username, owner_user_id=account.user_id,
            )
            store.restore_catalogued_asset(local_asset, account.username)
            operation = federation.complete_download(operation.operation_id, account.user_id, operation.local_asset_id)
            target.unlink(missing_ok=True)
        except (OSError, ValueError, FileExistsError):
            federation.set_download_operation_state(operation.operation_id, account.user_id, 'failed', failure_reason='canonical promotion failed')
            raise HTTPException(status_code=502, detail='Download promotion failed') from None
    return _download_response(operation)


@router.delete("/federation/incoming/{incoming_share_id}/cache", status_code=status.HTTP_204_NO_CONTENT)
def remove_federated_cache(incoming_share_id: UUID, username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency, cache_root: Path = Depends(get_federation_cache_root)) -> Response:
    account=auth_store.get_account(username)
    if account is None or not account.active: raise HTTPException(status_code=404)
    federation=FederationStore(get_database_conninfo())
    try: share,_,_=federation.incoming_for_content(incoming_share_id,account.user_id)
    except ValueError as error: raise HTTPException(status_code=404) from error
    _federation_cache_path(cache_root,share.origin_vault_id,share.origin_asset_id).unlink(missing_ok=True)
    federation.set_cache_state(share,'invalidated')
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/federation/origin-content/{share_id}/{asset_id}", response_model=None)
def serve_federated_origin_content(share_id: UUID, asset_id: UUID, x_pv_federation_signature: Annotated[str | None, Header()] = None, x_pv_federation_timestamp: Annotated[str | None, Header()] = None, x_pv_requester_vault: Annotated[UUID | None, Header()] = None, store: VaultMasterStoreDependency = None, preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots)) -> FileResponse:
    if not x_pv_federation_signature or not x_pv_federation_timestamp or x_pv_requester_vault is None: raise HTTPException(status_code=404)
    if not FederationStore(get_database_conninfo()).authorizes_origin_content(share_id,asset_id,x_pv_requester_vault,x_pv_federation_timestamp,x_pv_federation_signature): raise HTTPException(status_code=404)
    asset=store.get_catalogued_asset_by_id(asset_id)
    if asset is None or asset.asset_type.casefold() not in {'movie','movies','personal videos','personal video','home videos','home video'}: raise HTTPException(status_code=404)
    try: path=resolve_catalogued_asset_path(asset,preview_roots)
    except (OSError,ValueError): raise HTTPException(status_code=404) from None
    return FileResponse(path=path,media_type=asset.mime_type,headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff","Accept-Ranges":"bytes"})


@router.get("/federation/origin-download/{share_id}/{asset_id}", response_model=None)
def serve_federated_origin_download(
    share_id: UUID,
    asset_id: UUID,
    x_pv_federation_signature: Annotated[str | None, Header()] = None,
    x_pv_federation_timestamp: Annotated[str | None, Header()] = None,
    x_pv_requester_vault: Annotated[UUID | None, Header()] = None,
    store: VaultMasterStoreDependency = None,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
) -> FileResponse:
    """Origin bytes for an explicit independent copy, never a cache endpoint."""
    if not x_pv_federation_signature or not x_pv_federation_timestamp or x_pv_requester_vault is None:
        raise HTTPException(status_code=404)
    if not FederationStore(get_database_conninfo()).authorizes_origin_download(
        share_id, asset_id, x_pv_requester_vault, x_pv_federation_timestamp, x_pv_federation_signature
    ):
        raise HTTPException(status_code=404)
    asset = store.get_catalogued_asset_by_id(asset_id)
    if asset is None:
        raise HTTPException(status_code=404)
    try:
        path = resolve_catalogued_asset_path(asset, preview_roots)
    except (OSError, ValueError):
        raise HTTPException(status_code=404) from None
    return FileResponse(path=path, media_type=asset.mime_type, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "Accept-Ranges": "bytes"
    })


@router.get("/federation/origin-preview/{share_id}/{asset_id}", response_model=None)
def serve_federated_origin_preview(share_id: UUID, asset_id: UUID, x_pv_federation_signature: Annotated[str | None, Header()] = None, x_pv_federation_timestamp: Annotated[str | None, Header()] = None, x_pv_requester_vault: Annotated[UUID | None, Header()] = None, store: VaultMasterStoreDependency = None, preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots)) -> FileResponse | Response:
    if not x_pv_federation_signature or not x_pv_federation_timestamp or x_pv_requester_vault is None: raise HTTPException(status_code=404)
    if not FederationStore(get_database_conninfo()).authorizes_origin_preview(share_id,asset_id,x_pv_requester_vault,x_pv_federation_timestamp,x_pv_federation_signature): raise HTTPException(status_code=404)
    asset=store.get_catalogued_asset_by_id(asset_id)
    if asset is None or asset.asset_type.casefold()!='gallery': raise HTTPException(status_code=404)
    try: path=resolve_catalogued_asset_path(asset,preview_roots)
    except (OSError,ValueError): raise HTTPException(status_code=404) from None
    if asset.mime_type.startswith('image/'): return FileResponse(path=path,media_type=asset.mime_type,headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})
    try: return Response(content=render_gallery_pdf_preview(path),media_type='image/jpeg',headers={"Cache-Control":"private, no-store","X-Content-Type-Options":"nosniff"})
    except ValueError: raise HTTPException(status_code=404) from None


@router.get("/commons/shared-with-me/{asset_id}/preview", response_model=None)
def get_commons_shared_asset_preview(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    auth_store: AuthenticationStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
) -> FileResponse | Response:
    """Serve an existing Gallery preview only while Commons access is active."""
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    shared_assets = PostgresShareGrantStore(
        get_database_conninfo()
    ).list_assets_shared_with_user(account.user_id)
    if asset_id not in {shared.asset_id for shared in shared_assets}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    asset = store.get_catalogued_asset_by_id(asset_id)
    if asset is None or asset.asset_type.casefold() != "gallery":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        preview_path = resolve_catalogued_asset_path(asset, preview_roots)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    headers = {
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if asset.mime_type.startswith("image/"):
        return FileResponse(path=preview_path, media_type=asset.mime_type, headers=headers)
    if asset.mime_type != "application/pdf":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        preview = render_gallery_pdf_preview(preview_path)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from error
    return Response(content=preview, media_type="image/jpeg", headers=headers)


def _shared_collection_response(collection: object, auth_store: AuthenticationStore) -> SharedCollectionResponse:
    owner = auth_store.get_account_by_user_id(getattr(collection, "owner_user_id"))
    if owner is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Collection owner cannot be resolved")
    return SharedCollectionResponse(
        collection_id=getattr(collection, "collection_id"), name=getattr(collection, "name"),
        description=getattr(collection, "description"), owner_display_name=owner.display_name,
        member_count=getattr(collection, "member_count"),
    )


@router.post("/shared-collections", response_model=SharedCollectionResponse, status_code=status.HTTP_201_CREATED)
def create_shared_collection(
    request: SharedCollectionCreate, username: AuthenticatedUsername,
    auth_store: AuthenticationStoreDependency,
) -> SharedCollectionResponse:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        collection = PostgresShareGrantStore(get_database_conninfo()).create_collection(
            owner.user_id, request.name, request.asset_ids, description=request.description
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error
    return _shared_collection_response(collection, auth_store)


@router.put("/shared-collections/{collection_id}", response_model=SharedCollectionResponse)
def update_shared_collection(
    collection_id: UUID, request: SharedCollectionUpdate, username: AuthenticatedUsername,
    auth_store: AuthenticationStoreDependency,
) -> SharedCollectionResponse:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        collection = PostgresShareGrantStore(get_database_conninfo()).update_collection(collection_id, owner.user_id, request.name, request.description)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _shared_collection_response(collection, auth_store)


@router.delete("/shared-collections/{collection_id}", status_code=status.HTTP_204_NO_CONTENT)
def archive_shared_collection(
    collection_id: UUID, username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency,
) -> Response:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        PostgresShareGrantStore(get_database_conninfo()).archive_collection(collection_id, owner.user_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/shared-collections/{collection_id}/members", status_code=status.HTTP_204_NO_CONTENT)
def add_shared_collection_members(
    collection_id: UUID, request: SharedCollectionMembersEdit, username: AuthenticatedUsername,
    auth_store: AuthenticationStoreDependency,
) -> Response:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        PostgresShareGrantStore(get_database_conninfo()).add_collection_members(
            collection_id, owner.user_id, request.asset_ids, confirm_live_share=request.confirm_live_share
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/shared-collections/{collection_id}/members/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_shared_collection_member(
    collection_id: UUID, asset_id: UUID, username: AuthenticatedUsername,
    auth_store: AuthenticationStoreDependency,
) -> Response:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        PostgresShareGrantStore(get_database_conninfo()).remove_collection_member(collection_id, owner.user_id, asset_id)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/shared-collections/{collection_id}/share", response_model=OutgoingShareOperation)
def share_shared_collection(
    collection_id: UUID, request: SharedCollectionShareEdit, username: AuthenticatedUsername,
    auth_store: AuthenticationStoreDependency,
) -> OutgoingShareOperation:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if request.mode == "everyone" and request.recipient_user_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Everyone sharing cannot name recipients")
    try:
        operation = PostgresShareGrantStore(get_database_conninfo()).share_collection(
            collection_id, owner.user_id, LOCAL_ALL_TARGET if request.mode == "everyone" else LOCAL_USER_TARGET,
            target_local_user_ids=request.recipient_user_ids, share_mode=request.share_mode,
        )
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return OutgoingShareOperation(**operation.__dict__, grants=[])


@router.get("/commons/shared-collections", response_model=SharedCollectionListing)
def list_commons_shared_collections(
    username: AuthenticatedUsername, auth_store: AuthenticationStoreDependency, response: Response,
) -> SharedCollectionListing:
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    collections = PostgresShareGrantStore(get_database_conninfo()).list_shared_collections_for_user(account.user_id)
    local = [_shared_collection_response(collection, auth_store) for collection in collections]
    try:
        remote = FederationStore(get_database_conninfo()).list_incoming_collections_for_user(account.user_id)
    except psycopg.Error:
        remote = []
    return SharedCollectionListing(collections=local + [
        SharedCollectionResponse(collection_id=item.incoming_collection_id, name=item.name, description=item.description,
            owner_display_name=item.owner_label, member_count=item.member_count, is_federated=True,
            origin_vault_id=item.origin_vault_id, origin_collection_id=item.origin_collection_id, state=item.state)
        for item in remote
    ])


@router.get("/commons/shared-collections/{collection_id}/members", response_model=CommonsSharedListing)
def list_commons_shared_collection_members(
    collection_id: UUID, username: AuthenticatedUsername, store: VaultMasterStoreDependency,
    auth_store: AuthenticationStoreDependency, response: Response,
) -> CommonsSharedListing:
    account = auth_store.get_account(username)
    if account is None or not account.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    federation = FederationStore(get_database_conninfo())
    try:
        federated_members = federation.list_incoming_collection_members(collection_id, account.user_id)
        federated_visible = any(
            collection.incoming_collection_id == collection_id
            for collection in federation.list_incoming_collections_for_user(account.user_id)
        )
    except psycopg.Error:
        federated_members = []
        federated_visible = False
    if federated_visible:
        return CommonsSharedListing(assets=[_federated_commons_asset(member) for member in federated_members])
    member_ids = PostgresShareGrantStore(get_database_conninfo()).list_collection_members(collection_id, account.user_id)
    if not member_ids:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    assets = [store.get_visible_catalogued_asset_by_id(asset_id, username) for asset_id in member_ids]
    response.headers["Cache-Control"] = "private, no-store"
    return CommonsSharedListing(assets=[
        CommonsSharedAsset(
            asset_id=asset.id, asset_type=asset.asset_type, display_title=asset.display_title,
            captured_on=asset.captured_on,
            owner_display_name=(auth_store.get_account_by_user_id(asset.owner_user_id).display_name
                                if asset.owner_user_id and auth_store.get_account_by_user_id(asset.owner_user_id) else "Unknown owner"),
        )
        for asset in assets if asset is not None
    ])


@router.get("/sharing/outgoing", response_model=OutgoingShareListing)
def list_outgoing_shares(
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    auth_store: AuthenticationStoreDependency,
    response: Response,
) -> OutgoingShareListing:
    response.headers["Cache-Control"] = "private, no-store"
    return _outgoing_operations(username, auth_store, store)


@router.post("/sharing/outgoing/{operation_id}/share-now", response_model=OutgoingShareOperation)
def share_outgoing_operation_now(
    operation_id: UUID, username: AuthenticatedUsername, store: VaultMasterStoreDependency,
    auth_store: AuthenticationStoreDependency,
) -> OutgoingShareOperation:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        PostgresShareGrantStore(get_database_conninfo()).transition_operation(operation_id, owner.user_id, "activate")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    listed = _outgoing_operations(username, auth_store, store)
    return next(operation for operation in listed.operations if operation.operation_id == operation_id)


@router.post("/sharing/outgoing/{operation_id}/revoke", response_model=OutgoingShareOperation)
def revoke_outgoing_operation(
    operation_id: UUID, username: AuthenticatedUsername, store: VaultMasterStoreDependency,
    auth_store: AuthenticationStoreDependency,
) -> OutgoingShareOperation:
    owner = auth_store.get_account(username)
    if owner is None or not owner.active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        PostgresShareGrantStore(get_database_conninfo()).transition_operation(operation_id, owner.user_id, "revoke")
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    listed = _outgoing_operations(username, auth_store, store)
    return next(operation for operation in listed.operations if operation.operation_id == operation_id)


@router.get(
    "/assets/{asset_id}/history",
    response_model=VaultAssetHistory,
)
def get_catalogued_asset_history(
    asset_id: UUID,
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAssetHistory:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    return VaultAssetHistory(
        entries=[
            VaultAssetHistoryEntry(**entry)
            for entry in store.list_catalogued_asset_history(asset_id)
        ]
    )


@router.post("/assets/{asset_id}/lifecycle/hide", response_model=VaultAsset)
def hide_catalogued_asset(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAsset:
    """Hide an owner's canonical asset without changing its bytes or path."""
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    updated = store.set_catalogued_asset_lifecycle_state(
        asset_id, username.user_id, username, "hidden"
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return to_api_asset(updated, username)


@router.post("/assets/{asset_id}/lifecycle/unhide", response_model=VaultAsset)
def unhide_catalogued_asset(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAsset:
    """Return an owner's Hidden asset to normal presentation."""
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    updated = store.set_catalogued_asset_lifecycle_state(
        asset_id, username.user_id, username, "active"
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return to_api_asset(updated, username)


@router.post(
    "/assets/{asset_id}/lifecycle/quarantine-review",
    response_model=VaultAssetHistoryEntry,
    status_code=status.HTTP_201_CREATED,
)
def request_catalogued_asset_quarantine_review(
    asset_id: UUID,
    request: QuarantineReviewRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAssetHistoryEntry:
    """Record an owner request to review an asset for quarantine.

    This is deliberately non-destructive: it neither moves nor deletes the
    original. A later lifecycle step will turn an approved request into a
    recoverable quarantine action after revalidating the asset.
    """
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    entry = store.request_catalogued_asset_quarantine_review(
        asset_id,
        username,
        request.reason,
    )
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return VaultAssetHistoryEntry(**entry)


@router.post(
    "/assets/{asset_id}/lifecycle/quarantine-review/cancel",
    response_model=VaultAssetHistoryEntry,
    status_code=status.HTTP_201_CREATED,
)
def cancel_catalogued_asset_quarantine_review(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAssetHistoryEntry:
    """Withdraw a pending quarantine review without changing the file."""
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    entry = store.cancel_catalogued_asset_quarantine_review(asset_id, username)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending quarantine review can be withdrawn",
        )
    return VaultAssetHistoryEntry(**entry)


@router.get(
    "/assets/{asset_id}/lifecycle/quarantine-preflight",
    response_model=QuarantinePreflight,
)
def preflight_catalogued_asset_quarantine_endpoint(
    asset_id: UUID,
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
    quarantine_root: Path = Depends(get_quarantine_root),
) -> QuarantinePreflight:
    """Check that a recoverable quarantine move would be safe.

    This endpoint is intentionally read-only.  It never creates the quarantine
    folder, copies a file, changes the catalogue, or removes the original.
    """
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    return preflight_catalogued_asset_quarantine(
        asset,
        preview_roots,
        quarantine_root,
    )


@router.get(
    "/assets/{asset_id}/lifecycle/permanent-deletion-preflight",
    response_model=PermanentDeletionPreflight,
)
def preflight_catalogued_asset_permanent_deletion_endpoint(
    asset_id: UUID,
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
    quarantine_root: Path = Depends(get_quarantine_root),
    retention_days: int = Depends(get_quarantine_retention_days),
) -> PermanentDeletionPreflight:
    """Assess a quarantined asset for permanent deletion without mutation."""
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    return preflight_catalogued_asset_permanent_deletion(
        asset,
        quarantine_root,
        store.list_catalogued_asset_history(asset_id),
        retention_days,
        preview_roots,
    )


@router.post(
    "/assets/{asset_id}/lifecycle/permanent-deletion-review",
    response_model=VaultAssetHistoryEntry,
    status_code=status.HTTP_201_CREATED,
)
def request_catalogued_asset_permanent_deletion_review(
    asset_id: UUID,
    request: PermanentDeletionReviewRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
    quarantine_root: Path = Depends(get_quarantine_root),
    retention_days: int = Depends(get_quarantine_retention_days),
) -> VaultAssetHistoryEntry:
    """Record owner intent only after live deletion preflight passes."""
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    preflight = preflight_catalogued_asset_permanent_deletion(
        asset,
        quarantine_root,
        store.list_catalogued_asset_history(asset_id),
        retention_days,
        preview_roots,
    )
    if not preflight.ready or preflight.eligible_at is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=preflight.reason or "The asset is not eligible for permanent deletion",
        )
    entry = store.request_catalogued_asset_permanent_deletion_review(
        asset_id,
        username,
        request.reason,
        preflight.eligible_at,
    )
    if entry is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)
    return VaultAssetHistoryEntry(**entry)


@router.post(
    "/assets/{asset_id}/lifecycle/permanent-deletion-review/cancel",
    response_model=VaultAssetHistoryEntry,
    status_code=status.HTTP_201_CREATED,
)
def cancel_catalogued_asset_permanent_deletion_review(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultAssetHistoryEntry:
    """Withdraw a pending permanent-deletion review without mutation."""
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    entry = store.cancel_catalogued_asset_permanent_deletion_review(
        asset_id,
        username,
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending permanent-deletion review can be withdrawn",
        )
    return VaultAssetHistoryEntry(**entry)


@router.post(
    "/assets/{asset_id}/lifecycle/permanent-deletion-confirm",
    response_model=VaultAssetHistoryEntry,
    status_code=status.HTTP_201_CREATED,
)
def confirm_catalogued_asset_permanent_deletion_review(
    asset_id: UUID,
    confirmation: PermanentDeletionConfirmation,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
    quarantine_root: Path = Depends(get_quarantine_root),
    retention_days: int = Depends(get_quarantine_retention_days),
) -> VaultAssetHistoryEntry:
    """Record deliberate authorization after repeating live preflight."""
    del confirmation
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    preflight = preflight_catalogued_asset_permanent_deletion(
        asset,
        quarantine_root,
        store.list_catalogued_asset_history(asset_id),
        retention_days,
        preview_roots,
    )
    if not preflight.ready or not preflight.checksum_verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=preflight.reason or "The asset is not eligible for permanent deletion",
        )
    entry = store.confirm_catalogued_asset_permanent_deletion_review(
        asset_id,
        username,
        asset.sha256,
    )
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No pending permanent-deletion review can be confirmed",
        )
    return VaultAssetHistoryEntry(**entry)


@router.post(
    "/assets/{asset_id}/lifecycle/permanent-deletion-execute",
    response_model=VaultAssetHistoryEntry,
    status_code=status.HTTP_201_CREATED,
)
def execute_catalogued_asset_permanent_deletion(
    asset_id: UUID,
    execution: PermanentDeletionExecution,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
    quarantine_root: Path = Depends(get_quarantine_root),
    retention_days: int = Depends(get_quarantine_retention_days),
    metadata_root: Path = Depends(get_metadata_storage_root),
) -> VaultAssetHistoryEntry:
    """Execute a confirmed deletion while retaining a catalogue tombstone."""
    del execution
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    preflight = preflight_catalogued_asset_permanent_deletion(
        asset,
        quarantine_root,
        store.list_catalogued_asset_history(asset_id),
        retention_days,
        preview_roots,
    )
    if not preflight.ready or not preflight.checksum_verified:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=preflight.reason or "The asset is not eligible for permanent deletion",
        )

    if asset.vault_path.startswith("/vault/Quarantine/"):
        resolved_root = quarantine_root.resolve(strict=True)
        relative_path = asset.vault_path.removeprefix("/vault/Quarantine/")
        source = require_file_within_root(
            resolved_root.joinpath(*relative_path.split("/")),
            resolved_root,
        )
    else:
        source = resolve_catalogued_asset_path(asset, preview_roots)
    pending = source.with_name(f".vault-master-delete-{asset.id}.pending")
    if pending.exists() or pending.is_symlink():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A pending deletion file already exists for this asset",
        )
    source.replace(pending)
    try:
        entry = store.record_catalogued_asset_permanent_deletion(
            asset.id,
            asset.vault_path,
            asset.sha256,
            username,
        )
        if entry is None:
            pending.replace(source)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The permanent-deletion confirmation is no longer current",
            )
    except HTTPException:
        raise
    except Exception:
        pending.replace(source)
        raise

    try:
        canonical_sidecar_path(asset, metadata_root).unlink(missing_ok=True)
        pending.unlink()
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "The catalogue tombstone was committed, but deletion cleanup failed; "
                f"inspect {pending}: {error}"
            ),
        ) from error
    return VaultAssetHistoryEntry(**entry)


@router.post(
    "/assets/{asset_id}/lifecycle/quarantine-confirm",
    response_model=VaultAsset,
    status_code=status.HTTP_201_CREATED,
)
def confirm_catalogued_asset_quarantine(
    asset_id: UUID,
    confirmation: QuarantineConfirmation,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
    quarantine_root: Path = Depends(get_quarantine_root),
) -> VaultAsset:
    """Perform the owner-confirmed, recoverable Quarantine move.

    A new verified copy is made first.  Only then is the catalogue changed and
    the original removed.  A target is never overwritten and a stale review is
    rejected without removing the source file.
    """
    del confirmation  # The literal confirmation is validated by Pydantic.
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        source, destination, quarantine_vault_path = copy_catalogued_asset_to_quarantine(
            asset,
            preview_roots,
            quarantine_root,
        )
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))

    updated = store.confirm_catalogued_asset_quarantine(
        asset.id,
        asset.vault_path,
        quarantine_vault_path,
        username,
    )
    if updated is None:
        if destination.exists() and not destination.is_symlink():
            destination.unlink()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The pending Quarantine review is no longer valid",
        )
    try:
        source.unlink()
    except OSError as error:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "The verified Quarantine copy is catalogued, but the original "
                f"could not be removed: {error}"
            ),
        )
    return to_api_asset(updated, username)


@router.post(
    "/assets/{asset_id}/lifecycle/move-preflight",
    response_model=FolderMovePreflight,
)
def preflight_catalogued_asset_folder_move_endpoint(
    asset_id: UUID,
    request: FolderMoveRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
) -> FolderMovePreflight:
    """Read-only existing-folder move validation; it never creates a directory."""
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return preflight_catalogued_asset_folder_move(asset, request, preview_roots)


@router.get("/lifecycle/move-destinations")
def get_existing_folder_move_destinations(
    category: Literal["Gallery", "Home Videos", "Documents", "Archives", "Music"],
    username: AuthenticatedUsername,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
) -> dict[str, list[str]]:
    """List configured, existing folders only; paths are never accepted on trust."""
    del username
    return {"destinations": list_existing_move_destinations(category, preview_roots)}


@router.post(
    "/assets/{asset_id}/lifecycle/move-confirm",
    response_model=VaultAsset,
)
def confirm_catalogued_asset_folder_move(
    asset_id: UUID,
    request: FolderMoveRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
) -> VaultAsset:
    """Copy, checksum-verify, catalogue, then remove an explicitly confirmed source."""
    if request.confirm is not True:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Explicit move confirmation is required")
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        source, destination, destination_vault_path = copy_catalogued_asset_to_existing_folder(asset, request, preview_roots)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error))
    updated = store.relocate_catalogued_asset(asset.id, asset.vault_path, destination_vault_path, username, "moved_to_folder")
    if updated is None:
        if destination.exists() and not destination.is_symlink():
            destination.unlink()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The requested move is no longer valid")
    try:
        source.unlink()
    except OSError as error:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"The verified copy is catalogued, but the original could not be removed: {error}") from error
    return to_api_asset(updated, username)


@router.get("/assets/{asset_id}/lifecycle/bin-restore-preflight", response_model=BinRestorePreflight)
def preflight_catalogued_asset_bin_restore_endpoint(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
    quarantine_root: Path = Depends(get_quarantine_root),
) -> BinRestorePreflight:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return preflight_catalogued_asset_bin_restore(asset, preview_roots, quarantine_root)


@router.post("/assets/{asset_id}/lifecycle/bin-restore-confirm", response_model=VaultAsset)
def confirm_catalogued_asset_bin_restore(
    asset_id: UUID,
    confirmation: QuarantineConfirmation,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
    quarantine_root: Path = Depends(get_quarantine_root),
) -> VaultAsset:
    del confirmation
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None or not asset_is_editable_by(asset, username):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        source, destination, original_vault_path = copy_catalogued_asset_from_bin(asset, preview_roots, quarantine_root)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    updated = store.relocate_catalogued_asset(asset.id, asset.vault_path, original_vault_path, username, "restored_from_bin")
    if updated is None:
        if destination.exists() and not destination.is_symlink(): destination.unlink()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The Bin restore is no longer valid")
    try:
        source.unlink()
    except OSError as error:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"The verified restored copy is catalogued, but the Bin original could not be removed: {error}") from error
    return to_api_asset(updated, username)


@router.get(
    "/assets/{asset_id}/preview",
    response_class=FileResponse,
)
def preview_catalogued_asset(
    asset_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
) -> FileResponse:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not (
        asset.mime_type.startswith("image/")
        or asset.mime_type.startswith("video/")
        or asset.mime_type == "application/pdf"
    ):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="This file type cannot be previewed",
        )
    try:
        path = resolve_catalogued_asset_path(asset, preview_roots)
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The catalogued preview was not found",
        ) from error
    return FileResponse(
        path=path,
        media_type=asset.mime_type,
        filename=asset.filename,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/assets/{asset_id}/artwork/{kind}",
    response_class=FileResponse,
)
def get_catalogued_asset_artwork(
    asset_id: UUID,
    kind: Literal["poster", "backdrop", "primary"],
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    storage_root: Path = Depends(get_metadata_storage_root),
) -> FileResponse:
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        path, mime_type = resolve_owned_artwork_path(
            asset,
            kind,
            storage_root,
        )
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The retained artwork was not found",
        ) from error
    return FileResponse(
        path=path,
        media_type=mime_type,
        filename=f"{asset.display_title}-{kind}",
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/assets/{asset_id}/people/{portrait_id}",
    response_class=FileResponse,
)
def get_catalogued_person_image(
    asset_id: UUID,
    portrait_id: str,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    storage_root: Path = Depends(get_metadata_storage_root),
) -> FileResponse:
    if (
        len(portrait_id) != 16
        or any(character not in "0123456789abcdef" for character in portrait_id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        path, mime_type = resolve_owned_person_image_path(
            asset,
            portrait_id,
            storage_root,
        )
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The retained person portrait was not found",
        ) from error
    return FileResponse(
        path=path,
        media_type=mime_type,
        filename=f"{asset.display_title}-person-{portrait_id}",
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/assets/{asset_id}/features/{feature_id}",
    response_class=FileResponse,
)
def get_catalogued_feature_image(
    asset_id: UUID,
    feature_id: str,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    storage_root: Path = Depends(get_metadata_storage_root),
) -> FileResponse:
    if (
        len(feature_id) != 16
        or any(character not in "0123456789abcdef" for character in feature_id)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    asset = store.get_visible_catalogued_asset_by_id(asset_id, username)
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        path, mime_type = resolve_owned_feature_image_path(
            asset,
            feature_id,
            storage_root,
        )
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The retained feature thumbnail was not found",
        ) from error
    return FileResponse(
        path=path,
        media_type=mime_type,
        filename=f"{asset.display_title}-feature-{feature_id}",
        content_disposition_type="inline",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get(
    "/publication-bundles",
    response_model=PublicationBundleListing,
)
def list_publication_bundles(
    response: Response,
    username: AuthenticatedAdministrator,
    store: VaultMasterStoreDependency,
) -> PublicationBundleListing:
    response.headers["Cache-Control"] = "private, no-store"
    return PublicationBundleListing(
        bundles=[
            PublicationBundleResult(**bundle.__dict__)
            for bundle in build_publication_bundles(store.list_items())
        ]
    )


@router.patch(
    "/publication-bundles/{source_item_id}",
    response_model=PublicationBundleListing,
)
def correct_publication_bundle_identity(
    source_item_id: UUID,
    edit: PublicationIdentityCorrection,
    response: Response,
    username: AuthenticatedAdministrator,
    store: VaultMasterStoreDependency,
) -> PublicationBundleListing:
    current = store.get_item(source_item_id)
    if current is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    parsed = parse_publication_filename(current.filename)
    is_source_pdf = (
        current.source_kind == INCOMING_SOURCE
        and parsed is not None
        and parsed.role == "source"
    )
    if not is_source_pdf:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Only an Arrival Hall publication PDF can anchor corrections",
        )
    updated = store.update_metadata_overrides(
        source_item_id,
        {
            CORRECTION_AUTHOR_KEY: edit.author,
            CORRECTION_TITLE_KEY: edit.title,
        },
        username,
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    return PublicationBundleListing(
        bundles=[
            PublicationBundleResult(**bundle.__dict__)
            for bundle in build_publication_bundles(store.list_items())
        ]
    )


def _publication_source_item(source_item_id: UUID, username: object, store: VaultMasterStore) -> ImportItem:
    if (
        not getattr(username, "active", False)
        or getattr(username, "role", None) != "administrator"
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    item = store.get_item(source_item_id)
    if item is None or item.source_kind != INCOMING_SOURCE or item.mime_type != "application/pdf":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    parsed = parse_publication_filename(item.filename)
    if parsed is None or parsed.role != "source":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The item is not a valid publication source PDF")
    return item


def _publication_review(source_item_id: UUID, username: str, reviews: PublicationReviewStore) -> PublicationReview:
    review = reviews.get(source_item_id, username)
    if review is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Publication extraction is not ready for review")
    return review


def _save_review(reviews: PublicationReviewStore, review: PublicationReview) -> dict[str, object]:
    try:
        return review_document(reviews.save(review))
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


def _publication_file_id(source_item_id: UUID, role: str, filename: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"personal-vault:publication:{source_item_id}:{role}:{filename}")


def _publish_reviewed_bundle(
    review: PublicationReview,
    bundle: object,
    username: str,
    store: VaultMasterStore,
    reading_store: ReadingRoomStore,
    reviews: PublicationReviewStore,
    incoming_path: Path,
    library_path: Path,
    workspace_root: Path,
) -> PublicationReview:
    item_ids = (
        *bundle.source_item_ids,
        *bundle.front_cover_item_ids,
        *bundle.back_cover_item_ids,
    )
    items = {item_id: store.get_item(item_id) for item_id in item_ids}
    if any(item is None for item in items.values()):
        raise ValueError("A reviewed publication source is no longer available")
    source = items[bundle.source_item_ids[0]]
    if source is None:
        raise ValueError("The reviewed publication PDF is unavailable")
    workspace = workspace_root / "publication-extraction" / str(review.source_item_id)
    html_path = workspace / "publication.html"
    ocr_path = workspace / "document.json"
    if not workspace.is_dir() or not ocr_path.is_file():
        raise ValueError("Reviewed publication extraction evidence is unavailable")
    write_reviewed_html(review, html_path)
    effective = {**review.snapshot.metadata.detected, **review.snapshot.metadata.user_overrides}
    author = str(effective.get("author", bundle.author)).strip()
    title = str(effective.get("title", bundle.title)).strip()
    edition = str(effective["edition"]).strip() if effective.get("edition") else None
    relative_directory = publication_directory(author, title, edition)
    storage_files: list[PublicationStorageFile] = [
        PublicationStorageFile(Path(source.source_path), publication_role_path("source_pdf", original_filename=source.filename), source.sha256),
    ]
    for role, ids in (("front_cover", bundle.front_cover_item_ids), ("back_cover", bundle.back_cover_item_ids)):
        if len(ids) == 1:
            cover = items[ids[0]]
            if cover is not None:
                storage_files.append(PublicationStorageFile(Path(cover.source_path), publication_role_path(role), cover.sha256))
    storage_files.extend((
        PublicationStorageFile(html_path, publication_role_path("structured_html"), sha256_file(html_path), workspace),
        PublicationStorageFile(ocr_path, publication_role_path("ocr_data"), sha256_file(ocr_path), workspace),
    ))
    for publication_file in review.snapshot.files:
        if publication_file.role != "illustration":
            continue
        illustration = workspace / "illustrations" / publication_file.filename
        storage_files.append(PublicationStorageFile(illustration, publication_role_path("illustration", original_filename=publication_file.filename), publication_file.sha256, workspace))
    destination = safely_publish_publication_directory(incoming_path, library_path, relative_directory, tuple(storage_files))
    source_relative = publication_role_path("source_pdf", original_filename=source.filename)
    source_vault_path = str(PurePosixPath("/vault/Library") / relative_directory / source_relative)
    canonical = CataloguedAsset(
        id=review.source_item_id,
        asset_type="Library",
        display_title=title,
        captured_on=None,
        location=None,
        vault_path=source_vault_path,
        filename=source.filename,
        size_bytes=source.size_bytes,
        mime_type=source.mime_type,
        sha256=source.sha256,
        metadata={"author": author, "publication_type": review.snapshot.metadata.publication_type, "language": review.snapshot.metadata.language, **({"edition": edition} if edition else {})},
        metadata_provenance={"author": "reviewed_publication", "display_title": "reviewed_publication"},
        detected_metadata=review.snapshot.metadata.detected,
        user_overrides=review.snapshot.metadata.user_overrides,
        effective_metadata=effective,
        owner_username=username,
    )
    store.restore_catalogued_asset(canonical, username)
    published_files: list[PublicationFile] = [
        PublicationFile(_publication_file_id(review.source_item_id, "source_pdf", source.filename), review.source_item_id, "source_pdf", source_vault_path, source.filename, source.mime_type, source.sha256, True, 0),
    ]
    ordinal = 1
    for role, ids, filename in (("front_cover", bundle.front_cover_item_ids, "front.jpg"), ("back_cover", bundle.back_cover_item_ids, "back.jpg")):
        if len(ids) == 1 and items[ids[0]] is not None:
            cover = items[ids[0]]
            vault_path = str(PurePosixPath("/vault/Library") / relative_directory / "covers" / filename)
            published_files.append(PublicationFile(_publication_file_id(review.source_item_id, role, filename), review.source_item_id, role, vault_path, filename, "image/jpeg", cover.sha256, True, ordinal))
            ordinal += 1
    html_vault = str(PurePosixPath("/vault/Library") / relative_directory / "reading" / "publication.html")
    ocr_vault = str(PurePosixPath("/vault/Library") / relative_directory / "ocr" / "document.json")
    published_files.extend((
        PublicationFile(_publication_file_id(review.source_item_id, "reading_content", "publication.html"), review.source_item_id, "reading_content", html_vault, "publication.html", "text/html", next(file.sha256 for file in storage_files if file.source_path == html_path), False, ordinal),
        PublicationFile(_publication_file_id(review.source_item_id, "page_evidence", "document.json"), review.source_item_id, "page_evidence", ocr_vault, "document.json", "application/json", next(file.sha256 for file in storage_files if file.source_path == ocr_path), False, ordinal + 1),
    ))
    for original in review.snapshot.files:
        if original.role == "illustration":
            vault_path = str(PurePosixPath("/vault/Library") / relative_directory / "reading" / "illustrations" / original.filename)
            published_files.append(replace(original, vault_path=vault_path, ordinal=len(published_files)))
    published_snapshot = PublicationSnapshot(replace(review.snapshot.metadata, extraction_state="approved", updated_at=datetime.now(timezone.utc)), tuple(published_files), review.snapshot.blocks, review.snapshot.issues)
    reading_store.save_publication(published_snapshot)
    for item_id in item_ids:
        store.record_move_result(item_id, "moved", username, f"Published Reading Room bundle at {destination.directory}", publish_catalogue=False)
    published_review = replace(review, state="published", snapshot=published_snapshot, revision=review.revision + 1, updated_at=datetime.now(timezone.utc), updated_by=username)
    reviews.save(published_review)
    return published_review


@router.post("/publication-bundles/{source_item_id}/extract", response_model=PublicationExtractionResult)
def extract_publication_bundle(
    source_item_id: UUID,
    request: PublicationExtractionRequest,
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    reviews: PublicationReviewStoreDependency,
    incoming_path: Path = Depends(get_incoming_path),
    workspace_root: Path = Depends(get_metadata_storage_root),
) -> PublicationExtractionResult:
    item = _publication_source_item(source_item_id, username, store)
    bundle = next(
        (
            candidate
            for candidate in build_publication_bundles(store.list_items())
            if source_item_id in candidate.source_item_ids
        ),
        None,
    )
    parsed = parse_publication_filename(item.filename)
    if bundle is not None and any(issue in bundle.issues for issue in ("multiple_source_pdfs", "duplicate_checksum", "normalised_identity_collision")):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Publication bundle ambiguity must be corrected before extraction")
    try:
        progress = extract_publication(
            asset_id=source_item_id,
            source=Path(item.source_path),
            source_root=incoming_path,
            expected_sha256=item.sha256,
            author=(bundle.author if bundle is not None else parsed.author),
            title=(bundle.title if bundle is not None else parsed.title),
            workspace_root=workspace_root / "publication-extraction",
            florence_ocr=request_florence_ocr,
            max_florence_pages=request.max_florence_pages,
        )
    except (OSError, ValueError, RuntimeError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    review_value = None
    if progress.snapshot is not None:
        current = reviews.get(source_item_id, username)
        review = PublicationReview(source_item_id, username, "needs_review", progress.snapshot, revision=(current.revision + 1 if current else 1), updated_by=username)
        review_value = review_document(reviews.save(review))
    response.headers["Cache-Control"] = "private, no-store"
    return PublicationExtractionResult(source_item_id=source_item_id, page_count=progress.page_count, completed_pages=progress.completed_pages, pending_pages=progress.pending_pages, review=review_value)


@router.get("/publication-bundles/{source_item_id}/review")
def get_publication_bundle_review(source_item_id: UUID, response: Response, username: AuthenticatedUsername, store: VaultMasterStoreDependency, reviews: PublicationReviewStoreDependency) -> dict[str, object]:
    _publication_source_item(source_item_id, username, store)
    response.headers["Cache-Control"] = "private, no-store"
    return review_document(_publication_review(source_item_id, username, reviews))


@router.patch("/publication-bundles/{source_item_id}/review/metadata")
def correct_publication_review_metadata(source_item_id: UUID, edit: PublicationMetadataCorrection, username: AuthenticatedUsername, store: VaultMasterStoreDependency, reviews: PublicationReviewStoreDependency) -> dict[str, object]:
    _publication_source_item(source_item_id, username, store)
    try:
        updated = correct_review_metadata(_publication_review(source_item_id, username, reviews), username, values=edit.values, language=edit.language, publication_type=edit.publication_type)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _save_review(reviews, updated)


@router.patch("/publication-bundles/{source_item_id}/review/blocks/{block_id}")
def correct_publication_review_text(source_item_id: UUID, block_id: UUID, edit: PublicationBlockCorrection, username: AuthenticatedUsername, store: VaultMasterStoreDependency, reviews: PublicationReviewStoreDependency) -> dict[str, object]:
    _publication_source_item(source_item_id, username, store)
    try:
        updated = correct_review_block(_publication_review(source_item_id, username, reviews), username, block_id, text=edit.text, block_type=edit.block_type)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _save_review(reviews, updated)


@router.patch("/publication-bundles/{source_item_id}/review/pages")
def correct_publication_review_pages(source_item_id: UUID, edit: PublicationPageCorrection, username: AuthenticatedUsername, store: VaultMasterStoreDependency, reviews: PublicationReviewStoreDependency) -> dict[str, object]:
    _publication_source_item(source_item_id, username, store)
    try:
        updated = correct_page_order(_publication_review(source_item_id, username, reviews), username, edit.page_order, edit.rotations)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _save_review(reviews, updated)


@router.patch("/publication-bundles/{source_item_id}/review/issues/{issue_id}")
def resolve_publication_review_issue(source_item_id: UUID, issue_id: UUID, edit: PublicationIssueReview, username: AuthenticatedUsername, store: VaultMasterStoreDependency, reviews: PublicationReviewStoreDependency) -> dict[str, object]:
    _publication_source_item(source_item_id, username, store)
    try:
        updated = review_publication_issue(_publication_review(source_item_id, username, reviews), username, issue_id, edit.state)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _save_review(reviews, updated)


@router.patch("/publication-bundles/{source_item_id}/review/illustrations/{block_id}")
def correct_publication_review_caption(source_item_id: UUID, block_id: UUID, edit: PublicationCaptionCorrection, username: AuthenticatedUsername, store: VaultMasterStoreDependency, reviews: PublicationReviewStoreDependency) -> dict[str, object]:
    _publication_source_item(source_item_id, username, store)
    try:
        updated = caption_illustration(_publication_review(source_item_id, username, reviews), username, block_id, edit.caption)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _save_review(reviews, updated)


@router.post("/publication-bundles/{source_item_id}/review/action")
def act_on_publication_review(source_item_id: UUID, edit: PublicationReviewAction, username: AuthenticatedUsername, store: VaultMasterStoreDependency, reviews: PublicationReviewStoreDependency, reading_store: ReadingRoomStoreDependency, incoming_path: Path = Depends(get_incoming_path), workspace_root: Path = Depends(get_metadata_storage_root), destination_paths: dict[str, Path] = Depends(get_destination_paths)) -> dict[str, object]:
    _publication_source_item(source_item_id, username, store)
    try:
        updated = transition_review(_publication_review(source_item_id, username, reviews), username, edit.action)
        if edit.action == "publish":
            bundle = next((bundle for bundle in build_publication_bundles(store.list_items()) if source_item_id in bundle.source_item_ids), None)
            if bundle is None:
                item = store.get_item(source_item_id)
                parsed = parse_publication_filename(item.filename) if item else None
                if item is not None and parsed is not None and parsed.role == "source":
                    bundle = PublicationBundle(
                        key=f"{parsed.author}::{parsed.title}",
                        author=parsed.author,
                        title=parsed.title,
                        source_item_ids=(source_item_id,),
                        front_cover_item_ids=(),
                        back_cover_item_ids=(),
                        issues=(),
                    )
            library_path = destination_paths.get("Library")
            if bundle is None or library_path is None:
                raise ValueError("Publication Library destination is unavailable")
            updated = _publish_reviewed_bundle(updated, bundle, username, store, reading_store, reviews, incoming_path, library_path, workspace_root)
            return review_document(updated)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return _save_review(reviews, updated)


@router.get("/publication-bundles/{source_item_id}/pages/{page_number}", response_class=FileResponse)
def get_publication_exact_page(source_item_id: UUID, page_number: int, username: AuthenticatedUsername, store: VaultMasterStoreDependency, incoming_path: Path = Depends(get_incoming_path), workspace_root: Path = Depends(get_metadata_storage_root)) -> FileResponse:
    item = _publication_source_item(source_item_id, username, store)
    destination = workspace_root / "publication-extraction" / str(source_item_id) / "pages" / f"page-{page_number:04d}.png"
    try:
        path = render_exact_page(Path(item.source_path), incoming_path, item.sha256, page_number, destination)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/items", response_model=VaultMasterListing)
def list_vault_master_items(
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultMasterListing:
    response.headers["Cache-Control"] = "private, no-store"
    items = store.list_items()
    incoming_items = [
        item
        for item in items
        if item.source_kind == INCOMING_SOURCE
        and item.owner_user_id == getattr(username, "user_id", None)
    ]
    current_user_id = getattr(username, "user_id", None)
    by_id = {item.id: item for item in items}
    referenced_ids = {
        item.duplicate_of_id
        for item in incoming_items
        if item.duplicate_of_id is not None
        and by_id.get(item.duplicate_of_id) is not None
        and by_id[item.duplicate_of_id].owner_user_id == current_user_id
    }
    return VaultMasterListing(
        items=[
            to_api_item(item)
            for item in items
            if (
                item.source_kind == INCOMING_SOURCE
                and item.owner_user_id == getattr(username, "user_id", None)
            )
            or item.id in referenced_ids
        ]
    )


@router.get("/tv-resolver/batches")
def list_tv_resolver_batches(
    response: Response,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> dict[str, object]:
    """Return owner-scoped, non-mutating TV disc proposals for staged items.

    Acceptance remains deliberately separate: no file is moved, renamed, or
    published by this inspection endpoint.
    """
    response.headers["Cache-Control"] = "private, no-store"
    owner_id = getattr(username, "user_id", None)
    batches = discover_tv_disc_batches(
        item for item in store.list_items() if item.owner_user_id == owner_id
    )
    # Unit-test and local memory stores retain the existing read-only view;
    # deployed PostgreSQL uses durable review records.
    durable = None if isinstance(store, MemoryVaultMasterStore) or owner_id is None else PostgresTvResolverStore(get_database_conninfo())
    if durable is not None:
        for batch in batches:
            durable.sync_proposal(owner_id, batch, resolve_tv_disc_batch(batch))
        return {"batches": durable.list_for_owner(owner_id)}
    proposals = []
    for batch in batches:
        proposal = resolve_tv_disc_batch(batch)
        proposals.append(
            {
                "batch_key": proposal.batch_key,
                "resolver_version": "pv-tv-disc-resolver.v1",
                "show_title": proposal.show_title,
                "confidence": proposal.confidence,
                "needs_review": proposal.needs_review,
                "evidence": list(proposal.evidence),
                "tracks": [
                    {
                        "item_id": str(track.item_id),
                        "filename": track.filename,
                        "season_number": track.season_number,
                        "disc_number": track.disc_number,
                        "track_number": track.track_number,
                        "duration_seconds": track.duration_seconds,
                        "classification": track.classification,
                        "proposed_episode_number": track.episode_number,
                        "confidence": track.confidence,
                        "destination": track.destination,
                        "evidence": list(track.evidence),
                    }
                    for track in proposal.tracks
                ],
            }
        )
    return {"batches": proposals}


class TvResolverApprovalRequest(BaseModel):
    publication_audience: Literal["private", "vault-wide"] = "vault-wide"


def _durable_tv_resolver_batch(
    batch_id: UUID, username: AuthenticatedUsername, store: VaultMasterStore
) -> PostgresTvResolverStore:
    # This boundary is intentionally PostgreSQL-only: a durable batch cannot be
    # fabricated by the in-memory test double or a client payload.
    if isinstance(store, MemoryVaultMasterStore) or getattr(username, "user_id", None) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    durable = PostgresTvResolverStore(get_database_conninfo())
    if durable.get_for_owner(batch_id, username.user_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return durable


@router.get("/tv-resolver/batches/{batch_id}")
def get_tv_resolver_batch(
    batch_id: UUID, response: Response, username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    durable = _durable_tv_resolver_batch(batch_id, username, store)
    batch = durable.get_for_owner(batch_id, username.user_id)
    if batch is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return batch


@router.post("/tv-resolver/batches/{batch_id}/approve")
def approve_tv_resolver_batch(
    batch_id: UUID, request: TvResolverApprovalRequest, username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> dict[str, object]:
    durable = _durable_tv_resolver_batch(batch_id, username, store)
    try:
        return durable.approve(batch_id, username.user_id, str(username), request.publication_audience)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


@router.post("/tv-resolver/batches/{batch_id}/retry")
def retry_tv_resolver_batch(
    batch_id: UUID, username: AuthenticatedUsername, store: VaultMasterStoreDependency,
) -> dict[str, object]:
    durable = _durable_tv_resolver_batch(batch_id, username, store)
    try:
        return durable.retry(batch_id, username.user_id, str(username))
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error


def require_owned_arrival_item(
    item_id: UUID,
    username: object,
    store: VaultMasterStore,
) -> ImportItem:
    item = store.get_item(item_id)
    if (
        item is None
        or item.source_kind != INCOMING_SOURCE
        or item.owner_user_id is None
        or item.owner_user_id != getattr(username, "user_id", None)
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return item


def _validate_bulk_approval_selection(
    item_ids: tuple[UUID, ...],
    username: AuthenticatedUsername,
    store: VaultMasterStore,
) -> list[ImportItem]:
    """Reject an invalid Theatre group before any selected item is approved."""
    all_items = store.list_items()
    by_id = {item.id: item for item in all_items}
    owner_user_id = getattr(username, "user_id", None)
    selected = [by_id.get(item_id) for item_id in item_ids]
    if not item_ids or any(item is None for item in selected):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Every selected file must still be available for approval",
        )
    items = [item for item in selected if item is not None]
    if any(
        item.source_kind != INCOMING_SOURCE
        or item.owner_user_id != owner_user_id
        or item.state != "needs_review"
        or item.duplicate_of_id is not None
        for item in items
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Every selected file must be an owned, non-duplicate proposal awaiting review",
        )

    selected_ids = {item.id for item in items}
    checked_groups: set[frozenset[UUID]] = set()
    for item in items:
        owner_items = [candidate for candidate in all_items if candidate.owner_user_id == item.owner_user_id]
        if item.proposed_category == "TV Shows":
            parsed = parse_reviewed_episode(item.relative_path, item.filename)
            if parsed is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="TV Show publication requires an explicit SnnEnn episode filename",
                )
            parent = PurePosixPath(item.relative_path.replace("\\", "/")).parent
            members = [
                candidate
                for candidate in owner_items
                if candidate.source_kind == INCOMING_SOURCE
                and candidate.state not in {"arrival_removed", "duplicate_removed", "rejected"}
                and PurePosixPath(candidate.relative_path.replace("\\", "/")).parent == parent
                and (candidate_parsed := parse_reviewed_episode(candidate.relative_path, candidate.filename)) is not None
                and candidate_parsed.show_title.casefold() == parsed.show_title.casefold()
                and candidate_parsed.season_number == parsed.season_number
            ]
            member_ids = frozenset(candidate.id for candidate in members)
            if member_ids in checked_groups:
                continue
            checked_groups.add(member_ids)
            if not member_ids.issubset(selected_ids):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Select every Episode in this TV Show Season before approving it together",
                )
            if any(candidate.proposed_category != "TV Shows" for candidate in members):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Every Episode in this TV Show Season must be reviewed as TV Shows",
                )
            audiences = {candidate.publication_audience or "vault-wide" for candidate in members}
            if len(audiences) != 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Every Episode in this TV Show Season must use the same audience",
                )
            try:
                tv_publication_set_destination(item, owner_items)
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error
        elif (
            item.proposed_category == "Movies"
            and MAKEMKV_TRACK_PATTERN.fullmatch(Path(item.filename).stem)
        ):
            try:
                _, _, marker = movie_publication_set_destination(item, owner_items)
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error
            if marker is None:
                continue
            member_ids = frozenset(
                UUID(str(entry["item_id"]))
                for entry in marker["members"]
                if isinstance(entry, dict)
            )
            if member_ids in checked_groups:
                continue
            checked_groups.add(member_ids)
            if not member_ids.issubset(selected_ids):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Select every file in this Movie publication set before approving it together",
                )
            audiences = {
                candidate.publication_audience or "vault-wide"
                for candidate in owner_items
                if candidate.id in member_ids
            }
            if len(audiences) != 1:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="Every file in this Movie publication set must use the same audience",
                )
    return items


@router.get("/jobs", response_model=VaultMasterJobListing)
def list_vault_master_jobs(
    response: Response,
    username: AuthenticatedAdministrator,
    store: VaultMasterStoreDependency,
) -> VaultMasterJobListing:
    response.headers["Cache-Control"] = "private, no-store"
    return VaultMasterJobListing(
        jobs=[VaultMasterJob(**job) for job in store.list_batches()]
    )


@router.get("/activity", response_model=VaultMasterActivityListing)
def list_vault_master_activity(
    response: Response,
    username: AuthenticatedAdministrator,
    store: VaultMasterStoreDependency,
    limit: int = Query(default=100, ge=1, le=200),
) -> VaultMasterActivityListing:
    response.headers["Cache-Control"] = "private, no-store"
    return VaultMasterActivityListing(
        events=[
            VaultMasterActivityEntry(**event.__dict__)
            for event in store.list_activity(
                limit,
                include_file_inventory=False,
                include_file_analysis=False,
                include_empty_scans=False,
            )
        ]
    )


@router.post(
    "/sidecars/reconcile",
    response_model=SidecarReconciliationResult,
)
def reconcile_vault_master_sidecars(
    username: AuthenticatedAdministrator,
    store: VaultMasterStoreDependency,
) -> SidecarReconciliationResult:
    result = store.reconcile_sidecars()
    return SidecarReconciliationResult(**result.__dict__)


@router.get(
    "/sidecars/recovery/assessment",
    response_model=SidecarRecoveryAssessmentResult,
)
def assess_vault_master_sidecar_recovery(
    response: Response,
    username: ElevatedVaultControlAdministrator,
    store: VaultMasterStoreDependency,
    metadata_root: Path = Depends(get_metadata_storage_root),
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
) -> SidecarRecoveryAssessmentResult:
    response.headers["Cache-Control"] = "private, no-store"

    def file_is_recoverable(asset: CataloguedAsset) -> bool:
        try:
            path = resolve_catalogued_asset_path(asset, preview_roots)
            return (
                path.is_file()
                and path.stat().st_size == asset.size_bytes
                and sha256_file(path) == asset.sha256
            )
        except OSError:
            return False

    result = compare_sidecar_recovery(
        metadata_root,
        store.get_catalogued_asset_by_id,
        store.get_catalogued_asset,
        store.has_catalogued_asset_deletion,
        file_is_recoverable,
    )
    values = dict(result.__dict__)
    values["candidates"] = [
        candidate.__dict__ for candidate in result.candidates
    ]
    return SidecarRecoveryAssessmentResult(**values)


@router.post(
    "/sidecars/recovery/{asset_id}/restore",
    response_model=VaultAsset,
)
def restore_vault_master_sidecar(
    asset_id: UUID,
    confirmation: SidecarRestoreConfirmation,
    username: ElevatedVaultControlAdministrator,
    store: VaultMasterStoreDependency,
    authentication: AuthenticationStoreDependency,
    metadata_root: Path = Depends(get_metadata_storage_root),
    preview_roots: dict[str, Path] = Depends(get_catalogue_preview_roots),
) -> VaultAsset:
    try:
        asset = read_restorable_sidecar(
            metadata_root,
            asset_id,
            store.get_catalogued_asset_by_id,
            store.get_catalogued_asset,
            store.has_catalogued_asset_deletion,
        )
        owner = authentication.get_account(asset.owner_username)
        if owner is None or not owner.active:
            raise ValueError("Sidecar owner identity cannot be resolved")
        if asset.owner_user_id is not None and asset.owner_user_id != owner.user_id:
            raise ValueError("Sidecar owner identity does not match its account")
        asset = replace(asset, owner_user_id=owner.user_id)
        file_path = resolve_catalogued_asset_path(asset, preview_roots)
        if file_path.stat().st_size != asset.size_bytes:
            raise ValueError("The permanent file size does not match")
        if sha256_file(file_path) != asset.sha256:
            raise ValueError("The permanent file checksum does not match")
        restored = store.restore_catalogued_asset(asset, username)
    except (OSError, ValueError) as error:
        store.record_sidecar_restore_failure(asset_id, username, str(error))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    return to_api_asset(restored, username)


@router.post("/scan/arrival-hall", response_model=ScanResult)
@router.post("/scan/incoming", response_model=ScanResult)
def scan_incoming(
    username: AuthenticatedAdministrator,
    store: VaultMasterStoreDependency,
    incoming_path: Path = Depends(get_incoming_path),
) -> ScanResult:

    try:
        batch_id = enqueue_root(store, incoming_path, INCOMING_SOURCE)
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Arrival Hall storage could not be scanned",
        ) from error

    return ScanResult(batch_ids=[batch_id], status="queued")


@router.post("/catalogue/backfill", response_model=ScanResult)
@router.post("/scan/inventory", response_model=ScanResult)
def scan_inventory(
    username: AuthenticatedAdministrator,
    store: VaultMasterStoreDependency,
    inventory_paths: tuple[Path, ...] = Depends(get_inventory_paths),
) -> ScanResult:
    try:
        batch_ids, reused_count = enqueue_catalogue_backfill(
            store,
            inventory_paths,
        )
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Permanent Vault storage could not be inventoried",
        ) from error

    return ScanResult(
        batch_ids=batch_ids,
        status="queued",
        reused_active_batches=reused_count,
    )


@router.patch("/items/{item_id}/proposal", response_model=VaultMasterItem)
def edit_proposal(
    item_id: UUID,
    edit: ProposalEdit,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
) -> VaultMasterItem:
    require_owned_arrival_item(item_id, username, store)
    item = store.update_proposal(
        item_id,
        edit.category,
        username,
        publication_audience=edit.publication_audience,
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return to_api_item(item)


@router.patch(
    "/items/{item_id}/metadata",
    response_model=VaultMasterItem,
)
def edit_metadata(
    item_id: UUID,
    edit: MetadataOverrideEdit,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultMasterItem:
    if not edit.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="At least one editable metadata field is required",
        )
    changes = {
        name: value.isoformat() if isinstance(value, date) else value
        for name, value in edit.model_dump(
            exclude_unset=True,
        ).items()
    }
    require_owned_arrival_item(item_id, username, store)
    item = store.update_metadata_overrides(
        item_id,
        changes,
        username,
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return to_api_item(item)


@router.post("/items/{item_id}/approve", response_model=VaultMasterItem)
def approve_proposal(
    item_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
) -> VaultMasterItem:
    original = require_owned_arrival_item(item_id, username, store)
    if original.duplicate_of_id is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "This item already exists in Theatre"
                if is_theatre_category(original.proposed_category)
                else "An exact duplicate requires review"
            ),
        )
    try:
        item = store.record_decision(item_id, "approved", username)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(error)
        ) from error
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if original is not None:
        ai_store.remember_decision(original, original.proposed_category, "approved", username)
    return to_api_item(item)


@router.post("/items/{item_id}/reject", response_model=VaultMasterItem)
def reject_proposal(
    item_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
) -> VaultMasterItem:
    original = require_owned_arrival_item(item_id, username, store)
    item = store.record_decision(item_id, "rejected", username)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if original is not None:
        ai_store.remember_decision(original, original.proposed_category, "rejected", username)
    return to_api_item(item)


@router.post("/items/{item_id}/return-to-review", response_model=VaultMasterItem)
def return_rejected_item_to_review(
    item_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultMasterItem:
    item = require_owned_arrival_item(item_id, username, store)
    if (
        item.state != "rejected"
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT)
    restored = store.record_decision(item_id, "needs_review", username)
    if restored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return to_api_item(restored)


@router.post("/items/{item_id}/rejected/remove", response_model=VaultMasterItem)
def remove_rejected_arrival_item(
    item_id: UUID,
    confirmation: RejectedArrivalRemovalConfirmation,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    incoming_path: Path = Depends(get_incoming_path),
) -> VaultMasterItem:
    del confirmation
    item = require_owned_arrival_item(item_id, username, store)
    try:
        safely_remove_rejected_arrival_item(item, incoming_path)
    except (OSError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    removed = store.record_decision(item_id, "arrival_removed", username)
    if removed is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return to_api_item(removed)


@router.post("/bulk/approve", response_model=BulkActionResult)
def bulk_approve_proposals(
    selection: ItemSelection,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
) -> BulkActionResult:
    item_ids = tuple(dict.fromkeys(selection.item_ids))
    items = _validate_bulk_approval_selection(item_ids, username, store)
    updated: list[VaultMasterItem] = []
    for item in items:
        try:
            approved = store.record_decision(item.id, "approved", username)
        except ValueError as error:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(error)
            ) from error
        if approved is not None:
            ai_store.remember_decision(item, item.proposed_category, "approved", username)
            updated.append(to_api_item(approved))
    return BulkActionResult(items=updated)


@router.post("/bulk/move", response_model=BulkActionResult)
def bulk_queue_moves(
    selection: ItemSelection,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> BulkActionResult:
    updated: list[VaultMasterItem] = []
    for item_id in dict.fromkeys(selection.item_ids):
        item = require_owned_arrival_item(item_id, username, store)
        queued = store.queue_move(item.id, username)
        if queued is not None:
            updated.append(to_api_item(queued))
    return BulkActionResult(items=updated)


@router.post(
    "/items/ai/review-batches",
    response_model=IngestionReviewBatchResult,
)
def execute_ingestion_review_batch(
    request: IngestionReviewBatchRequest,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
) -> IngestionReviewBatchResult:
    item_ids = tuple(dict.fromkeys(request.item_ids))
    outcomes: dict[str, str] = {}
    updated: list[VaultMasterItem] = []
    for item_id in item_ids:
        item = store.get_item(item_id)
        if (
            item is None
            or item.source_kind != INCOMING_SOURCE
            or item.owner_user_id != getattr(username, "user_id", None)
        ):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Every selected file must belong to the authenticated owner",
            )
        result = None
        if request.action == "approve":
            if item.state != "needs_review" or item.duplicate_of_id is not None:
                outcomes[str(item_id)] = "not_eligible"
                continue
            evidence = ai_store.list_evidence(item_id, getattr(username, "user_id", username))
            if not evidence or evidence[0].routing_band == "individual_review":
                outcomes[str(item_id)] = "individual_review_required"
                continue
            try:
                result = store.record_decision(item_id, "approved", username)
            except ValueError as error:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail=str(error)
                ) from error
        elif request.action == "reject":
            if item.state not in {"needs_review", "approved"}:
                outcomes[str(item_id)] = "not_eligible"
                continue
            result = store.record_decision(item_id, "rejected", username)
        else:
            if item.state not in {"approved", "move_failed"}:
                outcomes[str(item_id)] = "approval_required"
                continue
            result = store.queue_move(item_id, username)
        if result is None:
            outcomes[str(item_id)] = "not_updated"
        else:
            if request.action in {"approve", "reject"}:
                ai_store.remember_decision(
                    item, item.proposed_category, request.action + "d", username
                )
            outcomes[str(item_id)] = "queued" if request.action == "move" else request.action + "d"
            updated.append(to_api_item(result))
    audit_id = ai_store.record_review_batch(
        request.action, item_ids, outcomes, username
    )
    return IngestionReviewBatchResult(
        batch_id=audit_id,
        action=request.action,
        outcomes=outcomes,
        items=updated,
    )


@router.get("/routing-memory", response_model=RoutingMemoryListing)
def list_routing_memory(
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
    authentication: AuthenticationStoreDependency,
    response: Response,
) -> RoutingMemoryListing:
    account = authentication.get_account(username)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    staged = {item.id: item for item in store.list_items() if item.source_kind == INCOMING_SOURCE}
    matches: dict[str, list[UUID]] = {}
    for evidence in ai_store.list_user_evidence(getattr(username, "user_id", username)):
        item = staged.get(evidence.item_id)
        if item is None:
            continue
        signature = routing_signature(routing_features(item, evidence.content_type, evidence.ocr_text))
        matches.setdefault(signature, []).append(item.id)
    rules = []
    for rule in ai_store.list_routing_rules(account.user_id):
        result = RoutingMemoryRuleResult.model_validate(rule)
        result.affected_item_ids = list(dict.fromkeys(matches.get(rule.feature_signature, [])))
        rules.append(result)
    return RoutingMemoryListing(rules=rules)


@router.patch("/routing-memory/{rule_id}", response_model=RoutingMemoryRuleResult)
def update_routing_memory(
    rule_id: UUID,
    update: RoutingMemoryUpdate,
    username: AuthenticatedUsername,
    ai_store: IngestionAiStoreDependency,
    authentication: AuthenticationStoreDependency,
) -> RoutingMemoryRuleResult:
    account = authentication.get_account(username)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    status_value = {"enable": "enabled", "disable": "disabled", "reset": "reset", "edit": "edit"}[update.action]
    if update.action == "edit" and update.destination is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY)
    rule = ai_store.update_routing_rule(rule_id, account.user_id, status_value, update.destination)
    if rule is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return RoutingMemoryRuleResult.model_validate(rule)


@router.delete("/routing-memory/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_routing_memory(
    rule_id: UUID,
    username: AuthenticatedUsername,
    ai_store: IngestionAiStoreDependency,
    authentication: AuthenticationStoreDependency,
) -> Response:
    account = authentication.get_account(username)
    if account is None or not ai_store.delete_routing_rule(rule_id, account.user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/autopilot", response_model=AutopilotListing)
def list_autopilot(
    username: AuthenticatedUsername,
    autopilot_store: AutopilotStoreDependency,
    authentication: AuthenticationStoreDependency,
    response: Response,
) -> AutopilotListing:
    account = authentication.get_account(username)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    response.headers["Cache-Control"] = "private, no-store"
    return AutopilotListing(
        policies=[
            AutopilotPolicyResult.model_validate(policy)
            for policy in autopilot_store.list_policies(account.user_id)
        ],
        runs=[
            AutopilotRunResult.model_validate(run)
            for run in autopilot_store.list_runs(account.user_id)
        ],
    )


@router.get(
    "/autopilot/gallery-screenshot-audit",
    response_model=GalleryScreenshotAuditResult,
)
def gallery_screenshot_audit(
    response: Response,
    username: AuthenticatedAdministrator,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
    autopilot_store: AutopilotStoreDependency,
) -> GalleryScreenshotAuditResult:
    response.headers["Cache-Control"] = "private, no-store"
    return GalleryScreenshotAuditResult(
        suspects=[
            GalleryScreenshotSuspectResult.model_validate(suspect)
            for suspect in audit_recent_gallery_screenshots(
                autopilot_store,
                ai_store,
                store,
                username,
            )
        ]
    )


@router.put("/autopilot/policy", response_model=AutopilotPolicyResult)
def configure_autopilot(
    update: AutopilotPolicyUpdate,
    username: AuthenticatedUsername,
    autopilot_store: AutopilotStoreDependency,
    authentication: AuthenticationStoreDependency,
) -> AutopilotPolicyResult:
    account = authentication.get_account(username)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    try:
        policy = autopilot_store.upsert_policy(
            account.user_id,
            username,
            update.content_type,
            update.destination,
            update.threshold,
            update.max_items,
            update.max_failures,
            update.max_failure_percent,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(error),
        ) from error
    return AutopilotPolicyResult.model_validate(policy)


@router.patch(
    "/autopilot/policy/{policy_id}", response_model=AutopilotPolicyResult
)
def update_autopilot_status(
    policy_id: UUID,
    update: AutopilotPolicyStatusUpdate,
    username: AuthenticatedUsername,
    autopilot_store: AutopilotStoreDependency,
    authentication: AuthenticationStoreDependency,
) -> AutopilotPolicyResult:
    account = authentication.get_account(username)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    policy = autopilot_store.set_policy_status(
        policy_id, account.user_id, update.status
    )
    if policy is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return AutopilotPolicyResult.model_validate(policy)


@router.post("/autopilot/run", response_model=AutopilotListing)
def run_autopilot_now(
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    ai_store: IngestionAiStoreDependency,
    autopilot_store: AutopilotStoreDependency,
    authentication: AuthenticationStoreDependency,
    incoming_path: Path = Depends(get_incoming_path),
    destination_paths: dict[str, Path] = Depends(get_destination_paths),
) -> AutopilotListing:
    account = authentication.get_account(username)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    process_autopilot_batch(
        autopilot_store,
        ai_store,
        store,
        incoming_path,
        destination_paths,
    )
    return AutopilotListing(
        policies=[AutopilotPolicyResult.model_validate(policy) for policy in autopilot_store.list_policies(account.user_id)],
        runs=[AutopilotRunResult.model_validate(run) for run in autopilot_store.list_runs(account.user_id)],
    )


@router.post("/items/{item_id}/move", response_model=VaultMasterItem)
def move_approved_file(
    item_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultMasterItem:
    item = require_owned_arrival_item(item_id, username, store)
    if item.state not in {"approved", "move_failed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The proposal must be approved before it can be moved",
        )
    if not movie_publication_set_is_ready(item, store.list_items()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Every file in this Movie publication set must be approved "
                "before any member can be moved"
            ),
        )
    if item.proposed_category == "TV Shows" and not tv_publication_set_is_ready(item, store.list_items()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Every Episode in this TV Show Season must be approved before publication")
    if not movie_publication_set_has_consistent_audience(item, store.list_items()):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Every file in this Movie publication set must use the same audience",
        )
    if item.proposed_category == "TV Shows" and not tv_publication_set_has_consistent_audience(item, store.list_items()):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Every Episode in this TV Show Season must use the same audience")
    queued = store.queue_move(item.id, username)
    if queued is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The proposal must be approved before it can be moved",
        )
    return to_api_item(queued)


@router.post("/items/{item_id}/theatre-promotion/reissue", response_model=VaultMasterItem)
def reissue_theatre_promotion(
    item_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    incoming_path: Path = Depends(get_incoming_path),
) -> VaultMasterItem:
    """Recover an expired root request without a second owner approval."""
    item = require_owned_arrival_item(item_id, username, store)
    destination = item.proposed_destination
    if (
        item.state != "theatre_promotion_pending"
        or item.proposed_category not in {"Movies", "TV Shows"}
        or not destination
        or store.get_catalogued_asset(destination) is not None
    ):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Theatre promotion is not eligible for reissue")
    try:
        reissue_arrival_theatre_item(item, incoming_path)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(error)) from error
    return to_api_item(item)


def require_inventory_duplicate(
    item_id: UUID,
    username: str,
    store: VaultMasterStore,
) -> tuple[ImportItem, ImportItem]:
    item = require_owned_arrival_item(item_id, username, store)
    duplicate = (
        store.get_item(item.duplicate_of_id)
        if item is not None and item.duplicate_of_id is not None
        else None
    )
    if (
        duplicate is None
        or duplicate.source_kind != INVENTORY_SOURCE
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No matching Vault inventory file is recorded",
        )
    if (
        is_theatre_category(item.proposed_category)
        and duplicate.owner_user_id != item.owner_user_id
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This item already exists in Theatre",
        )
    return item, duplicate


@router.post(
    "/items/{item_id}/duplicate/keep",
    response_model=VaultMasterItem,
)
def keep_incoming_duplicate(
    item_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
) -> VaultMasterItem:
    item, duplicate = require_inventory_duplicate(item_id, username, store)
    if item.state not in {"needs_review", "duplicate_remove_failed"}:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The duplicate has already been decided",
        )
    kept = store.record_duplicate_result(
        item.id,
        "duplicate_kept",
        username,
        f"Kept Arrival Hall copy matching {duplicate.source_path}",
    )
    if kept is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return to_api_item(kept)


@router.post(
    "/items/{item_id}/duplicate/remove",
    response_model=VaultMasterItem,
)
def remove_incoming_duplicate(
    item_id: UUID,
    username: AuthenticatedUsername,
    store: VaultMasterStoreDependency,
    incoming_path: Path = Depends(get_incoming_path),
    inventory_paths: tuple[Path, ...] = Depends(get_inventory_paths),
) -> VaultMasterItem:
    item, duplicate = require_inventory_duplicate(item_id, username, store)
    try:
        safely_remove_exact_duplicate(
            item,
            duplicate,
            incoming_path,
            inventory_paths,
        )
    except (OSError, ValueError) as error:
        store.record_duplicate_result(
            item.id,
            "duplicate_remove_failed",
            username,
            str(error),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error

    removed = store.record_duplicate_result(
        item.id,
        "duplicate_removed",
        username,
        f"Removed Arrival Hall copy matching {duplicate.source_path}",
    )
    if removed is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    return to_api_item(removed)
