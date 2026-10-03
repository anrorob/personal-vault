"""Vault Master's versioned, class-first Arrival Hall scoring contract.

The vocabulary and confidence calculations mirror the existing pre-routing
Florence classifier.  This module retains every supported match so an earlier
keyword match cannot conceal a plausible competing semantic class.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Mapping
from uuid import UUID, NAMESPACE_URL, uuid5

from app.vault_master import ImportItem, ScannedFile, create_deterministic_proposal, has_hard_coded_screenshot_marker
from app.vault_master_ingestion_ai import _classify, _primary_capture_context, _structured_document_ocr, SCENE_PHOTO_TERMS


ROUTING_SCORE_VERSION = "vm-routing-score-v2"
SEMANTIC_MAX_POINTS = 90
# File structure corroborates content semantics; it cannot make a weak semantic
# classification meet the automatic threshold by itself.
DETERMINISTIC_POINTS = {"low": 3, "medium": 7, "high": 10}
MINIMUM_MARGIN = 15
SCREEN_AS_SCENE_OBJECT = ("computer screen", "phone screen")
CLASS_DESTINATIONS = {
    "personal_photo": "Gallery",
    "receipt": "Documents",
    "financial_document": "Documents",
    "general_document": "Documents",
    "screenshot": "Archives",
    "artwork": "Archives",
}


@dataclass(frozen=True)
class ClassScore:
    content_type: str
    semantic_confidence: float
    semantic_points: float
    deterministic_confidence: str | None
    deterministic_points: int
    total: float
    supporting_evidence: tuple[str, ...]
    conflicting_evidence: tuple[str, ...]


@dataclass(frozen=True)
class RoutingDecision:
    id: UUID
    item_id: UUID
    owner_user_id: UUID
    source_sha256: str
    version: str
    evidence_id: UUID | None
    analyser: dict[str, str]
    candidates: tuple[ClassScore, ...]
    winning_class: str | None
    runner_up_class: str | None
    winning_margin: float | None
    destination: str | None
    score: float | None
    threshold: int | None
    policy_snapshot: dict[str, str | int] | None
    gates: dict[str, bool]
    automatic_eligible: bool
    reason: str | None
    created_at: datetime
    supersedes_id: UUID | None = None


def _hits(text: str, terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(term for term in terms if re.search(rf"\b{re.escape(term)}\b", text))


def semantic_candidates(caption: str, ocr_text: str, *, screenshot_context: bool = False) -> dict[str, tuple[float, tuple[str, ...]]]:
    """Expose the existing classifier's individual matches without its early returns."""
    combined = f"{caption}\n{ocr_text}".casefold()
    visual = caption.casefold()
    primary_context = _primary_capture_context(caption)
    candidates: dict[str, tuple[float, tuple[str, ...]]] = {}

    def provenance(hits: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(("florence_caption:" if re.search(rf"\b{re.escape(term)}\b", visual)
                      else "florence_ocr:") + term for term in hits)

    financial = _hits(combined, ("bank statement", "account statement", "statement period", "account number", "sort code", "opening balance", "closing balance"))
    receipt = _hits(combined, ("receipt", "subtotal", "vat", "amount paid", "change due"))
    document = _hits(combined, ("document", "letter", "certificate", "invoice", "contract", "application form", "health insurance card", "european health insurance card", "insurance card", "identity card", "id card", "membership card", "official card", "government-issued card", "printed page", "paperwork", "form", "ticket"))
    artwork = _hits(combined, ("illustration", "painting", "drawing", "artwork", "poster"))
    publication = _hits(combined, ("cover of a book", "book cover", "front cover of", "back cover of", "cover features"))
    direct_photo = _hits(visual, ("photograph", "photo of", "person", "people", "human", "individual", "subject", "man", "woman", "male", "female", "gentleman", "lady", "boy", "girl", "child", "children", "baby", "toddler", "teenager", "adult", "couple", "family", "friends", "friend", "group", "crowd", "team", "mother", "father", "parent", "son", "daughter", "brother", "sister", "grandparent", "bride", "groom", "landscape", "outdoor", "portrait", "selfie", "headshot", "close-up", "dog", "cat", "puppy", "kitten", "pet", "horse") + SCENE_PHOTO_TERMS)
    photo_context = _hits(visual, ("smiling", "posing", "standing", "sitting", "walking", "dancing", "hugging", "holding hands", "living room", "kitchen", "bedroom", "garden", "beach", "park", "countryside", "holiday", "vacation", "birthday", "wedding", "celebration", "party", "family gathering", "school event"))
    from app.vault_master_ingestion_ai import SCREENSHOT_CONTENT_MARKERS
    primary_description = visual.split(".", 1)[0]
    screenshot = _hits(visual, SCREENSHOT_CONTENT_MARKERS)
    # A later mention of a screen as an object in a clearly described scene is
    # not evidence that the image itself is a screen capture. Explicit UI or
    # screenshot framing and hard capture metadata remain competing evidence.
    if (primary_context == "scene" and not screenshot_context
            and screenshot
            and set(screenshot) <= set(SCREEN_AS_SCENE_OBJECT)
            and (not _hits(primary_description, SCREEN_AS_SCENE_OBJECT)
                 or _hits(primary_description, ("desk", "room", "office", "physical", "photograph")))):
        screenshot = ()

    def supported_document_hits(hits: tuple[str, ...], terms: tuple[str, ...], content_type: str) -> tuple[str, ...]:
        if primary_context == "document":
            return hits
        if (primary_context in {"scene", "screen_reference"}
                and _structured_document_ocr(ocr_text, content_type)):
            return hits
        return ()

    financial = supported_document_hits(financial, ("bank statement", "account statement", "statement period", "account number", "sort code", "opening balance", "closing balance"), "financial_document")
    receipt = supported_document_hits(receipt, ("receipt", "subtotal", "vat", "amount paid", "change due"), "receipt")
    document = supported_document_hits(document, ("document", "letter", "certificate", "invoice", "contract", "application form", "health insurance card", "european health insurance card", "insurance card", "identity card", "id card", "membership card", "official card", "government-issued card", "printed page", "paperwork", "form", "ticket"), "general_document")
    if primary_context in {"scene", "screen_reference"} and not _hits(primary_description, ("illustration", "painting", "drawing", "artwork")):
        artwork = ()

    if financial:
        candidates["financial_document"] = (min(0.99, 0.88 + 0.03 * len(financial)), provenance(financial))
    if receipt:
        candidates["receipt"] = (min(0.97, 0.84 + 0.03 * len(receipt)), provenance(receipt))
    if publication and primary_context not in {"scene", "screen_reference"}:
        candidates["publication_cover"] = (min(0.97, 0.88 + 0.03 * len(publication)), provenance(publication))
    if document:
        confidence = min(0.94, 0.78 + 0.03 * len(document))
        candidates["general_document"] = (max(0.92, confidence) if primary_context == "document" else confidence, provenance(document))
    if artwork:
        candidates["artwork"] = (min(0.92, 0.78 + 0.04 * len(artwork)), provenance(artwork))
    if direct_photo and primary_context != "document" and primary_context != "screenshot":
        base = 0.87 if {"portrait", "selfie", "headshot"} & set(direct_photo) else 0.82
        confidence = min(0.95, base + 0.02 * min(4, len(direct_photo) + len(photo_context)))
        if primary_context == "scene":
            confidence = max(confidence, 0.92)
        support = provenance((*direct_photo, *photo_context))
        if primary_context == "scene":
            support = (*support, "primary_context:real_world_scene")
        candidates["personal_photo"] = (confidence, support)
    elif photo_context and primary_context not in {"document", "screenshot"}:
        candidates["personal_photo"] = (min(0.56, 0.50 + 0.02 * len(photo_context)), provenance(photo_context))
    if primary_context == "scene" and "personal_photo" not in candidates:
        candidates["personal_photo"] = (0.92, ("primary_context:real_world_scene",))
    if primary_context == "screenshot" or screenshot or screenshot_context or primary_context == "screen_reference":
        confidence = (0.94 if primary_context == "screenshot" else
                      0.86 if primary_context == "screen_reference" else
                      min(0.94, 0.82 + 0.04 * max(1, len(screenshot))))
        evidence = (("primary_context:native_screenshot",) if primary_context == "screenshot" else
                    ("primary_context:photographed_screen_reference",) if primary_context == "screen_reference" else
                    provenance(screenshot) if screenshot else ("capture_context:screenshot",))
        candidates["screenshot"] = (confidence, evidence)
    if not candidates:
        classification, confidence, reasons = _classify(caption, ocr_text, hard_coded_screenshot=screenshot_context)
        candidates[classification] = (confidence, reasons)
    return candidates


def score_classes(item: ImportItem, candidates: Mapping[str, tuple[float, tuple[str, ...]]]) -> tuple[ClassScore, ...]:
    """Score classes; safety, policy and owner learning never contribute points."""
    scored = []
    # Recompute from immutable scan facts: AI/user proposal changes can replace
    # proposed_category while leaving its old confidence label in place.
    scanned = ScannedFile(item.source_path, item.relative_path, item.filename,
                          item.size_bytes, item.mime_type, item.modified_at,
                          item.sha256, item.metadata, item.owner_username,
                          item.owner_user_id)
    proposed, _, deterministic_reason, deterministic_level = create_deterministic_proposal(scanned)
    for content_type, (confidence, support) in candidates.items():
        if not 0 <= confidence <= 1:
            raise ValueError("Semantic confidence is outside the routing contract")
        destination = CLASS_DESTINATIONS.get(content_type)
        level: str | None = None
        deterministic_support: str | None = None
        if destination is not None and destination == proposed:
            level = deterministic_level if deterministic_level in DETERMINISTIC_POINTS else None
            deterministic_support = "deterministic_proposal:" + deterministic_reason if level else None
        elif (item.mime_type.startswith("image/")
              and content_type in {"receipt", "financial_document", "general_document"}):
            # Image structure is weakly compatible with a photographed document.
            # Camera metadata itself supplies no document points and its Gallery
            # proposal must not override the semantic class.
            level = "low"
            deterministic_support = "image_structure:photographed_document_compatible"
        deterministic = DETERMINISTIC_POINTS.get(level or "", 0)
        semantic = round(confidence * SEMANTIC_MAX_POINTS, 2)
        scored.append(ClassScore(content_type, confidence, semantic, level, deterministic,
                                 round(semantic + deterministic, 2),
                                 (*support, *((deterministic_support,) if deterministic_support else ())), ()))
    ordered = sorted(scored, key=lambda value: (-value.total, value.content_type))
    return tuple(replace(value, conflicting_evidence=tuple(
        other.content_type for other in ordered if other.content_type != value.content_type
    )) for value in ordered)


def score_staged_item(item: ImportItem, caption: str, ocr_text: str) -> tuple[ClassScore, ...]:
    return score_classes(item, semantic_candidates(
        caption, ocr_text,
        screenshot_context=has_hard_coded_screenshot_marker(item.filename, item.metadata),
    ))


def build_routing_decision(item: ImportItem, evidence: object | None,
                           policy: object | None, gates: Mapping[str, bool],
                           *, now: datetime | None = None) -> RoutingDecision:
    """Construct a deterministic decision from retained, source-bound evidence."""
    from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
    from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION

    if item.owner_user_id is None:
        raise ValueError("Routing decisions require an immutable owner")
    valid_evidence = bool(
        evidence is not None
        and getattr(evidence, "owner_user_id", None) == item.owner_user_id
        and getattr(evidence, "item_id", None) == item.id
        and getattr(evidence, "source_sha256", None) == item.sha256
        and getattr(evidence, "model_id", None) == AI_MODEL_ID
        and getattr(evidence, "model_revision", None) == AI_MODEL_REVISION
        and getattr(evidence, "task_version", None) == INGESTION_TASK_VERSION
    )
    candidates = score_staged_item(item, evidence.caption, evidence.ocr_text) if valid_evidence else ()
    winner = candidates[0] if candidates else None
    runner_up = candidates[1] if len(candidates) > 1 else None
    margin = round(winner.total - (runner_up.total if runner_up else 0), 2) if winner else None
    destination = CLASS_DESTINATIONS.get(winner.content_type) if winner else None
    snapshot = None
    if policy is not None:
        snapshot = {
            "id": str(policy.id), "owner_user_id": str(policy.owner_user_id),
            "status": policy.status, "threshold": int(policy.threshold),
            "content_type": policy.content_type, "destination": policy.destination,
            "updated_at": policy.updated_at.isoformat(),
        }
    threshold = int(policy.threshold) if policy is not None else None
    effective_gates = dict(gates)
    if destination == "Gallery":
        # V2 semantics can clear 80 without format corroboration. Keep an
        # unrelated PDF or other non-image from gaining a Gallery route.
        effective_gates["gallery_image_mime_compatible"] = item.mime_type.startswith("image/")
    reason = next((name for name, passed in effective_gates.items() if not passed), None)
    if reason is None:
        if not valid_evidence:
            reason = "required_semantic_evidence_missing_or_stale"
        elif winner is None or destination is None:
            reason = "unsupported_or_unknown_semantic_class"
        elif margin is None or margin < MINIMUM_MARGIN:
            reason = "semantic_class_margin_below_15"
        elif policy is None:
            reason = "matching_owner_policy_absent"
        elif policy.owner_user_id != item.owner_user_id or policy.content_type != winner.content_type or policy.destination != destination:
            reason = "owner_policy_does_not_match_class_and_destination"
        elif policy.status != "enabled":
            reason = "owner_policy_not_enabled"
        elif winner.total < policy.threshold:
            reason = "class_score_below_owner_threshold"
    stable = {
        "item_id": str(item.id), "owner_user_id": str(item.owner_user_id),
        "sha256": item.sha256, "version": ROUTING_SCORE_VERSION,
        "evidence_id": str(evidence.id) if valid_evidence else None,
        "candidates": [candidate.__dict__ for candidate in candidates],
        "policy_snapshot": snapshot, "gates": effective_gates, "reason": reason,
    }
    fingerprint = hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()
    return RoutingDecision(
        uuid5(NAMESPACE_URL, fingerprint), item.id, item.owner_user_id,
        item.sha256, ROUTING_SCORE_VERSION,
        evidence.id if valid_evidence else None,
        {"model_id": evidence.model_id, "model_revision": evidence.model_revision,
         "task_version": evidence.task_version} if valid_evidence else {},
        candidates, winner.content_type if winner else None,
        runner_up.content_type if runner_up else None, margin, destination,
        winner.total if winner else None, threshold, snapshot, effective_gates,
        reason is None, reason, now or datetime.now(timezone.utc),
    )
