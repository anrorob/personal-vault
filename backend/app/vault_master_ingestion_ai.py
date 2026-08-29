from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO
import json
import re
import mimetypes
import os
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

import psycopg
import pypdfium2 as pdfium
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError
from psycopg.rows import dict_row

from app.config import get_database_conninfo
from app.incoming import get_arrival_hall_path
from app.vault_master import (
    INCOMING_SOURCE,
    SCREENSHOT_ARCHIVE_SUBFOLDER,
    ImportItem,
    VaultMasterStore,
    has_hard_coded_screenshot_marker,
    sha256_file,
)
from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
from app.vault_master_semantic import SemanticSignal, assess_semantic_signals


INGESTION_TASK_VERSION = "semantic-intake-v5"
ROUTING_MODEL_VERSION = "intelligent-routing-v5"
ROUTING_MEMORY_VERSION = "routing-memory-v1"
AUTO_PILOT_ELIGIBILITY_SCORE = 80
MAX_SEMANTIC_PDF_PAGES = 3
MAX_SEMANTIC_PDF_RENDER_PIXELS = 16_000_000
MAX_SEMANTIC_PDF_EMBEDDED_TEXT_CHARS = 64_000
MAX_GALLERY_PDF_PREVIEW_PIXELS = 4_000_000
SUPPORTED_CONTENT_TYPES = {
    "personal_photo",
    "receipt",
    "financial_document",
    "general_document",
    "screenshot",
    "artwork",
    "publication_cover",
    "unknown",
}
SCREENSHOT_CONTENT_MARKERS = (
    "screenshot",
    "user interface",
    "computer screen",
    "phone screen",
    "google maps",
    "mobile app",
    "application interface",
    "status bar",
)


def render_gallery_pdf_preview(source: Path) -> bytes:
    """Return a bounded local JPEG rendering of a Gallery PDF's first page.

    The caller establishes access and validates the source path. Rendering is
    private and read-only: it never changes the permanent source file.
    """
    try:
        document = pdfium.PdfDocument(source)
    except Exception as error:
        raise ValueError("The PDF preview could not be opened") from error

    try:
        if len(document) < 1:
            raise ValueError("The PDF has no previewable pages")
        page = document[0]
        width, height = page.get_size()
        if width <= 0 or height <= 0:
            raise ValueError("The PDF page has invalid dimensions")
        scale = min(
            1.5,
            (MAX_GALLERY_PDF_PREVIEW_PIXELS / (width * height)) ** 0.5,
        )
        bitmap = page.render(scale=scale, rotation=0)
        try:
            image = bitmap.to_pil().convert("RGB")
            output = BytesIO()
            image.save(output, format="JPEG", quality=85, optimize=True)
            return output.getvalue()
        finally:
            bitmap.close()
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("The PDF preview could not be rendered") from error
    finally:
        document.close()


@dataclass(frozen=True)
class IngestionAiJob:
    id: UUID
    item_id: UUID
    requested_by: str
    owner_user_id: UUID | None
    status: str
    attempts: int
    error: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(frozen=True)
class IngestionAiEvidence:
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
    owner_user_id: UUID | None = None


@dataclass(frozen=True)
class IngestionAnalysisBatch:
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


@dataclass(frozen=True)
class RoutingMemoryRule:
    id: UUID
    owner_user_id: UUID | None
    requested_by: str
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


class IngestionAiStore(Protocol):
    def queue_analysis(self, item_id: UUID, requested_by: str, owner_user_id: UUID | None = None) -> IngestionAiJob: ...
    def list_jobs(self, item_id: UUID, owner_user_id: UUID | str) -> list[IngestionAiJob]: ...
    def list_user_jobs(self, username: str) -> list[IngestionAiJob]: ...
    def list_all_jobs(self) -> list[IngestionAiJob]: ...
    def list_evidence(
        self, item_id: UUID, owner_user_id: UUID | str
    ) -> list[IngestionAiEvidence]: ...
    def list_evidence_for_learning(self, item_id: UUID) -> list[IngestionAiEvidence]: ...
    def list_user_evidence(self, owner_user_id: UUID | str) -> list[IngestionAiEvidence]: ...
    def list_all_evidence(self) -> list[IngestionAiEvidence]: ...
    def claim_next_job(self) -> IngestionAiJob | None: ...
    def complete_job(
        self,
        job_id: UUID,
        content_type: str,
        caption: str,
        ocr_text: str,
        confidence: float,
        reasons: tuple[str, ...],
        processing_ms: int,
        assessment: "RoutingAssessment",
    ) -> IngestionAiEvidence: ...
    def fail_job(self, job_id: UUID, error: str) -> None: ...
    def create_analysis_batch(
        self, item_ids: tuple[UUID, ...], username: str, owner_user_id: UUID | None = None
    ) -> IngestionAnalysisBatch: ...
    def list_analysis_batches(
        self, username: str
    ) -> list[IngestionAnalysisBatch]: ...
    def set_analysis_batch_status(
        self, batch_id: UUID, username: str, status: str
    ) -> IngestionAnalysisBatch | None: ...
    def retry_analysis_batch(
        self, batch_id: UUID, username: str
    ) -> IngestionAnalysisBatch | None: ...
    def list_analysis_batch_item_ids(
        self, batch_id: UUID, username: str
    ) -> tuple[UUID, ...]: ...
    def record_review_batch(
        self, action: str, item_ids: tuple[UUID, ...], outcomes: dict[str, str], username: str
    ) -> UUID: ...
    def remember_decision(
        self, item: ImportItem, chosen_destination: str | None, action: str, username: str
    ) -> RoutingMemoryRule | None: ...
    def list_routing_rules(self, owner_user_id: UUID) -> list[RoutingMemoryRule]: ...
    def update_routing_rule(
        self, rule_id: UUID, owner_user_id: UUID, status: str, destination: str | None = None
    ) -> RoutingMemoryRule | None: ...
    def delete_routing_rule(self, rule_id: UUID, owner_user_id: UUID) -> bool: ...
    def apply_routing_memory(
        self, item: ImportItem, content_type: str, ocr_text: str, assessment: "RoutingAssessment"
    ) -> "RoutingAssessment": ...


SAFE_OCR_CONCEPTS = {
    "receipt": ("receipt", "subtotal", "vat", "amount paid"),
    "statement": ("bank statement", "account statement", "statement period"),
    "invoice": ("invoice",),
    "certificate": ("certificate",),
}


def routing_features(item: ImportItem, content_type: str, ocr_text: str = "") -> dict[str, str]:
    suffix = Path(item.filename).suffix.casefold().lstrip(".") or "none"
    parent = Path(item.relative_path).parent.name.casefold()
    parent_pattern = re.sub(r"[^a-z0-9]+", "-", parent).strip("-")[:40] or "root"
    folded = ocr_text.casefold()
    concept = next(
        (name for name, terms in SAFE_OCR_CONCEPTS.items() if any(term in folded for term in terms)),
        "none",
    )
    return {
        "content_type": content_type,
        "extension": suffix,
        "folder_pattern": parent_pattern,
        "ocr_concept": concept,
    }


def routing_signature(features: dict[str, str]) -> str:
    return "|".join(f"{key}={features[key]}" for key in sorted(features))


def routing_maturity(examples: int, contradictions: int) -> tuple[str, float]:
    consistency = examples / max(1, examples + contradictions * 2)
    if examples < 3:
        return "evidence", round(consistency * 0.4, 3)
    if contradictions:
        return "review", round(consistency * 0.65, 3)
    if examples < 10:
        return "suggestion", round(0.6 + min(examples - 3, 6) * 0.04, 3)
    return "established", min(0.95, round(0.84 + min(examples - 10, 11) * 0.01, 3))


