from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi.testclient import TestClient

from app.main import app
import app.reading_room_catalogue as reading_room_catalogue
from app.vault_master import CataloguedAsset, MemoryVaultMasterStore, get_vault_master_store, sha256_file
from app.vault_master_api import get_destination_paths
from app.vault_master_reading import (
    MemoryReadingRoomStore,
    PublicationBlock,
    PublicationFile,
    PublicationMetadata,
    PublicationSnapshot,
    ReaderPosition,
    get_reading_room_store,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


ASSET_ID = UUID("11111111-2222-4333-8444-555555555555")
ILLUSTRATION_ID = UUID("66666666-7777-4888-8999-000000000000")


def authenticate(client: TestClient) -> None:
    assert client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    ).status_code == 200


def asset(asset_id: UUID = ASSET_ID, *, owner: str = TEST_USERNAME) -> CataloguedAsset:
    return CataloguedAsset(
        asset_id,
        "Library",
        "The Book",
        None,
        None,
        f"/vault/Library/Author/The Book/source/Author - The Book.pdf",
        "Author - The Book.pdf",
        100,
        "application/pdf",
        "a" * 64,
        {"author": "Author"},
        {"author": "reviewed_publication"},
        owner_username=owner,
        owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{owner}"),
    )


def snapshot(root: Path, *, state: str = "approved") -> PublicationSnapshot:
    cover = root / "Author" / "The Book" / "covers" / "front.jpg"
    cover.parent.mkdir(parents=True, exist_ok=True)
    cover.write_bytes(b"jpeg-cover")
    illustration = root / "Author" / "The Book" / "reading" / "illustrations" / "figure.jpg"
    illustration.parent.mkdir(parents=True, exist_ok=True)
    illustration.write_bytes(b"jpeg-illustration")
    files = (
        PublicationFile(
            uuid4(),
            ASSET_ID,
            "source_pdf",
            "/vault/Library/Author/The Book/source/Author - The Book.pdf",
            "Author - The Book.pdf",
            "application/pdf",
            "a" * 64,
            True,
            0,
        ),
        PublicationFile(
            uuid4(),
            ASSET_ID,
            "front_cover",
            "/vault/Library/Author/The Book/covers/front.jpg",
            "front.jpg",
            "image/jpeg",
            sha256_file(cover),
            True,
            1,
        ),
        PublicationFile(
            ILLUSTRATION_ID,
            ASSET_ID,
            "illustration",
            "/vault/Library/Author/The Book/reading/illustrations/figure.jpg",
            "figure.jpg",
            "image/jpeg",
            sha256_file(illustration),
            False,
            2,
        ),
    )
    chapter = PublicationBlock(
        uuid4(), ASSET_ID, "chapter", 0, "chapter-1", content_text="Chapter One"
    )
    paragraph = PublicationBlock(
        uuid4(), ASSET_ID, "paragraph", 1, "chapter-1/p-1", parent_id=chapter.id,
        content_text="Zażółć gęślą jaźń. An English searchable passage."
    )
    figure = PublicationBlock(
        uuid4(),
        ASSET_ID,
        "illustration",
        2,
        "chapter-1/figure-1",
        parent_id=chapter.id,
        content_html='<img src="javascript:alert(1)">',
        illustration_file_id=ILLUSTRATION_ID,
    )
    footnote = PublicationBlock(
        uuid4(),
        ASSET_ID,
        "footnote",
        3,
        "chapter-1/note-1",
        parent_id=chapter.id,
        content_html="<script>alert(1)</script>",
        content_text="A reviewed note.",
    )
    metadata = PublicationMetadata(
        ASSET_ID,
        "book",
        "reflowable",
        state,
        "pl",
        effective={
            "title": "The Book",
            "author": "Author",
            "edition": "First edition",
            "description": "A description.",
            "publisher": "Vault Press",
            "isbn": "9780000000000",
        },
    )
    return PublicationSnapshot(metadata, files, (chapter, paragraph, figure, footnote), ())


def configure(root: Path, *, state: str = "approved", owner: str = TEST_USERNAME):
    catalogue = MemoryVaultMasterStore()
    canonical = asset(owner=owner)
    catalogue.catalogued_assets[canonical.vault_path] = canonical
    publications = MemoryReadingRoomStore()
    publications.publications[ASSET_ID] = snapshot(root, state=state)
    app.dependency_overrides[get_vault_master_store] = lambda: catalogue
    app.dependency_overrides[get_reading_room_store] = lambda: publications
    app.dependency_overrides[get_destination_paths] = lambda: {"Library": root}
    return publications


