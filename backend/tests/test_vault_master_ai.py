from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, NAMESPACE_URL, uuid5

from fastapi.testclient import TestClient
import pytest

from app.main import app
from app.auth import require_authenticated_user
from app.vault_master import (
    CataloguedAsset,
    ImportItem,
    MemoryVaultMasterStore,
    get_vault_master_store,
    sha256_file,
)
import app.vault_master_ai as ai_module
from app.vault_master_ai import MemoryAiStore, get_ai_store, process_next_ai_job
from app.vault_master_ingestion_ai import (
    MemoryIngestionAiStore,
    assess_destination,
    get_ingestion_ai_store,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def gallery_asset(owner: str = TEST_USERNAME) -> CataloguedAsset:
    return CataloguedAsset(
        id=UUID(int=601),
        asset_type="Gallery",
        display_title="Letter",
        captured_on=None,
        location=None,
        vault_path="/vault/Gallery/letter.jpg",
        filename="letter.jpg",
        size_bytes=3,
        mime_type="image/jpeg",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={},
        owner_username=owner,
        owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{owner}"),
    )


def authenticate(client: TestClient) -> None:
    assert client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    ).status_code == 200


def test_memory_ai_job_is_idempotent_auditable_and_reviewed_once() -> None:
    store = MemoryAiStore()
    asset_id = UUID(int=601)
    queued = store.queue_ocr(asset_id, TEST_USERNAME)
    assert store.queue_ocr(asset_id, TEST_USERNAME).id == queued.id
    claimed = store.claim_next_job()
    assert claimed is not None and claimed.attempts == 1
    suggestion = store.complete_job(claimed.id, "Private letter", None, 1234)
    assert suggestion.model_revision == "21a599d414c4d928c9032694c424fb94458e3594"
    accepted = store.review_suggestion(
        suggestion.id, TEST_USERNAME, "accepted", "Corrected private letter"
    )
    assert accepted is not None and accepted.reviewed_value == "Corrected private letter"
    assert store.review_suggestion(suggestion.id, TEST_USERNAME, "rejected", None) is None


def test_owner_can_queue_and_review_ocr_without_publishing_metadata(
    client: TestClient,
) -> None:
    vault_store = MemoryVaultMasterStore()
    asset = gallery_asset()
    vault_store.catalogued_assets[asset.vault_path] = asset
    ai_store = MemoryAiStore()
    ingestion_ai_store = MemoryIngestionAiStore()
    source_item = ImportItem(
        id=UUID(int=602),
        batch_id=UUID(int=603),
        source_kind="incoming",
        source_path="/vault/Incoming/letter.jpg",
        relative_path="letter.jpg",
        filename="letter.jpg",
        size_bytes=asset.size_bytes,
        mime_type=asset.mime_type,
        modified_at=datetime.now(timezone.utc),
        sha256=asset.sha256,
        state="moved",
        duplicate_of_id=None,
        proposed_category="Gallery",
        proposed_destination=asset.vault_path,
        proposal_reason="Owner approved Gallery destination",
        proposal_confidence="high",
        metadata={},
        metadata_overrides={},
            owner_username=TEST_USERNAME,
            owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),
    )
    vault_store.items[source_item.source_path] = source_item
    ingestion_ai_store.queue_analysis(source_item.id, TEST_USERNAME, source_item.owner_user_id)
    ingestion_job = ingestion_ai_store.claim_next_job()
    assert ingestion_job is not None
    ingestion_ai_store.complete_job(
        ingestion_job.id,
        "personal_photo",
        "A person standing beside a blue car.",
        "",
        0.91,
        ("Photograph indicators: person, car",),
        700,
        assess_destination(source_item, "personal_photo", 0.91, ""),
    )
    app.dependency_overrides[get_vault_master_store] = lambda: vault_store
    app.dependency_overrides[get_ai_store] = lambda: ai_store
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ingestion_ai_store
    authenticate(client)

    queued = client.post(f"/api/vault-master/assets/{asset.id}/ai/ocr")
    assert queued.status_code == 202
    job = ai_store.claim_next_job()
    assert job is not None
    suggestion = ai_store.complete_job(job.id, "SECRET TEXT", 0.9, 800)

    evidence = client.get(f"/api/vault-master/assets/{asset.id}/ai")
    assert evidence.status_code == 200
    assert evidence.headers["cache-control"] == "private, no-store"
    assert evidence.json()["publication_rule"] == "owner_review_evidence_only"
    assert evidence.json()["visual_description"]["caption"] == (
        "A person standing beside a blue car."
    )
    vault_store.items[source_item.source_path] = replace(
        source_item,
        proposed_destination="/vault/Gallery/a-different-photo.jpg",
    )
    unrelated = client.get(f"/api/vault-master/assets/{asset.id}/ai")
    assert unrelated.status_code == 200
    assert unrelated.json()["visual_description"] is None
    vault_store.items[source_item.source_path] = source_item
    reviewed = client.post(
        f"/api/vault-master/assets/{asset.id}/ai/suggestions/{suggestion.id}/review",
        json={"status": "accepted", "value": "SECRET TEXT corrected"},
    )
    assert reviewed.status_code == 200
    assert vault_store.get_catalogued_asset_by_id(asset.id) == asset

    vault_store.catalogued_assets[asset.vault_path] = replace(
        asset, visibility="shared", shared_with=("son",)
    )
    app.dependency_overrides[require_authenticated_user] = lambda: "son"
    assert client.get(f"/api/vault-master/assets/{asset.id}/ai").status_code == 404
    assert client.post(f"/api/vault-master/assets/{asset.id}/ai/ocr").status_code == 404


def test_processing_validates_owner_and_records_service_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault_store = MemoryVaultMasterStore()
    asset = gallery_asset(owner="someone-else")
    vault_store.catalogued_assets[asset.vault_path] = asset
    ai_store = MemoryAiStore()
    job = ai_store.queue_ocr(asset.id, TEST_USERNAME)
    assert process_next_ai_job(ai_store, vault_store) == job.id
    failed = ai_store.list_jobs(asset.id, TEST_USERNAME)[0]
    assert failed.status == "failed"
    assert "no longer owned" in (failed.error or "")


def test_processing_sends_only_a_matching_catalogued_gallery_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gallery = tmp_path / "gallery"
    gallery.mkdir()
    source = gallery / "letter.jpg"
    source.write_bytes(b"image-bytes")
    asset = replace(
        gallery_asset(), size_bytes=source.stat().st_size, sha256=sha256_file(source)
    )
    vault_store = MemoryVaultMasterStore()
    vault_store.catalogued_assets[asset.vault_path] = asset
    ai_store = MemoryAiStore()
    job = ai_store.queue_ocr(asset.id, TEST_USERNAME)
    monkeypatch.setenv("PV_GALLERY_PATH", str(gallery))
    monkeypatch.setattr(
        ai_module,
        "request_florence_ocr",
        lambda path: ("LOCAL TEXT", 0.88, 700),
    )

    assert process_next_ai_job(ai_store, vault_store) == job.id
    suggestion = ai_store.list_suggestions(asset.id, TEST_USERNAME)[0]
    assert suggestion.raw_value == "LOCAL TEXT"
    assert suggestion.confidence == 0.88
