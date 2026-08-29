from pathlib import Path
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from fastapi.testclient import TestClient

from app.config import get_upload_max_bytes
from app.auth_store import Account, MemoryAuthenticationStore
from app.incoming import (
    get_arrival_hall_path,
    get_incoming_path,
    record_arrival_hall_file_owner,
)
from app.main import app
from app.security import hash_password
from app.vault_master_intake import MemoryIntakeStore, get_intake_store
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


def configure_incoming(tmp_path: Path) -> None:
    app.dependency_overrides[get_incoming_path] = lambda: tmp_path
    # Arrival Hall upload admission is now governed by the persistent Intake
    # Gate. Keep this isolated endpoint suite on an explicitly Open test store.
    intake_store = MemoryIntakeStore()
    app.dependency_overrides[get_intake_store] = lambda: intake_store


def record_test_owner(incoming_path: Path, *paths: Path) -> None:
    owner = SimpleNamespace(
        user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),
    )
    for path in paths:
        record_arrival_hall_file_owner(incoming_path, path, owner)


def test_arrival_hall_path_prefers_new_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PV_INCOMING_PATH", "/vault/Incoming")
    monkeypatch.setenv(
        "PV_ARRIVAL_HALL_PATH",
        "/vault/Arrival Hall",
    )

    assert get_arrival_hall_path() == Path("/vault/Arrival Hall")


def test_arrival_hall_path_supports_legacy_configuration(
    monkeypatch,
) -> None:
    monkeypatch.delenv("PV_ARRIVAL_HALL_PATH", raising=False)
    monkeypatch.setenv("PV_INCOMING_PATH", "/vault/Incoming")

    assert get_arrival_hall_path() == Path("/vault/Incoming")


def upload(
    client: TestClient,
    filename: str,
    content: bytes,
    last_modified: str | None = None,
):
    headers = {
        "Content-Type": "application/octet-stream",
        "X-PV-Upload": "1",
        "X-PV-Filename": filename,
    }
    if last_modified is not None:
        headers["X-PV-Last-Modified"] = last_modified
    return client.post(
        "/api/arrival-hall",
        content=content,
        headers=headers,
    )


def test_incoming_endpoints_require_authentication(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)

    assert client.get("/api/incoming").status_code == 401
    assert upload(client, "photo.jpg", b"image").status_code == 401


