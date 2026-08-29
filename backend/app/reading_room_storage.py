"""Fail-closed permanent storage primitives for reviewed publications."""

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import unicodedata
from uuid import uuid4

from app.vault_master import CHECKSUM_CHUNK_BYTES, require_file_within_root, sha256_file


PUBLICATION_READY_MARKER = ".publication-ready"


@dataclass(frozen=True)
class PublicationStorageFile:
    source_path: Path
    relative_path: PurePosixPath
    sha256: str
    allowed_root: Path | None = None


@dataclass(frozen=True)
class PublicationStorageResult:
    directory: Path
    retained_source_paths: tuple[Path, ...]


def _human_path_segment(value: str, label: str) -> str:
    cleaned = unicodedata.normalize("NFC", value)
    cleaned = re.sub(r"[\\/\x00-\x1f\x7f]+", " - ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError(f"{label} is not valid for permanent storage")
    if len(cleaned) > 180:
        digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:10]
        cleaned = f"{cleaned[:165].rstrip()} [{digest}]"
    return cleaned


def publication_directory(
    author: str,
    title: str,
    edition_label: str | None = None,
) -> PurePosixPath:
    author_segment = _human_path_segment(author, "Author")
    title_segment = _human_path_segment(title, "Title")
    if edition_label:
        edition_segment = _human_path_segment(edition_label, "Edition label")
        title_segment = f"{title_segment} [{edition_segment}]"
    return PurePosixPath(author_segment) / title_segment


def publication_role_path(
    role: str,
    *,
    original_filename: str | None = None,
) -> PurePosixPath:
    if role == "source_pdf":
        if not original_filename or Path(original_filename).name != original_filename:
            raise ValueError("The source PDF filename is invalid")
        if Path(original_filename).suffix.casefold() != ".pdf":
            raise ValueError("The publication source must be a PDF")
        return PurePosixPath("source") / original_filename
    if role == "front_cover":
        return PurePosixPath("covers/front.jpg")
    if role == "back_cover":
        return PurePosixPath("covers/back.jpg")
    if role == "structured_html":
        return PurePosixPath("reading/publication.html")
    if role == "ocr_data":
        return PurePosixPath("ocr/document.json")
    if role == "illustration":
        if not original_filename or Path(original_filename).name != original_filename:
            raise ValueError("The illustration filename is invalid")
        return PurePosixPath("reading/illustrations") / original_filename
    raise ValueError("The publication file role is invalid")


def _validate_relative_path(path: PurePosixPath) -> None:
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("The publication relative path is invalid")


def _prepare_destination_parent(
    root: Path,
    relative_directory: PurePosixPath,
) -> Path:
    parent = root
    for part in relative_directory.parent.parts:
        candidate = parent / part
        if candidate.is_symlink():
            raise ValueError("The Library destination contains a symbolic link")
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("The Library destination parent is not a directory")
        candidate.mkdir(exist_ok=True)
        parent = candidate
    if not parent.resolve(strict=True).is_relative_to(root):
        raise ValueError("The publication destination is outside Library storage")
    return parent


def safely_publish_publication_directory(
    arrival_root: Path,
    library_root: Path,
    relative_directory: PurePosixPath,
    files: tuple[PublicationStorageFile, ...],
) -> PublicationStorageResult:
    """Publish one fully verified directory without overwriting existing data."""

    if not files:
        raise ValueError("A publication must contain at least one file")
    _validate_relative_path(relative_directory)
    resolved_arrival_root = arrival_root.resolve(strict=True)
    resolved_library_root = library_root.resolve(strict=True)
    if not resolved_library_root.is_dir():
        raise ValueError("Library storage is not a directory")

    final_directory = resolved_library_root.joinpath(*relative_directory.parts)
    if final_directory.exists() or final_directory.is_symlink():
        raise FileExistsError("The publication directory already exists")

    checked: list[tuple[PublicationStorageFile, Path]] = []
    destinations: set[PurePosixPath] = set()
    for publication_file in files:
        _validate_relative_path(publication_file.relative_path)
        if publication_file.relative_path == PurePosixPath(PUBLICATION_READY_MARKER):
            raise ValueError("The publication readiness marker is reserved")
        if publication_file.relative_path in destinations:
            raise ValueError("Two publication files have the same destination")
        destinations.add(publication_file.relative_path)
        allowed_root = (
            publication_file.allowed_root.resolve(strict=True)
            if publication_file.allowed_root is not None
            else resolved_arrival_root
        )
        source = require_file_within_root(publication_file.source_path, allowed_root)
        if sha256_file(source) != publication_file.sha256:
            raise ValueError("A publication source checksum changed before publishing")
        checked.append((publication_file, source))

    temporary = resolved_library_root / (
        f".pv-reading-room-{uuid4().hex}.part"
    )
    temporary.mkdir(mode=0o750)
    published = False
    try:
        for publication_file, source in checked:
            destination = temporary.joinpath(*publication_file.relative_path.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as source_handle:
                with destination.open("xb") as destination_handle:
                    shutil.copyfileobj(
                        source_handle,
                        destination_handle,
                        CHECKSUM_CHUNK_BYTES,
                    )
                    destination_handle.flush()
                    os.fsync(destination_handle.fileno())
            shutil.copystat(source, destination, follow_symlinks=False)
            if sha256_file(destination) != publication_file.sha256:
                raise ValueError("A copied publication file failed verification")
            if sha256_file(source) != publication_file.sha256:
                raise ValueError("A publication source changed while publishing")

        marker = temporary / PUBLICATION_READY_MARKER
        marker.write_text("ready\n", encoding="ascii")
        with marker.open("r+b") as marker_handle:
            os.fsync(marker_handle.fileno())
        _prepare_destination_parent(
            resolved_library_root,
            relative_directory,
        )
        if final_directory.exists() or final_directory.is_symlink():
            raise FileExistsError("The publication directory already exists")
        temporary.rename(final_directory)
        published = True
    finally:
        if not published and temporary.exists():
            shutil.rmtree(temporary)

    retained_sources: list[Path] = []
    for publication_file, source in checked:
        destination = final_directory.joinpath(*publication_file.relative_path.parts)
        if sha256_file(destination) != publication_file.sha256:
            retained_sources.append(source)
            continue
        try:
            source.unlink()
        except OSError:
            retained_sources.append(source)
    return PublicationStorageResult(
        directory=final_directory,
        retained_source_paths=tuple(retained_sources),
    )
