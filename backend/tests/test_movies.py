import hashlib
import json
from pathlib import Path
from io import BytesIO
from types import SimpleNamespace
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi.testclient import TestClient

from app.main import app
from app.config import get_metadata_storage_root
from app.jellyfin import (
    JellyfinMovie,
    JellyfinMovieDetails,
    JellyfinSubtitleTrack,
    JellyfinStream,
    JellyfinUnavailableError,
)
from app.movie_playback import get_jellyfin_client
from app.movies import (
    get_movies_library_path,
    reconcile_historical_exclusive_movie_paths,
    resolve_catalogued_movie_path,
    scan_movie_library,
)
from app.vault_master import (
    CataloguedAsset,
    MemoryVaultMasterStore,
    SHARED_ASSET_VISIBILITY,
    get_vault_master_store,
)
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def create_video(
    library_path: Path,
    relative_path: str,
    content: bytes = b"test-video",
) -> None:
    video_path = library_path / relative_path
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(content)


class FakeJellyfinClient:
    def __init__(
        self,
        movie: JellyfinMovie | None = None,
        error: Exception | None = None,
        stream: JellyfinStream | None = None,
        stream_error: Exception | None = None,
        details: JellyfinMovieDetails | None = None,
    ) -> None:
        self.movie = movie
        self.error = error
        self.stream = stream
        self.stream_error = stream_error
        self.details = details
        self.requested_path: Path | None = None
        self.requested_range: str | None = None
        self.requested_hls_url: str | None = None
        self.requested_hls_urls: list[str] = []
        self.requested_item_id: str | None = None
        self.requested_subtitle_index: int | None = None

    def find_movie_by_path(
        self,
        source_path: Path,
    ) -> JellyfinMovie | None:
        self.requested_path = source_path

        if self.error:
            raise self.error

        return self.movie

    def get_video_by_id(self, item_id: str) -> JellyfinMovie | None:
        self.requested_item_id = item_id
        return self.movie

    def open_browser_stream(
        self,
        movie: JellyfinMovie,
        range_header: str | None = None,
    ) -> JellyfinStream:
        self.requested_range = range_header

        if self.stream_error:
            raise self.stream_error

        if self.stream is None:
            raise AssertionError("No fake stream configured")

        return self.stream

    def open_hls_master(
        self,
        movie: JellyfinMovie,
        subtitle_stream_index: int | None = None,
    ) -> JellyfinStream:
        self.requested_subtitle_index = subtitle_stream_index
        if self.stream_error:
            raise self.stream_error

        if self.stream is None:
            raise AssertionError("No fake stream configured")

        return self.stream

    def open_hls_resource(self, url: str) -> JellyfinStream:
        self.requested_hls_url = url
        self.requested_hls_urls.append(url)

        if self.stream_error:
            raise self.stream_error

        if self.stream is None:
            raise AssertionError("No fake stream configured")

        return self.stream

    def get_movie_details(
        self,
        movie: JellyfinMovie,
    ) -> JellyfinMovieDetails:
        if self.error:
            raise self.error

        if self.details is None:
            raise AssertionError("No fake details configured")

        return self.details

    def get_image_url(
        self,
        item_id: str,
        image_type: str,
        *,
        image_index: int | None = None,
        max_width: int,
    ) -> str:
        index = f"/{image_index}" if image_index is not None else ""
        return (
            f"http://pv-jellyfin:8096/Items/{item_id}/Images/"
            f"{image_type}{index}?maxWidth={max_width}"
        )

    def open_resource(self, url: str) -> JellyfinStream:
        self.requested_hls_url = url

        if self.stream_error:
            raise self.stream_error

        if self.stream is None:
            raise AssertionError("No fake stream configured")

        return self.stream


def authenticate(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={
            "username": TEST_USERNAME,
            "password": TEST_PASSWORD,
        },
    )
    assert response.status_code == 200


def owner_identity(username: str = TEST_USERNAME) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=uuid5(NAMESPACE_URL, f"personal-vault-test:{username}"),
        username=username,
    )