def test_arrival_hall_endpoint_is_canonical_alias(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authenticate(client)

    response = client.get("/api/arrival-hall")

    assert response.status_code == 200
    assert response.json()["files"] == []


def test_upload_streams_file_into_incoming(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authenticate(client)

    response = upload(
        client,
        "Family%20Photo.jpg",
        b"owned-image-content",
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "status": "uploaded",
        "original_name": "Family Photo.jpg",
        "stored_name": "Family Photo.jpg",
        "size": 19,
    }
    assert (tmp_path / "Family Photo.jpg").read_bytes() == (
        b"owned-image-content"
    )
    assert not list(tmp_path.glob(".pv-upload-*.part"))


def test_arrival_hall_files_and_previews_are_isolated_by_uploader(
    client: TestClient,
    authentication_store: MemoryAuthenticationStore,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authentication_store.create_account(
        Account(
            username="recipient",
            display_name="Recipient",
            email="recipient@example.test",
            password_hash=hash_password("recipient-password"),
            role="user",
            active=True,
            password_change_required=False,
            created_at=datetime.now(timezone.utc),
            last_sign_in_at=None,
        )
    )

    authenticate(client)
    assert upload(client, "owner.jpg", b"owner-file").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post(
        "/api/auth/login",
        json={"username": "recipient", "password": "recipient-password"},
    ).status_code == 200
    assert upload(client, "recipient.jpg", b"recipient-file").status_code == 200

    listing = client.get("/api/arrival-hall")
    assert [entry["name"] for entry in listing.json()["files"]] == ["recipient.jpg"]
    assert client.get("/api/arrival-hall/recipient.jpg/preview").status_code == 200
    assert client.get("/api/arrival-hall/owner.jpg/preview").status_code == 404


def test_upload_never_overwrites_existing_file(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    (tmp_path / "archive.zip").write_bytes(b"original")
    authenticate(client)

    response = upload(client, "archive.zip", b"new")

    assert response.status_code == 200
    assert response.json()["stored_name"] == "archive (1).zip"
    assert (tmp_path / "archive.zip").read_bytes() == b"original"
    assert (tmp_path / "archive (1).zip").read_bytes() == b"new"


def test_upload_preserves_original_file_modified_time(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authenticate(client)
    original = datetime(2018, 4, 3, 12, 30, tzinfo=timezone.utc)

    response = upload(
        client,
        "old-photo.jpg",
        b"image",
        str(int(original.timestamp() * 1000)),
    )

    assert response.status_code == 200
    stored = tmp_path / "old-photo.jpg"
    assert abs(stored.stat().st_mtime - original.timestamp()) < 0.01


def test_arrival_hall_uploaded_time_is_intake_time_not_client_mtime(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authenticate(client)
    original = datetime(2013, 11, 23, 12, 2, tzinfo=timezone.utc)
    before_upload = datetime.now(timezone.utc)

    response = upload(
        client,
        "historic-photo.jpg",
        b"image",
        str(int(original.timestamp() * 1000)),
    )

    assert response.status_code == 200
    listing = client.get("/api/arrival-hall")
    assert listing.status_code == 200
    uploaded_at = datetime.fromisoformat(
        listing.json()["files"][0]["uploaded_at"].replace("Z", "+00:00")
    )
    assert before_upload <= uploaded_at <= datetime.now(timezone.utc)
    assert uploaded_at != original
    assert abs((tmp_path / "historic-photo.jpg").stat().st_mtime - original.timestamp()) < 0.01

    manifest = json.loads((tmp_path / ".pv-arrival-hall-owners.json").read_text())
    entry = manifest["historic-photo.jpg"]
    assert isinstance(entry["owner_user_id"], str)
    assert entry["uploaded_at"] == uploaded_at.isoformat()


def test_upload_rejects_invalid_original_file_timestamp(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authenticate(client)

    response = upload(
        client,
        "photo.jpg",
        b"image",
        "not-a-timestamp",
    )

    assert response.status_code == 400
    assert not list(tmp_path.iterdir())


def test_upload_rejects_unsafe_filename(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authenticate(client)

    response = upload(client, "..%2Foutside.txt", b"unsafe")

    assert response.status_code == 400
    assert response.json() == {"detail": "Upload filename is invalid"}
    assert not list(tmp_path.iterdir())


def test_upload_limit_removes_partial_file(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    app.dependency_overrides[get_upload_max_bytes] = lambda: 4
    authenticate(client)

    response = upload(client, "large.bin", b"12345")

    assert response.status_code == 413
    assert response.json() == {
        "detail": "File exceeds the upload size limit"
    }
    assert not list(tmp_path.iterdir())


def test_listing_returns_real_files_and_hides_partial_uploads(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    (tmp_path / "ready.txt").write_bytes(b"ready")
    (tmp_path / ".pv-upload-incomplete.part").write_bytes(b"partial")
    record_test_owner(tmp_path, tmp_path / "ready.txt")
    authenticate(client)

    response = client.get("/api/incoming")
    body = response.json()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert body["max_upload_bytes"] == 100 * 1024 * 1024 * 1024
    assert len(body["files"]) == 1
    assert body["files"][0]["name"] == "ready.txt"
    assert body["files"][0]["relative_path"] == "ready.txt"
    assert body["files"][0]["folder"] is None
    assert body["files"][0]["size"] == 5


def test_listing_recurses_into_album_folders_and_keeps_paths_distinct(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    first_album = tmp_path / "Artist" / "First Album"
    second_album = tmp_path / "Artist" / "Second Album"
    first_album.mkdir(parents=True)
    second_album.mkdir(parents=True)
    (first_album / "01 Track.wma").write_bytes(b"first")
    (second_album / "01 Track.wma").write_bytes(b"second")
    record_test_owner(
        tmp_path,
        first_album / "01 Track.wma",
        second_album / "01 Track.wma",
    )
    authenticate(client)

    response = client.get("/api/arrival-hall")

    assert response.status_code == 200
    files = {
        item["relative_path"]: item
        for item in response.json()["files"]
    }
    assert set(files) == {
        "Artist/First Album/01 Track.wma",
        "Artist/Second Album/01 Track.wma",
    }
    assert files["Artist/First Album/01 Track.wma"]["name"] == (
        "01 Track.wma"
    )
    assert files["Artist/First Album/01 Track.wma"]["folder"] == (
        "Artist/First Album"
    )


def test_listing_and_preview_exclude_symlinked_content(
    client: TestClient,
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / "outside-arrival-hall.jpg"
    outside.write_bytes(b"private-outside-image")
    link = tmp_path / "linked.jpg"
    try:
        link.symlink_to(outside)
    except OSError:
        return
    configure_incoming(tmp_path)
    authenticate(client)

    listing = client.get("/api/arrival-hall")
    preview = client.get("/api/arrival-hall/linked.jpg/preview")

    assert listing.status_code == 200
    assert listing.json()["files"] == []
    assert preview.status_code == 404


def test_missing_incoming_storage_returns_service_unavailable(
    client: TestClient,
    tmp_path: Path,
) -> None:
    app.dependency_overrides[get_incoming_path] = (
        lambda: tmp_path / "missing"
    )
    authenticate(client)

    response = client.get("/api/incoming")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Arrival Hall storage is unavailable"
    }


def test_incoming_preview_is_authenticated_and_image_video_only(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    (tmp_path / "Family Photo.jpg").write_bytes(b"private-image")
    (tmp_path / "notes.txt").write_bytes(b"private-notes")
    record_test_owner(
        tmp_path,
        tmp_path / "Family Photo.jpg",
        tmp_path / "notes.txt",
    )

    assert (
        client.get("/api/incoming/Family%20Photo.jpg/preview").status_code
        == 401
    )
    authenticate(client)
    image = client.get("/api/incoming/Family%20Photo.jpg/preview")
    text = client.get("/api/incoming/notes.txt/preview")

    assert image.status_code == 200
    assert image.content == b"private-image"
    assert image.headers["content-type"].startswith("image/jpeg")
    assert image.headers["cache-control"] == "private, no-store"
    assert text.status_code == 415


def test_incoming_preview_supports_safe_nested_paths(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    album = tmp_path / "Family Album"
    album.mkdir()
    (album / "Cover Art.jpg").write_bytes(b"nested-private-image")
    record_test_owner(tmp_path, album / "Cover Art.jpg")
    authenticate(client)

    response = client.get(
        "/api/arrival-hall/Family%20Album/Cover%20Art.jpg/preview"
    )

    assert response.status_code == 200
    assert response.content == b"nested-private-image"
    assert response.headers["cache-control"] == "private, no-store"


def test_incoming_preview_rejects_nested_path_traversal(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authenticate(client)

    response = client.get(
        "/api/arrival-hall/Album/%2E%2E/outside.jpg/preview"
    )

    assert response.status_code in {400, 404}


def test_incoming_preview_rejects_unsafe_paths(
    client: TestClient,
    tmp_path: Path,
) -> None:
    configure_incoming(tmp_path)
    authenticate(client)

    response = client.get("/api/incoming/..%5Coutside.jpg/preview")

    assert response.status_code == 400