def _classify(
    caption: str,
    ocr_text: str,
    *,
    hard_coded_screenshot: bool = False,
) -> tuple[str, float, tuple[str, ...]]:
    combined = f"{caption}\n{ocr_text}".casefold()
    caption_text = caption.casefold()
    reasons: list[str] = []

    financial = (
        "bank statement",
        "account statement",
        "statement period",
        "account number",
        "sort code",
        "opening balance",
        "closing balance",
    )
    receipt = (
        "receipt",
        "subtotal",
        "vat",
        "amount paid",
        "change due",
    )
    document = (
        "document",
        "letter",
        "certificate",
        "invoice",
        "contract",
        "application form",
        "health insurance card",
        "european health insurance card",
        "insurance card",
        "identity card",
        "id card",
        "membership card",
        "official card",
        "government-issued card",
    )
    artwork = ("illustration", "painting", "drawing", "artwork", "poster")
    publication_cover = (
        "cover of a book",
        "book cover",
        "front cover of",
        "back cover of",
        "cover features",
    )
    # Florence captions do not use one canonical word for an ordinary personal
    # photograph.  These are visual-caption concepts only: OCR is intentionally
    # excluded so text on a photograph cannot turn it into a document.  More
    # specific document, screenshot, artwork and publication evidence above
    # always takes precedence.
    direct_personal_photo = (
        "photograph",
        "photo of",
        "person",
        "people",
        "human",
        "individual",
        "subject",
        "man",
        "woman",
        "male",
        "female",
        "gentleman",
        "lady",
        "boy",
        "girl",
        "child",
        "children",
        "baby",
        "toddler",
        "teenager",
        "adult",
        "couple",
        "family",
        "friends",
        "friend",
        "group",
        "crowd",
        "team",
        "mother",
        "father",
        "parent",
        "son",
        "daughter",
        "brother",
        "sister",
        "grandparent",
        "bride",
        "groom",
        "landscape",
        "outdoor",
        "portrait",
        "selfie",
        "headshot",
        "close-up",
        "dog",
        "cat",
        "puppy",
        "kitten",
        "pet",
        "horse",
    )
    personal_photo_context = (
        "smiling",
        "posing",
        "standing",
        "sitting",
        "walking",
        "dancing",
        "hugging",
        "holding hands",
        "living room",
        "kitchen",
        "bedroom",
        "garden",
        "beach",
        "park",
        "countryside",
        "holiday",
        "vacation",
        "birthday",
        "wedding",
        "celebration",
        "party",
        "family gathering",
        "school event",
    )

    def matches(terms: tuple[str, ...], text: str = combined) -> list[str]:
        return [
            term
            for term in terms
            if re.search(rf"\b{re.escape(term)}\b", text)
        ]

    if hits := matches(financial):
        reasons.append(f"Financial statement indicators: {', '.join(hits[:3])}")
        return "financial_document", min(0.99, 0.88 + 0.03 * len(hits)), tuple(reasons)
    if hits := matches(receipt):
        reasons.append(f"Receipt indicators: {', '.join(hits[:3])}")
        return "receipt", min(0.97, 0.84 + 0.03 * len(hits)), tuple(reasons)
    if hits := matches(publication_cover):
        reasons.append(f"Publication-cover indicators: {', '.join(hits[:2])}")
        return "publication_cover", min(0.97, 0.88 + 0.03 * len(hits)), tuple(reasons)
    if hits := matches(document):
        reasons.append(f"Document indicators: {', '.join(hits[:3])}")
        return "general_document", min(0.94, 0.78 + 0.03 * len(hits)), tuple(reasons)
    screenshot_hits = matches(SCREENSHOT_CONTENT_MARKERS)
    if hits := matches(artwork):
        reasons.append(f"Artwork indicators: {', '.join(hits[:2])}")
        return "artwork", min(0.92, 0.78 + 0.04 * len(hits)), tuple(reasons)
    if direct_hits := matches(direct_personal_photo, caption_text):
        context_hits = matches(personal_photo_context, caption_text)
        reasons.append(
            "Personal-photograph indicators: "
            + ", ".join((*direct_hits[:3], *context_hits[:2]))
        )
        # A visible person, pet, portrait, or other direct personal-photo
        # subject is strong visual evidence.  Context can strengthen it but is
        # never enough by itself to make an image automatic-move eligible.
        base_confidence = 0.87 if {"portrait", "selfie", "headshot"} & set(direct_hits) else 0.82
        return "personal_photo", min(
            0.95,
            base_confidence + 0.02 * min(4, len(direct_hits) + len(context_hits)),
        ), tuple(reasons)
    if context_hits := matches(personal_photo_context, caption_text):
        reasons.append(f"Personal-photo context only: {', '.join(context_hits[:3])}")
        return "personal_photo", min(0.56, 0.50 + 0.02 * len(context_hits)), tuple(reasons)
    if hard_coded_screenshot or screenshot_hits:
        marker_reason = (
            "Screenshot capture context in filename or embedded metadata"
            if hard_coded_screenshot
            else f"Screenshot indicators: {', '.join(screenshot_hits[:2])}"
        )
        reasons.append(marker_reason)
        marker_count = max(1, len(screenshot_hits))
        return "screenshot", min(0.94, 0.82 + 0.04 * marker_count), tuple(reasons)
    if ocr_text.strip():
        reasons.append("OCR found text but no reliable document category")
        return "unknown", 0.45, tuple(reasons)
    reasons.append("No reliable visual or text category was detected")
    return "unknown", 0.25, tuple(reasons)


def _centralise_semantic_assessment(
    caption: str,
    ocr_text: str,
    *,
    hard_coded_screenshot: bool,
) -> tuple[str, float, tuple[str, ...], str | None, bool]:
    """Apply the Stage 1 contract before proposing a review-only destination."""
    content_type, confidence, classification_reasons = _classify(
        caption,
        ocr_text,
        hard_coded_screenshot=hard_coded_screenshot,
    )
    signals: list[SemanticSignal] = []
    if hard_coded_screenshot or content_type == "screenshot":
        signals.append(
            SemanticSignal(
                source="capture_context",
                capture_context="screenshot",
                detail="Screenshot capture context was detected locally.",
            )
        )
    semantic_type = {"publication_cover": "publication"}.get(
        content_type,
        content_type,
    )
    if semantic_type not in {"screenshot", "unknown"}:
        signals.append(
            SemanticSignal(
                source="local_semantic_analysis",
                content_type=semantic_type,  # type: ignore[arg-type]
                confidence=confidence,
                detail="; ".join(classification_reasons),
            )
        )
    assessment = assess_semantic_signals(tuple(signals))
    screenshot_fallback = (
        assessment.content_type == "unknown"
        and "screenshot" in assessment.capture_contexts
    )
    return (
        content_type,
        confidence,
        tuple(dict.fromkeys((*assessment.reasons, *classification_reasons))),
        assessment.recommended_destination,
        screenshot_fallback,
    )


@dataclass(frozen=True)
class RoutingAssessment:
    recommended_destination: str | None
    decision_score: int
    routing_band: str
    confidence_components: dict[str, float]
    conflicts: tuple[str, ...]
    automatic_disqualifiers: tuple[str, ...]
    decision_model_version: str = ROUTING_MODEL_VERSION


def assess_destination(
    item: ImportItem,
    content_type: str,
    classification_confidence: float,
    ocr_text: str,
) -> RoutingAssessment:
    screenshot_context = has_hard_coded_screenshot_marker(item.filename, item.metadata)
    semantic_type, semantic_confidence, _ = _classify("", ocr_text)
    # Specific semantic evidence can improve an earlier visual classification,
    # but a generic-document fallback must not downgrade an already specific
    # document classification (for example, a financial statement whose saved
    # OCR excerpt no longer contains the original banking keywords).
    if semantic_type in {
        "receipt",
        "financial_document",
        "publication_cover",
    } or (
        semantic_type == "general_document"
        and content_type
        not in {"receipt", "financial_document", "publication_cover"}
    ):
        content_type = semantic_type
        classification_confidence = max(
            classification_confidence,
            semantic_confidence,
        )
    destinations = {
        "personal_photo": "Gallery",
        "receipt": "Documents",
        "financial_document": "Ledger",
        "general_document": "Documents",
        "screenshot": "Archives",
        "artwork": "Archives",
        "publication_cover": "Library",
        "unknown": None,
    }
    recommended = destinations[content_type]
    proposal_confidence = {"low": 40.0, "medium": 65.0, "high": 85.0}.get(
        item.proposal_confidence or "", 25.0
    )
    document_like = content_type in {
        "receipt",
        "financial_document",
        "general_document",
    }
    ocr_confidence = (
        100.0
        if document_like and len(ocr_text.strip()) >= 15
        else 30.0
        if document_like
        else 80.0
        if content_type == "personal_photo" and len(ocr_text.strip()) < 40
        else 40.0
        if content_type == "personal_photo"
        else 50.0
    )
    agrees = recommended is not None and item.proposed_category == recommended
    conflicts: list[str] = []
    disqualifiers: list[str] = []
    if recommended is None:
        disqualifiers.append("No reliable destination was identified")
    if recommended and item.proposed_category and not agrees:
        conflicts.append(
            f"Existing proposal is {item.proposed_category}; content evidence suggests {recommended}"
        )
        disqualifiers.append("Destination evidence conflicts")
    if item.duplicate_of_id is not None:
        disqualifiers.append("An exact duplicate requires review")
    if document_like and len(ocr_text.strip()) < 15:
        disqualifiers.append("Document-like image has insufficient OCR evidence")
    if content_type == "publication_cover":
        disqualifiers.append("Publication candidates require Reading Room owner review")
    if content_type == "screenshot" or screenshot_context:
        disqualifiers.append("Screenshot capture context requires owner review")

    components = {
        "file_safety": 100.0,
        "destination_safety": 0.0 if item.duplicate_of_id else 100.0,
        "deterministic_rule": proposal_confidence,
        "visual_classification": round(classification_confidence * 100, 1),
        "ocr_document_type": ocr_confidence,
        "evidence_agreement": 100.0 if agrees else 20.0 if recommended else 0.0,
        "learned_routing": 0.0,
    }
    score = round(
        components["file_safety"] * 0.10
        + components["destination_safety"] * 0.15
        + components["deterministic_rule"] * 0.15
        + components["visual_classification"] * 0.25
        + components["ocr_document_type"] * 0.20
        + components["evidence_agreement"] * 0.15
    )
    hard_disqualified = bool(disqualifiers)
    routing_band = (
        "individual_review"
        if hard_disqualified or score < 70
        else "batch_review"
        if score < AUTO_PILOT_ELIGIBILITY_SCORE
        else "automatic_eligible"
    )
    if score < AUTO_PILOT_ELIGIBILITY_SCORE:
        disqualifiers.append(
            f"Decision score is below the {AUTO_PILOT_ELIGIBILITY_SCORE}-point auto-pilot threshold"
        )
    return RoutingAssessment(
        recommended,
        score,
        routing_band,
        components,
        tuple(conflicts),
        tuple(dict.fromkeys(disqualifiers)),
    )


