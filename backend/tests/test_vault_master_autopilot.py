from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from fastapi.testclient import TestClient

from app.auth import require_authenticated_user
from app.main import app
from app.vault_master import (
    INCOMING_SOURCE,
    MemoryVaultMasterStore,
    get_vault_master_store,
    scan_root,
)
from app.vault_master_autopilot import (
    AUTOPILOT_ACTIVITY_USERNAME,
    MemoryAutopilotStore,
    audit_recent_gallery_screenshots,
    get_autopilot_store,
    process_autopilot_batch,
    reconcile_autopilot_runs,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def owner_id(username: str):
    return uuid5(NAMESPACE_URL, f"test-owner:{username}")


def assign_owner(vault_store: MemoryVaultMasterStore, item: object, username: str) -> object:
    owned = replace(item, owner_username=username, owner_user_id=owner_id(username))
    vault_store.items[item.source_path] = owned
    return owned


def authenticate(client: TestClient) -> None:
    assert client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    ).status_code == 200
from app.vault_master_ingestion_ai import (
    MemoryIngestionAiStore,
    assess_destination,
    get_ingestion_ai_store,
)


def eligible_photo(tmp_path: Path, username: str = "owner"):
    arrival = tmp_path / "Arrival Hall"
    gallery = tmp_path / "Gallery"
    arrival.mkdir()
    gallery.mkdir()
    source = arrival / "holiday.jpg"
    source.write_bytes(b"valid-photo")
    vault_store = MemoryVaultMasterStore()
    scan_root(vault_store, arrival, INCOMING_SOURCE)
    item = assign_owner(vault_store, vault_store.list_items()[0], username)
    assert item.proposed_category == "Gallery"
    ai_store = MemoryIngestionAiStore()
    job = ai_store.queue_analysis(item.id, username)
    claimed = ai_store.claim_next_job()
    assert claimed and claimed.id == job.id
    ai_store.complete_job(
        job.id,
        "personal_photo",
        "A photograph of people outdoors",
        "",
        0.95,
        ("Photograph indicators: people, outdoor",),
        50,
        assess_destination(item, "personal_photo", 0.95, ""),
    )
    return arrival, gallery, source, vault_store, ai_store, item


def eligible_document(tmp_path: Path, username: str = "owner"):
    arrival = tmp_path / "Arrival Hall"
    documents = tmp_path / "Documents"
    arrival.mkdir()
    documents.mkdir()
    source = arrival / "receipt.pdf"
    source.write_bytes(b"valid-document")
    vault_store = MemoryVaultMasterStore()
    scan_root(vault_store, arrival, INCOMING_SOURCE)
    item = assign_owner(vault_store, vault_store.list_items()[0], username)
    assert item.proposed_category == "Documents"
    ai_store = MemoryIngestionAiStore()
    job = ai_store.queue_analysis(item.id, username)
    claimed = ai_store.claim_next_job()
    assert claimed and claimed.id == job.id
    ai_store.complete_job(
        job.id,
        "receipt",
        "A purchase receipt",
        "RECEIPT VAT amount paid",
        0.95,
        ("Receipt indicators: receipt, vat",),
        50,
        assess_destination(item, "receipt", 0.95, "RECEIPT VAT amount paid"),
    )
    return arrival, documents, vault_store, ai_store, item


def test_policy_enforces_safe_minimum_and_starts_disabled() -> None:
    store = MemoryAutopilotStore()
    with pytest.raises(ValueError, match="below 80"):
        store.upsert_policy(owner_id("owner"), "owner", "personal_photo", "Gallery", 79, 50, 2, 5)
    financial = store.upsert_policy(owner_id("owner"), "owner", "financial_document", "Ledger", 90, 50, 2, 5)
    assert financial.status == "disabled"
    with pytest.raises(ValueError, match="not eligible for auto-pilot"):
        store.upsert_policy(owner_id("owner"), "owner", "publication_cover", "Library", 90, 50, 2, 5)
    policy = store.upsert_policy(owner_id("owner"), "owner", "personal_photo", "Gallery", 80, 50, 2, 5)
    assert policy.status == "disabled"


def test_policy_api_is_owner_scoped_and_requires_explicit_enable(
    client: TestClient,
) -> None:
    store = MemoryAutopilotStore()
    app.dependency_overrides[get_autopilot_store] = lambda: store
    authenticate(client)
    created = client.put(
        "/api/vault-master/autopilot/policy",
        json={
            "content_type": "personal_photo",
            "destination": "Gallery",
            "threshold": 80,
            "max_items": 50,
            "max_failures": 2,
            "max_failure_percent": 5,
        },
    )
    assert created.status_code == 200
    assert created.json()["status"] == "disabled"
    policy_id = created.json()["id"]
    enabled = client.patch(
        f"/api/vault-master/autopilot/policy/{policy_id}",
        json={"status": "enabled"},
    )
    assert enabled.status_code == 200
    assert enabled.json()["status"] == "enabled"
    assert client.get("/api/vault-master/autopilot").headers["cache-control"] == "private, no-store"

    app.dependency_overrides[require_authenticated_user] = lambda: "shared-user"
    assert client.get("/api/vault-master/autopilot").status_code == 404


