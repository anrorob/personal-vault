from app.vault_master_semantic import (
    SEMANTIC_ASSESSMENT_VERSION,
    SemanticSignal,
    assessment_requires_reanalysis,
    assess_semantic_signals,
)


def test_screenshot_is_capture_context_not_an_absolute_destination() -> None:
    assessment = assess_semantic_signals(
        (
            SemanticSignal(
                source="embedded_metadata",
                capture_context="screenshot",
                detail="The file was captured as a screenshot.",
            ),
            SemanticSignal(
                source="florence_ocr",
                content_type="financial_document",
                confidence=0.96,
                detail="Bank statement and closing-balance evidence.",
            ),
        )
    )

    assert assessment.content_type == "financial_document"
    assert assessment.recommended_destination == "Ledger"
    assert assessment.capture_contexts == ("screenshot",)
    assert assessment.action == "individual_review"


def test_photo_pdf_evidence_can_lead_to_gallery_independent_of_container() -> None:
    assessment = assess_semantic_signals(
        (
            SemanticSignal(
                source="pdf_page_visual_analysis",
                content_type="personal_photo",
                confidence=0.93,
                detail="Rendered PDF pages are personal photographs.",
            ),
            SemanticSignal(
                source="pdf_metadata",
                detail="The source container is a PDF.",
            ),
        )
    )

    assert assessment.content_type == "personal_photo"
    assert assessment.recommended_destination == "Gallery"


def test_publication_semantics_enter_specialist_review() -> None:
    assessment = assess_semantic_signals(
        (
            SemanticSignal(
                source="florence_caption",
                content_type="publication",
                confidence=0.91,
                detail="Book cover with title and author.",
            ),
        )
    )

    assert assessment.recommended_destination == "Library"
    assert assessment.specialist_workflow == "reading_room"
    assert assessment.action == "specialist_review"


def test_screenshot_falls_back_to_archives_only_without_semantic_content() -> None:
    assessment = assess_semantic_signals(
        (
            SemanticSignal(
                source="embedded_metadata",
                capture_context="screenshot",
            ),
        )
    )

    assert assessment.content_type == "unknown"
    assert assessment.recommended_destination == "Archives"


def test_reanalysis_tracks_the_complete_decision_contract_version() -> None:
    assert assessment_requires_reanalysis("intelligent-routing-v3")
    assert not assessment_requires_reanalysis(SEMANTIC_ASSESSMENT_VERSION)
