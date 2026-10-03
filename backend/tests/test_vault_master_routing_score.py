from pathlib import Path
from dataclasses import replace
import json
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from PIL import Image

from app.vault_master import INCOMING_SOURCE, MemoryVaultMasterStore, arrival_routing_receipt_matches, proposed_destination_path, safely_move_approved_file, scan_root
from app.vault_master_autopilot import MemoryAutopilotStore, process_autopilot_batch, _authorize_scored_item
from app.vault_master_ingestion_ai import MemoryIngestionAiStore, process_next_ingestion_ai_job
from app.vault_master_ingestion_ai import _classify
from app.arrival_managed_publisher import ArrivalManagedPublicationRequest, queue_request, verify_request
from app.vault_master_routing_score import (
    CLASS_DESTINATIONS,
    ROUTING_SCORE_VERSION,
    score_classes,
    score_staged_item,
    semantic_candidates,
    build_routing_decision,
)


def staged_item(tmp_path: Path, filename: str):
    source = tmp_path / filename
    source.write_bytes(b"synthetic intake")
    store = MemoryVaultMasterStore()
    scan_root(store, tmp_path, INCOMING_SOURCE)
    return store.list_items()[0]


def test_class_scores_separate_semantic_and_deterministic_points(tmp_path: Path) -> None:
    item = staged_item(tmp_path, "example.pdf")
    scores = score_classes(item, {"receipt": (0.9, ("receipt",)), "general_document": (0.8, ("document",))})
    assert ROUTING_SCORE_VERSION == "vm-routing-score-v2"
    assert scores[0].content_type == "receipt"
    assert scores[0].semantic_points == 81
    assert scores[0].deterministic_points == 7
    assert scores[0].total == 88
    assert scores[0].total - scores[1].total < 15


def test_competing_financial_and_receipt_classes_share_documents(tmp_path: Path) -> None:
    item = staged_item(tmp_path, "statement.pdf")
    scores = score_staged_item(item, "A bank statement with a receipt", "account number subtotal")
    assert {score.content_type for score in scores} >= {"financial_document", "receipt"}
    assert CLASS_DESTINATIONS["financial_document"] == "Documents"
    assert CLASS_DESTINATIONS["receipt"] == "Documents"
    assert "Ledger" not in CLASS_DESTINATIONS.values()


def test_screenshot_is_retained_as_competing_class() -> None:
    candidates = semantic_candidates("A photograph of a screen screenshot", "")
    assert "personal_photo" in candidates
    assert "screenshot" in candidates


@pytest.mark.parametrize(("caption", "ocr", "expected"), [
    ("A photograph of a tram stop information board in the street.",
     "DEPARTURES PLATFORM TIMES DESTINATION", "personal_photo"),
    ("A car on a street beside shops and many road signs.",
     "STOP PARKING SALE OPEN HOURS MAIN STREET", "personal_photo"),
    ("A photograph of a magazine rack in a bookshop.",
     "MAGAZINE BOOK TITLE ISSUE SALE COVER", "personal_photo"),
    ("People in an office with monitors on nearby desks.",
     "MENU SETTINGS SEARCH EMAIL", "personal_photo"),
    ("A camera photograph of a physical computer monitor on a desk in a room.",
     "WOLF PUPS", "personal_photo"),
    ("A photograph of a shopfront and information signs.",
     "MENU HOURS SALE INVOICE", "personal_photo"),
    ("A photograph of a poster on a shop wall.",
     "SALE TODAY", "personal_photo"),
    ("A native screenshot of flight search results.", "", "screenshot"),
    ("A screenshot of a calendar app.", "MON TUE WED", "screenshot"),
    ("A settings screen in an app interface.", "", "screenshot"),
    ("A photograph of a receipt on a table.", "", "receipt"),
    ("A photographed invoice page.", "", "general_document"),
    ("A photograph of a bank statement.", "", "financial_document"),
    ("A printed page containing a letter.", "", "general_document"),
])
def test_primary_capture_context_outweighs_incidental_text(
    tmp_path: Path, caption: str, ocr: str, expected: str,
) -> None:
    item = staged_item(tmp_path, "example.png")
    candidates = semantic_candidates(caption, ocr)
    scores = score_staged_item(item, caption, ocr)
    assert _classify(caption, ocr)[0] == expected
    assert scores[0].content_type == expected
    assert expected in candidates
    if expected == "personal_photo":
        assert scores[0].total >= 80
    if expected == "screenshot":
        assert CLASS_DESTINATIONS[expected] == "Archives"


