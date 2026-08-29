from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from fastapi.testclient import TestClient
from io import BytesIO

from app.main import app
from app import music as music_module
from app.music import get_music_library_path
from app.movie_playback import get_jellyfin_client
from app.jellyfin import JellyfinAudio, JellyfinStream
from app.music_playback import resolve_audio_with_index_retry
from app.vault_master import CataloguedAsset, MemoryVaultMasterStore, get_vault_master_store
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def authenticate(client: TestClient) -> None:
    assert client.post(
        "/api/auth/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    ).status_code == 200


def catalogue_track(
    store: MemoryVaultMasterStore,
    root: Path,
    relative_path: str,
    *,
    owner: str = TEST_USERNAME,
    metadata_overrides: dict[str, object] | None = None,
) -> CataloguedAsset:
    path = root / relative_path
    asset_id = uuid4()
    metadata = {
        "display_title": "Teardrop",
        "artist": "Massive Attack",
        "album": "Mezzanine",
        "album_artist": "Massive Attack",
        "genre": "Trip-hop",
        "track_number": "3/11",
        "disc_number": "1/1",
        "duration_seconds": 330.4,
        "release_year": 1998,
        "overview": "A retained album description.",
        "provider": {"name": "jellyfin"},
        "lyrics": {
            "text": "Love, love is a verb",
            "lines": [{"text": "Love, love is a verb"}],
            "metadata": {"artist": "Massive Attack"},
        },
        "artwork": {
            "owned": {
                "primary": {
                    "storage_key": f"artwork/{asset_id}/primary",
                    "mime_type": "image/jpeg",
                }
            }
        },
    }
    metadata.update(metadata_overrides or {})
    asset = CataloguedAsset(
        id=asset_id,
        asset_type="Music",
        display_title="Teardrop",
        captured_on=None,
        location=None,
        vault_path=f"/vault/Music/{relative_path}",
        filename=path.name,
        size_bytes=path.stat().st_size,
        mime_type="audio/flac",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={"display_title": "embedded"},
        imported_metadata=metadata,
        effective_metadata=metadata,
        owner_username=owner,
        owner_user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{owner}"),
    )
    store.catalogued_assets[asset.vault_path] = asset
    return asset


def configure(tmp_path: Path) -> tuple[Path, MemoryVaultMasterStore]:
    music = tmp_path / "music"
    (music / "Massive Attack" / "Mezzanine").mkdir(parents=True)
    store = MemoryVaultMasterStore()
    app.dependency_overrides[get_music_library_path] = lambda: music
    app.dependency_overrides[get_vault_master_store] = lambda: store
    return music, store


def test_music_requires_authentication(client: TestClient, tmp_path: Path) -> None:
    configure(tmp_path)
    assert client.get("/api/music").status_code == 401
    assert client.get("/api/music/private/stream").status_code == 401


def test_music_publishes_only_catalogued_metadata(
    client: TestClient, tmp_path: Path
) -> None:
    music, store = configure(tmp_path)
    relative = "Massive Attack/Mezzanine/03 Teardrop.flac"
    (music / relative).write_bytes(b"audio")
    catalogue_track(store, music, relative)
    (music / "uncatalogued.mp3").write_bytes(b"hidden")
    authenticate(client)

    response = client.get("/api/music")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == [
        {
            "id": response.json()[0]["id"],
            "asset_id": response.json()[0]["asset_id"],
            "title": "Teardrop",
            "artist": "Massive Attack",
            "album": "Mezzanine",
            "album_artist": "Massive Attack",
            "album_folder": "Massive Attack/Mezzanine",
            "genre": "Trip-hop",
            "genres": ["Trip-hop"],
            "track_number": 3,
            "disc_number": 1,
            "release_year": 1998,
            "overview": "A retained album description.",
            "duration_seconds": 330.4,
            "artwork_url": f"/api/vault-master/assets/{response.json()[0]['asset_id']}/artwork/primary",
            "lyrics_available": True,
            "enrichment_status": "identified",
            "playback_url": f"/api/music/{response.json()[0]['id']}/stream",
        }
    ]
    assert str(tmp_path) not in response.text
    assert "jellyfin" not in response.text.casefold()
    assert "uncatalogued" not in response.text


def test_music_lyrics_are_served_from_retained_catalogue(
    client: TestClient, tmp_path: Path
) -> None:
    music, store = configure(tmp_path)
    relative = "Massive Attack/Mezzanine/03 Teardrop.flac"
    (music / relative).write_bytes(b"audio")
    catalogue_track(store, music, relative)
    authenticate(client)
    track = client.get("/api/music").json()[0]

    response = client.get(f"/api/music/{track['id']}/lyrics")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["text"] == "Love, love is a verb"
    assert "jellyfin" not in response.text.casefold()


def test_owner_can_refresh_retained_music_information(
    client: TestClient,
    tmp_path: Path,
    monkeypatch,
) -> None:
    configure(tmp_path)
    monkeypatch.setattr(
        music_module,
        "run_jellyfin_music_import",
        lambda store: (7, 1),
    )
    authenticate(client)

    response = client.post("/api/music/refresh")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {"imported": 7, "failed": 1}


def test_music_publishes_catalogued_wma_tracks(
    client: TestClient, tmp_path: Path
) -> None:
    music, store = configure(tmp_path)
    relative = "Imagine Dragons/Mercury - Act 1/01 My Life.wma"
    path = music / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"wma-audio")
    asset = catalogue_track(store, music, relative)
    store.catalogued_assets[asset.vault_path] = CataloguedAsset(
        **{
            **asset.__dict__,
            "filename": path.name,
            "mime_type": "audio/x-ms-wma",
        }
    )
    authenticate(client)

    response = client.get("/api/music")

    assert response.status_code == 200
    assert [track["asset_id"] for track in response.json()] == [str(asset.id)]


