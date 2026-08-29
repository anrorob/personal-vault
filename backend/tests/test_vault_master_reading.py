from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.vault_master_reading import (
    MemoryReadingRoomStore,
    PostgresReadingRoomStore,
    POSTGRES_SCHEMA,
    PublicationBlock,
    PublicationFile,
    PublicationIssue,
    PublicationMetadata,
    PublicationSnapshot,
    ReaderBookmark,
    ReaderPosition,
    compose_publication_metadata,
    publication_sidecar_document,
    read_publication_sidecar,
    write_publication_sidecar,
)
from app.vault_master import CataloguedAsset, PostgresVaultMasterStore


ASSET_ID = UUID("12345678-1234-5678-1234-567812345678")
CHAPTER_ID = UUID("22345678-1234-5678-1234-567812345678")
PARAGRAPH_ID = UUID("32345678-1234-5678-1234-567812345678")
SOURCE_ID = UUID("42345678-1234-5678-1234-567812345678")


def snapshot() -> PublicationSnapshot:
    metadata = PublicationMetadata(
        asset_id=ASSET_ID,
        publication_type="book",
        reading_mode="reflowable",
        extraction_state="needs_review",
        language="pl",
        content_version="reading-html-v1",
        detected={"title": "Lalka", "author": "Bolesław Prus", "publisher": "Gebethner i Wolff"},
        imported={"publisher": "Gebethner i Wolff, Warszawa"},
        user_overrides={"title": "Lalka — tom pierwszy"},
        effective={},
        provenance={"author": {"source": "filename", "confidence": 1.0}},
    )
    source = PublicationFile(
        SOURCE_ID, ASSET_ID, "source_pdf", "/vault/Library/Lalka/source/Lalka.pdf",
        "Lalka.pdf", "application/pdf", "a" * 64, True,
    )
    chapter = PublicationBlock(
        CHAPTER_ID, ASSET_ID, "chapter", 0, "chapter-1", content_text="Rozdział pierwszy", source_page=5,
    )
    paragraph = PublicationBlock(
        PARAGRAPH_ID, ASSET_ID, "paragraph", 1, "chapter-1/p-1", parent_id=CHAPTER_ID,
        content_html="<p>W początkach roku 1878...</p>", content_text="W początkach roku 1878...",
        source_page=5, source_bbox=(0.1, 0.2, 0.8, 0.4),
    )
    issue = PublicationIssue(
        uuid4(), ASSET_ID, "uncertain_character", "warning", "open",
        "Uncertain Polish character near the opening paragraph", PARAGRAPH_ID, 5,
        {"candidate": "ą", "confidence": 0.62},
    )
    return PublicationSnapshot(metadata, (source,), (chapter, paragraph), (issue,))


def test_metadata_layers_compose_with_user_override_precedence() -> None:
    composed = compose_publication_metadata(snapshot().metadata)
    assert composed.effective == {
        "title": "Lalka — tom pierwszy",
        "author": "Bolesław Prus",
        "publisher": "Gebethner i Wolff, Warszawa",
    }
    assert composed.provenance["title"] == "user_override"
    assert composed.provenance["publisher"] == "imported"
    assert composed.provenance["author"] == "detected"


def test_memory_store_requires_a_canonical_asset_and_preserves_overrides_on_rescan(tmp_path: Path) -> None:
    store = MemoryReadingRoomStore(asset_exists=lambda asset_id: asset_id == ASSET_ID, sidecar_root=tmp_path)
    first = store.save_publication(snapshot())
    rescanned_metadata = PublicationMetadata(
        **{**first.metadata.__dict__, "detected": {**first.metadata.detected, "title": "Lalka"}, "effective": {}}
    )
    rescanned = store.save_publication(PublicationSnapshot(rescanned_metadata, first.files, first.blocks, first.issues))
    assert rescanned.metadata.effective["title"] == "Lalka — tom pierwszy"
    assert read_publication_sidecar(tmp_path / "publication-sidecars" / f"{ASSET_ID}.json") == rescanned
    with pytest.raises(ValueError, match="canonical catalogue"):
        store.save_publication(PublicationSnapshot(PublicationMetadata(uuid4(), "book", "reflowable", "pending")))


def test_publication_sidecar_round_trip_retains_polish_text_structure_and_review_evidence(tmp_path: Path) -> None:
    original = PublicationSnapshot(compose_publication_metadata(snapshot().metadata), snapshot().files, snapshot().blocks, snapshot().issues)
    path = write_publication_sidecar(original, tmp_path)
    restored = read_publication_sidecar(path)
    assert restored == original
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["publication"]["effective"]["author"] == "Bolesław Prus"
    assert document["blocks"][1]["content_text"].startswith("W początkach")
    assert document["issues"][0]["evidence"]["candidate"] == "ą"
    assert "reader" not in document and "bookmarks" not in document


def test_sidecar_rejects_filename_identity_mismatch(tmp_path: Path) -> None:
    path = write_publication_sidecar(snapshot(), tmp_path)
    renamed = path.with_name(f"{uuid4()}.json")
    path.rename(renamed)
    with pytest.raises(ValueError, match="filename"):
        read_publication_sidecar(renamed)


