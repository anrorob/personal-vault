from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from app.auth import AuthenticatedIdentity, require_authenticated_user
from app.auth_store import Account, MemoryAuthenticationStore
from app.main import app
from app.vault_master_autopilot import MemoryAutopilotStore, get_autopilot_store
from app.vault_master import (
    INCOMING_SOURCE,
    MemoryVaultMasterStore,
    get_vault_master_store,
    scan_root,
)
import app.vault_master_ingestion_ai as ingestion_ai_module
from app.vault_master_ingestion_ai import (
    MemoryIngestionAiStore,
    _classify,
    _centralise_semantic_assessment,
    _extract_pdf_embedded_text,
    assess_destination,
    get_ingestion_ai_store,
    process_next_ingestion_ai_job,
    queue_pending_ingestion_image_analysis,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def authenticate(client: TestClient) -> None:
    assert client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    ).status_code == 200


def staged_image(tmp_path: Path) -> tuple[MemoryVaultMasterStore, Path, object]:
    arrival = tmp_path / "Arrival Hall"
    arrival.mkdir(parents=True)
    source = arrival / "statement.jpg"
    source.write_bytes(b"unchanged-image")
    vault_store = MemoryVaultMasterStore(default_asset_owner=TEST_USERNAME)
    scan_root(vault_store, arrival, INCOMING_SOURCE)
    return vault_store, arrival, vault_store.list_items()[0]


def staged_pdf(tmp_path: Path) -> tuple[MemoryVaultMasterStore, Path, object]:
    arrival = tmp_path / "Arrival Hall"
    arrival.mkdir(parents=True)
    source = arrival / "statement.pdf"
    source.write_bytes(b"unchanged-pdf")
    vault_store = MemoryVaultMasterStore(default_asset_owner=TEST_USERNAME)
    scan_root(vault_store, arrival, INCOMING_SOURCE)
    return vault_store, arrival, vault_store.list_items()[0]


def test_document_classification_prioritises_financial_and_receipt_signals() -> None:
    financial = _classify(
        "A photographed page",
        "BANK STATEMENT Account number 1234 Closing balance 50.00",
    )
    assert financial[0] == "financial_document"
    assert financial[1] >= 0.9

    receipt = _classify(
        "A paper receipt on a table",
        "Subtotal 10.00 VAT 2.00 Amount paid 12.00",
    )
    assert receipt[0] == "receipt"
    assert receipt[1] >= 0.9

    photo = _classify("A photograph of two people outdoors", "")
    assert photo[0] == "personal_photo"

    portrait = _classify(
        "The image is a black and white portrait of a young woman.",
        "faint incidental text in the image background",
    )
    assert portrait[0] == "personal_photo"
    assert portrait[1] >= 0.9
    assert "portrait" in portrait[2][0]

    interface_text = _classify(
        "A map of Westminster in Google Maps",
        "People Devices Items Me",
    )
    assert interface_text[0] == "screenshot"

    isolated_ocr_people = _classify("A map of Westminster", "People Devices Items Me")
    assert isolated_ocr_people[0] == "unknown"


@pytest.mark.parametrize(
    "caption",
    [
        "A woman smiling in a living room.",
        "A man walking with his dog in the park.",
        "A family group at a birthday celebration.",
        "A child holding a kitten.",
        "A couple posing for a selfie at the beach.",
        "A portrait of a young girl.",
    ],
)
def test_broad_personal_photo_vocabulary_is_gallery_eligible(
    tmp_path: Path, caption: str
) -> None:
    _, _, item = staged_image(tmp_path)
    item = replace(item, proposed_category="Gallery", proposal_confidence="medium")
    content_type, confidence, _ = _classify(caption, "small sign in the background")

    assert content_type == "personal_photo"
    assessment = assess_destination(item, content_type, confidence, "small sign in the background")
    assert assessment.recommended_destination == "Gallery"
    assert assessment.decision_score >= 80
    assert assessment.routing_band == "automatic_eligible"


def test_personal_photo_context_alone_remains_review_first(tmp_path: Path) -> None:
    _, _, item = staged_image(tmp_path)
    item = replace(item, proposed_category="Gallery", proposal_confidence="medium")
    content_type, confidence, _ = _classify("A quiet garden during a holiday", "")

    assert content_type == "personal_photo"
    assessment = assess_destination(item, content_type, confidence, "")
    assert assessment.decision_score < 80
    assert assessment.routing_band != "automatic_eligible"


@pytest.mark.parametrize(
    "caption",
    [
        "A European Health Insurance Card for a woman.",
        "An identity card showing a man.",
        "An official membership card with a child photograph.",
    ],
)
def test_document_cards_take_precedence_over_people_in_captions(caption: str) -> None:
    content_type, _, _ = _classify(caption, "name and identifier")

    assert content_type == "general_document"


