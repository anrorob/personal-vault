"""Content-led, versioned semantic routing contract for Vault Master.

This module is deliberately pure domain logic.  The existing image, PDF,
Reading Room and future media extractors feed it evidence; it does not open a
file, move data, call Florence or grant movement permission.
"""

from dataclasses import dataclass
from typing import Literal


SEMANTIC_ASSESSMENT_VERSION = "semantic-intake-v2"

SemanticContentType = Literal[
    "personal_photo",
    "receipt",
    "financial_document",
    "general_document",
    "publication",
    "artwork",
    "unknown",
]
SemanticAction = Literal[
    "individual_review",
    "specialist_review",
]
SpecialistWorkflow = Literal["reading_room"]


@dataclass(frozen=True)
class SemanticSignal:
    """One non-authoritative fact discovered by an extractor or classifier."""

    source: str
    content_type: SemanticContentType | None = None
    confidence: float = 0.0
    capture_context: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class SemanticAssessment:
    """The consolidated, content-led assessment for one logical intake item."""

    version: str
    content_type: SemanticContentType
    recommended_destination: str | None
    specialist_workflow: SpecialistWorkflow | None
    action: SemanticAction
    confidence: float
    capture_contexts: tuple[str, ...]
    reasons: tuple[str, ...]


_PRIORITY: dict[SemanticContentType, int] = {
    "financial_document": 70,
    "receipt": 60,
    "publication": 50,
    "general_document": 40,
    "personal_photo": 30,
    "artwork": 20,
    "unknown": 0,
}

_DESTINATIONS: dict[SemanticContentType, str | None] = {
    "financial_document": "Ledger",
    "receipt": "Documents",
    "publication": "Library",
    "general_document": "Documents",
    "personal_photo": "Gallery",
    "artwork": "Archives",
    "unknown": None,
}


def assessment_requires_reanalysis(
    stored_version: str | None,
    *,
    current_version: str = SEMANTIC_ASSESSMENT_VERSION,
) -> bool:
    """Return whether a stored assessment is semantically stale.

    This deliberately compares the complete decision-contract version rather
    than only the Florence model revision.  A changed precedence, grouping or
    routing rule must cause a controlled reassessment.
    """

    return stored_version != current_version


def assess_semantic_signals(
    signals: tuple[SemanticSignal, ...],
    *,
    version: str = SEMANTIC_ASSESSMENT_VERSION,
) -> SemanticAssessment:
    """Resolve semantic evidence without treating capture context as meaning.

    Higher-priority semantic classes win only when they have the strongest
    confidence within that class.  Screenshot is retained as a context label
    and only supplies the Archives/Screenshots fallback when no content is
    known.  Every outcome remains review-first at this foundation stage.
    """

    contexts = tuple(
        dict.fromkeys(
            signal.capture_context
            for signal in signals
            if signal.capture_context
        )
    )
    candidates = [
        signal
        for signal in signals
        if signal.content_type is not None and signal.confidence > 0
    ]
    if candidates:
        chosen = max(
            candidates,
            key=lambda signal: (
                _PRIORITY[signal.content_type or "unknown"],
                signal.confidence,
            ),
        )
        assert chosen.content_type is not None
        content_type = chosen.content_type
        destination = _DESTINATIONS[content_type]
        specialist: SpecialistWorkflow | None = (
            "reading_room" if content_type == "publication" else None
        )
        action: SemanticAction = (
            "specialist_review" if specialist else "individual_review"
        )
        reasons = tuple(
            dict.fromkeys(
                signal.detail
                for signal in candidates
                if signal.content_type == content_type and signal.detail
            )
        ) or ("Local semantic evidence identified the content.",)
        return SemanticAssessment(
            version,
            content_type,
            destination,
            specialist,
            action,
            chosen.confidence,
            contexts,
            reasons,
        )

    screenshot_only = "screenshot" in contexts
    return SemanticAssessment(
        version,
        "unknown",
        "Archives" if screenshot_only else None,
        None,
        "individual_review",
        0.0,
        contexts,
        (
            "Only screenshot capture context is known; Archives/Screenshots is a fallback."
            if screenshot_only
            else "No reliable local semantic content was identified."
        ,),
    )
