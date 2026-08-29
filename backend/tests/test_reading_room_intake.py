from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app.incoming import get_incoming_path
from app.main import app
from app.reading_room_intake import (
    build_publication_bundles,
    parse_publication_filename,
)
from app.vault_master import (
    INCOMING_SOURCE,
    MemoryVaultMasterStore,
    ScannedFile,
    get_vault_master_store,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def add_item(
    store: MemoryVaultMasterStore,
    batch_id: UUID,
    filename: str,
    checksum: str,
):
    return store.record_file(
        batch_id,
        INCOMING_SOURCE,
        ScannedFile(
            source_path=f"/vault/Arrival Hall/{filename}",
            relative_path=filename,
            filename=filename,
            size_bytes=100,
            mime_type=(
                "application/pdf"
                if filename.casefold().endswith(".pdf")
                else "image/jpeg"
            ),
            modified_at=datetime.now(timezone.utc),
            sha256=checksum,
            metadata={},
        ),
    )


def authenticate(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200


def test_parser_retains_polish_unicode_and_requires_approved_names() -> None:
    parsed = parse_publication_filename("Stanisław Lem - Solaris.pdf")

    assert parsed is not None
    assert (parsed.author, parsed.title, parsed.role) == (
        "Stanisław Lem",
        "Solaris",
        "source",
    )
    assert parse_publication_filename("Solaris.pdf") is None
    assert parse_publication_filename("Stanisław Lem - Solaris - cover.jpg") is None


def test_imperfect_supplier_names_and_png_cover_form_review_bundle() -> None:
    store = MemoryVaultMasterStore()
    batch_id = store.create_batch(INCOMING_SOURCE, "/vault/Arrival Hall")
    source = add_item(
        store,
        batch_id,
        "Karen Traviss-Aspho Fields - Gears of War.pdf",
        "2" * 64,
    )
    cover = add_item(
        store,
        batch_id,
        "Karen Traviss-Aspho Fields-front.png",
        "3" * 64,
    )

    bundles = build_publication_bundles(store.list_items())

    assert len(bundles) == 1
    assert bundles[0].source_item_ids == (source.id,)
    assert bundles[0].front_cover_item_ids == (cover.id,)
    assert bundles[0].issues == ()
    assert bundles[0].review_status == "review_required"


def test_unrelated_pdf_from_another_upload_is_not_a_publication_candidate() -> None:
    store = MemoryVaultMasterStore()
    old_batch = store.create_batch(INCOMING_SOURCE, "/vault/Arrival Hall")
    add_item(store, old_batch, "Scanned_20260802-1426 (1).pdf", "4" * 64)
    book_batch = store.create_batch(INCOMING_SOURCE, "/vault/Arrival Hall")
    source = add_item(store, book_batch, "Karen Traviss-Aspho Fields.pdf", "5" * 64)
    add_item(store, book_batch, "Karen Traviss-Aspho Fields-front.jpg", "6" * 64)

    bundles = build_publication_bundles(store.list_items())

    assert len(bundles) == 1
    assert bundles[0].source_item_ids == (source.id,)


def test_bundles_flag_ambiguous_missing_and_duplicate_scans() -> None:
    store = MemoryVaultMasterStore()
    batch_id = store.create_batch(INCOMING_SOURCE, "/vault/Arrival Hall")
    source = add_item(store, batch_id, "Olga Tokarczuk - Bieguni.pdf", "a" * 64)
    add_item(store, batch_id, "Olga Tokarczuk - Bieguni - front.jpg", "b" * 64)
    add_item(store, batch_id, "OLGA TOKARCZUK - Bieguni - front.jpeg", "b" * 64)
    add_item(store, batch_id, "Wisława Szymborska - Wiersze - back.jpg", "c" * 64)

    bundles = build_publication_bundles(store.list_items())

    bieguni = next(bundle for bundle in bundles if source.id in bundle.source_item_ids)
    assert bieguni.review_status == "review_required"
    assert "ambiguous_front_cover" in bieguni.issues
    assert "duplicate_checksum" in bieguni.issues
    assert "normalised_identity_collision" in bieguni.issues
    orphan = next(bundle for bundle in bundles if bundle.title == "Wiersze")
    assert orphan.issues == ("missing_source_pdf",)


def test_owner_can_correct_pdf_identity_without_changing_item_state(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    batch_id = store.create_batch(INCOMING_SOURCE, str(tmp_path))
    source = add_item(store, batch_id, "Author - Tytul.pdf", "d" * 64)
    original_state = source.state
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_incoming_path] = lambda: tmp_path
    authenticate(client)

    response = client.patch(
        f"/api/vault-master/publication-bundles/{source.id}",
        json={"author": "Autor", "title": "Tytuł"},
    )

    assert response.status_code == 200
    assert response.json()["publication_rule"] == "owner_review_required"
    assert response.json()["bundles"][0]["author"] == "Autor"
    assert response.json()["bundles"][0]["title"] == "Tytuł"
    updated = store.get_item(source.id)
    assert updated is not None
    assert updated.state == original_state
    assert updated.metadata_overrides["reading_room_author"] == "Autor"
    assert updated.metadata_overrides["reading_room_title"] == "Tytuł"


def test_pdf_correction_keeps_matching_covers_in_the_same_bundle() -> None:
    store = MemoryVaultMasterStore()
    batch_id = store.create_batch(INCOMING_SOURCE, "/vault/Arrival Hall")
    source = add_item(store, batch_id, "Author - Title.pdf", "f" * 64)
    cover = add_item(store, batch_id, "Author - Title - front.jpg", "1" * 64)
    store.update_metadata_overrides(
        source.id,
        {
            "reading_room_author": "Corrected Author",
            "reading_room_title": "Corrected Title",
        },
        TEST_USERNAME,
    )

    bundles = build_publication_bundles(store.list_items())

    assert len(bundles) == 1
    assert bundles[0].front_cover_item_ids == (cover.id,)
    assert (bundles[0].author, bundles[0].title) == (
        "Corrected Author",
        "Corrected Title",
    )


def test_cover_cannot_anchor_identity_correction(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore()
    batch_id = store.create_batch(INCOMING_SOURCE, str(tmp_path))
    cover = add_item(store, batch_id, "Author - Title - front.jpg", "e" * 64)
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_incoming_path] = lambda: tmp_path
    authenticate(client)

    response = client.patch(
        f"/api/vault-master/publication-bundles/{cover.id}",
        json={"author": "Author", "title": "Corrected"},
    )

    assert response.status_code == 409