def catalogue_movie(
    store: MemoryVaultMasterStore,
    library_path: Path,
    relative_path: str,
    *,
    artwork: tuple[str, ...] = (),
    metadata: dict[str, object] | None = None,
    owner_username: str = TEST_USERNAME,
    visibility: str = "private",
    shared_with: tuple[str, ...] = (),
    shared_with_user_ids: tuple[UUID, ...] = (),
) -> CataloguedAsset:
    asset_id = uuid4()
    owned = {
        kind: {
            "storage_key": f"artwork/{asset_id}/{kind}",
            "mime_type": "image/jpeg",
            "size_bytes": 12,
        }
        for kind in artwork
    }
    imported_metadata = {
        **(metadata or {}),
        "artwork": {"owned": owned},
    }
    asset = CataloguedAsset(
        id=asset_id,
        asset_type="movie",
        display_title=Path(relative_path).stem,
        captured_on=None,
        location=None,
        vault_path=f"/vault/Theatre/Movies/{relative_path}",
        filename=Path(relative_path).name,
        size_bytes=(library_path / relative_path).stat().st_size,
        mime_type="video/x-matroska",
        sha256=hashlib.sha256((library_path / relative_path).read_bytes()).hexdigest(),
        metadata={},
        metadata_provenance={},
        imported_metadata=imported_metadata,
        effective_metadata=imported_metadata,
        owner_username=owner_username,
        owner_user_id=uuid5(
            NAMESPACE_URL, f"personal-vault-test:{owner_username}"
        ),
        visibility=visibility,
        shared_with=shared_with,
        shared_with_user_ids=shared_with_user_ids,
    )
    store.catalogued_assets[asset.vault_path] = asset
    app.dependency_overrides[get_vault_master_store] = lambda: store
    return asset


def test_scanner_lists_supported_movies_and_ignores_other_files(
    tmp_path: Path,
) -> None:
    create_video(
        tmp_path,
        "First Man (2018)/First.Man.2018.1080p.mkv",
    )
    create_video(
        tmp_path,
        "The Matrix (1999)/THE_MATRIX/The Matrix (1999).mkv",
    )
    create_video(
        tmp_path,
        "The Matrix (1999)/extras/behind-the-scenes.mp4",
    )
    (tmp_path / "The Matrix (1999)" / "poster.jpg").write_bytes(b"image")

    movies = scan_movie_library(tmp_path)

    assert [(movie.title, movie.year) for movie in movies] == [
        ("First Man", 2018),
        ("The Matrix", 1999),
    ]
    assert all(len(movie.id) == 16 for movie in movies)


def test_scanner_falls_back_to_filename_metadata(tmp_path: Path) -> None:
    create_video(tmp_path, "Loose.Movie.2024.mp4")

    movies = scan_movie_library(tmp_path)

    assert len(movies) == 1
    assert movies[0].title == "Loose Movie"
    assert movies[0].year == 2024


def test_movies_endpoint_requires_authentication(
    client: TestClient,
) -> None:
    response = client.get("/api/movies")

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_movies_listing_excludes_separately_catalogued_companion_videos(
    client: TestClient, tmp_path: Path
) -> None:
    create_video(tmp_path, "Film (2024)/Film.mkv", b"main")
    create_video(tmp_path, "Film (2024)/extras/title_t00.mkv", b"extra")
    store = MemoryVaultMasterStore()
    main = catalogue_movie(store, tmp_path, "Film (2024)/Film.mkv")
    companion = catalogue_movie(store, tmp_path, "Film (2024)/extras/title_t00.mkv")
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    authenticate(client)

    response = client.get("/api/movies")
    companion_details = client.get(
        f"/api/movies/{companion.id}/details"
    )

    assert response.status_code == 200
    assert [movie["id"] for movie in response.json()] == [str(main.id)]
    assert companion_details.status_code == 404
    assert store.get_catalogued_asset_by_id(companion.id) is not None


def test_movies_endpoint_returns_live_library(
    client: TestClient,
    tmp_path: Path,
) -> None:
    create_video(
        tmp_path,
        "The Matrix Revolutions (2003)/Matrix Revolutions.mkv",
    )
    store = MemoryVaultMasterStore()
    asset = catalogue_movie(
        store,
        tmp_path,
        "The Matrix Revolutions (2003)/Matrix Revolutions.mkv",
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: FakeJellyfinClient()
    )
    authenticate(client)

    response = client.get("/api/movies")
    response_body = response.json()

    assert response.status_code == 200
    assert response_body == [
        {
            "id": response_body[0]["id"],
            "asset_id": str(asset.id),
            "title": "Matrix Revolutions",
            "year": None,
            "poster_url": None,
            "is_exclusive_movie": False,
        }
    ]


def test_movies_endpoint_remains_catalogue_browsable_without_a_library_mount(
    client: TestClient,
    tmp_path: Path,
) -> None:
    unavailable_path = tmp_path / "missing"
    app.dependency_overrides[get_vault_master_store] = (
        lambda: MemoryVaultMasterStore()
    )
    app.dependency_overrides[get_movies_library_path] = (
        lambda: unavailable_path
    )
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: FakeJellyfinClient()
    )
    authenticate(client)

    response = client.get("/api/movies")

    assert response.status_code == 200
    assert response.json() == []


def test_movies_endpoint_uses_retained_vault_master_poster(
    client: TestClient,
    tmp_path: Path,
) -> None:
    relative_path = "The Matrix (1999)/The Matrix.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    asset = catalogue_movie(
        store,
        tmp_path,
        relative_path,
        artwork=("poster",),
    )
    playback_movie = JellyfinMovie(
        item_id="private-jellyfin-id",
        media_source_id="private-source-id",
        path=str(tmp_path / relative_path),
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
        has_primary_image=True,
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: FakeJellyfinClient(movie=playback_movie)
    )
    authenticate(client)

    response = client.get("/api/movies")
    body = response.json()

    assert response.status_code == 200
    assert body[0]["asset_id"] == str(asset.id)
    assert body[0]["poster_url"] == (
        f"/api/vault-master/assets/{asset.id}/artwork/poster"
    )
    assert "private-jellyfin-id" not in response.text