def test_catalogue_requires_authentication(client: TestClient, tmp_path: Path) -> None:
    configure(tmp_path)
    assert client.get("/api/reading-room/publications").status_code == 401


def test_catalogue_exposes_only_approved_owned_metadata_and_progress(
    client: TestClient, tmp_path: Path
) -> None:
    publications = configure(tmp_path)
    publications.save_position(ReaderPosition(TEST_USERNAME, ASSET_ID, "chapter-1/p-1"))
    authenticate(client)

    response = client.get("/api/reading-room/publications")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == [
        {
            "id": str(ASSET_ID),
            "title": "The Book",
            "author": "Author",
            "publication_type": "book",
            "edition": "First edition",
            "language": "pl",
            "description": "A description.",
            "publisher": "Vault Press",
            "isbn": "9780000000000",
            "publication_details": None,
            "cover_url": f"/api/reading-room/publications/{ASSET_ID}/cover",
            "chapter_count": 1,
            "progress": {
                "locator": "chapter-1/p-1",
                "character_offset": 0,
                "completed": False,
                "percent": 33,
            },
        }
    ]
    encoded = response.text
    assert "source_pdf" not in encoded
    assert "/vault/" not in encoded
    assert "ocr" not in encoded.casefold()


def test_detail_and_cover_are_approved_and_checksum_verified(
    client: TestClient, tmp_path: Path
) -> None:
    configure(tmp_path)
    authenticate(client)
    detail = client.get(f"/api/reading-room/publications/{ASSET_ID}")
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "private, no-store"
    assert detail.json()["chapters"] == [
        {"locator": "chapter-1", "title": "Chapter One", "level": 1}
    ]
    cover = client.get(f"/api/reading-room/publications/{ASSET_ID}/cover")
    assert cover.status_code == 200
    assert cover.headers["content-type"] == "image/jpeg"
    assert cover.headers["cache-control"] == "private, max-age=3600"
    assert cover.content == b"jpeg-cover"


def test_unpublished_or_unowned_publications_are_not_discoverable(
    client: TestClient, tmp_path: Path
) -> None:
    configure(tmp_path, state="needs_review")
    authenticate(client)
    assert client.get("/api/reading-room/publications").json() == []
    assert client.get(f"/api/reading-room/publications/{ASSET_ID}").status_code == 404

    configure(tmp_path, owner="someone-else")
    assert client.get("/api/reading-room/publications").json() == []
    assert client.get(f"/api/reading-room/publications/{ASSET_ID}").status_code == 404


def test_full_text_search_is_access_scoped_and_returns_stable_reader_locators(
    client: TestClient, tmp_path: Path
) -> None:
    configure(tmp_path)
    assert client.get("/api/reading-room/search", params={"q": "searchable"}).status_code == 401
    authenticate(client)

    english = client.get("/api/reading-room/search", params={"q": "searchable"})
    assert english.status_code == 200
    assert english.headers["cache-control"] == "private, no-store"
    assert english.json()[0]["locator"] == "chapter-1/p-1"
    assert english.json()[0]["publication_id"] == str(ASSET_ID)
    assert "/vault/" not in english.text and "source_pdf" not in english.text

    polish = client.get("/api/reading-room/search", params={"q": "zazolc"})
    assert polish.status_code == 200
    assert polish.json()[0]["locator"] == "chapter-1/p-1"

    configure(tmp_path, state="needs_review")
    assert client.get("/api/reading-room/search", params={"q": "searchable"}).json() == []
    configure(tmp_path, owner="someone-else")
    assert client.get("/api/reading-room/search", params={"q": "searchable"}).json() == []

def test_reader_returns_semantic_blocks_without_stored_html_or_pdf_paths(
    client: TestClient, tmp_path: Path
) -> None:
    configure(tmp_path)
    authenticate(client)

    response = client.get(f"/api/reading-room/publications/{ASSET_ID}/reader")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    document = response.json()
    assert document["preferences"] == {
        "theme": "sepia",
        "font_family": "serif",
        "font_size": 18,
    }
    illustration = next(
        block for block in document["blocks"] if block["block_type"] == "illustration"
    )
    assert illustration["illustration_url"] == (
        f"/api/reading-room/publications/{ASSET_ID}/illustrations/{ILLUSTRATION_ID}"
    )
    assert document["blocks"][-1]["text"] == "A reviewed note."
    assert "content_html" not in response.text
    assert "javascript:" not in response.text
    assert "/vault/" not in response.text
    assert ".pdf" not in response.text.casefold()