@pytest.mark.parametrize(("caption", "ocr"), [
    ("The image shows a bottle on a wooden table. Its label has the letter E.",
     "EXAMPLE BRAND LETTER E"),
    ("The image shows a packaged product on a shelf. Its label contains text.",
     "EXAMPLE INGREDIENTS LETTER E"),
    ("The image shows a shop sign beside a street. Letters are visible.",
     "EXAMPLE SHOP OPEN"),
    ("The image shows magazines on shelves in a shop. Covers have lettering.",
     "EXAMPLE MAGAZINE ISSUE"),
    ("The image shows an information board at a tram stop. A letter appears on it.",
     "EXAMPLE ROUTE LETTER E"),
    ("The image shows a letter E printed on a product label.",
     "EXAMPLE LETTER E"),
])
def test_text_on_physical_scene_objects_is_not_document_evidence(
    tmp_path: Path, caption: str, ocr: str,
) -> None:
    assert _classify(caption, ocr)[0] == "personal_photo"
    candidates = semantic_candidates(caption, ocr)
    assert "general_document" not in candidates
    assert score_staged_item(staged_item(tmp_path, "example.jpg"), caption, ocr)[0].content_type == "personal_photo"


@pytest.mark.parametrize(("caption", "expected"), [
    ("The image shows a photographed letter on a desk.", "general_document"),
    ("The image shows a receipt on a wooden table.", "receipt"),
    ("The image shows a native screenshot of an app.", "screenshot"),
])
def test_document_subject_and_native_capture_still_determine_class(
    caption: str, expected: str,
) -> None:
    assert _classify(caption, "EXAMPLE TEXT")[0] == expected
    assert expected in semantic_candidates(caption, "EXAMPLE TEXT")


def test_isolated_ocr_letter_cannot_create_document_class() -> None:
    assert _classify("", "letter")[0] == "unknown"
    assert "general_document" not in semantic_candidates("", "letter")


def test_photographed_screen_reference_keeps_competing_evidence_for_review(
    tmp_path: Path,
) -> None:
    caption = ("A tightly framed photograph of a computer screen showing "
               "File Explorer as a reference capture.")
    candidates = semantic_candidates(caption, "FOLDERS FILES")
    assert {"personal_photo", "screenshot"} <= candidates.keys()
    item = replace(staged_item(tmp_path, "reference.jpg"),
                   owner_user_id=uuid5(NAMESPACE_URL, "screen-reference-owner"))
    from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
    from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION
    evidence = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "screen-reference-evidence"),
        item_id=item.id, owner_user_id=item.owner_user_id,
        source_sha256=item.sha256, caption=caption, ocr_text="FOLDERS FILES",
        model_id=AI_MODEL_ID, model_revision=AI_MODEL_REVISION,
        task_version=INGESTION_TASK_VERSION,
    )
    decision = build_routing_decision(item, evidence, None, {"source_safe": True})
    assert decision.winning_margin is not None and decision.winning_margin < 15
    assert decision.reason == "semantic_class_margin_below_15"
    assert not decision.automatic_eligible


def test_ocr_and_png_container_alone_do_not_choose_a_destination(tmp_path: Path) -> None:
    ocr = "RECEIPT subtotal 10.00 VAT 2.00 amount paid 12.00"
    assert _classify("", ocr)[0] == "unknown"
    scores = score_staged_item(staged_item(tmp_path, "unframed.png"), "", ocr)
    assert scores[0].content_type == "unknown"
    assert "receipt" not in semantic_candidates("", ocr)


