import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import get_metadata_storage_root
from app.main import app
from app.vault_master import CataloguedAsset, MemoryVaultMasterStore, get_vault_master_store
from app.vault_master_music import (
    MusicBrainzClient,
    MusicMetadataProviderError,
    ProviderRelease,
    ProviderTrack,
    get_musicbrainz_client,
)
from app import vault_master_music
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def authenticate(client: TestClient) -> None:
    assert client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    ).status_code == 200


def album_track(
    store: MemoryVaultMasterStore,
    number: int,
    *,
    owner: str = TEST_USERNAME,
) -> CataloguedAsset:
    filename = f"{number:02d} Track{number:02d}.wma"
    vault_path = f"/vault/Music/ID - Mercury Act 1/{filename}"
    asset = CataloguedAsset(
        id=uuid4(),
        asset_type="Music",
        display_title=f"Track{number:02d}",
        captured_on=None,
        location=None,
        vault_path=vault_path,
        filename=filename,
        size_bytes=10,
        mime_type="audio/x-ms-wma",
        sha256=f"{number:064x}",
        metadata={},
        metadata_provenance={"display_title": "filename"},
        detected_metadata={
            "display_title": f"Track{number:02d}",
            "track_number": str(number),
        },
        effective_metadata={
            "display_title": f"Track{number:02d}",
            "track_number": str(number),
        },
        owner_username=owner,
    )
    store.catalogued_assets[vault_path] = asset
    return asset


def selected_release() -> ProviderRelease:
    return ProviderRelease(
        release_id="12345678-1234-4123-8123-123456789abc",
        release_group_id="87654321-4321-4321-8321-cba987654321",
        title="Mercury – Act 1",
        artist="Imagine Dragons",
        date="2021-09-03",
        country="GB",
        genres=("Alternative rock",),
        tracks=(
            ProviderTrack(1, 1, "1", "My Life", "Imagine Dragons", "recording-1", 224.0),
            ProviderTrack(1, 2, "2", "Lonely", "Imagine Dragons", "recording-2", 159.0),
            ProviderTrack(1, 7, "7", "No Time for Toxic People", "Imagine Dragons", "recording-7", 207.0),
        ),
        cover_art_available=True,
    )


class FakeProvider:
    def search_releases(self, artist: str, album: str, limit: int = 5):
        assert (artist, album, limit) == ("Imagine Dragons", "Mercury Act 1", 5)
        return [
            {
                "release_id": selected_release().release_id,
                "title": selected_release().title,
                "artist": selected_release().artist,
                "date": selected_release().date,
                "country": selected_release().country,
                "track_count": 3,
                "score": 100,
                "cover_art_available": True,
            }
        ]

    def get_release(self, release_id: str) -> ProviderRelease:
        assert release_id == selected_release().release_id
        return selected_release()

    def get_front_cover(self, release_id: str, max_bytes: int):
        assert release_id == selected_release().release_id
        assert max_bytes >= 10
        return b"jpeg-cover", "image/jpeg"


def configure(tmp_path: Path):
    store = MemoryVaultMasterStore()
    first = album_track(store, 1)
    seventh = album_track(store, 7)
    album_track(store, 11, owner="another-family-member")
    metadata_root = tmp_path / "metadata"
    metadata_root.mkdir()
    app.dependency_overrides[get_vault_master_store] = lambda: store
    app.dependency_overrides[get_musicbrainz_client] = lambda: FakeProvider()
    app.dependency_overrides[get_metadata_storage_root] = lambda: metadata_root
    return store, first, seventh, metadata_root