def with_learned_rule(
    assessment: RoutingAssessment, rule: RoutingMemoryRule | None
) -> RoutingAssessment:
    # Immature owner learning is display-only evidence.  It must never weaken
    # the deterministic 80-point routing decision or block auto-pilot.
    if rule is None or rule.status != "enabled" or rule.maturity != "established":
        return assessment
    components = dict(assessment.confidence_components)
    components["learned_routing"] = round(rule.confidence * 100, 1)
    conflicts = list(assessment.conflicts)
    disqualifiers = list(assessment.automatic_disqualifiers)
    recommended = assessment.recommended_destination
    if recommended and recommended != rule.destination:
        conflicts.append(
            f"Learned routing suggests {rule.destination}; content evidence suggests {recommended}"
        )
        disqualifiers.append("Learned routing conflicts with current evidence")
    else:
        recommended = rule.destination
    score = min(100, round(assessment.decision_score * 0.85 + components["learned_routing"] * 0.15))
    if rule.maturity != "established":
        disqualifiers.append("Learned routing has not reached established maturity")
    band = (
        "individual_review"
        if conflicts or any("duplicate" in value.casefold() for value in disqualifiers)
        else "automatic_eligible"
        if score >= AUTO_PILOT_ELIGIBILITY_SCORE and rule.maturity == "established"
        else "batch_review"
    )
    return RoutingAssessment(
        recommended,
        score,
        band,
        components,
        tuple(dict.fromkeys(conflicts)),
        tuple(dict.fromkeys(disqualifiers)),
        f"{ROUTING_MODEL_VERSION}+{ROUTING_MEMORY_VERSION}",
    )