def test_snapshot_rejects_missing_hierarchy_and_duplicate_locators() -> None:
    base = snapshot()
    orphan = PublicationBlock(uuid4(), ASSET_ID, "paragraph", 3, "orphan", parent_id=uuid4())
    with pytest.raises(ValueError, match="parent"):
        PublicationSnapshot(base.metadata, base.files, base.blocks + (orphan,), base.issues)
    duplicate = PublicationBlock(uuid4(), ASSET_ID, "paragraph", 4, "chapter-1")
    with pytest.raises(ValueError, match="locators"):
        PublicationSnapshot(base.metadata, base.files, base.blocks + (duplicate,), base.issues)


def test_reader_state_and_bookmarks_are_owner_scoped_and_use_stable_locators() -> None:
    store = MemoryReadingRoomStore(asset_exists=lambda asset_id: asset_id == ASSET_ID)
    position = ReaderPosition("Owner", ASSET_ID, "chapter-1/p-1", 12, False, {"theme": "sepia", "font_size": 19})
    store.save_position(position)
    assert store.get_position("owner", ASSET_ID) == position
    assert store.get_position("another-user", ASSET_ID) is None
    bookmark = ReaderBookmark(uuid4(), "Owner", ASSET_ID, "chapter-1/p-1", 12, "Opening")
    store.add_bookmark(bookmark)
    assert store.list_bookmarks("owner", ASSET_ID) == [bookmark]
    assert store.list_bookmarks("another-user", ASSET_ID) == []
    assert not store.delete_bookmark("another-user", ASSET_ID, bookmark.id)
    assert store.delete_bookmark("owner", ASSET_ID, bookmark.id)
    assert store.list_bookmarks("owner", ASSET_ID) == []


def test_invalid_publication_contract_values_fail_closed() -> None:
    with pytest.raises(ValueError, match="publication type"):
        PublicationMetadata(ASSET_ID, "pdf", "reflowable", "pending")
    with pytest.raises(ValueError, match="language"):
        PublicationMetadata(ASSET_ID, "book", "reflowable", "pending", "Polish")
    with pytest.raises(ValueError, match="locator"):
        ReaderPosition("owner", ASSET_ID, "<script>")
    with pytest.raises(ValueError, match="checksum"):
        PublicationFile(uuid4(), ASSET_ID, "source_pdf", "/vault/Library/book.pdf", "book.pdf", "application/pdf", "bad", True)


def test_postgres_schema_is_additive_and_enforces_canonical_asset_foreign_keys() -> None:
    assert "REFERENCES vault_assets(id) ON DELETE CASCADE" in POSTGRES_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS vault_publications" in POSTGRES_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS vault_publication_blocks" in POSTGRES_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS vault_publication_issues" in POSTGRES_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS user_reading_state" in POSTGRES_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS user_reading_bookmarks" in POSTGRES_SCHEMA
    assert "vault_publication_blocks_search_idx" in POSTGRES_SCHEMA
    assert "vault_search_normalize" in POSTGRES_SCHEMA
    assert "source_pdf" in POSTGRES_SCHEMA
    assert "fixed_layout" in POSTGRES_SCHEMA


def test_product_boundary_has_no_pdf_florence_or_catalogue_dependencies() -> None:
    # Increment 1 defines only a Vault Master producer/persistence module. The
    # future product section must never import this producer module directly.
    module = Path(__file__).parents[1] / "app" / "vault_master_reading.py"
    source = module.read_text(encoding="utf-8")
    assert "PdfReader" not in source
    assert "pv-florence2" not in source
    assert "FastAPI" not in source


def test_postgres_publication_reader_state_and_bookmarks_survive_store_recreation(tmp_path: Path) -> None:
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    catalogue = PostgresVaultMasterStore(conninfo)
    catalogue.reset()
    try:
        asset = CataloguedAsset(
            id=ASSET_ID, asset_type="Library", display_title="Lalka",
            captured_on=None, location=None, vault_path="/vault/Library/Lalka/Lalka.pdf",
            filename="Lalka.pdf", size_bytes=123, mime_type="application/pdf",
            sha256="a" * 64, metadata={}, metadata_provenance={}, owner_username="owner",
        )
        catalogue.restore_catalogued_asset(asset, "owner")
        store = PostgresReadingRoomStore(conninfo, sidecar_root=tmp_path / "metadata")
        saved = store.save_publication(snapshot())
        position = ReaderPosition("Owner", ASSET_ID, "chapter-1/p-1", 7, False, {"theme": "dark"})
        bookmark = ReaderBookmark(uuid4(), "Owner", ASSET_ID, "chapter-1/p-1", 7, "Początek")
        store.save_position(position)
        store.add_bookmark(bookmark)

        recreated = PostgresReadingRoomStore(conninfo)
        assert recreated.get_publication(ASSET_ID) == saved
        restored_position = recreated.get_position("owner", ASSET_ID)
        assert restored_position is not None
        assert restored_position.locator == position.locator
        assert restored_position.preferences == {"theme": "dark"}
        assert recreated.list_bookmarks("owner", ASSET_ID)[0].label == "Początek"
        assert recreated.delete_bookmark("owner", ASSET_ID, bookmark.id)
        assert recreated.list_bookmarks("owner", ASSET_ID) == []

        approved = PublicationSnapshot(
            replace(saved.metadata, extraction_state="approved"),
            saved.files,
            saved.blocks,
            saved.issues,
        )
        recreated.save_publication(approved)
        search_hits = recreated.search_publications("Lalka", {ASSET_ID})
        assert search_hits
        assert search_hits[0].asset_id == ASSET_ID
        assert search_hits[0].locator == "chapter-1"
    finally:
        catalogue.reset()