def test_movie_details_fall_back_to_local_ffprobe_media_facts(
    client: TestClient, tmp_path: Path
) -> None:
    relative_path = "Local Facts (1991)/Local Facts.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    asset = catalogue_movie(
        store,
        tmp_path,
        relative_path,
        metadata={
            "container_format": "matroska,webm",
            "video_format": "mkv",
            "video_codec": "mpeg2video",
            "streams": [
                {"type": "video", "codec": "mpeg2video"},
                {"type": "audio", "codec": "ac3"},
                {"type": "audio", "codec": "dts"},
                {"type": "audio", "codec": "ac3"},
            ],
        },
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    authenticate(client)

    response = client.get(f"/api/movies/{asset.id}/details")

    assert response.status_code == 200
    assert response.json()["container"] == "mkv"
    assert response.json()["video_codec"] == "mpeg2video"
    assert response.json()["audio_codecs"] == ["ac3", "dts"]


def test_movies_endpoint_only_lists_visible_catalogued_assets(
    client: TestClient,
    tmp_path: Path,
) -> None:
    private_path = "Private (2020)/Private.mkv"
    shared_path = "Shared (2021)/Shared.mkv"
    create_video(tmp_path, private_path)
    create_video(tmp_path, shared_path)
    store = MemoryVaultMasterStore()
    catalogue_movie(
        store,
        tmp_path,
        private_path,
        owner_username="parent",
    )
    shared_asset = catalogue_movie(
        store,
        tmp_path,
        shared_path,
        owner_username="parent",
        visibility=SHARED_ASSET_VISIBILITY,
        shared_with=(TEST_USERNAME,),
        shared_with_user_ids=(uuid5(NAMESPACE_URL, f"personal-vault-test:{TEST_USERNAME}"),),
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    authenticate(client)

    response = client.get("/api/movies")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0] == {
        "id": body[0]["id"],
        "asset_id": str(shared_asset.id),
        "title": "Shared",
        "year": None,
        "poster_url": None,
        "is_exclusive_movie": False,
    }


def test_movies_endpoint_omits_uncatalogued_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    create_video(tmp_path, "Uncatalogued (2026)/Uncatalogued.mkv")
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_vault_master_store] = (
        lambda: MemoryVaultMasterStore()
    )
    authenticate(client)

    response = client.get("/api/movies")

    assert response.status_code == 200
    assert response.json() == []


def test_exclusive_movie_state_is_owner_audited_and_exported(
    tmp_path: Path,
) -> None:
    relative_path = "Stateful (2026)/Stateful.mkv"
    create_video(tmp_path, relative_path)
    metadata_root = tmp_path / "metadata"
    store = MemoryVaultMasterStore(sidecar_root=metadata_root)
    asset = catalogue_movie(store, tmp_path, relative_path)

    updated = store.set_movie_exclusive_state(
        asset.id, owner_identity(), True
    )

    assert updated is not None
    assert updated.effective_metadata["exclusive_movie"] is True
    assert store.asset_history[-1]["action"] == "exclusive_movie_state_changed"
    document = json.loads(
        (metadata_root / "sidecars" / f"{asset.id}.json").read_text()
    )
    assert document["metadata"]["user_overrides"]["exclusive_movie"] is True


def test_exclusive_movies_filter_shows_only_owner_selected_titles(
    client: TestClient,
    tmp_path: Path,
) -> None:
    create_video(tmp_path, "Selected (2026)/Selected.mkv")
    create_video(tmp_path, "Ordinary (2026)/Ordinary.mkv")
    store = MemoryVaultMasterStore()
    selected = catalogue_movie(store, tmp_path, "Selected (2026)/Selected.mkv")
    catalogue_movie(store, tmp_path, "Ordinary (2026)/Ordinary.mkv")
    store.set_movie_exclusive_state(selected.id, owner_identity(), True)
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    authenticate(client)

    response = client.get("/api/movies?view=exclusive")

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": str(selected.id),
            "asset_id": str(selected.id),
            "title": "Selected",
            "year": None,
            "poster_url": None,
            "is_exclusive_movie": True,
        }
    ]


