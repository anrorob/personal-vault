from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi.testclient import TestClient

from app.main import app
from app.vault_libraries import (
    get_archives_path,
    get_documents_path,
    get_personal_videos_path,
    scan_vault_library,
)
from app.vault_master import (
    CataloguedAsset,
    MemoryVaultMasterStore,
    SHARED_ASSET_VISIBILITY,
    get_vault_master_store,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def authenticate(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
        },
    )
    assert response.status_code == 200


def configure_libraries(
    tmp_path: Path,
) -> tuple[Path, Path, Path, MemoryVaultMasterStore]:
    videos = tmp_path / "videos"
    documents = tmp_path / "documents"
    archives = tmp_path / "archives"
    for path in (videos, documents, archives):
        path.mkdir()

    app.dependency_overrides[get_personal_videos_path] = lambda: videos
    app.dependency_overrides[get_documents_path] = lambda: documents
    app.dependency_overrides[get_archives_path] = lambda: archives
    store = MemoryVaultMasterStore()
    app.dependency_overrides[get_vault_master_store] = lambda: store
    return videos, documents, archives, store


def catalogue_file(
    store: MemoryVaultMasterStore,
    library_path: Path,
    file_path: Path,
    *,
    asset_type: str,
    vault_root: str,
    mime_type: str,
    owner_username: str = TEST_USERNAME,
    visibility: str = "private",
    shared_with: tuple[str, ...] = (),
    shared_with_user_ids: tuple[UUID, ...] = (),
) -> CataloguedAsset:
    relative_path = file_path.relative_to(library_path).as_posix()
    vault_path = f"{vault_root}/{relative_path}"
    asset = CataloguedAsset(
        id=uuid4(),
        asset_type=asset_type,
        display_title=f"Published {asset_type.casefold()} record",
        captured_on=None,
        location="Gdansk",
        vault_path=vault_path,
        filename=file_path.name,
        size_bytes=file_path.stat().st_size,
        mime_type=mime_type,
        sha256="a" * 64,
        metadata={},
        metadata_provenance={
            "display_title": "user_override",
            "captured_on": "unavailable",
            "location": "user_override",
        },
        owner_username=owner_username,
        owner_user_id=uuid5(
            NAMESPACE_URL, f"personal-vault-test:{owner_username}"
        ),
        visibility=visibility,
        shared_with=shared_with,
        shared_with_user_ids=shared_with_user_ids,
    )
    store.catalogued_assets[vault_path] = asset
    return asset


def test_library_endpoints_require_authentication(
    client: TestClient,
    tmp_path: Path,
) -> None:
    videos, documents, archives, _ = configure_libraries(tmp_path)
    (videos / "clip.mp4").write_bytes(b"video")
    (documents / "record.pdf").write_bytes(b"pdf")
    (archives / "collection.zip").write_bytes(b"archive")

    for api_path in ("personal-videos", "documents", "archives"):
        listing = client.get(f"/api/{api_path}")
        assert listing.status_code == 401
        content = client.get(f"/api/{api_path}/private-file/content")
        assert content.status_code == 401