def test_album_search_and_preview_are_review_only(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store, first, seventh, _ = configure(tmp_path)
    authenticate(client)

    search = client.post(
        "/api/vault-master/music/albums/search",
        json={
            "folder": "ID - Mercury Act 1",
            "artist": "Imagine Dragons",
            "album": "Mercury Act 1",
        },
    )
    preview = client.post(
        "/api/vault-master/music/albums/preview",
        json={
            "folder": "ID - Mercury Act 1",
            "release_id": selected_release().release_id,
        },
    )

    assert search.status_code == 200
    assert search.json()["local_track_count"] == 2
    assert search.json()["candidates"][0]["artist"] == "Imagine Dragons"
    assert preview.status_code == 200
    assert preview.json()["matched_track_count"] == 2
    assert [track["matched"] for track in preview.json()["tracks"]] == [True, False, True]
    assert store.get_catalogued_asset_by_id(first.id).user_overrides == {}
    assert store.get_catalogued_asset_by_id(seventh.id).user_overrides == {}


def test_provider_outage_does_not_change_album_catalogue(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store, first, _, _ = configure(tmp_path)

    class UnavailableProvider(FakeProvider):
        def search_releases(self, artist: str, album: str, limit: int = 5):
            raise MusicMetadataProviderError("The online music catalogue is unavailable")

    app.dependency_overrides[get_musicbrainz_client] = lambda: UnavailableProvider()
    authenticate(client)

    response = client.post(
        "/api/vault-master/music/albums/search",
        json={
            "folder": "ID - Mercury Act 1",
            "artist": "Imagine Dragons",
            "album": "Mercury Act 1",
        },
    )

    assert response.status_code == 503
    assert store.get_catalogued_asset_by_id(first.id).user_overrides == {}


def test_approved_album_retains_metadata_and_cover_without_changing_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    store, first, seventh, metadata_root = configure(tmp_path)
    authenticate(client)

    response = client.post(
        "/api/vault-master/music/albums/approve",
        json={
            "folder": "ID - Mercury Act 1",
            "release_id": selected_release().release_id,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "folder": "ID - Mercury Act 1",
        "release_id": selected_release().release_id,
        "updated_track_count": 2,
        "artwork_retained": True,
    }
    updated_first = store.get_catalogued_asset_by_id(first.id)
    updated_seventh = store.get_catalogued_asset_by_id(seventh.id)
    assert updated_first is not None
    assert updated_seventh is not None
    assert updated_first.effective_metadata["display_title"] == "My Life"
    assert updated_seventh.effective_metadata["display_title"] == "No Time for Toxic People"
    assert updated_first.effective_metadata["artist"] == "Imagine Dragons"
    assert updated_first.effective_metadata["album"] == "Mercury – Act 1"
    assert updated_first.imported_metadata["musicbrainz"]["release_id"] == selected_release().release_id
    assert updated_first.user_overrides["artist"] == "Imagine Dragons"
    assert updated_first.sha256 == first.sha256
    history = store.list_catalogued_asset_history(first.id)
    assert history[0]["current_values"]["artist"] == "Imagine Dragons"
    assert (metadata_root / "artwork" / str(first.id) / "primary").read_bytes() == b"jpeg-cover"


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self._payload if size < 0 else self._payload[:size]


def test_musicbrainz_search_uses_fixed_release_api(
    monkeypatch,
) -> None:
    requested: list[Request] = []

    def fake_urlopen(request: Request, timeout: float):
        requested.append(request)
        assert timeout == 15
        return FakeResponse(
            {
                "releases": [
                    {
                        "id": selected_release().release_id,
                        "title": "Mercury – Act 1",
                        "artist-credit": [{"name": "Imagine Dragons"}],
                        "date": "2021-09-03",
                        "country": "GB",
                        "score": 100,
                        "media": [{"track-count": 13}],
                        "cover-art-archive": {"front": True},
                    }
                ]
            }
        )

    monkeypatch.setattr(vault_master_music, "urlopen", fake_urlopen)
    provider = MusicBrainzClient(minimum_interval_seconds=0)

    results = provider.search_releases("Imagine Dragons", "Mercury Act 1")

    assert results[0]["track_count"] == 13
    parts = urlsplit(requested[0].full_url)
    assert parts.scheme == "https"
    assert parts.hostname == "musicbrainz.org"
    assert parts.path == "/ws/2/release/"
    assert parse_qs(parts.query)["fmt"] == ["json"]
    assert "Imagine Dragons" in parse_qs(parts.query)["query"][0]
