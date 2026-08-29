"""Final, routing-free Vault Master reconciliation for Gallery evidence."""
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable, TYPE_CHECKING
from uuid import UUID

GALLERY_RECONCILIATION_VERSION = "gallery-reconciliation-v1"
_HUMAN_PRESENCE = re.compile(
    r"\b(?:person|people|human|man|woman|boy|girl|child|children)\b",
    re.IGNORECASE,
)

if TYPE_CHECKING:
    from app.vault_master import CataloguedAsset, VaultMasterStore
    from app.vault_master_ingestion_ai import IngestionAiStore


@dataclass(frozen=True)
class FlorenceVisualEvidence:
    """A reference to the authoritative retained Florence caption."""

    id: UUID
    provider: str
    model_id: str
    model_revision: str
    task_version: str
    description: str
    created_at: datetime


@dataclass(frozen=True)
class GalleryReconciliation:
    asset_id: UUID
    terms: tuple[tuple[str, str], ...]
    people_ids: tuple[UUID, ...]
    unresolved_person_presence: bool
    evidence: dict[str, object]
    version: str = GALLERY_RECONCILIATION_VERSION


def latest_retained_florence_visual_evidence(
    vault_store: "VaultMasterStore",
    ingestion_ai_store: "IngestionAiStore | None",
    asset: "CataloguedAsset",
) -> FlorenceVisualEvidence | None:
    """Return the same latest retained visual description shown to the owner.

    Florence descriptions belong to the original moved Arrival Hall item, not a
    Gallery-private copy.  The SHA-256 plus published destination join mirrors
    the Gallery detail evidence endpoint and keeps the retained caption as the
    single source of truth.
    """
    if ingestion_ai_store is None:
        return None
    source_items = [
        item
        for item in vault_store.list_items()
        if item.source_kind == "incoming"
        and item.state == "moved"
        and (
            item.owner_user_id == asset.owner_user_id
            if asset.owner_user_id is not None
            else item.owner_username == asset.owner_username
        )
        and item.sha256 == asset.sha256
        and item.proposed_destination == asset.vault_path
    ]
    evidence = [
        candidate
        for item in source_items
        for candidate in ingestion_ai_store.list_evidence(
            item.id, asset.owner_user_id if asset.owner_user_id is not None else asset.owner_username
        )
        if candidate.caption.strip()
    ]
    latest = max(evidence, key=lambda candidate: candidate.created_at, default=None)
    if latest is None:
        return None
    return FlorenceVisualEvidence(
        id=latest.id,
        provider="florence",
        model_id=latest.model_id,
        model_revision=latest.model_revision,
        task_version=latest.task_version,
        description=latest.caption,
        created_at=latest.created_at,
    )


def florence_indicates_human_presence(description: str | None) -> bool:
    """Extract only human presence from a retained Florence description."""
    return bool(description and _HUMAN_PRESENCE.search(description))


def reconcile_gallery_evidence(
    asset_id: UUID,
    terms: Iterable[tuple[str, str]],
    people_ids: Iterable[UUID],
    raw_rampp_tags: object,
    face_count: int,
    body_count: int,
    florence_evidence: FlorenceVisualEvidence | None = None,
    florence_error: str | None = None,
) -> GalleryReconciliation:
    """Publish conservative metadata from independent specialist evidence.

    A YOLOX box is supporting evidence only; it cannot by itself manufacture
    person presence.  Florence/RAAM human observations and usable faces are
    direct presence evidence, while explicit People remain authoritative.
    """
    canonical_terms = tuple(dict.fromkeys(terms))
    people = tuple(dict.fromkeys(people_ids))
    tags = [tag.casefold() for tag in raw_rampp_tags] if isinstance(raw_rampp_tags, list) and all(isinstance(tag, str) for tag in raw_rampp_tags) else []
    florence_presence = florence_indicates_human_presence(
        florence_evidence.description if florence_evidence else None
    )
    raam_presence = any(
        tag in {"person", "people", "human", "man", "woman", "boy", "girl", "child", "children"}
        for tag in tags
    )
    direct_presence = bool(people) or face_count > 0 or florence_presence or raam_presence
    return GalleryReconciliation(
        asset_id=asset_id,
        terms=canonical_terms,
        people_ids=people,
        unresolved_person_presence=direct_presence and not people,
        evidence={
            "rampp_tags": tags,
            "florence": (
                {
                    "available": True,
                    "evidence_id": str(florence_evidence.id),
                    "provider": florence_evidence.provider,
                    "model_id": florence_evidence.model_id,
                    "model_revision": florence_evidence.model_revision,
                    "task_version": florence_evidence.task_version,
                    "created_at": florence_evidence.created_at.isoformat(),
                    "human_presence": florence_presence,
                }
                if florence_evidence
                else {
                    "available": False,
                    "human_presence": False,
                    "error": florence_error,
                }
            ),
            "face_count": face_count,
            "yolox_body_count": body_count,
            "person_presence": direct_presence,
            "yolox_supporting_only": body_count > 0 and not direct_presence,
        },
    )