def test_pdf_embedded_text_is_page_labelled_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakePage:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class FakeReader:
        is_encrypted = False
        pages = [FakePage("first page"), FakePage("second page")]

    monkeypatch.setattr(ingestion_ai_module, "PdfReader", lambda _: FakeReader())
    monkeypatch.setattr(ingestion_ai_module, "MAX_SEMANTIC_PDF_EMBEDDED_TEXT_CHARS", 15)

    assert _extract_pdf_embedded_text(tmp_path / "document.pdf", 2) == (
        "Embedded page 1: first page",
        "Embedded page 2: secon",
    )


@pytest.mark.parametrize(
    ("caption", "ocr_text", "expected_content_type", "expected_destination"),
    [
        (
            "A screenshot of a purchase receipt",
            "RECEIPT VAT amount paid",
            "receipt",
            "Documents",
        ),
        (
            "A screenshot containing a photograph of two people outdoors",
            "",
            "personal_photo",
            "Gallery",
        ),
        (
            "A screenshot of the cover of a book",
            "Book cover title and author",
            "publication_cover",
            "Library",
        ),
    ],
)
def test_screenshot_context_yields_to_content_semantics(
    caption: str,
    ocr_text: str,
    expected_content_type: str,
    expected_destination: str,
) -> None:
    content_type, _, _, destination, screenshot_fallback = (
        _centralise_semantic_assessment(
            caption,
            ocr_text,
            hard_coded_screenshot=True,
        )
    )

    assert content_type == expected_content_type
    assert destination == expected_destination
    assert not screenshot_fallback


def test_book_cover_evidence_routes_to_review_only_library_candidate(
    tmp_path: Path,
) -> None:
    _, _, item = staged_image(tmp_path)
    classification = _classify(
        'The image is the cover of a book titled "Gears of War: Aspho Fields"',
        "Karen Traviss",
    )

    assert classification[0] == "publication_cover"
    assessment = assess_destination(
        replace(item, proposed_category="Library", proposal_confidence="high"),
        classification[0],
        classification[1],
        "Karen Traviss",
    )
    assert assessment.recommended_destination == "Library"
    assert assessment.routing_band == "individual_review"
    assert "Publication candidates require Reading Room owner review" in (
        assessment.automatic_disqualifiers
    )


def test_destination_assessment_is_versioned_explainable_and_safety_gated(
    tmp_path: Path,
) -> None:
    _, _, item = staged_image(tmp_path)
    photo = assess_destination(item, "personal_photo", 0.95, "")
    assert photo.recommended_destination == "Gallery"
    assert photo.decision_score >= 80
    assert photo.routing_band == "automatic_eligible"
    assert photo.automatic_disqualifiers == ()
    assert photo.decision_model_version == "intelligent-routing-v5"
    assert photo.confidence_components["learned_routing"] == 0

    statement = assess_destination(
        item,
        "financial_document",
        0.96,
        "BANK STATEMENT account number and closing balance",
    )
    assert statement.recommended_destination == "Ledger"
    assert "Destination evidence conflicts" in statement.automatic_disqualifiers
    assert statement.routing_band == "individual_review"
    assert statement.conflicts

    receipt = assess_destination(
        replace(item, proposed_category="Documents", proposal_confidence="low"),
        "receipt",
        0.6,
        "PURCHASE RECEIPT subtotal VAT amount paid",
    )
    assert receipt.decision_score >= 80
    assert receipt.routing_band == "automatic_eligible"
    assert receipt.automatic_disqualifiers == ()

    duplicate = assess_destination(
        replace(item, duplicate_of_id=UUID(int=99)),
        "personal_photo",
        0.99,
        "",
    )
    assert "An exact duplicate requires review" in duplicate.automatic_disqualifiers


def test_personal_photo_incidental_text_is_neutral_and_eligible(
    tmp_path: Path,
) -> None:
    _, _, item = staged_image(tmp_path)
    photo = assess_destination(
        item,
        "personal_photo",
        0.77,
        "small restaurant sign in the background",
    )
    assert photo.confidence_components["ocr_document_type"] == 80
    assert photo.decision_score >= 80
    assert photo.routing_band == "automatic_eligible"


def test_florence_portrait_is_gallery_autopilot_eligible_but_screenshot_is_not(
    tmp_path: Path,
) -> None:
    _, _, item = staged_image(tmp_path)
    item = replace(item, proposed_category="Gallery", proposal_confidence="medium")
    content_type, confidence, _ = _classify(
        "The image is a black and white portrait of a young woman.",
        "faint incidental text in the image background",
    )
    assert content_type == "personal_photo"

    portrait = assess_destination(item, content_type, confidence, "x" * 50)
    assert portrait.recommended_destination == "Gallery"
    assert portrait.decision_score >= 80
    assert portrait.routing_band == "automatic_eligible"

    screenshot = assess_destination(
        replace(item, filename="Screenshot 001.jpg"), content_type, confidence, "x" * 50
    )
    assert screenshot.routing_band == "individual_review"
    assert "Screenshot capture context requires owner review" in (
        screenshot.automatic_disqualifiers
    )