def test_v2_semantics_can_cross_80_without_deterministic_metadata(tmp_path: Path) -> None:
    # An image with scanner software has no compatible Gallery proposal. An
    # ordinary PNG supplies only weak image structure, without camera EXIF.
    scanner_root, png_root, camera_root, weak_root = (
        tmp_path / name for name in ("scanner", "png", "camera", "weak"))
    for root in (scanner_root, png_root, camera_root, weak_root):
        root.mkdir()
    scanner_item = replace(staged_item(scanner_root, "example.png"),
                           metadata={"software": "Example Scanner"})
    no_support = score_classes(scanner_item,
                               {"personal_photo": (0.9, ("scene",))})[0]
    assert (no_support.semantic_points, no_support.deterministic_points,
            no_support.total) == (81, 0, 81)
    weak_support = score_classes(staged_item(png_root, "example.png"),
                                 {"personal_photo": (0.9, ("scene",))})[0]
    assert (weak_support.semantic_points, weak_support.deterministic_points,
            weak_support.total) == (81, 3, 84)
    camera_item = replace(staged_item(camera_root, "camera.jpg"),
                          metadata={"camera_make": "Example Camera"})
    medium_support = score_classes(camera_item,
                                   {"personal_photo": (0.9, ("scene",))})[0]
    assert (medium_support.semantic_points, medium_support.deterministic_points,
            medium_support.total) == (81, 7, 88)
    weak_semantics = score_classes(staged_item(weak_root, "weak.jpg"),
                                   {"personal_photo": (0.5, ("weak scene",))})[0]
    assert weak_semantics.total == 48


def test_screen_object_in_later_sentence_is_not_screenshot_framing(tmp_path: Path) -> None:
    caption = ("A group of people and a man sitting in an office. A computer "
               "screen is visible on a nearby desk.")
    candidates = semantic_candidates(caption, "")
    assert candidates["personal_photo"][0] == pytest.approx(0.92)
    assert "screenshot" not in candidates
    assert "screenshot" not in semantic_candidates(caption, "OFFICE TEXT")
    scores = score_staged_item(staged_item(tmp_path, "scene.png"), caption, "")
    assert scores[0].content_type == "personal_photo"
    assert scores[0].total == 85.8


def test_explicit_screenshot_and_primary_screen_framing_remain_competitors() -> None:
    explicit = semantic_candidates(
        "A group of people in an office. A screenshot of the meeting is shown.", "")
    assert "screenshot" in explicit
    primary_screen = semantic_candidates(
        "A group of people on a computer screen. The room is visible.", "")
    assert "screenshot" in primary_screen


@pytest.mark.parametrize(("caption", "expected"), [
    ("A receipt with subtotal, VAT, and amount paid", "receipt"),
    ("A bank statement with account number and closing balance", "financial_document"),
    ("An official document with a letter, certificate, and contract", "general_document"),
    ("An illustration painting and artwork", "artwork"),
])
def test_document_and_artwork_classes_use_the_same_v2_balance(
    tmp_path: Path, caption: str, expected: str,
) -> None:
    score = score_staged_item(staged_item(tmp_path, "example.png"), caption, "")[0]
    assert score.content_type == expected
    assert score.semantic_points == round(score.semantic_confidence * 90, 2)
    assert score.deterministic_points <= 10
    assert score.total >= 80


@pytest.mark.parametrize(("caption", "ocr"), [
    ("A portrait of a smiling person", ""),
    ("A purchase receipt", "RECEIPT VAT amount paid"),
    ("A bank statement", "account number closing balance"),
    ("An official document", "contract application form"),
    ("An artwork painting", ""),
    ("A screenshot", ""),
])
def test_candidate_confidence_preserves_existing_single_class_mapping(caption: str, ocr: str) -> None:
    content_type, confidence, _ = _classify(caption, ocr)
    assert semantic_candidates(caption, ocr)[content_type][0] == confidence


@pytest.mark.parametrize(("caption", "ocr"), [
    ("The image shows a black sports car parked on the side of a street.", "PARKING"),
    ("A restaurant interior with food on the table and a menu in the background.", "MENU TODAY"),
    ("People standing together outside a shop.", "EXAMPLE TEAM"),
    ("A building beside a busy road with a street sign.", "MAIN STREET"),
])
def test_scene_caption_with_incidental_ocr_remains_strong_photo(
    tmp_path: Path, caption: str, ocr: str,
) -> None:
    content_type, confidence, _ = _classify(caption, ocr)
    assert content_type == "personal_photo"
    assert confidence >= 0.82
    candidates = semantic_candidates(caption, ocr)
    assert candidates["personal_photo"][0] == confidence
    assert not any(name in candidates for name in ("receipt", "financial_document", "general_document"))
    scores = score_staged_item(staged_item(tmp_path, "example.jpg"), caption, ocr)
    assert scores[0].content_type == "personal_photo"
    assert scores[0].semantic_points >= 60


