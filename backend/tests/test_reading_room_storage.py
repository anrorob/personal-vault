from pathlib import Path

from app.vault_master import get_inventory_paths, inventory_catalogue_location
from app.vault_master_api import (
    get_catalogue_preview_roots,
    get_destination_paths,
)


def test_library_storage_is_configured_for_movement_preview_and_inventory(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "Library"
    publication_root = library / "Author" / "Title"
    source = publication_root / "source" / "Author - Title.pdf"
    supporting_file = publication_root / "reading" / "publication.html"
    source.parent.mkdir(parents=True)
    supporting_file.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    supporting_file.write_text("<p>Book</p>", encoding="utf-8")
    (publication_root / ".publication-ready").write_text("ready\n")
    monkeypatch.setenv("PV_LIBRARY_PATH", str(library))

    assert get_destination_paths()["Library"] == library
    assert get_catalogue_preview_roots()["/vault/Library"] == library
    assert inventory_catalogue_location(str(source)) == (
        "Library",
        "/vault/Library/Author/Title/source/Author - Title.pdf",
    )
    assert inventory_catalogue_location(str(supporting_file)) is None


def test_incomplete_publication_is_not_catalogued(
    monkeypatch,
    tmp_path: Path,
) -> None:
    library = tmp_path / "Library"
    source = library / "Author" / "Title" / "source" / "Author - Title.pdf"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"pdf")
    monkeypatch.setenv("PV_LIBRARY_PATH", str(library))

    assert inventory_catalogue_location(str(source)) is None


def test_default_inventory_paths_include_library(
    monkeypatch,
) -> None:
    monkeypatch.delenv("PV_VAULT_MASTER_INVENTORY_PATHS", raising=False)

    assert Path("/media/library") in get_inventory_paths()


def test_backend_compose_uses_stable_library_mount() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    compose = (repository_root / "docker-compose.yml").read_text(encoding="utf-8")
    override = (repository_root / "docker-compose.override.yml.example").read_text(
        encoding="utf-8"
    )

    assert "PV_LIBRARY_PATH: /media/library" in compose
    assert "PV_LIBRARY_PATH: /media/library" in override
    assert "/vault/Library:/media/library:rw" in override