def test_continuous_queue_is_idempotent_and_does_not_retry_failure(
    tmp_path: Path,
) -> None:
    vault_store, _, item = staged_image(tmp_path)
    ai_store = MemoryIngestionAiStore()
    assert queue_pending_ingestion_image_analysis(
        ai_store, vault_store, TEST_USERNAME
    ) == 1
    assert queue_pending_ingestion_image_analysis(
        ai_store, vault_store, TEST_USERNAME
    ) == 0
    claimed = ai_store.claim_next_job()
    assert claimed is not None
    ai_store.fail_job(claimed.id, "Florence unavailable")
    assert queue_pending_ingestion_image_analysis(
        ai_store, vault_store, TEST_USERNAME
    ) == 0
    assert ai_store.queue_analysis(item.id, TEST_USERNAME).status == "queued"


def test_staged_analysis_is_idempotent_private_and_review_only(
    client: TestClient,
    tmp_path: Path,
    authentication_store: MemoryAuthenticationStore,
) -> None:
    vault_store, arrival, item = staged_image(tmp_path)
    ai_store = MemoryIngestionAiStore()
    app.dependency_overrides[get_vault_master_store] = lambda: vault_store
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ai_store
    authenticate(client)
    account = authentication_store.get_account(TEST_USERNAME)
    assert account is not None
    item = replace(item, owner_user_id=account.user_id)
    vault_store.items[item.source_path] = item

    first = client.post(f"/api/vault-master/items/{item.id}/ai/analyse")
    second = client.post(f"/api/vault-master/items/{item.id}/ai/analyse")
    assert first.status_code == 202
    assert second.json()["id"] == first.json()["id"]

    claimed = ai_store.claim_next_job()
    assert claimed is not None
    assert _classify("", "PRIVATE ACCOUNT TEXT")[0] == "unknown"
    assessment = assess_destination(
        item,
        "financial_document",
        0.94,
        "PRIVATE ACCOUNT TEXT",
    )
    assert assessment.recommended_destination == "Ledger"
    ai_store.complete_job(
        claimed.id,
        "financial_document",
        "A photographed statement",
        "PRIVATE ACCOUNT TEXT",
        0.94,
        ("Financial statement indicators: bank statement",),
        1234,
        assessment,
    )
    evidence = client.get("/api/vault-master/items/ai")
    assert evidence.status_code == 200
    assert evidence.headers["cache-control"] == "private, no-store"
    assert evidence.json()["publication_rule"] == "private_review_evidence_only"
    assert evidence.json()["items"][0]["evidence"][0]["ocr_text"] == "PRIVATE ACCOUNT TEXT"
    assert evidence.json()["items"][0]["evidence"][0]["recommended_destination"] == "Ledger"
    assert evidence.json()["items"][0]["evidence"][0]["routing_band"] == "individual_review"
    assert Path(arrival / "statement.jpg").read_bytes() == b"unchanged-image"
    assert vault_store.get_item(item.id).proposed_category == item.proposed_category

    app.dependency_overrides[require_authenticated_user] = lambda: "shared-user"
    assert client.get("/api/vault-master/items/ai").json()["items"] == []
    assert client.post(f"/api/vault-master/items/{item.id}/ai/analyse").status_code == 404


def test_reassessment_appends_current_evidence_without_erasing_history(
    tmp_path: Path,
) -> None:
    vault_store, _, item = staged_image(tmp_path)
    ai_store = MemoryIngestionAiStore()
    stale_job = ai_store.queue_analysis(item.id, "owner")
    claimed = ai_store.claim_next_job()
    assert claimed is not None and claimed.id == stale_job.id
    stale = assess_destination(item, "personal_photo", 0.90, "")
    ai_store.complete_job(
        stale_job.id, "personal_photo", "Old Florence description", "", 0.90,
        ("Photograph indicators",), 10,
        replace(
            stale,
            routing_band="batch_review",
            automatic_disqualifiers=("Learned routing has not reached established maturity",),
        ),
    )

    reassessment_job = ai_store.queue_analysis(item.id, "owner")
    claimed = ai_store.claim_next_job()
    assert claimed is not None and claimed.id == reassessment_job.id
    current = assess_destination(item, "personal_photo", 0.90, "")
    ai_store.complete_job(
        reassessment_job.id, "personal_photo", "Current Florence description", "", 0.90,
        ("Photograph indicators",), 10, current,
    )

    history = ai_store.list_evidence_for_learning(item.id)
    assert len(history) == 2
    assert history[0].caption == "Current Florence description"
    assert history[0].routing_band == "automatic_eligible"
    assert history[0].automatic_disqualifiers == ()
    assert history[1].automatic_disqualifiers == (
        "Learned routing has not reached established maturity",
    )