def test_scene_with_weak_document_word_in_ocr_does_not_lose_photo() -> None:
    caption = "A car parked near a cafe on a street."
    assert _classify(caption, "RECEIPT",)[0] == "personal_photo"
    assert "receipt" not in semantic_candidates(caption, "RECEIPT")
    later_sign = "The image shows a sports car on a street. A shop sign reads BANK STATEMENT."
    assert _classify(later_sign, "BANK STATEMENT")[0] == "personal_photo"
    assert "financial_document" not in semantic_candidates(later_sign, "BANK STATEMENT")
    later_poster = "The image shows a sports car on a street. A poster is visible behind it."
    assert _classify(later_poster, "SALE TODAY")[0] == "personal_photo"
    assert "artwork" not in semantic_candidates(later_poster, "SALE TODAY")


def test_scene_with_structured_document_ocr_keeps_competing_evidence() -> None:
    caption = "A car parked outside a restaurant."
    candidates = semantic_candidates(caption, "RECEIPT subtotal £10.00 VAT £2.00 amount paid £12.00")
    assert {"personal_photo", "receipt"} <= candidates.keys()
    assert any(value.startswith("florence_caption:") for value in candidates["personal_photo"][1])
    assert any(value.startswith("florence_ocr:") for value in candidates["receipt"][1])


@pytest.mark.parametrize(("caption", "ocr", "expected"), [
    ("A photograph of a receipt on a table.", "Subtotal 10.00 VAT 2.00", "receipt"),
    ("A photograph of a bank statement.", "account number closing balance", "financial_document"),
    ("A photograph of a printed document page.", "", "general_document"),
    ("A screenshot of a phone screen showing a message.", "MENU SETTINGS SEARCH", "screenshot"),
    ("A printed document page.", "", "general_document"),
])
def test_document_and_screen_visual_framing_remains_strong(caption: str, ocr: str, expected: str) -> None:
    content_type, _, _ = _classify(caption, ocr)
    assert content_type == expected
    assert expected in semantic_candidates(caption, ocr)


def test_weak_ocr_and_camera_metadata_alone_do_not_supply_photo_semantics(tmp_path: Path) -> None:
    assert _classify("", "PARKING")[0] == "unknown"
    assert semantic_candidates("", "PARKING") == {"unknown": (0.45, ("OCR found text but no reliable document category",))}
    item = staged_item(tmp_path, "example.jpg")
    assert score_staged_item(item, "", "PARKING")[0].content_type == "unknown"


def test_ledger_cannot_be_a_canonical_destination(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="derived view"):
        proposed_destination_path("Ledger", "example.pdf", "example.pdf")
    item = staged_item(tmp_path, "example.pdf")
    with pytest.raises(ValueError, match="derived view"):
        safely_move_approved_file(replace(item, state="approved", proposed_category="Ledger"),
                                 tmp_path, tmp_path)


def test_margin_and_policy_are_separate_from_class_score(tmp_path: Path) -> None:
    item = staged_item(tmp_path, "example.pdf")
    owner = uuid5(NAMESPACE_URL, "example-owner")
    item = replace(item, owner_user_id=owner)
    from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
    from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION
    evidence = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "example-evidence"), item_id=item.id,
        owner_user_id=owner, source_sha256=item.sha256,
        caption="A bank statement receipt", ocr_text="account number subtotal",
        model_id=AI_MODEL_ID, model_revision=AI_MODEL_REVISION,
        task_version=INGESTION_TASK_VERSION,
    )
    decision = build_routing_decision(item, evidence, None, {"source_safe": True})
    assert decision.winning_class == "financial_document"
    assert decision.destination == "Documents"
    assert decision.score is not None and decision.score >= 80
    assert decision.winning_margin is not None and decision.winning_margin < 15
    assert decision.reason == "semantic_class_margin_below_15"
    assert not decision.automatic_eligible
    assert build_routing_decision(item, evidence, None, {"source_safe": True}).id == decision.id
    stored = MemoryAutopilotStore()
    stored.save_routing_decision(decision)
    assert stored.list_routing_decisions(item.id, owner)[0]["candidates"]
    assert stored.list_routing_decisions(item.id, uuid5(NAMESPACE_URL, "other-owner")) == []


