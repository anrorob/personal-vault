"""Restart-safe Vault Master extraction of reviewed Reading Room PDFs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import html
import json
import os
from pathlib import Path
import re
import statistics
from uuid import UUID, uuid5, NAMESPACE_URL

import pypdfium2 as pdfium
from pypdf import PdfReader
from pypdf.errors import FileNotDecryptedError, PdfReadError

from app.vault_master import sha256_file
from app.vault_master_reading import (
    PublicationBlock,
    PublicationFile,
    PublicationIssue,
    PublicationMetadata,
    PublicationSnapshot,
)
from app.reading_room_storage import publication_directory


PIPELINE_VERSION = "reading-extraction-v1"
HTML_VERSION = "reading-html-v1"
MAX_PAGES = 4000
MAX_FLORENCE_PAGES_PER_RUN = 8
MAX_SOURCE_BYTES = 2 * 1024 * 1024 * 1024
MAX_PAGE_RENDER_PIXELS = 200_000_000
MAX_PAGE_TEXT_CHARACTERS = 250_000
MAX_ILLUSTRATION_BYTES = 64 * 1024 * 1024
MAX_TOTAL_ILLUSTRATION_BYTES = 1024 * 1024 * 1024
MAX_ILLUSTRATIONS = 20_000
MIN_EMBEDDED_TEXT_CHARACTERS = 80
POLISH_MARKERS = frozenset("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")
ENGLISH_WORDS = frozenset({"the", "and", "of", "to", "in", "that", "is", "for"})
POLISH_WORDS = frozenset({"i", "w", "na", "z", "że", "do", "się", "nie", "jest"})


@dataclass(frozen=True)
class ExtractionProgress:
    asset_id: UUID
    page_count: int
    completed_pages: int
    pending_pages: tuple[int, ...]
    snapshot: PublicationSnapshot | None


FlorenceOcr = Callable[[Path], tuple[str, float | None, int]]


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _regular_source(source: Path, source_root: Path, expected_sha256: str) -> Path:
    root = source_root.resolve(strict=True)
    resolved = source.resolve(strict=True)
    if source.is_symlink() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Publication source must be a regular file inside its approved root")
    if resolved.suffix.casefold() != ".pdf":
        raise ValueError("Publication source must be a PDF")
    if resolved.stat().st_size < 1 or resolved.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Publication PDF size is outside the approved bound")
    if sha256_file(resolved) != expected_sha256:
        raise ValueError("Publication source checksum does not match its reviewed record")
    return resolved


def _page_id(asset_id: UUID, page: int, kind: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"personal-vault:{PIPELINE_VERSION}:{asset_id}:{page}:{kind}")


def _normalise_text(value: str) -> str:
    if len(value) > MAX_PAGE_TEXT_CHARACTERS:
        raise ValueError("Publication page text exceeds the approved bound")
    value = value.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in value.splitlines()).strip()


def _language(text: str) -> str | None:
    words = re.findall(r"[^\W\d_]+", text.casefold(), flags=re.UNICODE)
    if not words:
        return None
    polish = sum(word in POLISH_WORDS for word in words) + sum(ch in POLISH_MARKERS for ch in text) * 2
    english = sum(word in ENGLISH_WORDS for word in words)
    if polish > english and polish >= 2:
        return "pl"
    if english >= 2:
        return "en"
    return None


def _render_page(document: pdfium.PdfDocument, page_number: int, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    bitmap = document[page_number - 1].render(scale=2, rotation=0)
    try:
        bitmap.to_pil().save(temporary, format="PNG", optimize=True)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def render_exact_page(
    source: Path,
    source_root: Path,
    expected_sha256: str,
    page_number: int,
    destination: Path,
) -> Path:
    """Render one checksum-verified source page for private Arrival Hall review."""
    source = _regular_source(source, source_root, expected_sha256)
    reader = PdfReader(source, strict=False)
    if reader.is_encrypted:
        raise ValueError("Encrypted publication PDFs cannot be rendered")
    _validate_page_dimensions(reader)
    document = pdfium.PdfDocument(source)
    try:
        if page_number < 1 or page_number > len(document):
            raise ValueError("Publication source page is outside the document")
        if not destination.exists():
            _render_page(document, page_number, destination)
    finally:
        document.close()
    return destination


def _validate_page_dimensions(reader: PdfReader) -> None:
    for page in reader.pages:
        width = max(0.0, float(page.mediabox.width)) * 2
        height = max(0.0, float(page.mediabox.height)) * 2
        if width < 1 or height < 1 or width * height > MAX_PAGE_RENDER_PIXELS:
            raise ValueError("Publication PDF page dimensions are outside the approved bound")


def _page_document(
    page_number: int,
    embedded_text: str,
    rotation: int,
    image_count: int,
    ocr_text: str | None = None,
    confidence: float | None = None,
    processing_ms: int | None = None,
) -> dict[str, object]:
    selected = _normalise_text(ocr_text if ocr_text is not None else embedded_text)
    return {
        "schema": "personal-vault.reading-page",
        "version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "page": page_number,
        "embedded_text": _normalise_text(embedded_text),
        "ocr_text": _normalise_text(ocr_text) if ocr_text is not None else None,
        "selected_text": selected,
        "text_source": "florence" if ocr_text is not None else "embedded_pdf",
        "ocr_confidence": confidence,
        "processing_ms": processing_ms,
        "rotation": rotation,
        "image_count": image_count,
    }


def _blocks(
    asset_id: UUID,
    pages: list[dict[str, object]],
    illustrations: tuple[PublicationFile, ...],
) -> tuple[PublicationBlock, ...]:
    blocks: list[PublicationBlock] = []
    ordinal = 0
    chapter_id: UUID | None = None
    for page in pages:
        page_number = int(page["page"])
        text = str(page["selected_text"])
        marker_id = _page_id(asset_id, page_number, "marker")
        blocks.append(PublicationBlock(marker_id, asset_id, "page_marker", ordinal, f"page-{page_number}", source_page=page_number))
        ordinal += 1
        for illustration in illustrations:
            if not illustration.filename.startswith(f"page-{page_number:04d}-"):
                continue
            blocks.append(PublicationBlock(
                _page_id(asset_id, page_number, f"illustration-{illustration.id}"),
                asset_id, "illustration", ordinal,
                f"page-{page_number}/illustration-{illustration.ordinal + 1}",
                content_html=(
                    '<figure><img src="illustrations/'
                    + html.escape(illustration.filename, quote=True)
                    + '" alt=""></figure>'
                ),
                source_page=page_number,
                illustration_file_id=illustration.id,
                metadata={"placement": "source_page", "requires_caption_review": True},
            ))
            ordinal += 1
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n|(?<=\.)\s*\n", text) if part.strip()]
        for index, paragraph in enumerate(paragraphs, 1):
            short = len(paragraph) <= 100 and len(paragraph.splitlines()) == 1
            heading = bool(re.match(r"^(chapter|rozdział|część|part)\b", paragraph, re.I))
            contents = bool(re.match(r"^(contents|table of contents|spis treści)\b", paragraph, re.I))
            footnote = bool(re.match(r"^(?:\[?\d{1,3}\]?|[*†‡])[.)]?\s+\S", paragraph)) and len(paragraph) <= 500
            caption = bool(re.match(r"^(?:fig(?:ure)?|illustration|rys(?:unek)?)[ .:\-]+", paragraph, re.I))
            block_type = (
                "chapter" if heading else "footnote" if footnote else
                "caption" if caption else "heading" if contents or (short and paragraph.isupper())
                else "paragraph"
            )
            block_id = _page_id(asset_id, page_number, f"{block_type}-{index}")
            if block_type == "chapter":
                chapter_id = block_id
            locator = f"page-{page_number}/{block_type}-{index}"
            tag = "h2" if block_type == "chapter" else ("h3" if block_type == "heading" else "p")
            blocks.append(PublicationBlock(
                block_id, asset_id, block_type, ordinal, locator,
                parent_id=None if block_type == "chapter" else chapter_id,
                content_html=f"<{tag}>{html.escape(paragraph)}</{tag}>",
                content_text=paragraph, source_page=page_number,
                metadata={"text_source": page["text_source"], "contents_page": contents},
            ))
            ordinal += 1
    return tuple(blocks)


def _extract_illustrations(
    asset_id: UUID,
    reader: PdfReader,
    workspace: Path,
    author: str,
    title: str,
) -> tuple[PublicationFile, ...]:
    """Extract embedded image streams as owned files; never reference PDF internals."""
    files: list[PublicationFile] = []
    seen: set[str] = set()
    total_bytes = 0
    target_root = publication_directory(author, title)
    for page_number, page in enumerate(reader.pages, 1):
        try:
            images = page.images
        except Exception:
            continue
        for image_number, image in enumerate(images, 1):
            data = bytes(image.data)
            if len(data) > MAX_ILLUSTRATION_BYTES:
                raise ValueError("Publication illustration exceeds the approved size bound")
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_ILLUSTRATION_BYTES or len(files) >= MAX_ILLUSTRATIONS:
                raise ValueError("Publication illustrations exceed the approved aggregate bound")
            digest = hashlib.sha256(data).hexdigest()
            if not data or digest in seen:
                continue
            seen.add(digest)
            suffix = Path(image.name).suffix.casefold()
            if suffix not in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff"}:
                suffix = ".bin"
            filename = f"page-{page_number:04d}-image-{image_number:02d}{suffix}"
            destination = workspace / "illustrations" / filename
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part")
            try:
                with temporary.open("xb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
            mime = {
                ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                ".gif": "image/gif", ".webp": "image/webp", ".tif": "image/tiff",
                ".tiff": "image/tiff",
            }.get(suffix, "application/octet-stream")
            vault_path = "/vault/Library/" + str(target_root / "reading" / "illustrations" / filename)
            files.append(PublicationFile(
                _page_id(asset_id, page_number, f"illustration-file-{image_number}"),
                asset_id, "illustration", vault_path, filename, mime, digest, False,
                len(files),
            ))
    return tuple(files)


def _publication_facts(text: str) -> dict[str, object]:
    facts: dict[str, object] = {}
    isbn = re.search(r"\bISBN(?:-1[03])?[ \t]*:?[ \t-]*((?:97[89][ \t-]*)?[0-9][0-9Xx \t-]{8,20})", text)
    if isbn:
        compact = re.sub(r"[^0-9Xx]", "", isbn.group(1)).upper()
        if len(compact) in {10, 13}:
            facts["isbn"] = compact
    publisher = re.search(r"(?im)^\s*(?:publisher|wydawnictwo)\s*[:—-]\s*(.{2,160})$", text)
    if publisher:
        facts["publisher"] = publisher.group(1).strip()
    edition = re.search(r"(?im)^\s*((?:first|second|third|\d+(?:st|nd|rd|th)) edition|wydanie\s+[^\n]{1,80})\s*$", text)
    if edition:
        facts["edition"] = edition.group(1).strip()
    return facts


def _issues(asset_id: UUID, pages: list[dict[str, object]], blocks: tuple[PublicationBlock, ...]) -> tuple[PublicationIssue, ...]:
    issues: list[PublicationIssue] = []
    hashes: dict[str, int] = {}
    printed_pages: list[tuple[int, int]] = []
    for page in pages:
        number = int(page["page"])
        text = str(page["selected_text"])
        candidates = re.findall(r"(?m)^\s*(\d{1,4})\s*$", text)
        if candidates:
            printed_pages.append((number, int(candidates[-1])))
        digest = hashlib.sha256(re.sub(r"\s+", "", text.casefold()).encode("utf-8")).hexdigest() if text else ""
        if digest and digest in hashes:
            issues.append(PublicationIssue(_page_id(asset_id, number, "duplicate"), asset_id, "duplicated_page", "critical", "open", f"Page text duplicates page {hashes[digest]}", source_page=number, evidence={"matching_page": hashes[digest]}))
        elif digest:
            hashes[digest] = number
        if not text:
            issues.append(PublicationIssue(_page_id(asset_id, number, "unreadable"), asset_id, "unreadable_passage", "critical", "open", "No readable text was extracted", source_page=number, evidence={"text_source": page["text_source"]}))
        if int(page["rotation"]) % 360:
            issues.append(PublicationIssue(_page_id(asset_id, number, "rotation"), asset_id, "incorrect_rotation", "warning", "open", f"Source page rotation is {page['rotation']} degrees", source_page=number, evidence={"rotation": page["rotation"]}))
        if "�" in text or re.search(r"\b\w*[|]{1,}\w*\b", text):
            issues.append(PublicationIssue(_page_id(asset_id, number, "ocr"), asset_id, "likely_ocr_mistake", "warning", "open", "Extracted text contains suspicious OCR characters", source_page=number, evidence={"text_source": page["text_source"]}))
        confidence = page.get("ocr_confidence")
        if isinstance(confidence, (int, float)) and confidence < 0.75:
            issues.append(PublicationIssue(_page_id(asset_id, number, "uncertain"), asset_id, "uncertain_character", "warning", "open", "OCR confidence is below the review threshold", source_page=number, evidence={"confidence": confidence}))
    for (source_a, printed_a), (source_b, printed_b) in zip(printed_pages, printed_pages[1:]):
        if printed_b > printed_a + 1:
            issues.append(PublicationIssue(_page_id(asset_id, source_b, "missing"), asset_id, "missing_page", "critical", "open", f"Printed page sequence jumps from {printed_a} to {printed_b}", source_page=source_b, evidence={"previous_source_page": source_a, "previous_printed_page": printed_a, "printed_page": printed_b}))
        elif printed_b <= printed_a:
            issues.append(PublicationIssue(_page_id(asset_id, source_b, "order"), asset_id, "uncertain_reading_order", "warning", "open", "Printed page sequence is not increasing", source_page=source_b, evidence={"previous_printed_page": printed_a, "printed_page": printed_b}))
    return tuple(issues)


def build_structured_html(blocks: tuple[PublicationBlock, ...], language: str | None) -> str:
    body = "\n".join(block.content_html for block in blocks if block.content_html)
    lang = html.escape(language or "und", quote=True)
    return f'<!doctype html>\n<html lang="{lang}"><head><meta charset="utf-8"><meta name="pv-content-version" content="{HTML_VERSION}"><title>Publication</title></head><body>\n{body}\n</body></html>\n'


def extract_publication(
    *, asset_id: UUID, source: Path, source_root: Path, expected_sha256: str,
    author: str, title: str, workspace_root: Path, florence_ocr: FlorenceOcr,
    max_florence_pages: int = MAX_FLORENCE_PAGES_PER_RUN,
) -> ExtractionProgress:
    """Advance one extraction by a bounded number of OCR pages and resume safely."""
    if max_florence_pages < 0 or max_florence_pages > MAX_FLORENCE_PAGES_PER_RUN:
        raise ValueError("Florence page limit is outside the approved bound")
    source = _regular_source(source, source_root, expected_sha256)
    workspace = workspace_root / str(asset_id)
    pages_root = workspace / "pages"
    evidence_root = workspace / "evidence"
    workspace.mkdir(parents=True, exist_ok=True)
    try:
        reader = PdfReader(source, strict=False)
        if reader.is_encrypted:
            raise ValueError("Encrypted publication PDFs cannot be extracted")
        page_count = len(reader.pages)
    except (OSError, PdfReadError, FileNotDecryptedError) as error:
        raise ValueError("Publication PDF is invalid or unreadable") from error
    if page_count < 1 or page_count > MAX_PAGES:
        raise ValueError("Publication PDF page count is outside the approved bound")
    _validate_page_dimensions(reader)
    document = pdfium.PdfDocument(source)
    manifest_path = workspace / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            document.close()
            raise ValueError("Extraction workspace manifest is invalid") from error
        if (
            manifest.get("pipeline_version") != PIPELINE_VERSION
            or manifest.get("asset_id") != str(asset_id)
            or manifest.get("source_sha256") != expected_sha256
            or manifest.get("page_count") != page_count
        ):
            document.close()
            raise ValueError("Extraction workspace does not match this source")
    else:
        _atomic_json(manifest_path, {"pipeline_version": PIPELINE_VERSION, "asset_id": str(asset_id), "source_sha256": expected_sha256, "page_count": page_count, "pending_pages": list(range(1, page_count + 1))})
    pending: list[int] = []
    page_documents: list[dict[str, object]] = []
    try:
        for index, pdf_page in enumerate(reader.pages, 1):
            evidence = evidence_root / f"page-{index:04d}.json"
            if evidence.exists():
                page_documents.append(json.loads(evidence.read_text(encoding="utf-8")))
                continue
            embedded = _normalise_text(pdf_page.extract_text() or "")
            rotation = int(pdf_page.rotation or 0)
            try:
                image_count = len(pdf_page.images)
            except Exception:
                image_count = 0
            if len(embedded) >= MIN_EMBEDDED_TEXT_CHARACTERS:
                page = _page_document(index, embedded, rotation, image_count)
                _atomic_json(evidence, page)
                page_documents.append(page)
            else:
                pending.append(index)
        for number in pending[:max_florence_pages]:
            image_path = pages_root / f"page-{number:04d}.png"
            if not image_path.exists():
                _render_page(document, number, image_path)
            text, confidence, processing_ms = florence_ocr(image_path)
            pdf_page = reader.pages[number - 1]
            page = _page_document(number, pdf_page.extract_text() or "", int(pdf_page.rotation or 0), len(pdf_page.images), text, confidence, processing_ms)
            _atomic_json(evidence_root / f"page-{number:04d}.json", page)
    finally:
        document.close()
    completed = sorted(evidence_root.glob("page-*.json"))
    remaining = tuple(number for number in range(1, page_count + 1) if not (evidence_root / f"page-{number:04d}.json").exists())
    if remaining:
        _atomic_json(manifest_path, {"pipeline_version": PIPELINE_VERSION, "asset_id": str(asset_id), "source_sha256": expected_sha256, "page_count": page_count, "pending_pages": remaining})
        return ExtractionProgress(asset_id, page_count, len(completed), remaining, None)
    pages = [json.loads(path.read_text(encoding="utf-8")) for path in completed]
    text = "\n".join(str(page["selected_text"]) for page in pages)
    language = _language(text)
    lengths = [len(str(page["selected_text"])) for page in pages]
    image_heavy = sum(int(page["image_count"]) > 0 and len(str(page["selected_text"])) < 120 for page in pages)
    fixed_layout = bool(lengths and statistics.median(lengths) < 80 and image_heavy / page_count >= 0.3)
    reading_mode = "fixed_layout" if fixed_layout else "reflowable"
    files = _extract_illustrations(asset_id, reader, workspace, author, title)
    blocks = _blocks(asset_id, pages, files)
    issues = list(_issues(asset_id, pages, blocks))
    if fixed_layout:
        issues.append(PublicationIssue(_page_id(asset_id, 1, "layout"), asset_id, "uncertain_structure", "critical", "open", "Image-heavy publication is held for fixed-layout review", evidence={"image_heavy_pages": image_heavy, "page_count": page_count}))
    facts = _publication_facts("\n".join(str(page["selected_text"]) for page in pages[:20]))
    detected = {"author": author, "title": title, "page_count": page_count, **facts}
    metadata = PublicationMetadata(asset_id, "book", reading_mode, "needs_review", language, HTML_VERSION, detected=detected, provenance={"author": {"source": "filename"}, "title": {"source": "filename"}, "text": {"pipeline": PIPELINE_VERSION}})
    snapshot = PublicationSnapshot(metadata, files, blocks, tuple(issues))
    _atomic_text(workspace / "publication.html", build_structured_html(blocks, language))
    _atomic_json(workspace / "document.json", {"schema": "personal-vault.reading-extraction", "version": 1, "pipeline_version": PIPELINE_VERSION, "asset_id": str(asset_id), "source_sha256": expected_sha256, "reading_mode": reading_mode, "language": language, "pages": pages})
    _atomic_json(manifest_path, {"pipeline_version": PIPELINE_VERSION, "asset_id": str(asset_id), "source_sha256": expected_sha256, "page_count": page_count, "pending_pages": []})
    if sha256_file(source) != expected_sha256:
        raise ValueError("Publication source changed during extraction")
    return ExtractionProgress(asset_id, page_count, page_count, (), snapshot)