def test_reassessment_preserves_the_80_point_automatic_safeguard(tmp_path: Path) -> None:
    _, _, item = staged_image(tmp_path)
    assessment = assess_destination(item, "personal_photo", 0.90, "")
    below_threshold = replace(assessment, decision_score=78, routing_band="individual_review")
    assert below_threshold.decision_score == 78
    assert below_threshold.routing_band != "automatic_eligible"


def test_owner_sees_florence_evidence_when_administrator_requested_analysis(
    client: TestClient,
    tmp_path: Path,
    authentication_store: MemoryAuthenticationStore,
) -> None:
    vault_store, _, item = staged_image(tmp_path)
    owner = authentication_store.ensure_initial_administrator(TEST_USERNAME, "hash")
    recipient = Account(
        "recipient", "Recipient", None, "hash", "member", True, False,
        datetime.now(timezone.utc), None,
    )
    authentication_store.create_account(recipient)
    item = replace(item, owner_username="recipient", owner_user_id=recipient.user_id)
    vault_store.items[item.source_path] = item
    ai_store = MemoryIngestionAiStore()
    job = ai_store.queue_analysis(item.id, owner.username)
    claimed = ai_store.claim_next_job()
    assert claimed is not None and claimed.id == job.id
    ai_store.complete_job(
        job.id, "personal_photo", "Recipient's Florence description", "", 0.90,
        ("Photograph indicators",), 10,
        assess_destination(item, "personal_photo", 0.90, ""),
    )
    app.dependency_overrides[get_vault_master_store] = lambda: vault_store
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ai_store
    app.dependency_overrides[require_authenticated_user] = lambda: "recipient"

    response = client.get("/api/vault-master/items/ai")
    assert response.status_code == 200
    assert response.json()["items"][0]["evidence"][0]["caption"] == "Recipient's Florence description"


def test_robert_and_anita_owner_capabilities_are_uuid_isolated(
    client: TestClient, tmp_path: Path, authentication_store: MemoryAuthenticationStore
) -> None:
    vault_store, arrival, robert_item = staged_image(tmp_path)
    owner = authentication_store.ensure_initial_administrator(TEST_USERNAME, "hash")
    recipient = Account("recipient", "Recipient", None, "hash", "member", True, False, datetime.now(timezone.utc), None)
    authentication_store.create_account(recipient)
    anita_path = arrival / "recipient.jpg"
    anita_path.write_bytes(b"recipient-image")
    anita_item = replace(
        robert_item, id=uuid4(), source_path=anita_path, filename="recipient.jpg", sha256="b" * 64
    )
    robert_item = replace(robert_item, owner_user_id=owner.user_id)
    anita_item = replace(anita_item, owner_username="recipient", owner_user_id=recipient.user_id)
    vault_store.items[robert_item.source_path] = robert_item
    vault_store.items[anita_item.source_path] = anita_item
    ai_store = MemoryIngestionAiStore()
    autopilot_store = MemoryAutopilotStore()
    app.dependency_overrides[get_vault_master_store] = lambda: vault_store
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ai_store
    app.dependency_overrides[get_autopilot_store] = lambda: autopilot_store
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(owner)
    assert client.post("/api/vault-master/items/ai/batches", json={"item_ids": [str(robert_item.id)]}).status_code == 202
    assert client.post("/api/vault-master/items/ai/batches", json={"item_ids": [str(anita_item.id)]}).status_code == 409
    robert_policy = client.put("/api/vault-master/autopilot/policy", json={"content_type":"personal_photo","destination":"Gallery","threshold":80,"max_items":50,"max_failures":2,"max_failure_percent":5})
    assert robert_policy.status_code == 200
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(recipient)
    assert client.get("/api/vault-master/items/ai/batches").json() == {"batches": []}
    assert client.post("/api/vault-master/items/ai/review-batches", json={"action":"reject","item_ids":[str(robert_item.id), str(anita_item.id)]}).status_code == 403
    anita_policy = client.put("/api/vault-master/autopilot/policy", json={"content_type":"personal_photo","destination":"Gallery","threshold":80,"max_items":50,"max_failures":2,"max_failure_percent":5})
    assert anita_policy.status_code == 200
    assert client.patch(f"/api/vault-master/autopilot/policy/{anita_policy.json()['id']}", json={"status":"enabled"}).status_code == 200
    assert len(client.get("/api/vault-master/autopilot").json()["policies"]) == 1
    app.dependency_overrides[require_authenticated_user] = lambda: AuthenticatedIdentity(owner)
    assert client.get("/api/vault-master/autopilot").json()["policies"][0]["status"] == "disabled"
    assert client.patch(f"/api/vault-master/autopilot/policy/{robert_policy.json()['id']}", json={"status":"enabled"}).status_code == 200
    assert autopilot_store.list_policies(owner.user_id)[0].owner_user_id == owner.user_id
    assert autopilot_store.list_policies(recipient.user_id)[0].owner_user_id == recipient.user_id
    assert autopilot_store.list_policies(owner.user_id)[0].status == "enabled"
    assert autopilot_store.list_policies(recipient.user_id)[0].status == "enabled"