def test_v2_reassessment_preserves_v1_decision_and_is_idempotent(tmp_path: Path) -> None:
    item = replace(staged_item(tmp_path, "scene.png"),
                   owner_user_id=uuid5(NAMESPACE_URL, "reassessment-owner"))
    from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
    from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION
    evidence = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "reassessment-evidence"), item_id=item.id,
        owner_user_id=item.owner_user_id, source_sha256=item.sha256,
        caption="A group of people and a man sitting in an office. A computer screen is nearby.",
        ocr_text="", model_id=AI_MODEL_ID, model_revision=AI_MODEL_REVISION,
        task_version=INGESTION_TASK_VERSION,
    )
    store = MemoryAutopilotStore()
    prior = build_routing_decision(item, evidence, None, {"safe": True})
    prior = replace(prior, id=uuid5(NAMESPACE_URL, "historic-v1-decision"),
                    version="vm-routing-score-v1")
    store.save_routing_decision(prior)
    revised = store.save_routing_decision(build_routing_decision(
        item, evidence, None, {"safe": True}))
    again = store.save_routing_decision(build_routing_decision(
        item, evidence, None, {"safe": True}))
    assert revised.version == "vm-routing-score-v2"
    assert revised.supersedes_id == prior.id
    assert again.id == revised.id
    assert [value["version"] for value in store.list_routing_decisions(
        item.id, item.owner_user_id)] == ["vm-routing-score-v2", "vm-routing-score-v1"]
    assert store.routing_decisions[prior.id] == prior


def test_staged_v1_photo_without_exif_gets_v2_decision_and_normal_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    arrival, gallery = tmp_path / "Arrival Hall", tmp_path / "Gallery"
    arrival.mkdir(); gallery.mkdir()
    Image.new("RGB", (48, 48), "blue").save(arrival / "example.png")
    vault = MemoryVaultMasterStore()
    scan_root(vault, arrival, INCOMING_SOURCE)
    item = vault.list_items()[0]
    owner = uuid5(NAMESPACE_URL, "v2-photo-owner")
    item = replace(item, owner_user_id=owner, owner_username="Example Owner")
    vault.items[item.source_path] = item
    assert not item.metadata.get("camera_model")
    ai = MemoryIngestionAiStore()
    ai.queue_analysis(item.id, "Example Owner", owner)
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(arrival))
    monkeypatch.setattr("app.vault_master_ingestion_ai._analyse_semantic_source",
                        lambda source: (
                            "A group of people and a man sitting in an office. "
                            "A computer screen is visible nearby.", "", 1))
    assert process_next_ingestion_ai_job(ai, vault)
    evidence = ai.list_evidence(item.id, owner)[0]
    policies = MemoryAutopilotStore()
    policy = policies.upsert_policy(owner, "Example Owner", "personal_photo",
                                    "Gallery", 80, 50, 2, 5)
    policies.set_policy_status(policy.id, owner, "enabled")
    old = build_routing_decision(item, evidence, policy,
                                 {"source_and_destination_preflight": True})
    old = replace(old, id=uuid5(NAMESPACE_URL, "old-photo-v1"),
                  version="vm-routing-score-v1")
    policies.save_routing_decision(old)
    assert process_autopilot_batch(policies, ai, vault, arrival,
                                   {"Gallery": gallery}) is not None
    assert vault.get_item(item.id).state == "move_queued"
    assert policies.routing_decisions[old.id] == old
    revised = [decision for decision in policies.routing_decisions.values()
               if decision.version == ROUTING_SCORE_VERSION]
    assert len(revised) == 1
    assert revised[0].supersedes_id == old.id
    assert revised[0].winning_class == "personal_photo"
    assert revised[0].score == 85.8
    assert revised[0].automatic_eligible
    assert not any(candidate.content_type == "screenshot"
                   for candidate in revised[0].candidates)
    assert next(iter(policies.routing_authorizations.values()))["decision_id"] == str(revised[0].id)