def test_libraries_are_separate_and_hide_server_paths(
    client: TestClient,
    tmp_path: Path,
) -> None:
    videos, documents, archives, store = configure_libraries(tmp_path)
    (videos / "Family").mkdir()
    video_path = videos / "Family" / "birthday.mp4"
    video_path.write_bytes(b"video")
    catalogue_file(
        store,
        videos,
        video_path,
        asset_type="Home Videos",
        vault_root="/vault/Home Videos",
        mime_type="video/mp4",
    )
    (videos / "ignore.txt").write_bytes(b"not-video")
    letter_path = documents / "letter.pdf"
    letter_path.write_bytes(b"pdf")
    catalogue_file(
        store,
        documents,
        letter_path,
        asset_type="Documents",
        vault_root="/vault/Documents",
        mime_type="application/pdf",
    )
    scanned_path = documents / "scanned-record.png"
    scanned_path.write_bytes(b"image")
    catalogue_file(
        store,
        documents,
        scanned_path,
        asset_type="Documents",
        vault_root="/vault/Documents",
        mime_type="image/png",
    )
    (documents / "ignore.exe").write_bytes(b"program")
    (archives / "Mixed").mkdir()
    memory_path = archives / "Mixed" / "memory.jpg"
    memory_path.write_bytes(b"image")
    catalogue_file(
        store,
        archives,
        memory_path,
        asset_type="Archives",
        vault_root="/vault/Archives",
        mime_type="image/jpeg",
    )
    installer_path = archives / "Mixed" / "installer.exe"
    installer_path.write_bytes(b"program")
    catalogue_file(
        store,
        archives,
        installer_path,
        asset_type="Archives",
        vault_root="/vault/Archives",
        mime_type="application/octet-stream",
    )
    authenticate(client)

    video_response = client.get("/api/personal-videos")
    document_response = client.get("/api/documents")
    archive_response = client.get("/api/archives")

    assert video_response.status_code == 200
    assert document_response.status_code == 200
    assert archive_response.status_code == 200
    assert video_response.headers["cache-control"] == "private, no-store"
    assert [item["name"] for item in video_response.json()] == [
        "birthday.mp4"
    ]
    assert video_response.json()[0]["directory"] == "Family"
    assert video_response.json()[0]["display_title"] == (
        "Published home videos record"
    )
    assert video_response.json()[0]["location"] == "Gdansk"
    assert [item["name"] for item in document_response.json()] == [
        "letter.pdf",
        "scanned-record.png",
    ]
    assert document_response.json()[0]["opens_inline"] is True
    assert document_response.json()[0]["display_title"] == (
        "Published documents record"
    )
    assert document_response.json()[0]["location"] == "Gdansk"
    assert document_response.json()[1]["kind"] == "image"
    assert {item["kind"] for item in archive_response.json()} == {
        "image",
        "software",
    }
    assert str(tmp_path) not in (
        video_response.text
        + document_response.text
        + archive_response.text
    )