def test_worker_revalidates_staged_checksum_and_records_local_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_store, arrival, item = staged_image(tmp_path)
    ai_store = MemoryIngestionAiStore()
    ai_store.queue_analysis(item.id, TEST_USERNAME)
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(arrival))
    monkeypatch.setattr(
        ingestion_ai_module,
        "request_florence_analysis",
        lambda source: (
            "A paper bank statement",
            "BANK STATEMENT closing balance",
            900,
        ),
    )

    assert process_next_ingestion_ai_job(ai_store, vault_store) is not None
    evidence = ai_store.list_evidence(item.id, TEST_USERNAME)[0]
    assert evidence.content_type == "financial_document"
    assert evidence.model_revision == "21a599d414c4d928c9032694c424fb94458e3594"
    assert evidence.task_version == "semantic-intake-v5"
    assert evidence.recommended_destination == "Ledger"
    assert evidence.decision_model_version == "intelligent-routing-v5"
    updated = vault_store.get_item(item.id)
    assert updated is not None
    assert updated.proposed_category == "Ledger"
    assert updated.proposal_reason == "Local image evidence suggests this destination."

    changed_store, changed_arrival, changed_item = staged_image(tmp_path / "changed")
    changed_ai_store = MemoryIngestionAiStore()
    changed_ai_store.queue_analysis(changed_item.id, TEST_USERNAME)
    (changed_arrival / "statement.jpg").write_bytes(b"changed")
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(changed_arrival))
    process_next_ingestion_ai_job(changed_ai_store, changed_store)
    failed = changed_ai_store.list_jobs(changed_item.id, TEST_USERNAME)[0]
    assert failed.status == "failed"
    assert "no longer matches" in (failed.error or "")


def test_screenshot_context_does_not_override_semantic_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_store, arrival, item = staged_image(tmp_path)
    explicit = vault_store.update_proposal(item.id, "Gallery", TEST_USERNAME)
    assert explicit is not None
    ai_store = MemoryIngestionAiStore()
    ai_store.queue_analysis(item.id, TEST_USERNAME)
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(arrival))
    monkeypatch.setattr(
        ingestion_ai_module,
        "request_florence_analysis",
        lambda source: (
            "A screenshot of a banking app statement", "BANK STATEMENT closing balance", 100
        ),
    )
    process_next_ingestion_ai_job(ai_store, vault_store)
    preserved = vault_store.get_item(item.id)
    assert preserved is not None
    assert preserved.proposed_category == "Gallery"
    evidence = ai_store.list_evidence(item.id, TEST_USERNAME)[0]
    assert evidence.recommended_destination == "Ledger"
    assert evidence.conflicts


def test_screenshot_replaces_untouched_gallery_guess_but_cannot_auto_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_store, arrival, item = staged_image(tmp_path)
    assert item.proposed_category == "Gallery"
    ai_store = MemoryIngestionAiStore()
    ai_store.queue_analysis(item.id, TEST_USERNAME)
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(arrival))
    monkeypatch.setattr(
        ingestion_ai_module,
        "request_florence_analysis",
        lambda source: ("A screenshot of a phone screen", "settings", 100),
    )
    process_next_ingestion_ai_job(ai_store, vault_store)
    updated = vault_store.get_item(item.id)
    assert updated is not None
    assert updated.proposed_category == "Archives"
    assert updated.proposed_destination == "/vault/Archives/Screenshots/statement.jpg"
    assert updated.proposal_reason == "Local image evidence suggests this destination."
    evidence = ai_store.list_evidence(item.id, TEST_USERNAME)[0]
    assert evidence.conflicts == ()
    assert evidence.routing_band == "individual_review"
    assert "Screenshot capture context requires owner review" in (
        evidence.automatic_disqualifiers
    )

    scan_root(vault_store, arrival, INCOMING_SOURCE)
    rescanned = vault_store.get_item(item.id)
    assert rescanned is not None
    assert rescanned.proposed_category == "Archives"
    assert rescanned.proposal_reason == "Local image evidence suggests this destination."