def test_v2_receipt_binding_and_unknown_version_fail_closed(tmp_path: Path) -> None:
    item = staged_item(tmp_path, "example.png")
    rule = {"version": "vm-routing-score-v2", "decision_id": str(uuid5(NAMESPACE_URL, "decision")),
            "authorization_id": str(uuid5(NAMESPACE_URL, "authorization")),
            "policy_id": str(uuid5(NAMESPACE_URL, "policy")),
            "owner_user_id": str(uuid5(NAMESPACE_URL, "owner")),
            "semantic_class": "personal_photo", "destination": "Gallery",
            "score": 84, "threshold": 80, "source_sha256": item.sha256}
    item = replace(item, owner_user_id=uuid5(NAMESPACE_URL, "owner"),
                   proposed_category="Gallery",
                   proposed_destination="/vault/Gallery/example.png",
                   metadata={"arrival_publication_rule": rule})
    request = ArrivalManagedPublicationRequest.create(item=item)
    assert request.routing_authorization is not None
    assert request.routing_authorization["version"] == "vm-routing-score-v2"
    assert arrival_routing_receipt_matches(item, {"routing_authorization":
                                           request.routing_authorization})
    unknown = replace(item, metadata={"arrival_publication_rule":
                                      {**rule, "version": "vm-routing-score-v3"}})
    with pytest.raises(ValueError, match="Unsupported automatic routing"):
        ArrivalManagedPublicationRequest.create(item=unknown)
    assert not arrival_routing_receipt_matches(
        unknown, {"routing_authorization": request.routing_authorization})
    malformed = replace(item, metadata={"arrival_publication_rule": "invalid"})
    with pytest.raises(ValueError, match="Malformed automatic routing"):
        ArrivalManagedPublicationRequest.create(item=malformed)
    assert not arrival_routing_receipt_matches(malformed, {})


@pytest.mark.parametrize(("caption", "destination", "reason"), [
    ("A screenshot of a screen", "Archives", "matching_owner_policy_absent"),
    ("An unclear image", None, "unsupported_or_unknown_semantic_class"),
])
def test_unconfigured_or_unknown_content_stays_staged(
    tmp_path: Path, caption: str, destination: str | None, reason: str,
) -> None:
    item = replace(staged_item(tmp_path, "example.jpg"),
                   owner_user_id=uuid5(NAMESPACE_URL, "unsupported-owner"))
    from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
    from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION
    evidence = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, caption), item_id=item.id,
        owner_user_id=item.owner_user_id, source_sha256=item.sha256,
        caption=caption, ocr_text="", model_id=AI_MODEL_ID,
        model_revision=AI_MODEL_REVISION, task_version=INGESTION_TASK_VERSION,
    )
    decision = build_routing_decision(item, evidence, None, {"source_safe": True})
    assert decision.destination == destination
    assert decision.reason == reason


def test_missing_or_stale_semantic_evidence_is_insufficient(tmp_path: Path) -> None:
    item = replace(staged_item(tmp_path, "example.pdf"),
                   owner_user_id=uuid5(NAMESPACE_URL, "missing-evidence-owner"))
    decision = build_routing_decision(item, None, None, {"source_safe": True})
    assert decision.candidates == ()
    assert decision.reason == "required_semantic_evidence_missing_or_stale"
    assert not decision.automatic_eligible


def test_high_photo_semantics_cannot_route_a_pdf_to_gallery(tmp_path: Path) -> None:
    item = replace(staged_item(tmp_path, "example.pdf"),
                   owner_user_id=uuid5(NAMESPACE_URL, "incompatible-format-owner"))
    from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
    from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION
    evidence = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "incompatible-format-evidence"), item_id=item.id,
        owner_user_id=item.owner_user_id, source_sha256=item.sha256,
        caption="A group of people and a man sitting in an office", ocr_text="",
        model_id=AI_MODEL_ID, model_revision=AI_MODEL_REVISION,
        task_version=INGESTION_TASK_VERSION,
    )
    decision = build_routing_decision(item, evidence, None, {"source_safe": True})
    assert decision.score == 82.8
    assert decision.gates["gallery_image_mime_compatible"] is False
    assert decision.reason == "gallery_image_mime_compatible"
    assert not decision.automatic_eligible


