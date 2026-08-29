import hashlib
from pathlib import Path, PurePosixPath

import pytest

from app.reading_room_storage import (
    PUBLICATION_READY_MARKER,
    PublicationStorageFile,
    publication_directory,
    publication_role_path,
    safely_publish_publication_directory,
)


def stored_file(source: Path, relative: PurePosixPath) -> PublicationStorageFile:
    return PublicationStorageFile(
        source_path=source,
        relative_path=relative,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    )


def test_approved_human_readable_layout() -> None:
    directory = publication_directory("Bolesław Prus", "Lalka", "1890 edition")

    assert directory == PurePosixPath("Bolesław Prus/Lalka [1890 edition]")
    assert publication_role_path(
        "source_pdf", original_filename="Bolesław Prus - Lalka.pdf"
    ) == PurePosixPath("source/Bolesław Prus - Lalka.pdf")
    assert publication_role_path("front_cover") == PurePosixPath(
        "covers/front.jpg"
    )
    assert publication_role_path("back_cover") == PurePosixPath("covers/back.jpg")
    assert publication_role_path("structured_html") == PurePosixPath(
        "reading/publication.html"
    )
    assert publication_role_path(
        "illustration", original_filename="illustration-001.jpg"
    ) == PurePosixPath("reading/illustrations/illustration-001.jpg")
    assert publication_role_path("ocr_data") == PurePosixPath("ocr/document.json")


def test_layout_normalises_unsafe_separators_without_losing_polish_text() -> None:
    assert publication_directory("Jan / Kowalski", "Łódź\\Warszawa") == (
        PurePosixPath("Jan - Kowalski/Łódź - Warszawa")
    )


def test_publish_verifies_every_file_then_removes_arrival_sources(
    tmp_path: Path,
) -> None:
    arrival = tmp_path / "Arrival Hall"
    library = tmp_path / "Library"
    arrival.mkdir()
    library.mkdir()
    pdf = arrival / "Author - Title.pdf"
    front = arrival / "Author - Title - front.jpg"
    pdf.write_bytes(b"pdf")
    front.write_bytes(b"front")
    directory = publication_directory("Author", "Title")

    result = safely_publish_publication_directory(
        arrival,
        library,
        directory,
        (
            stored_file(
                pdf,
                publication_role_path("source_pdf", original_filename=pdf.name),
            ),
            stored_file(front, publication_role_path("front_cover")),
        ),
    )

    assert result.directory == library / "Author" / "Title"
    assert result.retained_source_paths == ()
    assert (result.directory / "source" / pdf.name).read_bytes() == b"pdf"
    assert (result.directory / "covers" / "front.jpg").read_bytes() == b"front"
    assert (result.directory / PUBLICATION_READY_MARKER).read_text() == "ready\n"
    assert not pdf.exists()
    assert not front.exists()


def test_publish_refuses_collision_without_touching_sources(tmp_path: Path) -> None:
    arrival = tmp_path / "Arrival Hall"
    library = tmp_path / "Library"
    arrival.mkdir()
    library.mkdir()
    pdf = arrival / "Author - Title.pdf"
    pdf.write_bytes(b"pdf")
    existing = library / "Author" / "Title"
    existing.mkdir(parents=True)

    with pytest.raises(FileExistsError, match="already exists"):
        safely_publish_publication_directory(
            arrival,
            library,
            publication_directory("Author", "Title"),
            (
                stored_file(
                    pdf,
                    publication_role_path("source_pdf", original_filename=pdf.name),
                ),
            ),
        )

    assert pdf.read_bytes() == b"pdf"
    assert list(existing.iterdir()) == []


def test_publish_refuses_changed_checksum_without_creating_destination(
    tmp_path: Path,
) -> None:
    arrival = tmp_path / "Arrival Hall"
    library = tmp_path / "Library"
    arrival.mkdir()
    library.mkdir()
    pdf = arrival / "Author - Title.pdf"
    pdf.write_bytes(b"changed")
    publication_file = PublicationStorageFile(
        source_path=pdf,
        relative_path=publication_role_path("source_pdf", original_filename=pdf.name),
        sha256=hashlib.sha256(b"approved").hexdigest(),
    )

    with pytest.raises(ValueError, match="checksum changed"):
        safely_publish_publication_directory(
            arrival,
            library,
            publication_directory("Author", "Title"),
            (publication_file,),
        )

    assert pdf.exists()
    assert list(library.iterdir()) == []


def test_publish_accepts_checksum_verified_derived_files_from_bounded_workspace(tmp_path: Path) -> None:
    arrival = tmp_path / "Arrival Hall"
    workspace = tmp_path / "Metadata" / "publication"
    library = tmp_path / "Library"
    arrival.mkdir()
    workspace.mkdir(parents=True)
    library.mkdir()
    pdf = arrival / "Author - Title.pdf"
    html = workspace / "publication.html"
    pdf.write_bytes(b"pdf")
    html.write_text("<p>Reviewed</p>", encoding="utf-8")

    result = safely_publish_publication_directory(
        arrival,
        library,
        publication_directory("Author", "Title"),
        (
            stored_file(pdf, publication_role_path("source_pdf", original_filename=pdf.name)),
            PublicationStorageFile(html, publication_role_path("structured_html"), hashlib.sha256(html.read_bytes()).hexdigest(), workspace),
        ),
    )

    assert (result.directory / "reading" / "publication.html").read_text(encoding="utf-8") == "<p>Reviewed</p>"
    assert not html.exists()


def test_publish_refuses_symlinked_author_folder(tmp_path: Path) -> None:
    arrival = tmp_path / "Arrival Hall"
    library = tmp_path / "Library"
    outside = tmp_path / "Outside"
    arrival.mkdir()
    library.mkdir()
    outside.mkdir()
    pdf = arrival / "Author - Title.pdf"
    pdf.write_bytes(b"pdf")
    try:
        (library / "Author").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Directory symlinks are unavailable on this platform")

    with pytest.raises(ValueError, match="symbolic link"):
        safely_publish_publication_directory(
            arrival,
            library,
            publication_directory("Author", "Title"),
            (
                stored_file(
                    pdf,
                    publication_role_path("source_pdf", original_filename=pdf.name),
                ),
            ),
        )

    assert pdf.exists()
    assert list(outside.iterdir()) == []