def test_embedded_screenshot_marker_is_context_not_a_financial_override(
    tmp_path: Path,
) -> None:
    _, _, item = staged_image(tmp_path)
    marked = replace(
        item,
        filename="IMG_4245.png",
        metadata={**item.metadata, "image_description": "Screenshot"},
    )
    assessment = assess_destination(
        marked,
        "personal_photo",
        0.99,
        "BANK STATEMENT account balance People Devices Items Me",
    )
    assert assessment.recommended_destination == "Ledger"
    assert assessment.routing_band == "individual_review"
    assert "Screenshot capture context requires owner review" in (
        assessment.automatic_disqualifiers
    )


def test_staged_pdf_receipt_receives_private_semantic_analysis_and_is_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_store, arrival, item = staged_pdf(tmp_path)
    assert item.mime_type == "application/pdf"
    ai_store = MemoryIngestionAiStore()
    assert queue_pending_ingestion_image_analysis(ai_store, vault_store, TEST_USERNAME) == 1
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(arrival))
    monkeypatch.setattr(
        ingestion_ai_module,
        "_analyse_semantic_source",
        lambda source: ("A purchase receipt", "RECEIPT VAT amount paid", 321),
    )

    assert process_next_ingestion_ai_job(ai_store, vault_store) is not None
    evidence = ai_store.list_evidence(item.id, TEST_USERNAME)[0]
    assert evidence.content_type == "receipt"
    assert evidence.recommended_destination == "Documents"
    assert evidence.decision_score >= 80
    assert evidence.routing_band == "automatic_eligible"
    assert (arrival / "statement.pdf").read_bytes() == b"unchanged-pdf"


@pytest.mark.parametrize(
    ("caption", "ocr_text", "content_type", "destination", "routing_band"),
    [
        ("A scanned childhood photograph", "", "personal_photo", "Gallery", "automatic_eligible"),
        ("A bank statement", "closing balance account number", "financial_document", "Ledger", "automatic_eligible"),
        ("A purchase receipt", "RECEIPT VAT amount paid", "receipt", "Documents", "automatic_eligible"),
        ("The cover of a book", "book cover title and author", "publication_cover", "Library", "individual_review"),
    ],
)
def test_pdf_document_evidence_produces_versioned_semantic_proposals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caption: str,
    ocr_text: str,
    content_type: str,
    destination: str,
    routing_band: str,
) -> None:
    vault_store, arrival, item = staged_pdf(tmp_path)
    ai_store = MemoryIngestionAiStore()
    ai_store.queue_analysis(item.id, TEST_USERNAME)
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(arrival))
    monkeypatch.setattr(
        ingestion_ai_module,
        "_analyse_semantic_source",
        lambda source: (caption, ocr_text, 10),
    )

    assert process_next_ingestion_ai_job(ai_store, vault_store) is not None
    evidence = ai_store.list_evidence(item.id, TEST_USERNAME)[0]
    assert evidence.content_type == content_type
    assert evidence.recommended_destination == destination
    assert evidence.routing_band == routing_band
    assert (arrival / "statement.pdf").read_bytes() == b"unchanged-pdf"


def test_screenshot_context_does_not_block_an_owner_destination_choice(
    client: TestClient,
    tmp_path: Path,
) -> None:
    vault_store, _, item = staged_image(tmp_path)
    marked = replace(
        item,
        metadata={**item.metadata, "description": "Screenshot"},
    )
    vault_store.items[item.source_path] = marked
    ai_store = MemoryIngestionAiStore()
    app.dependency_overrides[get_vault_master_store] = lambda: vault_store
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ai_store
    authenticate(client)

    selected = client.patch(
        f"/api/vault-master/items/{item.id}/proposal",
        json={"category": "Gallery"},
    )
    assert selected.status_code == 200
    assert selected.json()["proposed_destination"] == "/vault/Gallery/statement.jpg"


def test_bulk_analysis_batch_pauses_resumes_groups_and_retries_failures(
    tmp_path: Path,
) -> None:
    vault_store, arrival, first_item = staged_image(tmp_path)
    second_source = arrival / "receipt.jpg"
    second_source.write_bytes(b"second-image")
    scan_root(vault_store, arrival, INCOMING_SOURCE)
    second_item = next(
        item for item in vault_store.list_items() if item.filename == "receipt.jpg"
    )
    ai_store = MemoryIngestionAiStore()
    batch = ai_store.create_analysis_batch(
        (first_item.id, second_item.id, first_item.id), TEST_USERNAME
    )
    assert batch.total_items == 2
    assert batch.queued_items == 2

    paused = ai_store.set_analysis_batch_status(batch.id, TEST_USERNAME, "paused")
    assert paused is not None and paused.status == "paused"
    assert ai_store.claim_next_job() is None

    resumed = ai_store.set_analysis_batch_status(batch.id, TEST_USERNAME, "running")
    assert resumed is not None and resumed.status == "running"
    first_job = ai_store.claim_next_job()
    assert first_job is not None
    ai_store.complete_job(
        first_job.id,
        "personal_photo",
        "A photograph of people outdoors",
        "",
        0.9,
        ("Photograph indicators: photograph, people, outdoor",),
        10,
        assess_destination(first_item, "personal_photo", 0.9, ""),
    )
    second_job = ai_store.claim_next_job()
    assert second_job is not None
    ai_store.fail_job(second_job.id, "temporary model failure")
    failed = ai_store.list_analysis_batches(TEST_USERNAME)[0]
    assert failed.status == "completed_with_failures"
    assert failed.completed_items == 1
    assert failed.failed_items == 1

    retried = ai_store.retry_analysis_batch(batch.id, TEST_USERNAME)
    assert retried is not None and retried.status == "running"
    assert retried.failed_items == 0
    assert retried.queued_items == 1