def test_reader_position_preferences_and_bookmarks_persist_by_locator(
    client: TestClient, tmp_path: Path
) -> None:
    configure(tmp_path)
    authenticate(client)
    position = client.put(
        f"/api/reading-room/publications/{ASSET_ID}/position",
        json={
            "locator": "chapter-1/p-1",
            "character_offset": 4,
            "completed": False,
            "theme": "dark",
            "font_family": "sans",
            "font_size": 22,
        },
    )
    assert position.status_code == 200
    assert position.json()["percent"] == 33
    bookmark = client.post(
        f"/api/reading-room/publications/{ASSET_ID}/bookmarks",
        json={"locator": "chapter-1/p-1", "character_offset": 4, "label": "Continue"},
    )
    assert bookmark.status_code == 201

    reopened = client.get(f"/api/reading-room/publications/{ASSET_ID}/reader").json()
    assert reopened["position"]["locator"] == "chapter-1/p-1"
    assert reopened["preferences"] == {
        "theme": "dark",
        "font_family": "sans",
        "font_size": 22,
    }
    assert reopened["bookmarks"][0]["label"] == "Continue"
    assert client.delete(
        f"/api/reading-room/publications/{ASSET_ID}/bookmarks/{bookmark.json()['id']}"
    ).status_code == 204
    assert client.get(f"/api/reading-room/publications/{ASSET_ID}/reader").json()[
        "bookmarks"
    ] == []
    assert client.put(
        f"/api/reading-room/publications/{ASSET_ID}/position",
        json={"locator": "missing", "theme": "sepia", "font_family": "serif", "font_size": 18},
    ).status_code == 422


def test_reader_serves_only_owned_checksum_verified_illustrations(
    client: TestClient, tmp_path: Path
) -> None:
    configure(tmp_path)
    authenticate(client)
    response = client.get(
        f"/api/reading-room/publications/{ASSET_ID}/illustrations/{ILLUSTRATION_ID}"
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.content == b"jpeg-illustration"
    assert client.get(
        f"/api/reading-room/publications/{ASSET_ID}/illustrations/{uuid4()}"
    ).status_code == 404


def test_reader_state_search_and_files_are_cross_owner_hidden(
    client: TestClient, tmp_path: Path
) -> None:
    configure(tmp_path, owner="another-owner")
    authenticate(client)
    assert client.get(f"/api/reading-room/publications/{ASSET_ID}/reader").status_code == 404
    assert client.put(
        f"/api/reading-room/publications/{ASSET_ID}/position",
        json={"locator": "chapter-1", "theme": "sepia", "font_family": "serif", "font_size": 18},
    ).status_code == 404
    assert client.post(
        f"/api/reading-room/publications/{ASSET_ID}/bookmarks",
        json={"locator": "chapter-1"},
    ).status_code == 404
    assert client.get(f"/api/reading-room/publications/{ASSET_ID}/cover").status_code == 404
    assert client.get(
        f"/api/reading-room/publications/{ASSET_ID}/illustrations/{ILLUSTRATION_ID}"
    ).status_code == 404
    assert client.get("/api/reading-room/search", params={"q": "searchable"}).json() == []


def test_reader_payload_and_bookmark_limits_fail_closed(
    client: TestClient, tmp_path: Path, monkeypatch
) -> None:
    publications = configure(tmp_path)
    authenticate(client)
    monkeypatch.setattr(reading_room_catalogue, "MAX_READER_TEXT", 10)
    response = client.get(f"/api/reading-room/publications/{ASSET_ID}/reader")
    assert response.status_code == 413
    assert "text limit" in response.json()["detail"]

    monkeypatch.setattr(reading_room_catalogue, "MAX_READER_TEXT", 32 * 1024 * 1024)
    monkeypatch.setattr(reading_room_catalogue, "MAX_BOOKMARKS_PER_PUBLICATION", 1)
    first = client.post(
        f"/api/reading-room/publications/{ASSET_ID}/bookmarks",
        json={"locator": "chapter-1"},
    )
    assert first.status_code == 201
    second = client.post(
        f"/api/reading-room/publications/{ASSET_ID}/bookmarks",
        json={"locator": "chapter-1/p-1"},
    )
    assert second.status_code == 409
    assert len(publications.list_bookmarks(TEST_USERNAME, ASSET_ID)) == 1
