from pathlib import Path
from uuid import UUID

from PIL import Image
from pypdf import PdfWriter

import app.vault_master_reading_extraction as extraction
from app.vault_master_reading_extraction import (
    MAX_PAGE_TEXT_CHARACTERS,
    extract_publication,
)
from app.vault_master import sha256_file


ASSET_ID = UUID("aaaaaaaa-1234-5678-1234-567812345678")


def make_pdf(path: Path, pages: int = 2, *, rotate_second: bool = False) -> None:
    writer = PdfWriter()
    for index in range(pages):
        page = writer.add_blank_page(width=300, height=400)
        if rotate_second and index == 1:
            page.rotate(90)
    with path.open("wb") as handle:
        writer.write(handle)


def test_scanned_pdf_ocr_is_bounded_restart_safe_and_preserves_polish(tmp_path: Path) -> None:
    source = tmp_path / "Stanisław Lem - Solaris.pdf"
    make_pdf(source, 2)
    calls: list[str] = []

    def ocr(image: Path):
        calls.append(image.name)
        assert Image.open(image).format == "PNG"
        return ("Rozdział pierwszy\n\nBył to tekst po polsku i nie był uszkodzony.", 0.91, 12)

    first = extract_publication(asset_id=ASSET_ID, source=source, source_root=tmp_path, expected_sha256=sha256_file(source), author="Stanisław Lem", title="Solaris", workspace_root=tmp_path / "work", florence_ocr=ocr, max_florence_pages=1)
    assert first.pending_pages == (2,)
    assert calls == ["page-0001.png"]
    second = extract_publication(asset_id=ASSET_ID, source=source, source_root=tmp_path, expected_sha256=sha256_file(source), author="Stanisław Lem", title="Solaris", workspace_root=tmp_path / "work", florence_ocr=ocr, max_florence_pages=1)
    assert second.pending_pages == ()
    assert second.snapshot is not None
    assert second.snapshot.metadata.language == "pl"
    assert calls == ["page-0001.png", "page-0002.png"]
    output = (tmp_path / "work" / str(ASSET_ID) / "publication.html").read_text(encoding="utf-8")
    assert "Rozdział" in output and "<script" not in output


def test_rotation_and_duplicate_pages_are_review_issues(tmp_path: Path) -> None:
    source = tmp_path / "Author - Book.pdf"
    make_pdf(source, 2, rotate_second=True)

    result = extract_publication(asset_id=ASSET_ID, source=source, source_root=tmp_path, expected_sha256=sha256_file(source), author="Author", title="Book", workspace_root=tmp_path / "work", florence_ocr=lambda _: ("Chapter one\n\nThe text and the words of the page.", None, 5), max_florence_pages=2)

    assert result.snapshot is not None
    issue_types = {issue.issue_type for issue in result.snapshot.issues}
    assert "incorrect_rotation" in issue_types
    assert "duplicated_page" in issue_types


def test_source_checksum_and_florence_limit_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "Author - Book.pdf"
    make_pdf(source, 1)
    common = dict(asset_id=ASSET_ID, source=source, source_root=tmp_path, author="Author", title="Book", workspace_root=tmp_path / "work", florence_ocr=lambda _: ("text", None, 1))
    try:
        extract_publication(expected_sha256="0" * 64, **common)
        raise AssertionError("checksum mismatch accepted")
    except ValueError as error:
        assert "checksum" in str(error)
    try:
        extract_publication(expected_sha256=sha256_file(source), max_florence_pages=9, **common)
        raise AssertionError("unbounded OCR accepted")
    except ValueError as error:
        assert "limit" in str(error)