def test_safe_files_open_inline_and_software_downloads(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, documents, archives, store = configure_libraries(tmp_path)
    document_path = documents / "record.pdf"
    document_path.write_bytes(b"private-pdf")
    catalogue_file(
        store,
        documents,
        document_path,
        asset_type="Documents",
        vault_root="/vault/Documents",
        mime_type="application/pdf",
    )
    archive_path = archives / "tool.exe"
    archive_path.write_bytes(b"private-tool")
    catalogue_file(
        store,
        archives,
        archive_path,
        asset_type="Archives",
        vault_root="/vault/Archives",
        mime_type="application/octet-stream",
    )
    authenticate(client)

    document = client.get("/api/documents").json()[0]
    archive = client.get("/api/archives").json()[0]
    document_content = client.get(document["open_url"])
    archive_content = client.get(archive["open_url"])

    assert document_content.content == b"private-pdf"
    assert document_content.headers["content-disposition"].startswith(
        "inline"
    )
    assert archive_content.content == b"private-tool"
    assert archive_content.headers["content-disposition"].startswith(
        "attachment"
    )
    assert document_content.headers["x-content-type-options"] == "nosniff"
    assert archive_content.headers["cache-control"] == "private, no-store"


def test_personal_videos_hides_uncatalogued_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    videos, _, _, store = configure_libraries(tmp_path)
    catalogued_path = videos / "catalogued.mp4"
    catalogued_path.write_bytes(b"catalogued")
    catalogue_file(
        store,
        videos,
        catalogued_path,
        asset_type="Home Videos",
        vault_root="/vault/Home Videos",
        mime_type="video/mp4",
    )
    uncatalogued_path = videos / "uncatalogued.mp4"
    uncatalogued_path.write_bytes(b"uncatalogued")
    authenticate(client)

    response = client.get("/api/personal-videos")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == [
        "catalogued.mp4"
    ]
    catalogued_content = client.get(
        response.json()[0]["open_url"]
    )
    assert catalogued_content.status_code == 200
    assert catalogued_content.content == b"catalogued"

    uncatalogued_id = next(
        entry.id
        for entry in scan_vault_library(videos)
        if entry.path == uncatalogued_path
    )
    hidden_content = client.get(
        f"/api/personal-videos/{uncatalogued_id}/content"
    )
    assert hidden_content.status_code == 404
    assert hidden_content.json()["detail"] == (
        "Video is not catalogued by Vault Master"
    )


def test_personal_videos_only_expose_visible_catalogued_assets(
    client: TestClient,
    tmp_path: Path,
) -> None:
    videos, _, _, store = configure_libraries(tmp_path)
    private_path = videos / "private.mp4"
    private_path.write_bytes(b"private")
    catalogue_file(
        store,
        videos,
        private_path,
        asset_type="Home Videos",
        vault_root="/vault/Home Videos",
        mime_type="video/mp4",
        owner_username="another-family-member",
    )
    shared_path = videos / "shared.mp4"
    shared_path.write_bytes(b"shared")
    catalogue_file(
        store,
        videos,
        shared_path,
        asset_type="Home Videos",
        vault_root="/vault/Home Videos",
        mime_type="video/mp4",
        owner_username="another-family-member",
        visibility=SHARED_ASSET_VISIBILITY,
        shared_with=(TEST_USERNAME,),
        shared_with_user_ids=(uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),),
    )
    authenticate(client)

    response = client.get("/api/personal-videos")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["shared.mp4"]
    assert response.json()[0]["metadata_provenance"] == {}
    assert client.get(response.json()[0]["open_url"]).content == b"shared"

    private_id = next(
        entry.id
        for entry in scan_vault_library(videos)
        if entry.path == private_path
    )
    hidden_content = client.get(
        f"/api/personal-videos/{private_id}/content"
    )
    assert hidden_content.status_code == 404
    assert hidden_content.json()["detail"] == (
        "Video is not catalogued by Vault Master"
    )


def test_documents_hide_uncatalogued_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, documents, _, store = configure_libraries(tmp_path)
    catalogued_path = documents / "catalogued.pdf"
    catalogued_path.write_bytes(b"catalogued")
    catalogue_file(
        store,
        documents,
        catalogued_path,
        asset_type="Documents",
        vault_root="/vault/Documents",
        mime_type="application/pdf",
    )
    uncatalogued_path = documents / "uncatalogued.pdf"
    uncatalogued_path.write_bytes(b"uncatalogued")
    authenticate(client)

    response = client.get("/api/documents")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == [
        "catalogued.pdf"
    ]
    catalogued_content = client.get(response.json()[0]["open_url"])
    assert catalogued_content.status_code == 200
    assert catalogued_content.content == b"catalogued"

    uncatalogued_id = next(
        entry.id
        for entry in scan_vault_library(documents)
        if entry.path == uncatalogued_path
    )
    hidden_content = client.get(
        f"/api/documents/{uncatalogued_id}/content"
    )
    assert hidden_content.status_code == 404
    assert hidden_content.json()["detail"] == (
        "Document is not catalogued by Vault Master"
    )


def test_archives_hide_uncatalogued_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, _, archives, store = configure_libraries(tmp_path)
    catalogued_path = archives / "catalogued.zip"
    catalogued_path.write_bytes(b"catalogued")
    catalogue_file(
        store,
        archives,
        catalogued_path,
        asset_type="Archives",
        vault_root="/vault/Archives",
        mime_type="application/zip",
    )
    uncatalogued_path = archives / "uncatalogued.zip"
    uncatalogued_path.write_bytes(b"uncatalogued")
    authenticate(client)

    response = client.get("/api/archives")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == [
        "catalogued.zip"
    ]
    catalogued_content = client.get(response.json()[0]["open_url"])
    assert catalogued_content.status_code == 200
    assert catalogued_content.content == b"catalogued"

    uncatalogued_id = next(
        entry.id
        for entry in scan_vault_library(archives)
        if entry.path == uncatalogued_path
    )
    hidden_content = client.get(
        f"/api/archives/{uncatalogued_id}/content"
    )
    assert hidden_content.status_code == 404
    assert hidden_content.json()["detail"] == (
        "Archive file is not catalogued by Vault Master"
    )