def test_music_orders_track_and_disc_numbers_numerically(
    client: TestClient, tmp_path: Path
) -> None:
    music, store = configure(tmp_path)
    tracks = (
        ("disc-2-track-1.flac", "2/2", "1/13", "Disc 2 opener"),
        ("track-10.flac", "1/2", "10/13", "Track ten"),
        ("track-2.flac", "1", "02", "Track two"),
        ("track-1.flac", None, "1", "Track one"),
        ("unnumbered.flac", "1", None, "Bonus track"),
    )
    for filename, disc_number, track_number, title in tracks:
        relative = f"Massive Attack/Mezzanine/{filename}"
        (music / relative).write_bytes(b"audio")
        catalogue_track(
            store,
            music,
            relative,
            metadata_overrides={
                "display_title": title,
                "disc_number": disc_number,
                "track_number": track_number,
            },
        )
    authenticate(client)

    response = client.get("/api/music")

    assert response.status_code == 200
    assert [track["title"] for track in response.json()] == [
        "Track one",
        "Track two",
        "Track ten",
        "Bonus track",
        "Disc 2 opener",
    ]


def test_music_hides_another_owners_private_track(
    client: TestClient, tmp_path: Path
) -> None:
    music, store = configure(tmp_path)
    relative = "Massive Attack/Mezzanine/03 Teardrop.flac"
    (music / relative).write_bytes(b"audio")
    catalogue_track(store, music, relative, owner="another-family-member")
    authenticate(client)
    assert client.get("/api/music").json() == []


def test_music_catalogue_remains_browsable_without_jellyfin(
    client: TestClient, tmp_path: Path
) -> None:
    music, store = configure(tmp_path)
    relative = "Massive Attack/Mezzanine/03 Teardrop.flac"
    (music / relative).write_bytes(b"audio")
    catalogue_track(store, music, relative)
    authenticate(client)
    assert client.get("/api/music").status_code == 200


def test_music_playback_uses_hidden_jellyfin_adapter(
    client: TestClient, tmp_path: Path
) -> None:
    music, store = configure(tmp_path)
    relative = "Massive Attack/Mezzanine/03 Teardrop.flac"
    path = music / relative
    path.write_bytes(b"source-audio")
    catalogue_track(store, music, relative)

    class Response(BytesIO):
        def close(self) -> None:
            super().close()

    class PlaybackClient:
        requested_path: Path | None = None

        def find_audio_by_path(self, source_path: Path) -> JellyfinAudio:
            self.requested_path = source_path
            return JellyfinAudio("private-id", "private-source", str(source_path), "flac", "flac")

        def open_audio_stream(self, audio: JellyfinAudio, range_header: str | None) -> JellyfinStream:
            assert audio.item_id == "private-id"
            assert range_header == "bytes=0-3"
            return JellyfinStream(
                response=Response(b"played-through-jellyfin"),
                status_code=206,
                headers={"Content-Range": "bytes 0-3/24"},
                url="http://private-playback/audio",
                content_type="audio/mpeg",
            )

    playback = PlaybackClient()
    app.dependency_overrides[get_jellyfin_client] = lambda: playback
    authenticate(client)
    track = client.get("/api/music").json()[0]
    response = client.get(track["playback_url"], headers={"Range": "bytes=0-3"})
    assert response.status_code == 206
    assert response.content == b"played-through-jellyfin"
    assert response.headers["cache-control"] == "private, no-store"
    assert "private-id" not in response.text
    assert playback.requested_path == path


def test_music_playback_requests_scan_and_retries_when_track_is_new(
    tmp_path: Path,
) -> None:
    path = tmp_path / "new-track.wma"
    path.write_bytes(b"audio")

    class DelayedClient:
        def __init__(self) -> None:
            self.lookups = 0
            self.notified: tuple[Path, ...] | None = None
            self.refreshes = 0

        def find_audio_by_path(self, source_path: Path):
            assert source_path == path
            self.lookups += 1
            if self.lookups < 3:
                return None
            return JellyfinAudio("item", "source", str(path), "wma", "wmalossless")

        def notify_media_updated(self, paths: tuple[Path, ...]) -> None:
            self.notified = paths

        def refresh_library(self) -> None:
            self.refreshes += 1

    playback = DelayedClient()

    audio = resolve_audio_with_index_retry(
        playback,  # type: ignore[arg-type]
        path,
        attempts=3,
        delay_seconds=0,
    )

    assert audio is not None
    assert playback.notified == (path,)
    assert playback.refreshes == 1
    assert playback.lookups == 3