def test_exclusive_toggle_keeps_theatre_identity_and_path(
    client: TestClient, tmp_path: Path
) -> None:
    relative_path = "Selected (2026)/Selected.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    asset = catalogue_movie(store, tmp_path, relative_path)
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    authenticate(client)

    response = client.post(f"/api/movies/{asset.id}/exclusive")

    assert response.status_code == 200
    updated = store.get_catalogued_asset_by_id(asset.id)
    assert updated is not None
    assert updated.id == asset.id
    assert updated.owner_user_id == asset.owner_user_id
    assert updated.vault_path == asset.vault_path
    assert updated.vault_path.startswith("/vault/Theatre/")
    assert updated.effective_metadata["exclusive_movie"] is True
    assert not list(tmp_path.rglob("*.json"))

    response = client.post(f"/api/movies/{asset.id}/exclusive")

    assert response.status_code == 200
    restored = store.get_catalogued_asset_by_id(asset.id)
    assert restored is not None
    assert restored.id == asset.id
    assert restored.owner_user_id == asset.owner_user_id
    assert restored.vault_path == asset.vault_path
    assert restored.effective_metadata["exclusive_movie"] is False


def test_historical_exclusive_path_reconciliation_restores_only_verified_theatre_file(
    tmp_path: Path,
) -> None:
    relative_path = "Historical (2026)/Historical.mkv"
    create_video(tmp_path, relative_path, b"canonical-theatre-bytes")
    store = MemoryVaultMasterStore()
    asset = catalogue_movie(store, tmp_path, relative_path)
    store.set_movie_exclusive_state(asset.id, owner_identity(), True)
    obsolete_path = f"/vault/Exclusive Movies/{asset.id}/Historical.mkv"
    relocated = store.relocate_catalogued_asset(
        asset.id,
        asset.vault_path,
        obsolete_path,
        owner_identity(),
        "exclusive_movie_verified_copy",
    )
    assert relocated is not None

    repaired = reconcile_historical_exclusive_movie_paths(store, tmp_path)

    restored = store.get_catalogued_asset_by_id(asset.id)
    assert repaired == (asset.id,)
    assert restored is not None
    assert restored.id == asset.id
    assert restored.owner_user_id == asset.owner_user_id
    assert restored.vault_path == asset.vault_path
    assert restored.effective_metadata["exclusive_movie"] is True
    assert reconcile_historical_exclusive_movie_paths(store, tmp_path) == ()


def test_historical_exclusive_path_reconciliation_fails_closed_without_history(
    tmp_path: Path,
) -> None:
    create_video(tmp_path, "Ambiguous (2026)/Ambiguous.mkv")
    store = MemoryVaultMasterStore()
    asset = catalogue_movie(store, tmp_path, "Ambiguous (2026)/Ambiguous.mkv")
    store.set_movie_exclusive_state(asset.id, owner_identity(), True)
    obsolete_path = f"/vault/Exclusive Movies/{asset.id}/Ambiguous.mkv"
    relocated = store.relocate_catalogued_asset(
        asset.id, asset.vault_path, obsolete_path, owner_identity(), "moved_to_folder"
    )
    assert relocated is not None

    assert reconcile_historical_exclusive_movie_paths(store, tmp_path) == ()
    assert store.get_catalogued_asset_by_id(asset.id).vault_path == obsolete_path


def test_playback_endpoint_requires_authentication(
    client: TestClient,
) -> None:
    response = client.get("/api/movies/example/playback")

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_playback_endpoint_matches_largest_movie_file_privately(
    client: TestClient,
    tmp_path: Path,
) -> None:
    main_relative_path = (
        "The Matrix (1999)/THE_MATRIX/The Matrix (1999).mkv"
    )
    create_video(
        tmp_path,
        main_relative_path,
        content=b"main-feature" * 10,
    )
    create_video(
        tmp_path,
        "The Matrix (1999)/extras/trailer.mp4",
        content=b"trailer",
    )
    store = MemoryVaultMasterStore()
    catalogue_movie(store, tmp_path, main_relative_path)
    movie = scan_movie_library(tmp_path)[0]
    playback_movie = JellyfinMovie(
        item_id="private-jellyfin-id",
        media_source_id="private-media-source-id",
        path=str(tmp_path / main_relative_path),
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3", "truehd"),
        subtitle_tracks=(
            JellyfinSubtitleTrack(
                index=8,
                title="Polish SDH",
                display_title="Polish SDH - PGSSUB",
                language="pol",
                codec="PGSSUB",
                is_external=False,
                is_default=True,
                is_forced=False,
                is_hearing_impaired=True,
            ),
        ),
    )
    jellyfin_client = FakeJellyfinClient(movie=playback_movie)
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: jellyfin_client
    )
    authenticate(client)

    response = client.get(f"/api/movies/{movie.id}/playback")

    assert response.status_code == 200
    assert response.json() == {
        "movie_id": movie.id,
        "status": "ready",
        "container": "mkv",
        "video_codec": "vc1",
        "audio_codecs": ["ac3", "truehd"],
        "subtitles": [
            {
                "index": 8,
                "title": "Polish SDH",
                "display_title": "Polish SDH - PGSSUB",
                "language": "pol",
                "codec": "PGSSUB",
                "is_external": False,
                "is_default": True,
                "is_forced": False,
                "is_hearing_impaired": True,
            }
        ],
    }
    assert jellyfin_client.requested_path == (
        tmp_path / main_relative_path
    )
    assert "private-jellyfin-id" not in response.text
    assert str(tmp_path) not in response.text