def test_documents_and_archives_only_expose_visible_assets(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, documents, archives, store = configure_libraries(tmp_path)
    private_document = documents / "private.pdf"
    private_document.write_bytes(b"private-document")
    catalogue_file(
        store,
        documents,
        private_document,
        asset_type="Documents",
        vault_root="/vault/Documents",
        mime_type="application/pdf",
        owner_username="another-family-member",
    )
    shared_document = documents / "shared.pdf"
    shared_document.write_bytes(b"shared-document")
    catalogue_file(
        store,
        documents,
        shared_document,
        asset_type="Documents",
        vault_root="/vault/Documents",
        mime_type="application/pdf",
        owner_username="another-family-member",
        visibility=SHARED_ASSET_VISIBILITY,
        shared_with=(TEST_USERNAME,),
        shared_with_user_ids=(uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),),
    )
    private_archive = archives / "private.zip"
    private_archive.write_bytes(b"private-archive")
    catalogue_file(
        store,
        archives,
        private_archive,
        asset_type="Archives",
        vault_root="/vault/Archives",
        mime_type="application/zip",
        owner_username="another-family-member",
    )
    shared_archive = archives / "shared.zip"
    shared_archive.write_bytes(b"shared-archive")
    catalogue_file(
        store,
        archives,
        shared_archive,
        asset_type="Archives",
        vault_root="/vault/Archives",
        mime_type="application/zip",
        owner_username="another-family-member",
        visibility=SHARED_ASSET_VISIBILITY,
        shared_with=(TEST_USERNAME,),
        shared_with_user_ids=(uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),),
    )
    authenticate(client)

    document_response = client.get("/api/documents")
    archive_response = client.get("/api/archives")

    assert [item["name"] for item in document_response.json()] == [
        "shared.pdf"
    ]
    assert document_response.json()[0]["metadata_provenance"] == {}
    assert client.get(
        document_response.json()[0]["open_url"]
    ).content == b"shared-document"
    assert [item["name"] for item in archive_response.json()] == [
        "shared.zip"
    ]
    assert archive_response.json()[0]["metadata_provenance"] == {}
    assert client.get(
        archive_response.json()[0]["open_url"]
    ).content == b"shared-archive"

    private_document_id = next(
        entry.id
        for entry in scan_vault_library(documents)
        if entry.path == private_document
    )
    private_archive_id = next(
        entry.id
        for entry in scan_vault_library(archives)
        if entry.path == private_archive
    )
    assert client.get(
        f"/api/documents/{private_document_id}/content"
    ).status_code == 404
    assert client.get(
        f"/api/archives/{private_archive_id}/content"
    ).status_code == 404


def test_symlinks_are_not_exposed(
    client: TestClient,
    tmp_path: Path,
) -> None:
    _, _, archives, _ = configure_libraries(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    link = archives / "linked.txt"

    try:
        link.symlink_to(outside)
    except OSError:
        return

    authenticate(client)
    assert client.get("/api/archives").json() == []


def test_missing_library_returns_service_unavailable(
    client: TestClient,
    tmp_path: Path,
) -> None:
    app.dependency_overrides[get_documents_path] = (
        lambda: tmp_path / "missing"
    )
    app.dependency_overrides[get_vault_master_store] = (
        MemoryVaultMasterStore
    )
    authenticate(client)

    response = client.get("/api/documents")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Documents storage is unavailable"
    }
