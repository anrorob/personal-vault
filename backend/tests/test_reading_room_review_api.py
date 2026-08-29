from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from pypdf import PdfWriter

from app.config import get_metadata_storage_root
from app.incoming import get_incoming_path
from app.main import app
from app.vault_master import INCOMING_SOURCE, MemoryVaultMasterStore, ScannedFile, get_vault_master_store, sha256_file
from app.vault_master_reading import MemoryReadingRoomStore, PublicationBlock, PublicationIssue, PublicationMetadata, PublicationSnapshot, get_reading_room_store
from app.vault_master_reading_extraction import ExtractionProgress
from app.vault_master_reading_review import MemoryPublicationReviewStore, PublicationReview, get_publication_review_store
from tests.conftest import TEST_PASSWORD, TEST_USERNAME
from app.vault_master_api import get_destination_paths


def authenticate(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}).status_code == 200


def source_item(store: MemoryVaultMasterStore, root: Path):
    path = root / "Author - Title.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    with path.open("wb") as handle:
        writer.write(handle)
    batch = store.create_batch(INCOMING_SOURCE, str(root))
    return store.record_file(batch, INCOMING_SOURCE, ScannedFile(str(path), path.name, path.name, path.stat().st_size, "application/pdf", datetime.now(timezone.utc), sha256_file(path), {}))


def snapshot(asset_id: UUID, *, critical: bool = False) -> PublicationSnapshot:
    metadata = PublicationMetadata(asset_id, "book", "reflowable", "needs_review", "en", "reading-html-v1", detected={"author": "Author", "title": "Title", "page_count": 1})
    block_id = uuid4()
    block = PublicationBlock(block_id, asset_id, "paragraph", 0, "page-1/p-1", content_text="Text", content_html="<p>Text</p>", source_page=1)
    issue = PublicationIssue(uuid4(), asset_id, "likely_ocr_mistake", "critical" if critical else "warning", "open", "Review text", block_id=block_id, source_page=1)
    return PublicationSnapshot(metadata, (), (block,), (issue,))


def configure(store: MemoryVaultMasterStore, reviews: MemoryPublicationReviewStore, root: Path) -> None:
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_publication_review_store] = lambda: reviews
    app.dependency_overrides[get_incoming_path] = lambda: root
    app.dependency_overrides[get_metadata_storage_root] = lambda: root / "metadata"
    library = root / "Library"
    library.mkdir(exist_ok=True)
    app.dependency_overrides[get_destination_paths] = lambda: {"Library": library}
    app.dependency_overrides[get_reading_room_store] = lambda: MemoryReadingRoomStore(asset_exists=lambda asset_id: store.get_catalogued_asset_by_id(asset_id) is not None)


def test_owner_can_extract_and_fetch_private_review(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    store = MemoryVaultMasterStore()
    reviews = MemoryPublicationReviewStore()
    item = source_item(store, tmp_path)
    configure(store, reviews, tmp_path)
    monkeypatch.setattr("app.vault_master_api.extract_publication", lambda **_: ExtractionProgress(item.id, 1, 1, (), snapshot(item.id)))
    authenticate(client)

    extracted = client.post(f"/api/vault-master/publication-bundles/{item.id}/extract", json={"max_florence_pages": 1})
    assert extracted.status_code == 200
    assert extracted.json()["review"]["state"] == "needs_review"
    fetched = client.get(f"/api/vault-master/publication-bundles/{item.id}/review")
    assert fetched.status_code == 200
    assert fetched.headers["cache-control"] == "private, no-store"


def test_corrections_and_critical_publish_gate_are_owner_reviewed(client: TestClient, tmp_path: Path) -> None:
    store = MemoryVaultMasterStore()
    reviews = MemoryPublicationReviewStore()
    item = source_item(store, tmp_path)
    configure(store, reviews, tmp_path)
    review = PublicationReview(item.id, TEST_USERNAME, "needs_review", snapshot(item.id, critical=True))
    reviews.save(review)
    authenticate(client)

    metadata = client.patch(f"/api/vault-master/publication-bundles/{item.id}/review/metadata", json={"values": {"title": "Corrected title"}, "language": "en"})
    assert metadata.status_code == 200
    block_id = metadata.json()["snapshot"]["blocks"][0]["id"]
    corrected = client.patch(f"/api/vault-master/publication-bundles/{item.id}/review/blocks/{block_id}", json={"text": "Safe <script>text</script>", "block_type": "paragraph"})
    assert "<script>" not in corrected.json()["snapshot"]["blocks"][0]["content_html"]
    blocked = client.post(f"/api/vault-master/publication-bundles/{item.id}/review/action", json={"action": "publish"})
    assert blocked.status_code == 409
    issue_id = corrected.json()["snapshot"]["issues"][0]["id"]
    assert client.patch(f"/api/vault-master/publication-bundles/{item.id}/review/issues/{issue_id}", json={"state": "resolved"}).status_code == 200
    evidence = tmp_path / "metadata" / "publication-extraction" / str(item.id)
    evidence.mkdir(parents=True)
    (evidence / "document.json").write_text("{}", encoding="utf-8")
    ready = client.post(f"/api/vault-master/publication-bundles/{item.id}/review/action", json={"action": "publish"})
    assert ready.status_code == 200
    assert ready.json()["state"] == "published"
    assert store.get_catalogued_asset_by_id(item.id) is not None
    assert not Path(item.source_path).exists()
    assert (tmp_path / "Library" / "Author" / "Corrected title" / "reading" / "publication.html").is_file()


def test_exact_page_is_png_not_pdf(client: TestClient, tmp_path: Path) -> None:
    store = MemoryVaultMasterStore()
    reviews = MemoryPublicationReviewStore()
    item = source_item(store, tmp_path)
    configure(store, reviews, tmp_path)
    authenticate(client)

    response = client.get(f"/api/vault-master/publication-bundles/{item.id}/pages/1")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content.startswith(b"\x89PNG")