def test_enabled_policy_queues_exact_safe_item_and_reconciles(tmp_path: Path) -> None:
    arrival, gallery, _, vault_store, ai_store, item = eligible_photo(tmp_path)
    policy_store = MemoryAutopilotStore()
    policy = policy_store.upsert_policy(owner_id("owner"), "owner", "personal_photo", "Gallery", 80, 50, 2, 5)
    policy_store.set_policy_status(policy.id, owner_id("owner"), "enabled")

    run_id = process_autopilot_batch(
        policy_store, ai_store, vault_store, arrival, {"Gallery": gallery}
    )

    assert run_id is not None
    run = policy_store.runs[run_id]
    assert run.item_ids == (item.id,)
    assert run.outcomes == {str(item.id): "queued"}
    assert vault_store.get_item(item.id).state == "move_queued"
    decision = next(
        event
        for event in vault_store.list_activity()
        if event.action == "proposal_approved"
    )
    assert decision.username == AUTOPILOT_ACTIVITY_USERNAME
    vault_store.record_move_result(item.id, "moved", "worker", "moved")
    assert reconcile_autopilot_runs(policy_store, vault_store) == run_id
    assert policy_store.runs[run_id].status == "completed"


def test_enabled_document_policy_queues_an_80_point_receipt(tmp_path: Path) -> None:
    arrival, documents, vault_store, ai_store, item = eligible_document(tmp_path)
    policy_store = MemoryAutopilotStore()
    policy = policy_store.upsert_policy(owner_id("owner"), "owner", "receipt", "Documents", 80, 50, 2, 5)
    policy_store.set_policy_status(policy.id, owner_id("owner"), "enabled")

    run_id = process_autopilot_batch(
        policy_store, ai_store, vault_store, arrival, {"Documents": documents}
    )

    assert run_id is not None
    assert policy_store.runs[run_id].outcomes == {str(item.id): "queued"}
    assert vault_store.get_item(item.id).state == "move_queued"


def test_reading_room_bundle_members_are_excluded_from_document_autopilot(
    tmp_path: Path,
) -> None:
    arrival = tmp_path / "Arrival Hall"
    documents = tmp_path / "Documents"
    arrival.mkdir()
    documents.mkdir()
    (arrival / "A Writer - A Book.pdf").write_bytes(b"book-source")
    (arrival / "A Writer - A Book - front.jpg").write_bytes(b"book-cover")
    vault_store = MemoryVaultMasterStore()
    scan_root(vault_store, arrival, INCOMING_SOURCE)
    source = assign_owner(vault_store, next(item for item in vault_store.list_items() if item.filename.endswith(".pdf")), "owner")
    ai_store = MemoryIngestionAiStore()
    job = ai_store.queue_analysis(source.id, "owner")
    claimed = ai_store.claim_next_job()
    assert claimed and claimed.id == job.id
    ai_store.complete_job(
        job.id,
        "general_document",
        "A scanned document",
        "A complete ordinary document with sufficient local text.",
        0.95,
        ("Document indicators: document",),
        50,
        assess_destination(
            source,
            "general_document",
            0.95,
            "A complete ordinary document with sufficient local text.",
        ),
    )
    policy_store = MemoryAutopilotStore()
    policy = policy_store.upsert_policy(
        owner_id("owner"), "owner", "general_document", "Documents", 80, 50, 2, 5
    )
    policy_store.set_policy_status(policy.id, owner_id("owner"), "enabled")

    assert (
        process_autopilot_batch(
            policy_store, ai_store, vault_store, arrival, {"Documents": documents}
        )
        is None
    )
    assert vault_store.get_item(source.id).state == "needs_review"


def test_preflight_change_stops_and_pauses_policy(tmp_path: Path) -> None:
    arrival, gallery, source, vault_store, ai_store, item = eligible_photo(tmp_path)
    source.write_bytes(b"changed-after-analysis")
    policy_store = MemoryAutopilotStore()
    policy = policy_store.upsert_policy(owner_id("owner"), "owner", "personal_photo", "Gallery", 80, 50, 1, 5)
    policy_store.set_policy_status(policy.id, owner_id("owner"), "enabled")

    run_id = process_autopilot_batch(
        policy_store, ai_store, vault_store, arrival, {"Gallery": gallery}
    )

    assert run_id is not None
    assert policy_store.runs[run_id].status == "stopped"
    assert policy_store.runs[run_id].outcomes[str(item.id)] == "source_changed"
    assert policy_store.policies[policy.id].status == "paused"
    assert vault_store.get_item(item.id).state == "needs_review"