@pytest.mark.parametrize(("caption", "ocr"), [
    ("A portrait of a smiling person outdoors", ""),
    ("The image shows a black sports car parked on a street", "PARKING"),
])
def test_source_bound_scored_photo_authorizes_gallery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caption: str, ocr: str,
) -> None:
    arrival, gallery = tmp_path / "Arrival Hall", tmp_path / "Gallery"
    arrival.mkdir(); gallery.mkdir()
    exif = Image.Exif()
    exif[271] = "Example Camera"
    exif[272] = "Example Model"
    exif[36867] = "2026:01:02 03:04:05"
    Image.new("RGB", (48, 48), "blue").save(arrival / "example.jpg", exif=exif)
    vault = MemoryVaultMasterStore()
    scan_root(vault, arrival, INCOMING_SOURCE)
    item = vault.list_items()[0]
    owner = uuid5(NAMESPACE_URL, "example-routing-owner")
    item = replace(item, owner_user_id=owner, owner_username="Example Owner")
    vault.items[item.source_path] = item
    assert item.proposal_confidence == "medium"
    ai = MemoryIngestionAiStore()
    ai.queue_analysis(item.id, "Example Owner", owner)
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(arrival))
    monkeypatch.setattr("app.vault_master_ingestion_ai._analyse_semantic_source",
                        lambda source: (caption, ocr, 1))
    assert process_next_ingestion_ai_job(ai, vault)
    evidence = ai.list_evidence(item.id, owner)[0]
    assert evidence.source_sha256 == item.sha256
    policies = MemoryAutopilotStore()
    policy = policies.upsert_policy(owner, "Example Owner", "personal_photo", "Gallery", 80, 50, 2, 5)
    policies.set_policy_status(policy.id, owner, "enabled")
    run_id = process_autopilot_batch(policies, ai, vault, arrival, {"Gallery": gallery})
    assert run_id is not None
    assert vault.get_item(item.id).state == "move_queued"
    decision = next(iter(policies.routing_decisions.values()))
    assert decision.automatic_eligible
    assert decision.score is not None and decision.score >= 80
    assert decision.destination == "Gallery"
    assert len(policies.routing_authorizations) == 1
    request = ArrivalManagedPublicationRequest.create(item=vault.get_item(item.id))
    key = b"synthetic-test-signing-key"
    request_path = queue_request(request, queue_root=tmp_path / "requests", key=key)
    signed = json.loads(request_path.read_text(encoding="utf-8"))
    assert verify_request(signed, key) == request
    assert signed["request"]["routing_authorization"]["decision_id"] == str(decision.id)


@pytest.mark.parametrize(("content_type", "caption", "ocr"), [
    ("receipt", "A purchase receipt", "RECEIPT subtotal VAT amount paid change due"),
    ("financial_document", "A bank statement", "account number sort code opening balance closing balance"),
    ("general_document", "An official document", "letter certificate invoice contract application form"),
])
def test_camera_image_document_semantics_authorize_documents(
    tmp_path: Path, content_type: str, caption: str, ocr: str,
) -> None:
    arrival, documents = tmp_path / "Arrival Hall", tmp_path / "Documents"
    arrival.mkdir(); documents.mkdir()
    exif = Image.Exif()
    exif[271] = "Example Camera"; exif[272] = "Example Model"
    Image.new("RGB", (48, 48), "blue").save(arrival / "example.jpg", exif=exif)
    vault = MemoryVaultMasterStore(); scan_root(vault, arrival, INCOMING_SOURCE)
    item = vault.list_items()[0]
    owner = uuid5(NAMESPACE_URL, "example-document-owner")
    item = replace(item, owner_user_id=owner, owner_username="Example Owner")
    vault.items[item.source_path] = item
    assert item.proposed_category == "Gallery"
    from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
    from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION
    evidence = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, content_type), item_id=item.id,
        owner_user_id=owner, source_sha256=item.sha256,
        caption=caption, ocr_text=ocr, model_id=AI_MODEL_ID,
        model_revision=AI_MODEL_REVISION, task_version=INGESTION_TASK_VERSION,
    )
    policy_store = MemoryAutopilotStore()
    policy = policy_store.upsert_policy(owner, "Example Owner", content_type,
                                       "Documents", 80, 50, 2, 5)
    policy = policy_store.set_policy_status(policy.id, owner, "enabled")
    decision = policy_store.save_routing_decision(build_routing_decision(
        item, evidence, policy, {"source_and_destination_preflight": True}))
    assert decision.winning_class == content_type
    assert decision.destination == "Documents"
    assert decision.score is not None and decision.score >= 80
    assert decision.winning_margin is not None and decision.winning_margin >= 15
    assert decision.candidates[0].deterministic_confidence == "low"
    queued = _authorize_scored_item(policy_store, vault, item, decision, arrival,
                                    {"Documents": documents})
    assert queued is not None and queued.proposed_category == "Documents"
    assert queued.state == "move_queued"


