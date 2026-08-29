from dataclasses import replace
from datetime import datetime, timezone
import os
from uuid import UUID, uuid4

import pytest

from app.vault_master_reading import PublicationBlock, PublicationIssue, PublicationMetadata, PublicationSnapshot
from app.vault_master_reading_review import (
    MemoryPublicationReviewStore,
    PublicationReview,
    caption_illustration,
    correct_page_order,
    correct_review_block,
    correct_review_metadata,
    review_from_document,
    review_document,
    review_publication_issue,
    transition_review,
    PostgresPublicationReviewStore,
    POSTGRES_REVIEW_SCHEMA,
)
from app.vault_master import INCOMING_SOURCE, PostgresVaultMasterStore, ScannedFile
from app.auth_store import Account, PostgresAuthenticationStore


ASSET_ID = UUID("abababab-1234-5678-1234-567812345678")
CHAPTER_ID = UUID("bcbcbcbc-1234-5678-1234-567812345678")


def sample_review(*, mode: str = "reflowable", severity: str = "warning") -> PublicationReview:
    metadata = PublicationMetadata(ASSET_ID, "book", mode, "needs_review", "pl", "reading-html-v1", detected={"author": "Autor", "title": "Tytuł", "page_count": 2})
    chapter = PublicationBlock(CHAPTER_ID, ASSET_ID, "chapter", 0, "page-1/chapter-1", content_text="Rozdział", content_html="<h2>Rozdział</h2>", source_page=1)
    issue = PublicationIssue(uuid4(), ASSET_ID, "likely_ocr_mistake", severity, "open", "Check text", block_id=CHAPTER_ID, source_page=1)
    return PublicationReview(ASSET_ID, "owner", "needs_review", PublicationSnapshot(metadata, (), (chapter,), (issue,)))


def test_review_round_trip_and_revision_guard() -> None:
    review = sample_review()
    assert review_from_document(review_document(review)) == review
    store = MemoryPublicationReviewStore()
    store.save(review)
    with pytest.raises(ValueError, match="stale"):
        store.save(review)


def test_owner_corrections_preserve_polish_and_escape_html() -> None:
    review = correct_review_metadata(sample_review(), "owner", values={"title": "Nowy tytuł", "publisher": "Czytelnik"}, language="pl")
    review = correct_review_block(review, "owner", CHAPTER_ID, text="Rozdział <script>alert(1)</script>", block_type="chapter")
    assert review.snapshot.metadata.user_overrides["title"] == "Nowy tytuł"
    block = review.snapshot.blocks[0]
    assert block.content_text.endswith("</script>")
    assert "<script>" not in (block.content_html or "")


def test_page_order_issue_resolution_and_publish_gate() -> None:
    review = sample_review(severity="critical")
    review = correct_page_order(review, "owner", (2, 1), {2: 90})
    assert review.snapshot.metadata.user_overrides["page_order"] == [2, 1]
    with pytest.raises(ValueError, match="Critical"):
        transition_review(review, "owner", "publish")
    review = review_publication_issue(review, "owner", review.snapshot.issues[0].id, "resolved")
    assert transition_review(review, "owner", "publish").state == "ready_to_publish"


def test_fixed_layout_and_accepted_critical_issue_remain_blocked() -> None:
    fixed = sample_review(mode="fixed_layout")
    with pytest.raises(ValueError, match="reflowable"):
        transition_review(fixed, "owner", "publish")
    critical = sample_review(severity="critical")
    accepted = review_publication_issue(critical, "owner", critical.snapshot.issues[0].id, "accepted")
    with pytest.raises(ValueError, match="Critical"):
        transition_review(accepted, "owner", "publish")


def test_review_schema_is_additive_and_source_bound() -> None:
    assert "CREATE TABLE IF NOT EXISTS vault_publication_reviews" in POSTGRES_REVIEW_SCHEMA
    assert "REFERENCES vault_master_items(id) ON DELETE CASCADE" in POSTGRES_REVIEW_SCHEMA
    assert "ready_to_publish" in POSTGRES_REVIEW_SCHEMA


def test_postgres_review_survives_store_recreation() -> None:
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    authentication = PostgresAuthenticationStore(conninfo)
    owner = authentication.get_account("owner")
    if owner is None:
        owner = Account(
            username="owner",
            display_name="Owner",
            email=None,
            password_hash="test-hash",
            role="user",
            active=True,
            password_change_required=False,
            created_at=datetime.now(timezone.utc),
            last_sign_in_at=None,
        )
        authentication.create_account(owner)
    vault = PostgresVaultMasterStore(conninfo)
    vault.reset()
    try:
        batch = vault.create_batch(INCOMING_SOURCE, "/vault/Arrival Hall")
        vault.record_file(
            batch,
            INCOMING_SOURCE,
            ScannedFile(
                "/vault/Arrival Hall/Author - Title.pdf",
                "Author - Title.pdf",
                "Author - Title.pdf",
                123,
                "application/pdf",
                datetime.now(timezone.utc),
                "a" * 64,
                {},
                owner_user_id=owner.user_id,
            ),
        )
        source_id = vault.list_items()[0].id
        original = replace(sample_review(), source_item_id=source_id, snapshot=replace(sample_review().snapshot, metadata=replace(sample_review().snapshot.metadata, asset_id=source_id), blocks=tuple(replace(block, asset_id=source_id) for block in sample_review().snapshot.blocks), issues=tuple(replace(issue, asset_id=source_id) for issue in sample_review().snapshot.issues)))
        store = PostgresPublicationReviewStore(conninfo)
        store.save(original)
        assert PostgresPublicationReviewStore(conninfo).get(source_id, "OWNER") == original
    finally:
        vault.reset()