class MemoryIngestionAiStore:
    def __init__(self) -> None:
        self.jobs: dict[UUID, IngestionAiJob] = {}
        self.evidence: dict[UUID, IngestionAiEvidence] = {}
        self.analysis_batches: dict[UUID, dict[str, object]] = {}
        self.analysis_batch_items: dict[UUID, set[UUID]] = {}
        self.review_batches: dict[UUID, dict[str, object]] = {}
        self.routing_rules: dict[UUID, RoutingMemoryRule] = {}
        self.routing_examples: list[dict[str, object]] = []
        self._last_evidence_created_at: datetime | None = None

    def queue_analysis(self, item_id: UUID, requested_by: str, owner_user_id: UUID | None = None) -> IngestionAiJob:
        existing = next(
            (
                job
                for job in self.jobs.values()
                if job.item_id == item_id and job.status in {"queued", "processing"}
            ),
            None,
        )
        if existing:
            return existing
        job = IngestionAiJob(
            uuid4(), item_id, requested_by, owner_user_id, "queued", 0, None,
            datetime.now(timezone.utc), None, None,
        )
        self.jobs[job.id] = job
        return job

    def list_jobs(self, item_id: UUID, owner_user_id: UUID | str) -> list[IngestionAiJob]:
        return sorted(
            (j for j in self.jobs.values() if j.item_id == item_id and (j.owner_user_id == owner_user_id if isinstance(owner_user_id, UUID) else j.requested_by == owner_user_id)),
            key=lambda j: j.created_at,
            reverse=True,
        )

    def list_user_jobs(self, username: str) -> list[IngestionAiJob]:
        return sorted(
            (j for j in self.jobs.values() if j.requested_by == username),
            key=lambda j: j.created_at,
            reverse=True,
        )

    def list_all_jobs(self) -> list[IngestionAiJob]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    def list_evidence(self, item_id: UUID, owner_user_id: UUID | str) -> list[IngestionAiEvidence]:
        return sorted(
            (e for e in self.evidence.values() if e.item_id == item_id and (e.owner_user_id == owner_user_id if isinstance(owner_user_id, UUID) else e.requested_by == owner_user_id)),
            key=lambda e: e.created_at,
            reverse=True,
        )

    def list_evidence_for_learning(self, item_id: UUID) -> list[IngestionAiEvidence]:
        return sorted(
            (e for e in self.evidence.values() if e.item_id == item_id),
            key=lambda e: e.created_at,
            reverse=True,
        )

    def list_user_evidence(self, owner_user_id: UUID | str) -> list[IngestionAiEvidence]:
        return sorted(
            (e for e in self.evidence.values() if (e.owner_user_id == owner_user_id if isinstance(owner_user_id, UUID) else e.requested_by == owner_user_id)),
            key=lambda e: e.created_at,
            reverse=True,
        )

    def list_all_evidence(self) -> list[IngestionAiEvidence]:
        return sorted(self.evidence.values(), key=lambda item: item.created_at, reverse=True)

    def claim_next_job(self) -> IngestionAiJob | None:
        paused_items = {
            item_id
            for batch_id, item_ids in self.analysis_batch_items.items()
            if self.analysis_batches[batch_id]["status"] == "paused"
            for item_id in item_ids
        }
        queued = sorted(
            (
                job for job in self.jobs.values()
                if job.status == "queued" and job.item_id not in paused_items
            ),
            key=lambda job: job.created_at,
        )
        if not queued:
            return None
        job = queued[0]
        claimed = IngestionAiJob(**{**job.__dict__, "status": "processing", "attempts": job.attempts + 1, "started_at": datetime.now(timezone.utc), "error": None})
        self.jobs[job.id] = claimed
        return claimed

    def complete_job(self, job_id: UUID, content_type: str, caption: str, ocr_text: str, confidence: float, reasons: tuple[str, ...], processing_ms: int, assessment: RoutingAssessment) -> IngestionAiEvidence:
        job = self.jobs[job_id]
        if job.status != "processing" or content_type not in SUPPORTED_CONTENT_TYPES:
            raise ValueError("Ingestion AI job is not processing")
        now = datetime.now(timezone.utc)
        if self._last_evidence_created_at is not None and now <= self._last_evidence_created_at:
            now = self._last_evidence_created_at + timedelta(microseconds=1)
        self._last_evidence_created_at = now
        evidence = IngestionAiEvidence(
            uuid4(), job.id, job.item_id, content_type, caption, ocr_text,
            confidence, reasons, AI_MODEL_ID, AI_MODEL_REVISION,
            INGESTION_TASK_VERSION, processing_ms,
            assessment.recommended_destination, assessment.decision_score,
            assessment.routing_band,
            assessment.confidence_components, assessment.conflicts,
            assessment.automatic_disqualifiers,
            assessment.decision_model_version, job.requested_by, now, job.owner_user_id,
        )
        self.evidence[evidence.id] = evidence
        self.jobs[job.id] = IngestionAiJob(**{**job.__dict__, "status": "completed", "completed_at": now})
        return evidence

    def fail_job(self, job_id: UUID, error: str) -> None:
        job = self.jobs[job_id]
        self.jobs[job.id] = IngestionAiJob(**{**job.__dict__, "status": "failed", "error": error[:2000], "completed_at": datetime.now(timezone.utc)})

    def create_analysis_batch(
        self, item_ids: tuple[UUID, ...], username: str, owner_user_id: UUID | None = None
    ) -> IngestionAnalysisBatch:
        unique_ids = tuple(dict.fromkeys(item_ids))
        if not unique_ids:
            raise ValueError("At least one staged image is required")
        now = datetime.now(timezone.utc)
        batch_id = uuid4()
        self.analysis_batches[batch_id] = {
            "requested_by": username,
            "status": "running",
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
        }
        self.analysis_batch_items[batch_id] = set(unique_ids)
        for item_id in unique_ids:
            self.queue_analysis(item_id, username, owner_user_id)
        return self._memory_batch(batch_id)

    def _memory_batch(self, batch_id: UUID) -> IngestionAnalysisBatch:
        row = self.analysis_batches[batch_id]
        item_ids = self.analysis_batch_items[batch_id]
        statuses = [
            next(
                (
                    job.status
                    for job in sorted(
                        self.jobs.values(), key=lambda job: job.created_at, reverse=True
                    )
                    if job.item_id == item_id
                    and job.requested_by == row["requested_by"]
                ),
                "queued",
            )
            for item_id in item_ids
        ]
        terminal = all(status in {"completed", "failed"} for status in statuses)
        status = str(row["status"])
        if terminal and status == "running":
            status = "completed" if "failed" not in statuses else "completed_with_failures"
            row["status"] = status
            row["completed_at"] = datetime.now(timezone.utc)
        return IngestionAnalysisBatch(
            batch_id,
            str(row["requested_by"]),
            status,
            len(item_ids),
            statuses.count("queued"),
            statuses.count("processing"),
            statuses.count("completed"),
            statuses.count("failed"),
            row["created_at"],  # type: ignore[arg-type]
            row["updated_at"],  # type: ignore[arg-type]
            row["completed_at"],  # type: ignore[arg-type]
        )

    def list_analysis_batches(self, username: str) -> list[IngestionAnalysisBatch]:
        return [
            self._memory_batch(batch_id)
            for batch_id, row in reversed(self.analysis_batches.items())
            if row["requested_by"] == username
        ]

    def set_analysis_batch_status(
        self, batch_id: UUID, username: str, status: str
    ) -> IngestionAnalysisBatch | None:
        row = self.analysis_batches.get(batch_id)
        if row is None or row["requested_by"] != username or status not in {"paused", "running"}:
            return None
        row["status"] = status
        row["updated_at"] = datetime.now(timezone.utc)
        return self._memory_batch(batch_id)

    def retry_analysis_batch(
        self, batch_id: UUID, username: str
    ) -> IngestionAnalysisBatch | None:
        row = self.analysis_batches.get(batch_id)
        if row is None or row["requested_by"] != username:
            return None
        for item_id in self.analysis_batch_items[batch_id]:
            failed = [
                job for job in self.jobs.values()
                if job.item_id == item_id and job.requested_by == username and job.status == "failed"
            ]
            for job in failed:
                self.jobs[job.id] = IngestionAiJob(
                    **{**job.__dict__, "status": "queued", "error": None,
                       "started_at": None, "completed_at": None}
                )
        row["status"] = "running"
        row["completed_at"] = None
        row["updated_at"] = datetime.now(timezone.utc)
        return self._memory_batch(batch_id)

    def list_analysis_batch_item_ids(
        self, batch_id: UUID, username: str
    ) -> tuple[UUID, ...]:
        row = self.analysis_batches.get(batch_id)
        if row is None or row["requested_by"] != username:
            return ()
        return tuple(sorted(self.analysis_batch_items[batch_id], key=str))

    def record_review_batch(
        self, action: str, item_ids: tuple[UUID, ...], outcomes: dict[str, str], username: str
    ) -> UUID:
        batch_id = uuid4()
        self.review_batches[batch_id] = {
            "action": action, "item_ids": item_ids, "outcomes": outcomes,
            "requested_by": username, "created_at": datetime.now(timezone.utc),
        }
        return batch_id

    def remember_decision(self, item: ImportItem, chosen_destination: str | None, action: str, username: str) -> RoutingMemoryRule | None:
        evidence = self.list_evidence_for_learning(item.id)
        if not evidence or not chosen_destination:
            return None
        latest = evidence[0]
        features = routing_features(item, latest.content_type, latest.ocr_text)
        signature = routing_signature(features)
        existing = next((rule for rule in self.routing_rules.values() if rule.owner_user_id == item.owner_user_id and rule.feature_signature == signature), None)
        now = datetime.now(timezone.utc)
        if existing is None:
            examples, contradictions, destination, rule_id, created = 1, 0, chosen_destination, uuid4(), now
        else:
            contradiction = chosen_destination != existing.destination or action == "rejected"
            examples = existing.example_count + (0 if contradiction else 1)
            contradictions = existing.contradiction_count + (1 if contradiction else 0)
            destination, rule_id, created = existing.destination, existing.id, existing.created_at
        maturity, confidence = routing_maturity(examples, contradictions)
        rule = RoutingMemoryRule(rule_id, item.owner_user_id, username, signature, features, destination, examples, contradictions, confidence, maturity, existing.status if existing else "enabled", now, created, now)
        self.routing_rules[rule.id] = rule
        self.routing_examples.append({"item_id": item.id, "requested_by": username, "action": action, "destination": chosen_destination, "features": features})
        return rule

    def list_routing_rules(self, owner_user_id: UUID) -> list[RoutingMemoryRule]:
        return sorted((rule for rule in self.routing_rules.values() if rule.owner_user_id == owner_user_id), key=lambda rule: rule.updated_at, reverse=True)

    def update_routing_rule(self, rule_id: UUID, owner_user_id: UUID, status: str, destination: str | None = None) -> RoutingMemoryRule | None:
        rule = self.routing_rules.get(rule_id)
        if rule is None or rule.owner_user_id != owner_user_id or status not in {"enabled", "disabled", "reset", "edit"}:
            return None
        if status == "edit" and not destination:
            return None
        examples, contradictions = (0, 0) if status == "reset" else (rule.example_count, rule.contradiction_count)
        maturity, confidence = routing_maturity(examples, contradictions)
        updated = RoutingMemoryRule(**{**rule.__dict__, "destination": destination if status == "edit" else rule.destination, "example_count": examples, "contradiction_count": contradictions, "maturity": maturity, "confidence": confidence, "status": rule.status if status == "edit" else "enabled" if status == "reset" else status, "updated_at": datetime.now(timezone.utc)})
        self.routing_rules[rule_id] = updated
        return updated

    def delete_routing_rule(self, rule_id: UUID, owner_user_id: UUID) -> bool:
        rule = self.routing_rules.get(rule_id)
        if rule is None or rule.owner_user_id != owner_user_id:
            return False
        del self.routing_rules[rule_id]
        return True

    def apply_routing_memory(self, item: ImportItem, content_type: str, ocr_text: str, assessment: RoutingAssessment) -> RoutingAssessment:
        signature = routing_signature(routing_features(item, content_type, ocr_text))
        rule = next((value for value in self.routing_rules.values() if value.owner_user_id == item.owner_user_id and value.feature_signature == signature), None)
        return with_learned_rule(assessment, rule)


def _job_from_row(row: dict[str, object]) -> IngestionAiJob:
    return IngestionAiJob(**row)  # type: ignore[arg-type]


def _evidence_from_row(row: dict[str, object]) -> IngestionAiEvidence:
    row["reasons"] = tuple(row["reasons"])  # type: ignore[arg-type]
    row["conflicts"] = tuple(row["conflicts"])  # type: ignore[arg-type]
    row["automatic_disqualifiers"] = tuple(row["automatic_disqualifiers"])  # type: ignore[arg-type]
    return IngestionAiEvidence(**row)  # type: ignore[arg-type]


def _analysis_batch_from_row(row: dict[str, object]) -> IngestionAnalysisBatch:
    return IngestionAnalysisBatch(**row)  # type: ignore[arg-type]


def _routing_rule_from_row(row: dict[str, object]) -> RoutingMemoryRule:
    row["features"] = dict(row["features"])  # type: ignore[arg-type]
    return RoutingMemoryRule(**row)  # type: ignore[arg-type]


class PostgresIngestionAiStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self):
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_ingestion_ai_jobs (
                    id UUID PRIMARY KEY,
                    item_id UUID NOT NULL REFERENCES vault_master_items(id) ON DELETE CASCADE,
                    requested_by TEXT NOT NULL,
                    owner_user_id UUID REFERENCES auth_accounts(user_id),
                    status TEXT NOT NULL CHECK (status IN ('queued','processing','completed','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                )
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS vault_ingestion_ai_jobs_active_item_idx
                ON vault_ingestion_ai_jobs (item_id)
                WHERE status IN ('queued','processing')
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_ingestion_ai_evidence (
                    id UUID PRIMARY KEY,
                    job_id UUID NOT NULL UNIQUE REFERENCES vault_ingestion_ai_jobs(id) ON DELETE CASCADE,
                    item_id UUID NOT NULL REFERENCES vault_master_items(id) ON DELETE CASCADE,
                    content_type TEXT NOT NULL CHECK (content_type IN ('personal_photo','receipt','financial_document','general_document','screenshot','artwork','publication_cover','unknown')),
                    caption TEXT NOT NULL,
                    ocr_text TEXT NOT NULL,
                    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
                    reasons JSONB NOT NULL,
                    model_id TEXT NOT NULL,
                    model_revision TEXT NOT NULL,
                    task_version TEXT NOT NULL,
                    processing_ms INTEGER NOT NULL CHECK (processing_ms >= 0),
                    recommended_destination TEXT,
                    decision_score INTEGER NOT NULL DEFAULT 0 CHECK (decision_score >= 0 AND decision_score <= 100),
                    routing_band TEXT NOT NULL DEFAULT 'individual_review' CHECK (routing_band IN ('automatic_eligible','batch_review','individual_review')),
                    confidence_components JSONB NOT NULL DEFAULT '{}'::jsonb,
                    conflicts JSONB NOT NULL DEFAULT '[]'::jsonb,
                    automatic_disqualifiers JSONB NOT NULL DEFAULT '[]'::jsonb,
                    decision_model_version TEXT NOT NULL DEFAULT 'intelligent-routing-v4',
                    requested_by TEXT NOT NULL,
                    owner_user_id UUID REFERENCES auth_accounts(user_id),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("ALTER TABLE vault_ingestion_ai_evidence ADD COLUMN IF NOT EXISTS recommended_destination TEXT")
            cursor.execute("ALTER TABLE vault_ingestion_ai_evidence ADD COLUMN IF NOT EXISTS decision_score INTEGER NOT NULL DEFAULT 0")
            cursor.execute("ALTER TABLE vault_ingestion_ai_evidence ADD COLUMN IF NOT EXISTS routing_band TEXT NOT NULL DEFAULT 'individual_review'")
            cursor.execute("ALTER TABLE vault_ingestion_ai_evidence ADD COLUMN IF NOT EXISTS confidence_components JSONB NOT NULL DEFAULT '{}'::jsonb")
            cursor.execute("ALTER TABLE vault_ingestion_ai_evidence ADD COLUMN IF NOT EXISTS conflicts JSONB NOT NULL DEFAULT '[]'::jsonb")
            cursor.execute("ALTER TABLE vault_ingestion_ai_evidence ADD COLUMN IF NOT EXISTS automatic_disqualifiers JSONB NOT NULL DEFAULT '[]'::jsonb")
            cursor.execute("ALTER TABLE vault_ingestion_ai_evidence ADD COLUMN IF NOT EXISTS decision_model_version TEXT NOT NULL DEFAULT 'intelligent-routing-v4'")
            cursor.execute("ALTER TABLE vault_ingestion_ai_jobs ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("ALTER TABLE vault_ingestion_ai_evidence ADD COLUMN IF NOT EXISTS owner_user_id UUID REFERENCES auth_accounts(user_id)")
            cursor.execute("""UPDATE vault_ingestion_ai_jobs AS jobs SET owner_user_id=items.owner_user_id
                FROM vault_master_items AS items WHERE jobs.owner_user_id IS NULL AND items.id=jobs.item_id
                AND items.owner_user_id IS NOT NULL""")
            cursor.execute("""UPDATE vault_ingestion_ai_evidence AS evidence SET owner_user_id=jobs.owner_user_id
                FROM vault_ingestion_ai_jobs AS jobs WHERE evidence.owner_user_id IS NULL AND jobs.id=evidence.job_id
                AND jobs.owner_user_id IS NOT NULL""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_ingestion_ai_jobs_owner_status_idx ON vault_ingestion_ai_jobs(owner_user_id,status,created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_ingestion_ai_evidence_owner_item_idx ON vault_ingestion_ai_evidence(owner_user_id,item_id,created_at)")
            cursor.execute(
                "ALTER TABLE vault_ingestion_ai_evidence DROP CONSTRAINT IF EXISTS vault_ingestion_ai_evidence_content_type_check"
            )
            cursor.execute(
                "ALTER TABLE vault_ingestion_ai_evidence ADD CONSTRAINT vault_ingestion_ai_evidence_content_type_check CHECK (content_type IN ('personal_photo','receipt','financial_document','general_document','screenshot','artwork','publication_cover','unknown'))"
            )
            cursor.execute(
                "ALTER TABLE vault_ingestion_ai_evidence ALTER COLUMN decision_model_version SET DEFAULT 'intelligent-routing-v4'"
            )
            cursor.execute("""
                UPDATE vault_ingestion_ai_jobs SET status='queued', started_at=NULL,
                    error='Recovered after worker restart' WHERE status='processing'
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_ingestion_analysis_batches (
                    id UUID PRIMARY KEY,
                    requested_by TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('running','paused','completed','completed_with_failures')),
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    completed_at TIMESTAMPTZ
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_ingestion_analysis_batch_items (
                    batch_id UUID NOT NULL REFERENCES vault_ingestion_analysis_batches(id) ON DELETE CASCADE,
                    item_id UUID NOT NULL REFERENCES vault_master_items(id) ON DELETE CASCADE,
                    job_id UUID NOT NULL REFERENCES vault_ingestion_ai_jobs(id) ON DELETE CASCADE,
                    PRIMARY KEY (batch_id, item_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_ingestion_review_batches (
                    id UUID PRIMARY KEY,
                    requested_by TEXT NOT NULL,
                    action TEXT NOT NULL CHECK (action IN ('approve','reject','move')),
                    item_ids JSONB NOT NULL,
                    outcomes JSONB NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_routing_memory_rules (
                    id UUID PRIMARY KEY,
                    owner_user_id UUID REFERENCES auth_accounts(user_id),
                    requested_by TEXT NOT NULL,
                    feature_signature TEXT NOT NULL,
                    features JSONB NOT NULL,
                    destination TEXT NOT NULL,
                    example_count INTEGER NOT NULL DEFAULT 0,
                    contradiction_count INTEGER NOT NULL DEFAULT 0,
                    confidence DOUBLE PRECISION NOT NULL DEFAULT 0,
                    maturity TEXT NOT NULL CHECK (maturity IN ('evidence','suggestion','review','established')),
                    status TEXT NOT NULL DEFAULT 'enabled' CHECK (status IN ('enabled','disabled')),
                    last_used_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (owner_user_id, feature_signature)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vault_routing_memory_examples (
                    id UUID PRIMARY KEY,
                    rule_id UUID NOT NULL REFERENCES vault_routing_memory_rules(id) ON DELETE CASCADE,
                    item_id UUID REFERENCES vault_master_items(id) ON DELETE SET NULL,
                    owner_user_id UUID REFERENCES auth_accounts(user_id),
                    requested_by TEXT NOT NULL,
                    action TEXT NOT NULL,
                    chosen_destination TEXT NOT NULL,
                    features JSONB NOT NULL,
                    active BOOLEAN NOT NULL DEFAULT true,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Stable ownership is additive.  Legacy text identities are retained
            # as audit fields only; rows that cannot be proven owner-consistent
            # are disabled rather than allowed to influence routing.
            cursor.execute("""
                ALTER TABLE vault_routing_memory_rules
                ADD COLUMN IF NOT EXISTS owner_user_id UUID
                    REFERENCES auth_accounts(user_id)
            """)
            cursor.execute("""
                ALTER TABLE vault_routing_memory_examples
                ADD COLUMN IF NOT EXISTS owner_user_id UUID
                    REFERENCES auth_accounts(user_id),
                ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT true
            """)
            cursor.execute("""
                ALTER TABLE vault_routing_memory_rules
                DROP CONSTRAINT IF EXISTS vault_routing_memory_rules_requested_by_feature_signature_key
            """)
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS vault_routing_memory_rules_owner_signature_idx
                ON vault_routing_memory_rules(owner_user_id, feature_signature)
                WHERE owner_user_id IS NOT NULL
            """)
            cursor.execute("""
                UPDATE vault_routing_memory_rules AS rule
                SET owner_user_id = account.user_id
                FROM auth_accounts AS account
                WHERE rule.owner_user_id IS NULL
                  AND account.username = rule.requested_by
            """)
            cursor.execute("""
                UPDATE vault_routing_memory_examples AS example
                SET owner_user_id = CASE
                        WHEN item.owner_user_id = rule.owner_user_id THEN item.owner_user_id
                        ELSE NULL
                    END,
                    active = (item.owner_user_id = rule.owner_user_id)
                FROM vault_master_items AS item,
                     vault_routing_memory_rules AS rule
                WHERE (example.owner_user_id IS NULL OR example.active)
                  AND rule.id = example.rule_id
                  AND item.id = example.item_id
            """)
            cursor.execute("""
                UPDATE vault_routing_memory_rules AS rule
                SET example_count = COALESCE(counts.examples, 0),
                    contradiction_count = COALESCE(counts.contradictions, 0),
                    confidence = CASE
                        WHEN COALESCE(counts.examples, 0) < 3
                            THEN ROUND((COALESCE(counts.examples, 0)::numeric / GREATEST(1, COALESCE(counts.examples, 0) + COALESCE(counts.contradictions, 0) * 2)) * 0.4, 3)
                        WHEN COALESCE(counts.contradictions, 0) > 0
                            THEN ROUND((COALESCE(counts.examples, 0)::numeric / GREATEST(1, COALESCE(counts.examples, 0) + COALESCE(counts.contradictions, 0) * 2)) * 0.65, 3)
                        WHEN COALESCE(counts.examples, 0) < 10
                            THEN 0.6 + LEAST(COALESCE(counts.examples, 0) - 3, 6) * 0.04
                        ELSE LEAST(0.95, 0.84 + LEAST(COALESCE(counts.examples, 0) - 10, 11) * 0.01)
                    END,
                    maturity = CASE
                        WHEN COALESCE(counts.examples, 0) < 3 THEN 'evidence'
                        WHEN COALESCE(counts.contradictions, 0) > 0 THEN 'review'
                        WHEN COALESCE(counts.examples, 0) < 10 THEN 'suggestion'
                        ELSE 'established'
                    END,
                    status = CASE WHEN rule.owner_user_id IS NULL THEN 'disabled' ELSE rule.status END,
                    updated_at = CURRENT_TIMESTAMP
                FROM (
                    SELECT rule_id,
                        count(*) FILTER (WHERE active AND action <> 'rejected' AND chosen_destination = rule.destination) AS examples,
                        count(*) FILTER (WHERE active AND (action = 'rejected' OR chosen_destination <> rule.destination)) AS contradictions
                    FROM vault_routing_memory_examples AS example
                    JOIN vault_routing_memory_rules AS rule ON rule.id = example.rule_id
                    GROUP BY rule_id
                ) AS counts
                WHERE rule.id = counts.rule_id
            """)
            cursor.execute("""
                UPDATE vault_routing_memory_rules AS rule
                SET example_count = 0,
                    contradiction_count = 0,
                    confidence = 0,
                    maturity = 'evidence',
                    status = 'disabled',
                    updated_at = CURRENT_TIMESTAMP
                WHERE rule.owner_user_id IS NULL
                   OR NOT EXISTS (
                       SELECT 1
                       FROM vault_routing_memory_examples AS example
                       WHERE example.rule_id = rule.id AND example.active
                   )
            """)

    def queue_analysis(self, item_id: UUID, requested_by: str, owner_user_id: UUID | None = None) -> IngestionAiJob:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_ingestion_ai_jobs WHERE item_id=%s AND status IN ('queued','processing') ORDER BY created_at DESC LIMIT 1", (item_id,))
            row = cursor.fetchone()
            if row is None:
                cursor.execute("INSERT INTO vault_ingestion_ai_jobs (id,item_id,requested_by,owner_user_id,status) VALUES (%s,%s,%s,%s,'queued') ON CONFLICT (item_id) WHERE status IN ('queued','processing') DO NOTHING RETURNING *", (uuid4(), item_id, requested_by, owner_user_id))
                row = cursor.fetchone()
            if row is None:
                cursor.execute("SELECT * FROM vault_ingestion_ai_jobs WHERE item_id=%s AND status IN ('queued','processing') ORDER BY created_at DESC LIMIT 1", (item_id,))
                row = cursor.fetchone()
            assert row is not None
            return _job_from_row(row)

    def list_jobs(self, item_id: UUID, owner_user_id: UUID | str) -> list[IngestionAiJob]:
        with self._connect() as connection, connection.cursor() as cursor:
            if isinstance(owner_user_id, UUID):
                cursor.execute("SELECT * FROM vault_ingestion_ai_jobs WHERE item_id=%s AND owner_user_id=%s ORDER BY created_at DESC", (item_id, owner_user_id))
            else:
                cursor.execute("SELECT * FROM vault_ingestion_ai_jobs WHERE item_id=%s AND requested_by=%s ORDER BY created_at DESC", (item_id, owner_user_id))
            return [_job_from_row(row) for row in cursor.fetchall()]

    def list_user_jobs(self, username: str) -> list[IngestionAiJob]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT * FROM vault_ingestion_ai_jobs WHERE requested_by=%s ORDER BY created_at DESC",
                (username,),
            )
            return [_job_from_row(row) for row in cursor.fetchall()]

    def list_all_jobs(self) -> list[IngestionAiJob]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_ingestion_ai_jobs ORDER BY created_at DESC")
            return [_job_from_row(row) for row in cursor.fetchall()]

    def list_evidence(self, item_id: UUID, owner_user_id: UUID | str) -> list[IngestionAiEvidence]:
        with self._connect() as connection, connection.cursor() as cursor:
            if isinstance(owner_user_id, UUID):
                cursor.execute("SELECT * FROM vault_ingestion_ai_evidence WHERE item_id=%s AND owner_user_id=%s ORDER BY created_at DESC", (item_id, owner_user_id))
            else:
                cursor.execute("SELECT * FROM vault_ingestion_ai_evidence WHERE item_id=%s AND requested_by=%s ORDER BY created_at DESC", (item_id, owner_user_id))
            return [_evidence_from_row(row) for row in cursor.fetchall()]

    def list_evidence_for_learning(self, item_id: UUID) -> list[IngestionAiEvidence]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_ingestion_ai_evidence WHERE item_id=%s ORDER BY created_at DESC", (item_id,))
            return [_evidence_from_row(row) for row in cursor.fetchall()]

    def list_user_evidence(self, owner_user_id: UUID | str) -> list[IngestionAiEvidence]:
        with self._connect() as connection, connection.cursor() as cursor:
            if isinstance(owner_user_id, UUID):
                cursor.execute("SELECT * FROM vault_ingestion_ai_evidence WHERE owner_user_id=%s ORDER BY created_at DESC", (owner_user_id,))
            else:
                cursor.execute("SELECT * FROM vault_ingestion_ai_evidence WHERE requested_by=%s ORDER BY created_at DESC", (owner_user_id,))
            return [_evidence_from_row(row) for row in cursor.fetchall()]

    def list_all_evidence(self) -> list[IngestionAiEvidence]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_ingestion_ai_evidence ORDER BY created_at DESC")
            return [_evidence_from_row(row) for row in cursor.fetchall()]

    def claim_next_job(self) -> IngestionAiJob | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT jobs.* FROM vault_ingestion_ai_jobs AS jobs
                WHERE jobs.status='queued'
                  AND NOT EXISTS (
                    SELECT 1 FROM vault_ingestion_analysis_batch_items AS links
                    JOIN vault_ingestion_analysis_batches AS batches ON batches.id=links.batch_id
                    WHERE links.job_id=jobs.id AND batches.status='paused'
                  )
                ORDER BY jobs.created_at FOR UPDATE OF jobs SKIP LOCKED LIMIT 1
            """)
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute("UPDATE vault_ingestion_ai_jobs SET status='processing', attempts=attempts+1, started_at=CURRENT_TIMESTAMP, error=NULL WHERE id=%s RETURNING *", (row["id"],))
            claimed = cursor.fetchone()
            assert claimed is not None
            return _job_from_row(claimed)

    def complete_job(self, job_id: UUID, content_type: str, caption: str, ocr_text: str, confidence: float, reasons: tuple[str, ...], processing_ms: int, assessment: RoutingAssessment) -> IngestionAiEvidence:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_ingestion_ai_jobs WHERE id=%s FOR UPDATE", (job_id,))
            job = cursor.fetchone()
            if job is None or job["status"] != "processing" or content_type not in SUPPORTED_CONTENT_TYPES:
                raise ValueError("Ingestion AI job is not processing")
            cursor.execute("""INSERT INTO vault_ingestion_ai_evidence (id,job_id,item_id,content_type,caption,ocr_text,confidence,reasons,model_id,model_revision,task_version,processing_ms,recommended_destination,decision_score,routing_band,confidence_components,conflicts,automatic_disqualifiers,decision_model_version,requested_by,owner_user_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""", (uuid4(), job_id, job["item_id"], content_type, caption, ocr_text, confidence, json.dumps(reasons), AI_MODEL_ID, AI_MODEL_REVISION, INGESTION_TASK_VERSION, processing_ms, assessment.recommended_destination, assessment.decision_score, assessment.routing_band, json.dumps(assessment.confidence_components), json.dumps(assessment.conflicts), json.dumps(assessment.automatic_disqualifiers), assessment.decision_model_version, job["requested_by"], job["owner_user_id"]))
            evidence = cursor.fetchone()
            cursor.execute("UPDATE vault_ingestion_ai_jobs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=%s", (job_id,))
            assert evidence is not None
            return _evidence_from_row(evidence)

    def fail_job(self, job_id: UUID, error: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_ingestion_ai_jobs SET status='failed', error=%s, completed_at=CURRENT_TIMESTAMP WHERE id=%s", (error[:2000], job_id))

    def create_analysis_batch(
        self, item_ids: tuple[UUID, ...], username: str, owner_user_id: UUID | None = None
    ) -> IngestionAnalysisBatch:
        unique_ids = tuple(dict.fromkeys(item_ids))
        if not unique_ids:
            raise ValueError("At least one staged image is required")
        batch_id = uuid4()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO vault_ingestion_analysis_batches (id,requested_by,status) VALUES (%s,%s,'running')",
                (batch_id, username),
            )
            for item_id in unique_ids:
                job = self.queue_analysis(item_id, username, owner_user_id)
                cursor.execute(
                    "INSERT INTO vault_ingestion_analysis_batch_items (batch_id,item_id,job_id) VALUES (%s,%s,%s)",
                    (batch_id, item_id, job.id),
                )
        batches = self.list_analysis_batches(username)
        return next(batch for batch in batches if batch.id == batch_id)

    def list_analysis_batches(self, username: str) -> list[IngestionAnalysisBatch]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                WITH progress AS (
                    SELECT batches.id, batches.requested_by,
                      CASE WHEN batches.status='running' AND bool_and(jobs.status IN ('completed','failed'))
                        THEN CASE WHEN bool_or(jobs.status='failed') THEN 'completed_with_failures' ELSE 'completed' END
                        ELSE batches.status END AS status,
                      count(*)::int AS total_items,
                      count(*) FILTER (WHERE jobs.status='queued')::int AS queued_items,
                      count(*) FILTER (WHERE jobs.status='processing')::int AS processing_items,
                      count(*) FILTER (WHERE jobs.status='completed')::int AS completed_items,
                      count(*) FILTER (WHERE jobs.status='failed')::int AS failed_items,
                      batches.created_at, batches.updated_at,
                      CASE WHEN bool_and(jobs.status IN ('completed','failed'))
                        THEN COALESCE(batches.completed_at, CURRENT_TIMESTAMP) ELSE batches.completed_at END AS completed_at
                    FROM vault_ingestion_analysis_batches AS batches
                    JOIN vault_ingestion_analysis_batch_items AS links ON links.batch_id=batches.id
                    JOIN vault_ingestion_ai_jobs AS jobs ON jobs.id=links.job_id
                    WHERE batches.requested_by=%s
                    GROUP BY batches.id
                )
                SELECT * FROM progress ORDER BY created_at DESC
            """, (username,))
            rows = cursor.fetchall()
            for row in rows:
                if row["status"] in {"completed", "completed_with_failures"}:
                    cursor.execute(
                        "UPDATE vault_ingestion_analysis_batches SET status=%s,completed_at=COALESCE(completed_at,%s),updated_at=CURRENT_TIMESTAMP WHERE id=%s",
                        (row["status"], row["completed_at"], row["id"]),
                    )
            return [_analysis_batch_from_row(row) for row in rows]

    def set_analysis_batch_status(
        self, batch_id: UUID, username: str, status: str
    ) -> IngestionAnalysisBatch | None:
        if status not in {"paused", "running"}:
            return None
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE vault_ingestion_analysis_batches SET status=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND requested_by=%s AND status IN ('running','paused') RETURNING id",
                (status, batch_id, username),
            )
            if cursor.fetchone() is None:
                return None
        return next((batch for batch in self.list_analysis_batches(username) if batch.id == batch_id), None)

    def retry_analysis_batch(
        self, batch_id: UUID, username: str
    ) -> IngestionAnalysisBatch | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                UPDATE vault_ingestion_ai_jobs AS jobs
                SET status='queued',error=NULL,started_at=NULL,completed_at=NULL
                FROM vault_ingestion_analysis_batch_items AS links
                JOIN vault_ingestion_analysis_batches AS batches ON batches.id=links.batch_id
                WHERE jobs.id=links.job_id AND links.batch_id=%s
                  AND batches.requested_by=%s AND jobs.status='failed'
            """, (batch_id, username))
            cursor.execute(
                "UPDATE vault_ingestion_analysis_batches SET status='running',completed_at=NULL,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND requested_by=%s RETURNING id",
                (batch_id, username),
            )
            if cursor.fetchone() is None:
                return None
        return next((batch for batch in self.list_analysis_batches(username) if batch.id == batch_id), None)

    def list_analysis_batch_item_ids(
        self, batch_id: UUID, username: str
    ) -> tuple[UUID, ...]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT links.item_id
                FROM vault_ingestion_analysis_batch_items AS links
                JOIN vault_ingestion_analysis_batches AS batches ON batches.id=links.batch_id
                WHERE links.batch_id=%s AND batches.requested_by=%s
                ORDER BY links.item_id
            """, (batch_id, username))
            return tuple(UUID(str(row["item_id"])) for row in cursor.fetchall())

    def record_review_batch(
        self, action: str, item_ids: tuple[UUID, ...], outcomes: dict[str, str], username: str
    ) -> UUID:
        if action not in {"approve", "reject", "move"}:
            raise ValueError("Unsupported batch review action")
        batch_id = uuid4()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO vault_ingestion_review_batches (id,requested_by,action,item_ids,outcomes) VALUES (%s,%s,%s,%s,%s)",
                (batch_id, username, action, json.dumps([str(item_id) for item_id in item_ids]), json.dumps(outcomes)),
            )
        return batch_id

    def remember_decision(self, item: ImportItem, chosen_destination: str | None, action: str, username: str) -> RoutingMemoryRule | None:
        evidence = self.list_evidence_for_learning(item.id)
        if not evidence or not chosen_destination:
            return None
        latest = evidence[0]
        features = routing_features(item, latest.content_type, latest.ocr_text)
        signature = routing_signature(features)
        now = datetime.now(timezone.utc)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_routing_memory_rules WHERE owner_user_id=%s AND feature_signature=%s FOR UPDATE", (item.owner_user_id, signature))
            existing = cursor.fetchone()
            if existing is None:
                rule_id, destination, examples, contradictions, status = uuid4(), chosen_destination, 1, 0, "enabled"
                maturity, confidence = routing_maturity(examples, contradictions)
                cursor.execute("INSERT INTO vault_routing_memory_rules (id,owner_user_id,requested_by,feature_signature,features,destination,example_count,contradiction_count,confidence,maturity,status,last_used_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", (rule_id, item.owner_user_id, username, signature, json.dumps(features), destination, examples, contradictions, confidence, maturity, status, now))
            else:
                rule_id, destination, status = existing["id"], existing["destination"], existing["status"]
                contradiction = chosen_destination != destination or action == "rejected"
                examples = int(existing["example_count"]) + (0 if contradiction else 1)
                contradictions = int(existing["contradiction_count"]) + (1 if contradiction else 0)
                maturity, confidence = routing_maturity(examples, contradictions)
                cursor.execute("UPDATE vault_routing_memory_rules SET example_count=%s,contradiction_count=%s,confidence=%s,maturity=%s,last_used_at=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s", (examples, contradictions, confidence, maturity, now, rule_id))
            cursor.execute("INSERT INTO vault_routing_memory_examples (id,rule_id,item_id,owner_user_id,requested_by,action,chosen_destination,features) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)", (uuid4(), rule_id, item.id, item.owner_user_id, username, action, chosen_destination, json.dumps(features)))
            cursor.execute("SELECT * FROM vault_routing_memory_rules WHERE id=%s", (rule_id,))
            row = cursor.fetchone()
            assert row is not None
            return _routing_rule_from_row(row)

    def list_routing_rules(self, owner_user_id: UUID) -> list[RoutingMemoryRule]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_routing_memory_rules WHERE owner_user_id=%s ORDER BY updated_at DESC", (owner_user_id,))
            return [_routing_rule_from_row(row) for row in cursor.fetchall()]

    def update_routing_rule(self, rule_id: UUID, owner_user_id: UUID, status: str, destination: str | None = None) -> RoutingMemoryRule | None:
        if status not in {"enabled", "disabled", "reset", "edit"}:
            return None
        if status == "edit" and not destination:
            return None
        with self._connect() as connection, connection.cursor() as cursor:
            if status == "reset":
                maturity, confidence = routing_maturity(0, 0)
                cursor.execute("UPDATE vault_routing_memory_rules SET example_count=0,contradiction_count=0,confidence=%s,maturity=%s,status='enabled',updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s RETURNING *", (confidence, maturity, rule_id, owner_user_id))
                row = cursor.fetchone()
                cursor.execute("DELETE FROM vault_routing_memory_examples WHERE rule_id=%s AND owner_user_id=%s", (rule_id, owner_user_id))
            elif status == "edit":
                cursor.execute("UPDATE vault_routing_memory_rules SET destination=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s RETURNING *", (destination, rule_id, owner_user_id))
                row = cursor.fetchone()
            else:
                cursor.execute("UPDATE vault_routing_memory_rules SET status=%s,updated_at=CURRENT_TIMESTAMP WHERE id=%s AND owner_user_id=%s RETURNING *", (status, rule_id, owner_user_id))
                row = cursor.fetchone()
            return _routing_rule_from_row(row) if row else None

    def delete_routing_rule(self, rule_id: UUID, owner_user_id: UUID) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM vault_routing_memory_rules WHERE id=%s AND owner_user_id=%s RETURNING id", (rule_id, owner_user_id))
            return cursor.fetchone() is not None

    def apply_routing_memory(self, item: ImportItem, content_type: str, ocr_text: str, assessment: RoutingAssessment) -> RoutingAssessment:
        signature = routing_signature(routing_features(item, content_type, ocr_text))
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT * FROM vault_routing_memory_rules WHERE owner_user_id=%s AND feature_signature=%s AND status='enabled'", (item.owner_user_id, signature))
            row = cursor.fetchone()
        return with_learned_rule(assessment, _routing_rule_from_row(row) if row else None)


@lru_cache
def get_ingestion_ai_store() -> IngestionAiStore:
    return PostgresIngestionAiStore(get_database_conninfo())


def _staged_semantic_source(item: ImportItem) -> Path:
    supported = item.mime_type.startswith("image/") or item.mime_type == "application/pdf"
    if item.source_kind != INCOMING_SOURCE or not supported:
        raise ValueError("Only staged Arrival Hall images and PDFs can be analysed")
    root = get_arrival_hall_path().resolve(strict=True)
    source = root.joinpath(*item.relative_path.split("/")).resolve(strict=True)
    if not source.is_relative_to(root) or not source.is_file():
        raise ValueError("Staged semantic source is unavailable")
    if source.stat().st_size != item.size_bytes or sha256_file(source) != item.sha256:
        raise ValueError("Staged semantic source no longer matches its analysed record")
    return source


def request_florence_analysis(source: Path) -> tuple[str, str, int]:
    endpoint = os.getenv("PV_FLORENCE_URL", "http://pv-florence2:8080/ocr").rsplit("/", 1)[0] + "/analyse"
    request = Request(endpoint, data=source.read_bytes(), method="POST", headers={"Content-Type": mimetypes.guess_type(source.name)[0] or "application/octet-stream"})
    try:
        with urlopen(request, timeout=180) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Local Florence-2 analysis service is unavailable") from error
    caption, text = payload.get("caption"), payload.get("text")
    if not isinstance(caption, str) or not isinstance(text, str):
        raise RuntimeError("Local Florence-2 service returned invalid analysis")
    processing_ms = payload.get("processing_ms")
    return caption.strip(), text.strip(), int(processing_ms) if isinstance(processing_ms, int) else 0


def _extract_pdf_embedded_text(source: Path, page_count: int) -> tuple[str, ...]:
    """Return bounded, page-labelled text without making the PDF a text authority."""
    try:
        reader = PdfReader(source)
        if reader.is_encrypted:
            return ()
        pages: list[str] = []
        remaining = MAX_SEMANTIC_PDF_EMBEDDED_TEXT_CHARS
        for page_index in range(min(len(reader.pages), page_count)):
            if remaining <= 0:
                break
            text = (reader.pages[page_index].extract_text() or "").strip()
            if text:
                pages.append(f"Embedded page {page_index + 1}: {text[:remaining]}")
                remaining -= len(text)
        return tuple(pages)
    except (FileNotDecryptedError, OSError, PdfReadError, ValueError):
        return ()


def _analyse_semantic_source(source: Path) -> tuple[str, str, int]:
    """Analyse an image, or at most the first three locally rendered PDF pages."""
    if source.suffix.casefold() != ".pdf":
        return request_florence_analysis(source)

    captions: list[str] = []
    texts: list[str] = []
    processing_ms = 0
    try:
        document = pdfium.PdfDocument(source)
    except Exception as error:
        raise ValueError("Staged PDF is invalid or unreadable") from error
    try:
        page_count = min(len(document), MAX_SEMANTIC_PDF_PAGES)
        if page_count < 1:
            raise ValueError("Staged PDF has no analysable pages")
        texts.extend(_extract_pdf_embedded_text(source, page_count))
        with TemporaryDirectory(prefix="pv-semantic-pdf-") as temporary:
            temporary_root = Path(temporary)
            for page_index in range(page_count):
                rendered = temporary_root / f"page-{page_index + 1}.png"
                page = document[page_index]
                try:
                    width, height = page.get_size()
                    if width * height * 1.5 * 1.5 > MAX_SEMANTIC_PDF_RENDER_PIXELS:
                        raise ValueError(
                            "Staged PDF page dimensions are outside the semantic-analysis bound"
                        )
                    bitmap = page.render(scale=1.5, rotation=0)
                    try:
                        bitmap.to_pil().save(rendered, format="PNG")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
                caption, text, elapsed = request_florence_analysis(rendered)
                captions.append(f"Page {page_index + 1}: {caption}")
                if text:
                    texts.append(f"Page {page_index + 1}: {text}")
                processing_ms += elapsed
    finally:
        document.close()
    return "\n".join(captions), "\n".join(texts), processing_ms


def process_next_ingestion_ai_job(store: IngestionAiStore, vault_store: VaultMasterStore) -> UUID | None:
    job = store.claim_next_job()
    if job is None:
        return None
    try:
        item = vault_store.get_item(job.item_id)
        if item is None:
            raise ValueError("Staged AI job item no longer exists")
        source = _staged_semantic_source(item)
        caption, ocr_text, processing_ms = _analyse_semantic_source(source)
        hard_coded_screenshot = has_hard_coded_screenshot_marker(
            item.filename,
            item.metadata,
        )
        content_type, confidence, reasons, recommended, screenshot_fallback = (
            _centralise_semantic_assessment(
                caption,
                ocr_text,
                hard_coded_screenshot=hard_coded_screenshot,
            )
        )
        if recommended:
            updated = vault_store.apply_ai_proposal(
                item.id,
                recommended,
                "Local image evidence suggests this destination.",
                (
                    SCREENSHOT_ARCHIVE_SUBFOLDER
                    if screenshot_fallback
                    else None
                ),
            )
            if updated is not None:
                item = updated
        assessment = assess_destination(item, content_type, confidence, ocr_text)
        assessment = store.apply_routing_memory(item, content_type, ocr_text, assessment)
        learned_confidence = assessment.confidence_components.get("learned_routing", 0)
        if learned_confidence:
            reasons = (
                f"Learned routing suggests {assessment.recommended_destination} at {learned_confidence:.0f}% confidence",
                *reasons,
            )
        store.complete_job(
            job.id, content_type, caption, ocr_text, confidence, reasons,
            processing_ms, assessment,
        )
    except Exception as error:
        store.fail_job(job.id, str(error))
    return job.id


def queue_pending_ingestion_image_analysis(
    store: IngestionAiStore,
    vault_store: VaultMasterStore,
    username: str,
    limit: int = 500,
) -> int:
    """Queue current, unanalysed Arrival Hall images and PDFs without retry storms."""
    queued = 0
    for item in vault_store.list_items():
        if queued >= limit:
            break
        if (
            item.source_kind != INCOMING_SOURCE
            or item.state not in {"inventoried", "needs_review"}
            or not (
                item.mime_type.startswith("image/")
                or item.mime_type == "application/pdf"
            )
            or item.duplicate_of_id is not None
        ):
            continue
        if item.owner_user_id is None:
            continue
        jobs = store.list_jobs(item.id, item.owner_user_id)
        if any(job.status in {"queued", "processing"} for job in jobs):
            continue
        evidence = store.list_evidence(item.id, item.owner_user_id)
        if evidence and (
            evidence[0].model_id == AI_MODEL_ID
            and evidence[0].model_revision == AI_MODEL_REVISION
            and evidence[0].task_version == INGESTION_TASK_VERSION
        ):
            continue
        if jobs and jobs[0].status == "failed":
            continue
        # Requester remains audit context only.  Routing-memory selection uses
        # the immutable owner UUID when the job is processed.
        store.queue_analysis(item.id, username, item.owner_user_id)
        queued += 1
    return queued