def test_recent_gallery_screenshot_audit_is_read_only_and_owner_scoped(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, _, _, vault_store, ai_store, original = eligible_photo(
        tmp_path,
        TEST_USERNAME,
    )
    marked = replace(
        original,
        metadata={**original.metadata, "image_description": "Screenshot"},
    )
    vault_store.items[original.source_path] = marked
    vault_store.record_move_result(
        marked.id,
        "moved",
        "legacy auto-pilot",
        "Moved to /media/gallery/holiday.jpg",
    )
    policy_store = MemoryAutopilotStore()
    policy = policy_store.upsert_policy(
        owner_id(TEST_USERNAME), TEST_USERNAME, "personal_photo", "Gallery", 80, 50, 2, 5
    )
    run = policy_store.create_run(policy, (marked.id,))
    policy_store.update_run(
        run.id,
        {str(marked.id): "moved"},
        "completed",
    )

    suspects = audit_recent_gallery_screenshots(
        policy_store,
        ai_store,
        vault_store,
        TEST_USERNAME,
    )
    assert len(suspects) == 1
    assert suspects[0].filename == "holiday.jpg"
    assert suspects[0].vault_path == "/vault/Gallery/holiday.jpg"
    assert "embedded metadata" in suspects[0].reasons[0]
    assert vault_store.get_item(marked.id).state == "moved"

    app.dependency_overrides[get_vault_master_store] = lambda: vault_store
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ai_store
    app.dependency_overrides[get_autopilot_store] = lambda: policy_store
    authenticate(client)
    response = client.get("/api/vault-master/autopilot/gallery-screenshot-audit")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["action"] == "report_only"
    assert response.json()["suspects"][0]["filename"] == "holiday.jpg"

    app.dependency_overrides[require_authenticated_user] = lambda: "shared-user"
    assert (
        client.get("/api/vault-master/autopilot/gallery-screenshot-audit").status_code
        == 403
    )


def test_model_or_task_change_stops_before_any_file_is_queued(tmp_path: Path) -> None:
    arrival, gallery, _, vault_store, ai_store, item = eligible_photo(tmp_path)
    evidence_id, evidence = next(iter(ai_store.evidence.items()))
    ai_store.evidence[evidence_id] = replace(evidence, task_version="unexpected-task-v2")
    policy_store = MemoryAutopilotStore()
    policy = policy_store.upsert_policy(owner_id("owner"), "owner", "personal_photo", "Gallery", 80, 50, 2, 5)
    policy_store.set_policy_status(policy.id, owner_id("owner"), "enabled")

    run_id = process_autopilot_batch(
        policy_store, ai_store, vault_store, arrival, {"Gallery": gallery}
    )

    assert run_id is not None
    run = policy_store.runs[run_id]
    assert run.status == "stopped"
    assert run.outcomes == {str(item.id): "model_or_task_version_mismatch"}
    assert "explicit owner review and resume" in (run.stop_reason or "")
    assert policy_store.policies[policy.id].status == "paused"
    assert vault_store.get_item(item.id).state == "needs_review"


def test_policy_uses_item_owner_not_analysis_requester(tmp_path: Path) -> None:
    arrival = tmp_path / "Arrival Hall"
    gallery = tmp_path / "Gallery"
    arrival.mkdir()
    gallery.mkdir()
    (arrival / "owner.jpg").write_bytes(b"owner-photo")
    (arrival / "recipient.jpg").write_bytes(b"recipient-photo")
    vault_store = MemoryVaultMasterStore()
    scan_root(vault_store, arrival, INCOMING_SOURCE)
    items = {item.filename: item for item in vault_store.list_items()}
    owner = assign_owner(vault_store, items["owner.jpg"], "owner")
    recipient = assign_owner(vault_store, items["recipient.jpg"], "recipient")
    ai_store = MemoryIngestionAiStore()
    for item in (owner, recipient):
        job = ai_store.queue_analysis(item.id, "owner")
        claimed = ai_store.claim_next_job()
        assert claimed is not None and claimed.id == job.id
        ai_store.complete_job(
            job.id, "personal_photo", "A family photograph", "", 0.95,
            ("Photograph indicators",), 10,
            assess_destination(item, "personal_photo", 0.95, ""),
        )
    policies = MemoryAutopilotStore()
    policy = policies.upsert_policy(
        owner_id("owner"), "owner", "personal_photo", "Gallery", 80, 50, 2, 5
    )
    assert policies.set_policy_status(policy.id, owner_id("owner"), "enabled")

    assert process_autopilot_batch(policies, ai_store, vault_store, arrival, {"Gallery": gallery})
    assert vault_store.get_item(owner.id).state == "move_queued"
    assert vault_store.get_item(recipient.id).state == "needs_review"