def test_policy_change_and_changed_bytes_fail_closed_at_approval(tmp_path: Path) -> None:
    arrival = tmp_path / "Arrival Hall"; arrival.mkdir()
    documents = tmp_path / "Documents"; documents.mkdir()
    source = arrival / "example.pdf"; source.write_bytes(b"synthetic receipt")
    vault = MemoryVaultMasterStore(); scan_root(vault, arrival, INCOMING_SOURCE)
    item = vault.list_items()[0]
    owner = uuid5(NAMESPACE_URL, "approval-example-owner")
    item = replace(item, owner_user_id=owner, owner_username="Example Owner")
    vault.items[item.source_path] = item
    from app.vault_master_ai import AI_MODEL_ID, AI_MODEL_REVISION
    from app.vault_master_ingestion_ai import INGESTION_TASK_VERSION
    evidence = SimpleNamespace(
        id=uuid5(NAMESPACE_URL, "approval-evidence"), item_id=item.id,
        owner_user_id=owner, source_sha256=item.sha256,
        caption="A purchase receipt", ocr_text="RECEIPT VAT amount paid",
        model_id=AI_MODEL_ID, model_revision=AI_MODEL_REVISION,
        task_version=INGESTION_TASK_VERSION,
    )
    policies = MemoryAutopilotStore()
    policy = policies.upsert_policy(owner, "Example Owner", "receipt", "Documents", 80, 50, 2, 5)
    policy = policies.set_policy_status(policy.id, owner, "enabled")
    decision = policies.save_routing_decision(build_routing_decision(item, evidence, policy, {"safe": True}))
    assert decision.automatic_eligible
    policies.set_policy_status(policy.id, owner, "paused")
    assert _authorize_scored_item(policies, vault, item, decision, arrival,
                                  {"Documents": documents}) is None
    assert vault.get_item(item.id).state == "needs_review"
    policy = policies.set_policy_status(policy.id, owner, "enabled")
    fresh = build_routing_decision(item, evidence, policy, {"safe": True})
    source.write_bytes(b"changed after decision")
    assert _authorize_scored_item(policies, vault, item, fresh, arrival,
                                  {"Documents": documents}) is None
    assert not policies.routing_authorizations


def test_camera_photo_of_person_holding_receipt_remains_gallery_scene(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    arrival, gallery = tmp_path / "Arrival Hall", tmp_path / "Gallery"
    arrival.mkdir(); gallery.mkdir()
    exif = Image.Exif(); exif[271] = "Example Camera"; exif[272] = "Example Model"
    Image.new("RGB", (48, 48), "blue").save(arrival / "example.jpg", exif=exif)
    vault = MemoryVaultMasterStore(); scan_root(vault, arrival, INCOMING_SOURCE)
    item = vault.list_items()[0]
    owner = uuid5(NAMESPACE_URL, "mixed-evidence-owner")
    item = replace(item, owner_user_id=owner, owner_username="Example Owner")
    vault.items[item.source_path] = item
    ai = MemoryIngestionAiStore(); ai.queue_analysis(item.id, "Example Owner", owner)
    monkeypatch.setenv("PV_ARRIVAL_HALL_PATH", str(arrival))
    monkeypatch.setattr("app.vault_master_ingestion_ai._analyse_semantic_source",
                        lambda source: ("A portrait photograph of a person holding a receipt", "RECEIPT", 1))
    assert process_next_ingestion_ai_job(ai, vault)
    policies = MemoryAutopilotStore()
    policy = policies.upsert_policy(owner, "Example Owner", "personal_photo", "Gallery", 80, 50, 2, 5)
    policies.set_policy_status(policy.id, owner, "enabled")
    assert process_autopilot_batch(policies, ai, vault, arrival, {"Gallery": gallery}) is not None
    assert vault.get_item(item.id).state == "move_queued"
    decision = next(iter(policies.routing_decisions.values()))
    assert decision.winning_class == "personal_photo"
    assert decision.destination == "Gallery"