def test_malicious_pdf_resource_bounds_fail_before_ocr(tmp_path: Path, monkeypatch) -> None:
    oversized = tmp_path / "Author - Oversized.pdf"
    original_max_source_bytes = extraction.MAX_SOURCE_BYTES
    monkeypatch.setattr(extraction, "MAX_SOURCE_BYTES", 8)
    oversized.write_bytes(b"%PDF-1.7\n")
    calls: list[Path] = []
    try:
        extract_publication(
            asset_id=ASSET_ID, source=oversized, source_root=tmp_path,
            expected_sha256="0" * 64, author="Author", title="Oversized",
            workspace_root=tmp_path / "work", florence_ocr=lambda path: calls.append(path),
        )
        raise AssertionError("oversized source accepted")
    except ValueError as error:
        assert "size" in str(error)
    assert calls == []
    monkeypatch.setattr(extraction, "MAX_SOURCE_BYTES", original_max_source_bytes)

    huge_page = tmp_path / "Author - Huge Page.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100_000, height=100_000)
    with huge_page.open("wb") as handle:
        writer.write(handle)
    try:
        extract_publication(
            asset_id=ASSET_ID, source=huge_page, source_root=tmp_path,
            expected_sha256=sha256_file(huge_page), author="Author", title="Huge Page",
            workspace_root=tmp_path / "work2", florence_ocr=lambda _: ("text", None, 1),
        )
        raise AssertionError("oversized page accepted")
    except ValueError as error:
        assert "dimensions" in str(error)


def test_oversized_ocr_text_is_rejected_without_persistent_evidence(tmp_path: Path) -> None:
    source = tmp_path / "Author - Text Bomb.pdf"
    make_pdf(source, 1)
    try:
        extract_publication(
            asset_id=ASSET_ID, source=source, source_root=tmp_path,
            expected_sha256=sha256_file(source), author="Author", title="Text Bomb",
            workspace_root=tmp_path / "work",
            florence_ocr=lambda _: ("x" * (MAX_PAGE_TEXT_CHARACTERS + 1), None, 1),
            max_florence_pages=1,
        )
        raise AssertionError("oversized OCR text accepted")
    except ValueError as error:
        assert "text" in str(error)
    assert not (tmp_path / "work" / str(ASSET_ID) / "evidence" / "page-0001.json").exists()


def test_embedded_illustrations_become_owned_files(tmp_path: Path) -> None:
    source = tmp_path / "Author - Illustrated.pdf"
    image = Image.new("RGB", (40, 40), "red")
    image.save(source, "PDF")

    result = extract_publication(asset_id=ASSET_ID, source=source, source_root=tmp_path, expected_sha256=sha256_file(source), author="Author", title="Illustrated", workspace_root=tmp_path / "work", florence_ocr=lambda _: ("The image and the text of this illustrated page.", None, 3), max_florence_pages=1)

    assert result.snapshot is not None
    assert result.snapshot.files
    illustration = result.snapshot.files[0]
    assert illustration.role == "illustration"
    assert illustration.vault_path.startswith("/vault/Library/Author/Illustrated/reading/illustrations/")
    assert (tmp_path / "work" / str(ASSET_ID) / "illustrations" / illustration.filename).is_file()
    placement = next(block for block in result.snapshot.blocks if block.block_type == "illustration")
    assert placement.illustration_file_id == illustration.id
    assert placement.source_page == 1
    assert result.snapshot.metadata.reading_mode == "fixed_layout"


def test_metadata_structure_missing_page_and_html_sanitising(tmp_path: Path) -> None:
    source = tmp_path / "Author - Edition.pdf"
    make_pdf(source, 2)
    texts = iter((
        "CONTENTS\n\nPublisher: Safe House\nISBN 978-0-306-40615-7\n\n10\n\n<script>alert(1)</script>",
        "Chapter One\n\n1. A short footnote\n\n12",
    ))
    result = extract_publication(asset_id=ASSET_ID, source=source, source_root=tmp_path, expected_sha256=sha256_file(source), author="Author", title="Edition", workspace_root=tmp_path / "work", florence_ocr=lambda _: (next(texts), 0.95, 2), max_florence_pages=2)

    assert result.snapshot is not None
    assert result.snapshot.metadata.detected["isbn"] == "9780306406157"
    assert result.snapshot.metadata.detected["publisher"] == "Safe House"
    types = {block.block_type for block in result.snapshot.blocks}
    assert {"heading", "chapter", "footnote"}.issubset(types)
    assert "missing_page" in {issue.issue_type for issue in result.snapshot.issues}
    output = (tmp_path / "work" / str(ASSET_ID) / "publication.html").read_text(encoding="utf-8")
    assert "<script>" not in output
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in output