def test_playback_endpoint_reports_unindexed_movie(
    client: TestClient,
    tmp_path: Path,
) -> None:
    create_video(tmp_path, "The Matrix (1999)/The Matrix.mkv")
    store = MemoryVaultMasterStore()
    catalogue_movie(store, tmp_path, "The Matrix (1999)/The Matrix.mkv")
    movie = scan_movie_library(tmp_path)[0]
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: FakeJellyfinClient()
    )
    authenticate(client)

    response = client.get(f"/api/movies/{movie.id}/playback")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Movie is not available for playback"
    }


def test_playback_endpoint_reports_unavailable_service(
    client: TestClient,
    tmp_path: Path,
) -> None:
    create_video(tmp_path, "The Matrix (1999)/The Matrix.mkv")
    store = MemoryVaultMasterStore()
    catalogue_movie(store, tmp_path, "The Matrix (1999)/The Matrix.mkv")
    movie = scan_movie_library(tmp_path)[0]
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = lambda: (
        FakeJellyfinClient(
            error=JellyfinUnavailableError("test failure")
        )
    )
    authenticate(client)

    response = client.get(f"/api/movies/{movie.id}/playback")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Playback service is unavailable"
    }


def test_stream_endpoint_requires_authentication(
    client: TestClient,
) -> None:
    response = client.get("/api/movies/example/stream.mp4")

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_stream_endpoint_relays_video_without_exposing_jellyfin(
    client: TestClient,
    tmp_path: Path,
) -> None:
    relative_path = "The Matrix (1999)/The Matrix.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    catalogue_movie(store, tmp_path, relative_path)
    movie = scan_movie_library(tmp_path)[0]
    playback_movie = JellyfinMovie(
        item_id="private-jellyfin-id",
        media_source_id="private-media-source-id",
        path=str(tmp_path / relative_path),
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
    )
    stream = JellyfinStream(
        response=BytesIO(b"browser-compatible-video"),
        status_code=206,
        headers={
            "Accept-Ranges": "bytes",
            "Content-Range": "bytes 0-23/24",
        },
        url="http://pv-jellyfin:8096/Videos/item/stream.mp4",
        content_type="video/mp4",
    )
    jellyfin_client = FakeJellyfinClient(
        movie=playback_movie,
        stream=stream,
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: jellyfin_client
    )
    authenticate(client)

    response = client.get(
        f"/api/movies/{movie.id}/stream.mp4",
        headers={"Range": "bytes=0-1023"},
    )

    assert response.status_code == 206
    assert response.content == b"browser-compatible-video"
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert jellyfin_client.requested_range == "bytes=0-1023"
    assert "jellyfin" not in response.text.casefold()


def test_movie_downloads_offer_original_and_private_compressed_copy(
    client: TestClient,
    tmp_path: Path,
) -> None:
    relative_path = "Download Me (2026)/Download Me.mkv"
    create_video(tmp_path, relative_path, b"untouched-original")
    store = MemoryVaultMasterStore()
    catalogue_movie(store, tmp_path, relative_path)
    movie = scan_movie_library(tmp_path)[0]
    playback_movie = JellyfinMovie(
        item_id="download-item",
        media_source_id="download-source",
        path=str(tmp_path / relative_path),
        container="mkv",
        video_codec="h264",
        audio_codecs=("aac",),
    )
    jellyfin_client = FakeJellyfinClient(
        movie=playback_movie,
        stream=JellyfinStream(
            response=BytesIO(b"compressed-mp4"),
            status_code=200,
            headers={},
            url="http://pv-jellyfin/private",
            content_type="video/mp4",
        ),
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = lambda: jellyfin_client
    authenticate(client)

    original = client.get(f"/api/movies/{movie.id}/download/original")
    compressed = client.get(f"/api/movies/{movie.id}/download/compressed.mp4")

    assert original.content == b"untouched-original"
    assert "Download%20Me.mkv" in original.headers["content-disposition"]
    assert compressed.content == b"compressed-mp4"
    assert compressed.headers["content-type"] == "video/mp4"
    assert "Download Me.mp4" in compressed.headers["content-disposition"]
    assert compressed.headers["cache-control"] == "private, no-store"
    assert jellyfin_client.requested_range is None


def test_playback_endpoint_hides_private_movie_from_other_user(
    client: TestClient,
    tmp_path: Path,
) -> None:
    relative_path = "Private (2020)/Private.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    catalogue_movie(
        store,
        tmp_path,
        relative_path,
        owner_username="parent",
    )
    movie = scan_movie_library(tmp_path)[0]
    jellyfin_client = FakeJellyfinClient()
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = lambda: jellyfin_client
    authenticate(client)

    response = client.get(f"/api/movies/{movie.id}/playback")

    assert response.status_code == 404
    assert response.json() == {"detail": "Movie not found"}
    assert jellyfin_client.requested_path is None