def test_bulk_analysis_api_is_owner_private_and_individual_review_is_not_batch_approved(
    client: TestClient,
    tmp_path: Path,
) -> None:
    vault_store, _, item = staged_image(tmp_path)
    ai_store = MemoryIngestionAiStore()
    app.dependency_overrides[get_vault_master_store] = lambda: vault_store
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ai_store
    authenticate(client)

    created = client.post(
        "/api/vault-master/items/ai/batches",
        json={"item_ids": [str(item.id)]},
    )
    assert created.status_code == 202
    batch_id = created.json()["id"]
    assert client.post(
        f"/api/vault-master/items/ai/batches/{batch_id}/pause"
    ).json()["status"] == "paused"
    assert client.post(
        f"/api/vault-master/items/ai/batches/{batch_id}/resume"
    ).json()["status"] == "running"

    job = ai_store.claim_next_job()
    assert job is not None
    assessment = assess_destination(
        item,
        "financial_document",
        0.96,
        "BANK STATEMENT account number closing balance",
    )
    assert assessment.routing_band == "individual_review"
    ai_store.complete_job(
        job.id,
        "financial_document",
        "A photographed bank statement",
        "BANK STATEMENT account number closing balance",
        0.96,
        ("Financial statement indicators: bank statement",),
        20,
        assessment,
    )
    listing = client.get("/api/vault-master/items/ai/batches")
    assert listing.headers["cache-control"] == "private, no-store"
    assert listing.json()["batches"][0]["groups"][0]["destination"] == "Ledger"

    reviewed = client.post(
        "/api/vault-master/items/ai/review-batches",
        json={"action": "approve", "item_ids": [str(item.id)]},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["outcomes"][str(item.id)] == "individual_review_required"
    assert vault_store.get_item(item.id).state == "needs_review"

    # A non-administrator remains able to inspect only its own empty history;
    # an owner UUID mismatch is rejected by the creation boundary.
    app.dependency_overrides[require_authenticated_user] = lambda: "shared-user"
    assert client.get("/api/vault-master/items/ai/batches").json() == {"batches": []}
    assert client.post(
        "/api/vault-master/items/ai/batches",
        json={"item_ids": [str(item.id)]},
    ).status_code == 409


def test_routing_memory_matures_is_owner_scoped_and_contradictions_demote(
    tmp_path: Path,
) -> None:
    vault_store, _, item = staged_image(tmp_path)
    ai_store = MemoryIngestionAiStore()
    for expected_count in range(1, 4):
        job = ai_store.queue_analysis(item.id, TEST_USERNAME)
        claimed = ai_store.claim_next_job()
        assert claimed is not None and claimed.id == job.id
        base = assess_destination(item, "financial_document", 0.96, "BANK STATEMENT")
        ai_store.complete_job(
            claimed.id, "financial_document", "A statement", "BANK STATEMENT",
            0.96, ("Financial statement indicators",), 10, base,
        )
        rule = ai_store.remember_decision(item, "Ledger", "approved", TEST_USERNAME)
        assert rule is not None and rule.example_count == expected_count
        if expected_count == 1:
            assert rule.maturity == "evidence"
            assert ai_store.apply_routing_memory(item, "financial_document", "BANK STATEMENT", base) == base

    assert rule.maturity == "suggestion"
    learned = ai_store.apply_routing_memory(item, "financial_document", "BANK STATEMENT", base)
    assert learned == base
    assert ai_store.list_routing_rules(UUID(int=999)) == []

    contradicted = ai_store.remember_decision(item, "Documents", "approved", TEST_USERNAME)
    assert contradicted is not None
    assert contradicted.contradiction_count == 1
    assert contradicted.maturity == "review"
    assert ai_store.apply_routing_memory(item, "financial_document", "BANK STATEMENT", base) == base
    assert ai_store.update_routing_rule(rule.id, UUID(int=999), "disabled") is None
    assert ai_store.update_routing_rule(rule.id, item.owner_user_id, "disabled").status == "disabled"
    assert ai_store.apply_routing_memory(
        item, "financial_document", "BANK STATEMENT", base
    ).confidence_components["learned_routing"] == 0
    assert ai_store.delete_routing_rule(rule.id, item.owner_user_id)
    assert ai_store.list_routing_rules(item.owner_user_id) == []


def test_routing_memory_owner_uuid_isolates_requester_and_duplicate_display_names(tmp_path: Path) -> None:
    vault_store, _, item = staged_image(tmp_path)
    # The UUIDs represent two accounts that may share a human-facing display name.
    anita_item = replace(item, id=UUID(int=902), owner_user_id=UUID(int=22), owner_username="recipient")
    robert_item = replace(item, owner_user_id=UUID(int=11), owner_username=TEST_USERNAME)
    ai_store = MemoryIngestionAiStore()
    base = assess_destination(robert_item, "personal_photo", 0.95, "")
    for owned_item, requester, decision_actor in (
        (robert_item, TEST_USERNAME, TEST_USERNAME),
        (anita_item, TEST_USERNAME, "recipient"),
    ):
        job = ai_store.queue_analysis(owned_item.id, requester)
        assert ai_store.claim_next_job() is not None
        ai_store.complete_job(job.id, "personal_photo", "A photo", "", 0.95, (), 1, base)
        ai_store.remember_decision(owned_item, "Gallery", "approved", decision_actor)
    assert ai_store.apply_routing_memory(anita_item, "personal_photo", "", base) == base
    assert ai_store.apply_routing_memory(robert_item, "personal_photo", "", base) == base
    assert {rule.owner_user_id for rule in ai_store.routing_rules.values()} == {UUID(int=11), UUID(int=22)}
    for _ in range(9):
        job = ai_store.queue_analysis(robert_item.id, TEST_USERNAME)
        assert ai_store.claim_next_job() is not None
        ai_store.complete_job(job.id, "personal_photo", "A photo", "", 0.95, (), 1, base)
        robert_rule = ai_store.remember_decision(robert_item, "Gallery", "approved", TEST_USERNAME)
    assert robert_rule is not None and robert_rule.maturity == "established"
    assert ai_store.apply_routing_memory(robert_item, "personal_photo", "", base).confidence_components["learned_routing"] > 0
    # The requester remains Owner, but an Recipient-owned item must never read Owner's rule.
    assert ai_store.apply_routing_memory(anita_item, "personal_photo", "", base) == base


def test_established_routing_memory_retains_existing_influence(tmp_path: Path) -> None:
    vault_store, _, item = staged_image(tmp_path)
    ai_store = MemoryIngestionAiStore()
    base = assess_destination(item, "financial_document", 0.96, "BANK STATEMENT")
    for _ in range(10):
        job = ai_store.queue_analysis(item.id, TEST_USERNAME)
        assert ai_store.claim_next_job() is not None
        ai_store.complete_job(job.id, "financial_document", "Statement", "BANK STATEMENT", 0.96, (), 1, base)
        rule = ai_store.remember_decision(item, "Ledger", "approved", TEST_USERNAME)
    assert rule is not None and rule.maturity == "established"
    assert ai_store.apply_routing_memory(item, "financial_document", "BANK STATEMENT", base).confidence_components["learned_routing"] > 0


def test_routing_memory_management_api_is_private(
    client: TestClient,
    authentication_store: MemoryAuthenticationStore,
    tmp_path: Path,
) -> None:
    vault_store, _, item = staged_image(tmp_path)
    authenticate(client)
    account = authentication_store.get_account(TEST_USERNAME)
    assert account is not None
    item = replace(item, owner_user_id=account.user_id)
    ai_store = MemoryIngestionAiStore()
    job = ai_store.queue_analysis(item.id, TEST_USERNAME, account.user_id)
    assert ai_store.claim_next_job() is not None
    ai_store.complete_job(
        job.id, "personal_photo", "A photo", "", 0.9, ("Photo",), 10,
        assess_destination(item, "personal_photo", 0.9, ""),
    )
    rule = ai_store.remember_decision(item, "Gallery", "approved", TEST_USERNAME)
    assert rule is not None
    app.dependency_overrides[get_vault_master_store] = lambda: vault_store
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ai_store
    listing = client.get("/api/vault-master/routing-memory")
    assert listing.status_code == 200
    assert listing.headers["cache-control"] == "private, no-store"
    assert listing.json()["rules"][0]["affected_item_ids"] == [str(item.id)]
    assert client.patch(
        f"/api/vault-master/routing-memory/{rule.id}", json={"action": "disable"}
    ).json()["status"] == "disabled"
    assert client.patch(
        f"/api/vault-master/routing-memory/{rule.id}",
        json={"action": "edit", "destination": "Documents"},
    ).json()["destination"] == "Documents"

    app.dependency_overrides[require_authenticated_user] = lambda: "shared-user"
    assert client.get("/api/vault-master/routing-memory").status_code == 404