def test_hls_master_requires_authentication(
    client: TestClient,
) -> None:
    response = client.get("/api/movies/example/hls/master.m3u8")

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_movie_catalogue_remains_browsable_without_jellyfin(
    client: TestClient,
    tmp_path: Path,
) -> None:
    relative_path = "The Matrix (1999)/The Matrix.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    asset = catalogue_movie(
        store,
        tmp_path,
        relative_path,
        artwork=("poster", "backdrop"),
        metadata={
            "display_title": "The Matrix: Canonical Edition",
            "release_year": 1999,
            "official_rating": "GB-15",
            "community_rating": 8.3,
            "runtime_ticks": 81_779_610_000,
            "overview": "A hacker discovers the truth.",
            "tagline": "Believe the unbelievable.",
            "genres": ["Action", "Science Fiction"],
            "studios": ["Warner Bros. Pictures"],
            "people": [
                {
                    "provider_item_id": "private-person-id",
                    "name": "Keanu Reeves",
                    "role": "Neo",
                    "type": "Actor",
                    "has_image": True,
                }
            ],
            "extras": [
                {
                    "provider_item_id": "private-extra-id",
                    "title": "title_t02",
                    "runtime_ticks": 25_040_430_000,
                }
            ],
            "trailers": [],
            "edition": "Anniversary Edition",
            "collections": ["The Matrix Collection"],
            "chapters": [
                {"name": "Wake up", "start_ticks": 0},
                {
                    "name": "Follow the white rabbit",
                    "start_ticks": 6_000_000_000,
                },
            ],
            "subtitles": [
                {
                    "index": 5,
                    "title": "English SDH",
                    "language": "eng",
                    "codec": "subrip",
                    "is_external": True,
                }
            ],
            "provider": {
                "name": "jellyfin",
                "imported_at": "2026-08-01T12:00:00+00:00",
            },
            "media": {
                "container": "mkv",
                "video_codec": "vc1",
                "audio_codecs": ["ac3", "truehd"],
            },
        },
    )
    metadata_root = tmp_path / "metadata"
    for kind in ("poster", "backdrop"):
        artwork_path = metadata_root / "artwork" / str(asset.id) / kind
        artwork_path.parent.mkdir(parents=True, exist_ok=True)
        artwork_path.write_bytes(b"private-poster")
    portrait_id = hashlib.sha256(b"private-person-id").hexdigest()[:16]
    portrait_key = f"artwork/{asset.id}/people/{portrait_id}"
    portrait_path = metadata_root / portrait_key
    portrait_path.parent.mkdir(parents=True, exist_ok=True)
    portrait_path.write_bytes(b"private-portrait")
    feature_id = hashlib.sha256(b"private-extra-id").hexdigest()[:16]
    feature_key = f"artwork/{asset.id}/features/{feature_id}"
    feature_path = metadata_root / feature_key
    feature_path.parent.mkdir(parents=True, exist_ok=True)
    feature_path.write_bytes(b"private-feature")
    retained_metadata = {
        **asset.imported_metadata,
        "people": [
            {
                **asset.imported_metadata["people"][0],
                "owned_image": {
                    "storage_key": portrait_key,
                    "mime_type": "image/jpeg",
                    "size_bytes": 16,
                },
            }
        ],
        "extras": [
            {
                **asset.imported_metadata["extras"][0],
                "owned_image": {
                    "storage_key": feature_key,
                    "mime_type": "image/jpeg",
                    "size_bytes": 15,
                },
            }
        ],
    }
    store.catalogued_assets[asset.vault_path] = type(asset)(
        **{
            **asset.__dict__,
            "imported_metadata": retained_metadata,
            "effective_metadata": retained_metadata,
        }
    )
    app.dependency_overrides[get_metadata_storage_root] = (
        lambda: metadata_root
    )
    movie = scan_movie_library(tmp_path)[0]
    def fail_if_jellyfin_is_resolved() -> FakeJellyfinClient:
        raise AssertionError(
            "Movie catalogue browsing must not resolve Jellyfin"
        )

    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        fail_if_jellyfin_is_resolved
    )
    authenticate(client)

    list_response = client.get("/api/movies")
    search_response = client.get(
        "/api/vault-master/assets/search",
        params={"query": "matrix"},
    )
    response = client.get(f"/api/movies/{movie.id}/details")
    body = response.json()

    assert list_response.status_code == 200
    assert list_response.json() == [
        {
            "id": str(asset.id),
            "asset_id": str(asset.id),
            "title": "The Matrix: Canonical Edition",
            "year": 1999,
            "poster_url": (
                f"/api/vault-master/assets/{asset.id}/artwork/poster"
            ),
            "is_exclusive_movie": False,
        }
    ]
    assert search_response.status_code == 200
    assert [item["id"] for item in search_response.json()["assets"]] == [
        str(asset.id)
    ]
    assert response.status_code == 200
    assert body["asset_id"] == str(asset.id)
    assert body["title"] == "The Matrix: Canonical Edition"
    assert body["runtime_minutes"] == 136
    assert body["genres"] == ["Action", "Science Fiction"]
    assert body["edition"] == "Anniversary Edition"
    assert body["collections"] == ["The Matrix Collection"]
    assert body["chapters"] == [
        {"name": "Wake up", "start_minutes": 0},
        {"name": "Follow the white rabbit", "start_minutes": 10},
    ]
    assert body["subtitles"] == [
        {
            "title": "English SDH",
            "language": "eng",
            "codec": "subrip",
            "is_external": True,
        }
    ]
    assert body["provider_imported_at"] == "2026-08-01T12:00:00+00:00"
    assert body["people"][0]["name"] == "Keanu Reeves"
    assert body["people"][0]["image_url"] == (
        f"/api/vault-master/assets/{asset.id}/people/{portrait_id}"
    )
    assert body["extras"][0]["title"] == "title_t02"
    assert body["extras"][0]["runtime_minutes"] == 42
    assert body["extras"][0]["id"] == feature_id
    assert body["extras"][0]["playback_available"] is True
    assert body["extras"][0]["thumbnail_url"] == (
        f"/api/vault-master/assets/{asset.id}/features/{feature_id}"
    )
    assert body["poster_url"] == (
        f"/api/vault-master/assets/{asset.id}/artwork/poster"
    )
    assert body["backdrop_url"] == (
        f"/api/vault-master/assets/{asset.id}/artwork/backdrop"
    )
    assert "private-person-id" not in response.text

    image_response = client.get(body["poster_url"])

    assert image_response.status_code == 200
    assert image_response.content == b"private-poster"
    assert image_response.headers["content-type"] == "image/jpeg"
    assert image_response.headers["x-content-type-options"] == "nosniff"
    portrait_response = client.get(body["people"][0]["image_url"])
    assert portrait_response.status_code == 200
    assert portrait_response.content == b"private-portrait"
    feature_response = client.get(body["extras"][0]["thumbnail_url"])
    assert feature_response.status_code == 200
    assert feature_response.content == b"private-feature"


def test_special_feature_hls_resolves_private_provider_after_catalogue_check(
    client: TestClient,
    tmp_path: Path,
) -> None:
    relative_path = "The Matrix (1999)/The Matrix.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    asset = catalogue_movie(
        store,
        tmp_path,
        relative_path,
        metadata={
            "extras": [
                {
                    "provider_item_id": "private-extra-id",
                    "title": "Making of",
                }
            ]
        },
    )
    movie = scan_movie_library(tmp_path)[0]
    feature_id = hashlib.sha256(b"private-extra-id").hexdigest()[:16]
    playback_movie = JellyfinMovie(
        item_id="private-extra-id",
        media_source_id="private-feature-source",
        path="/media/movies/The Matrix/extras/title_t02.mkv",
        container="mkv",
        video_codec="h264",
        audio_codecs=("aac",),
    )
    jellyfin_client = FakeJellyfinClient(
        movie=playback_movie,
        stream=JellyfinStream(
            response=BytesIO(b"#EXTM3U\nfeature.m3u8\n"),
            status_code=200,
            headers={},
            url=(
                "http://pv-jellyfin:8096/Videos/private-extra-id/"
                "master.m3u8"
            ),
            content_type="application/vnd.apple.mpegurl",
        ),
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: jellyfin_client
    )
    authenticate(client)

    response = client.get(
        f"/api/movies/{movie.id}/features/{feature_id}/hls/master.m3u8"
    )
    unknown_response = client.get(
        f"/api/movies/{movie.id}/features/unknown/hls/master.m3u8"
    )

    assert response.status_code == 200
    assert jellyfin_client.requested_item_id == "private-extra-id"
    assert "private-extra-id" not in response.text
    assert unknown_response.status_code == 404
    assert str(asset.id) not in response.text


def test_hls_master_rewrites_private_jellyfin_resources(
    client: TestClient,
    tmp_path: Path,
) -> None:
    relative_path = "The Matrix (1999)/The Matrix.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    catalogue_movie(store, tmp_path, relative_path)
    movie = scan_movie_library(tmp_path)[0]
    playback_movie = JellyfinMovie(
        item_id="private-jellyfin-id",
        media_source_id="private-media-source-id",
        path=str(tmp_path / relative_path),
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
    )
    playlist_url = (
        "http://pv-jellyfin:8096/Videos/private-jellyfin-id/"
        "master.m3u8"
    )
    stream = JellyfinStream(
        response=BytesIO(b"#EXTM3U\nmain.m3u8?session=private\n"),
        status_code=200,
        headers={},
        url=playlist_url,
        content_type="application/vnd.apple.mpegurl",
    )
    jellyfin_client = FakeJellyfinClient(
        movie=playback_movie,
        stream=stream,
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: jellyfin_client
    )
    authenticate(client)

    response = client.get(
        f"/api/movies/{movie.id}/hls/master.m3u8"
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "application/vnd.apple.mpegurl"
    )
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-pv-subtitle-stream-index"] == "off"
    assert response.headers["x-pv-subtitle-delivery-method"] == "off"
    assert jellyfin_client.requested_subtitle_index is None
    assert "pv-jellyfin" not in response.text
    assert "private-jellyfin-id" not in response.text
    resource_path = response.text.splitlines()[1]
    assert resource_path.startswith(
        f"/api/movies/{movie.id}/hls/"
    )

    nested_playlist_url = (
        "http://pv-jellyfin:8096/Videos/private-jellyfin-id/"
        "main.m3u8?session=private"
    )
    nested_stream = JellyfinStream(
        response=BytesIO(b"#EXTM3U\nsegment0.ts\n"),
        status_code=200,
        headers={},
        url=nested_playlist_url,
        content_type="application/vnd.apple.mpegurl",
    )
    jellyfin_client.stream = nested_stream
    nested_response = client.get(resource_path)

    assert nested_response.status_code == 200
    segment_path = nested_response.text.splitlines()[1]
    assert jellyfin_client.requested_hls_url == (
        "http://pv-jellyfin:8096/Videos/private-jellyfin-id/"
        "main.m3u8?session=private"
    )

    segment_url = (
        "http://pv-jellyfin:8096/Videos/private-jellyfin-id/"
        "segment0.ts"
    )
    jellyfin_client.stream = JellyfinStream(
        response=BytesIO(b"seekable-segment"),
        status_code=200,
        headers={"Content-Length": "16"},
        url=segment_url,
        content_type="video/mp2t",
    )
    resource_response = client.get(segment_path)

    assert resource_response.status_code == 200
    assert resource_response.content == b"seekable-segment"
    assert jellyfin_client.requested_hls_urls == [
        nested_playlist_url,
        segment_url,
    ]


def test_hls_master_selects_only_an_available_subtitle_track(
    client: TestClient,
    tmp_path: Path,
) -> None:
    relative_path = "The Matrix (1999)/The Matrix.mkv"
    create_video(tmp_path, relative_path)
    store = MemoryVaultMasterStore()
    catalogue_movie(store, tmp_path, relative_path)
    movie = scan_movie_library(tmp_path)[0]
    playback_movie = JellyfinMovie(
        item_id="private-jellyfin-id",
        media_source_id="private-media-source-id",
        path=str(tmp_path / relative_path),
        container="mkv",
        video_codec="vc1",
        audio_codecs=("ac3",),
        subtitle_tracks=(
            JellyfinSubtitleTrack(
                index=8,
                title=None,
                display_title="Polish - PGSSUB",
                language="pol",
                codec="PGSSUB",
                is_external=False,
                is_default=False,
                is_forced=False,
                is_hearing_impaired=False,
            ),
        ),
    )
    stream = JellyfinStream(
        response=BytesIO(b"#EXTM3U\nmain.m3u8\n"),
        status_code=200,
        headers={},
        url="http://pv-jellyfin:8096/Videos/private/master.m3u8",
        content_type="application/vnd.apple.mpegurl",
    )
    jellyfin_client = FakeJellyfinClient(
        movie=playback_movie,
        stream=stream,
    )
    app.dependency_overrides[get_movies_library_path] = lambda: tmp_path
    app.dependency_overrides[get_jellyfin_client] = (
        lambda: jellyfin_client
    )
    authenticate(client)

    selected = client.get(
        f"/api/movies/{movie.id}/hls/master.m3u8",
        params={"subtitle_index": 8},
    )

    assert selected.status_code == 200
    assert jellyfin_client.requested_subtitle_index == 8
    assert selected.headers["x-pv-subtitle-stream-index"] == "8"
    assert selected.headers["x-pv-subtitle-delivery-method"] == "encode"

    jellyfin_client.stream = JellyfinStream(
        response=BytesIO(b"#EXTM3U\nmain.m3u8\n"),
        status_code=200,
        headers={},
        url="http://pv-jellyfin:8096/Videos/private/master.m3u8",
        content_type="application/vnd.apple.mpegurl",
    )
    rejected = client.get(
        f"/api/movies/{movie.id}/hls/master.m3u8",
        params={"subtitle_index": 99},
    )

    assert rejected.status_code == 400
    assert rejected.json() == {
        "detail": "Subtitle track is not available"
    }
    assert jellyfin_client.requested_subtitle_index == 8
