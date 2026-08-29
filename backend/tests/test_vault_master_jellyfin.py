from datetime import date, datetime
from dataclasses import asdict, replace
import json
from pathlib import Path
from uuid import uuid4

import pytest

from app.jellyfin import (
    JellyfinAudio,
    JellyfinChapter,
    JellyfinExtra,
    JellyfinMovie,
    JellyfinMovieDetails,
    JellyfinPerson,
    JellyfinSubtitle,
    JellyfinUnavailableError,
)
from app.vault_master import CataloguedAsset, MemoryVaultMasterStore
from app.vault_master_jellyfin import (
    import_jellyfin_music_library,
    import_jellyfin_movie,
    import_jellyfin_movie_library,
    person_portrait_id,
    publish_jellyfin_media_update,
    publish_jellyfin_media_updates,
)
from app.theatre_movie_rename import TheatreMovieRenameRequest
from app import vault_master_jellyfin


def test_publication_routes_only_jellyfin_served_libraries(
    tmp_path: Path,
    monkeypatch,
) -> None:
    roots = {
        "PV_MOVIES_PATH": tmp_path / "movies",
        "PV_TV_SHOWS_PATH": tmp_path / "tv",
        "PV_MUSIC_PATH": tmp_path / "music",
    }
    for name, root in roots.items():
        root.mkdir()
        monkeypatch.setenv(name, str(root))
    personal_videos = tmp_path / "personal-videos"
    personal_videos.mkdir()
    paths = [
        roots["PV_MOVIES_PATH"] / "movie.mkv",
        roots["PV_TV_SHOWS_PATH"] / "episode.mkv",
        roots["PV_MUSIC_PATH"] / "track.wma",
        personal_videos / "home-video.mp4",
    ]
    for path in paths:
        path.write_bytes(b"media")
    published: list[tuple[Path, ...]] = []

    class Client:
        def __init__(self) -> None:
            self.refresh_count = 0

        def notify_media_updated(self, media_paths: tuple[Path, ...]) -> None:
            published.append(media_paths)

        def refresh_library(self) -> None:
            self.refresh_count += 1

    client = Client()
    monkeypatch.setattr(
        vault_master_jellyfin,
        "get_jellyfin_metadata_client",
        lambda: client,
    )

    results = [publish_jellyfin_media_update(path) for path in paths]

    assert results == [True, True, True, False]
    assert published == [(path.resolve(),) for path in paths[:3]]
    # TV discovery/one-refresh compatibility fallback is now owned by the
    # bounded VM-079 TV importer; ordinary publication only refreshes Music.
    assert client.refresh_count == 1

    published.clear()
    assert publish_jellyfin_media_updates(tuple(paths)) == 3
    assert published == [tuple(path.resolve() for path in paths[:3])]
    assert client.refresh_count == 2


def test_jellyfin_music_import_is_retained_by_vault_master(tmp_path: Path) -> None:
    music_root = tmp_path / "music"
    music_root.mkdir()
    track_path = music_root / "Teardrop.flac"
    track_path.write_bytes(b"audio")
    asset = CataloguedAsset(
        id=uuid4(),
        asset_type="Music",
        display_title="Teardrop",
        captured_on=None,
        location=None,
        vault_path="/vault/Music/Teardrop.flac",
        filename="Teardrop.flac",
        size_bytes=5,
        mime_type="audio/flac",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={},
        detected_metadata={"display_title": "Teardrop"},
        effective_metadata={"display_title": "Teardrop"},
    )
    store = MemoryVaultMasterStore()
    store.catalogued_assets[asset.vault_path] = asset

    class Client:
        def find_audio_by_path(self, path: Path) -> JellyfinAudio:
            assert path == track_path
            return JellyfinAudio(
                "jf-audio",
                "jf-source",
                str(path),
                "flac",
                "flac",
                True,
                "jf-album",
            )

        def get_audio_details(self, audio: JellyfinAudio) -> dict[str, object]:
            assert audio.item_id == "jf-audio"
            return {
                "display_title": "Teardrop",
                "artist": "Massive Attack",
                "album": "Mezzanine",
                "genres": ["Trip-hop"],
                "provider_ids": {"MusicBrainzTrack": "recording-id"},
            }

        def get_audio_lyrics(self, audio: JellyfinAudio) -> dict[str, object]:
            return {
                "text": "Love is a doing word",
                "lines": [{"text": "Love is a doing word"}],
                "metadata": {},
            }

        def get_image_url(
            self,
            item_id: str,
            image_type: str,
            *,
            max_width: int,
        ) -> str:
            assert (item_id, image_type, max_width) == (
                "jf-album",
                "Primary",
                1000,
            )
            return "https://jellyfin.invalid/jf-album/primary"

        def open_resource(self, url: str):
            assert url.endswith("/jf-album/primary")

            class Stream:
                content_type = "image/jpeg"

                def iter_bytes(self):
                    yield b"album-cover"

            return Stream()

    artwork_root = tmp_path / "metadata"
    imported, failed = import_jellyfin_music_library(
        store,
        music_root,
        Client(),
        artwork_root=artwork_root,
    )
    updated = store.get_catalogued_asset(asset.vault_path)
    assert (imported, failed) == (1, 0)
    assert updated is not None
    assert updated.effective_metadata["artist"] == "Massive Attack"
    assert updated.imported_metadata["provider_ids"] == {
        "MusicBrainzTrack": "recording-id"
    }
    assert updated.imported_metadata["provider"]["name"] == "jellyfin"
    assert updated.imported_metadata["lyrics"]["text"] == (
        "Love is a doing word"
    )
    owned_cover = updated.imported_metadata["artwork"]["owned"]["primary"]
    assert owned_cover["provider_item_id"] == "jf-album"
    assert (artwork_root / owned_cover["storage_key"]).read_bytes() == (
        b"album-cover"
    )
    assert "jf-audio" not in updated.display_title


def test_jellyfin_music_import_supports_wma_and_preserves_user_title(
    tmp_path: Path,
) -> None:
    music_root = tmp_path / "music"
    album = music_root / "Imagine Dragons" / "Mercury - Act 1"
    album.mkdir(parents=True)
    track_path = album / "13 One Day.wma"
    track_path.write_bytes(b"wma")
    asset = CataloguedAsset(
        id=uuid4(),
        asset_type="Music",
        display_title="One day",
        captured_on=None,
        location=None,
        vault_path="/vault/Music/Imagine Dragons/Mercury - Act 1/13 One Day.wma",
        filename=track_path.name,
        size_bytes=3,
        mime_type="audio/x-ms-wma",
        sha256="b" * 64,
        metadata={},
        metadata_provenance={"display_title": "user_override"},
        detected_metadata={"display_title": "Track13"},
        imported_metadata={
            "provider": {"name": "musicbrainz"},
            "musicbrainz": {"release_id": "release-id"},
            "genres": ["Alternative rock"],
        },
        user_overrides={
            "display_title": "One day",
            "artist": "Imagine Dragons",
            "album": "Mercury - Act 1",
        },
        effective_metadata={
            "display_title": "One day",
            "artist": "Imagine Dragons",
            "album": "Mercury - Act 1",
            "genres": ["Alternative rock"],
        },
    )
    store = MemoryVaultMasterStore()
    store.catalogued_assets[asset.vault_path] = asset

    class Client:
        def find_audio_by_path(self, path: Path) -> JellyfinAudio:
            assert path == track_path
            return JellyfinAudio("jf-wma", "jf-source", str(path), "asf", "wmapro")

        def get_audio_details(self, audio: JellyfinAudio) -> dict[str, object]:
            return {
                "display_title": "One Day",
                "artist": "Wrong Jellyfin artist",
                "album": "Wrong Jellyfin album",
                "genres": [],
            }

    imported, failed = import_jellyfin_music_library(store, music_root, Client())
    updated = store.get_catalogued_asset(asset.vault_path)

    assert (imported, failed) == (1, 0)
    assert updated is not None
    assert updated.display_title == "One day"
    assert updated.effective_metadata["display_title"] == "One day"
    assert updated.effective_metadata["artist"] == "Imagine Dragons"
    assert updated.effective_metadata["album"] == "Mercury - Act 1"
    assert updated.effective_metadata["genres"] == ["Alternative rock"]
    assert updated.imported_metadata["provider"]["name"] == "musicbrainz"
    assert updated.imported_metadata["jellyfin"]["name"] == "jellyfin"


def movie_asset() -> CataloguedAsset:
    return CataloguedAsset(
        id=uuid4(),
        asset_type="Movies",
        display_title="Detected title",
        captured_on=date(2001, 1, 1),
        location=None,
        vault_path="/vault/Theatre/Movies/example.mkv",
        filename="example.mkv",
        size_bytes=100,
        mime_type="video/x-matroska",
        sha256="a" * 64,
        metadata={},
        metadata_provenance={
            "display_title": "filename",
            "captured_on": "filesystem",
            "location": "unavailable",
        },
        detected_metadata={
            "display_title": "Detected title",
            "captured_on": "2001-01-01",
        },
        imported_metadata={},
        user_overrides={},
        effective_metadata={
            "display_title": "Detected title",
            "captured_on": "2001-01-01",
            "location": None,
        },
    )


def movie_provider_records() -> tuple[JellyfinMovie, JellyfinMovieDetails]:
    return (
        JellyfinMovie(
            item_id="jf-movie",
            media_source_id="jf-source",
            path="/media/movies/example.mkv",
            container="mkv",
            video_codec="hevc",
            audio_codecs=("dts",),
            has_primary_image=True,
        ),
        JellyfinMovieDetails(
            title="Imported title",
            year=1999,
            official_rating="12",
            community_rating=8.2,
            runtime_ticks=72_000_000_000,
            overview="A durable synopsis.",
            tagline="Keep the record.",
            genres=("Drama",),
            studios=("Example Studio",),
            people=(
                JellyfinPerson(
                    item_id="person-1",
                    name="Example Director",
                    role=None,
                    person_type="Director",
                    has_image=True,
                ),
            ),
            extras=(
                JellyfinExtra(
                    item_id="extra-1",
                    name="Behind the scenes",
                    runtime_ticks=1_000_000,
                ),
            ),
            trailers=(),
            has_primary_image=True,
            has_backdrop_image=True,
            provider_ids={"Imdb": "tt1234567", "Tmdb": "123"},
            edition="Director's Cut",
            collections=("Example Collection",),
            chapters=(
                JellyfinChapter(name="Opening", start_ticks=0),
                JellyfinChapter(name="Discovery", start_ticks=6_000_000_000),
            ),
            subtitles=(
                JellyfinSubtitle(
                    index=4,
                    title="English SDH",
                    language="eng",
                    codec="subrip",
                    is_external=True,
                ),
            ),
        ),
    )


def test_jellyfin_movie_import_is_stored_in_vault_master() -> None:
    store = MemoryVaultMasterStore()
    asset = movie_asset()
    store.catalogued_assets[asset.vault_path] = asset
    movie, details = movie_provider_records()

    imported = import_jellyfin_movie(store, asset, movie, details)

    assert imported.display_title == "Imported title"
    assert imported.detected_metadata == asset.detected_metadata
    assert (
        imported.metadata_provenance["display_title"]
        == "import:jellyfin"
    )
    assert imported.imported_metadata["overview"] == "A durable synopsis."
    assert imported.imported_metadata["provider_ids"] == {
        "Imdb": "tt1234567",
        "Tmdb": "123",
    }
    assert imported.imported_metadata["edition"] == "Director's Cut"
    assert imported.imported_metadata["collections"] == ["Example Collection"]
    assert imported.imported_metadata["chapters"][1] == {
        "name": "Discovery",
        "start_ticks": 6_000_000_000,
    }
    assert imported.imported_metadata["subtitles"][0]["language"] == "eng"
    imported_at = datetime.fromisoformat(
        imported.imported_metadata["provider"]["imported_at"]
    )
    assert imported_at.tzinfo is not None
    assert imported.imported_metadata["people"] == [
        {
            "provider_item_id": "person-1",
            "name": "Example Director",
            "role": None,
            "type": "Director",
            "has_image": True,
        }
    ]
    assert imported.effective_metadata["display_title"] == "Imported title"


def test_reliable_jellyfin_identity_queues_and_completes_provisional_rename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    movies_root = tmp_path / "movies"
    path = movies_root / "TRON" / "TRON.mkv"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"movie")
    owner = uuid4()
    asset = replace(
        movie_asset(),
        owner_user_id=owner,
        vault_path="/vault/Theatre/Movies/TRON/TRON.mkv",
        filename="TRON.mkv",
        detected_metadata={
            "display_title": "TRON",
            "movie_identity_provisional": {
                "state": "provisional",
                "hint": "TRON",
                "source_relative_path": "TRON/Tron_t00.mkv",
                "source_filename": "Tron_t00.mkv",
            },
        },
        effective_metadata={
            "display_title": "TRON",
            "storage_placement": {
                "slot_id": "PV-DISK-001",
                "relative_path": "Theatre/Movies/TRON/TRON.mkv",
            },
        },
    )
    store = MemoryVaultMasterStore()
    store.catalogued_assets[asset.vault_path] = asset
    queued: list[tuple[dict[str, object], str, str, int]] = []
    monkeypatch.setattr(
        vault_master_jellyfin,
        "queue_movie_rename",
        lambda snapshot, destination, title, year: queued.append(
            (snapshot, destination, title, year)
        ),
    )
    movie, details = movie_provider_records()
    movie = replace(movie, path=str(path))
    details = replace(details, title="Tron", year=1982)

    class Client(FakeJellyfinClient):
        def find_movie_by_path(self, candidate: Path) -> JellyfinMovie | None:
            return movie if candidate == path else None

    assert import_jellyfin_movie_library(store, movies_root, Client(movie, details)) == (1, 0)
    assert len(queued) == 1
    snapshot, destination, title, year = queued[0]
    assert destination == "/vault/Theatre/Movies/Tron (1982)/Tron (1982).mkv"
    request = TheatreMovieRenameRequest.create(snapshot, destination, title, year)
    receipt = json.loads(
        json.dumps(
            {
                **asdict(request),
                "completed_at": datetime.now().astimezone().isoformat(),
            },
            default=str,
        )
    )

    renamed = store.complete_theatre_movie_rename(receipt)

    assert renamed is not None
    assert renamed.vault_path == destination
    assert renamed.user_overrides == {}
    assert renamed.metadata_provenance["display_title"] == "import:jellyfin"
    assert renamed.effective_metadata["release_year"] == 1982


def test_companion_set_imports_extras_on_main_without_detaching_rename(
    tmp_path: Path,
    monkeypatch,
) -> None:
    movies_root = tmp_path / "movies"
    main_path = movies_root / "TRON" / "TRON.mkv"
    extra_path = movies_root / "TRON" / "extras" / "Tron_t01.mkv"
    extra_path.parent.mkdir(parents=True)
    main_path.write_bytes(b"main")
    extra_path.write_bytes(b"extra")
    owner = uuid4()
    publication_set = {
        "schema": "personal-vault.movie-publication-set.v1",
        "source_directory": "TRON",
        "main_item_id": str(uuid4()),
        "role": "main",
        "companion_count": 1,
        "members": [],
    }
    main = replace(
        movie_asset(),
        owner_user_id=owner,
        vault_path="/vault/Theatre/Movies/TRON/TRON.mkv",
        filename="TRON.mkv",
        detected_metadata={
            "movie_identity_provisional": {"state": "provisional"},
            "movie_publication_set": publication_set,
        },
    )
    companion = replace(
        movie_asset(),
        id=uuid4(),
        owner_user_id=owner,
        vault_path="/vault/Theatre/Movies/TRON/extras/Tron_t01.mkv",
        filename="Tron_t01.mkv",
        sha256="b" * 64,
        detected_metadata={
            "movie_publication_set": {
                **publication_set,
                "role": "extra",
            }
        },
    )
    store = MemoryVaultMasterStore()
    store.catalogued_assets[main.vault_path] = main
    store.catalogued_assets[companion.vault_path] = companion
    movie, details = movie_provider_records()
    movie = replace(movie, path=str(main_path))
    details = replace(details, title="Tron", year=1982)
    observed: list[Path] = []

    class Client(FakeJellyfinClient):
        def find_movie_by_path(self, candidate: Path) -> JellyfinMovie | None:
            observed.append(candidate)
            return movie if candidate == main_path else None

    monkeypatch.setattr(
        vault_master_jellyfin,
        "queue_movie_rename",
        lambda *args: pytest.fail(
            "a main-only rename must not detach managed companions"
        ),
    )

    assert import_jellyfin_movie_library(
        store, movies_root, Client(movie, details)
    ) == (1, 0)
    imported_main = store.get_catalogued_asset(main.vault_path)
    imported_companion = store.get_catalogued_asset(companion.vault_path)
    assert imported_main is not None and imported_companion is not None
    assert imported_main.imported_metadata["extras"][0]["provider_item_id"] == "extra-1"
    assert imported_companion.imported_metadata == {}
    assert set(observed) == {main_path, extra_path}


def test_unidentified_jellyfin_result_does_not_queue_provisional_rename(
    monkeypatch,
) -> None:
    asset = replace(
        movie_asset(),
        owner_user_id=uuid4(),
        detected_metadata={
            "movie_identity_provisional": {"state": "provisional"}
        },
        imported_metadata={
            "display_title": "Possible title",
            "release_year": 1982,
            "provider_ids": {},
        },
        effective_metadata={
            "storage_placement": {
                "slot_id": "PV-DISK-001",
                "relative_path": "Theatre/Movies/TRON/TRON.mkv",
            }
        },
    )
    store = MemoryVaultMasterStore()
    store.catalogued_assets[asset.vault_path] = asset
    monkeypatch.setattr(
        vault_master_jellyfin,
        "queue_movie_rename",
        lambda *args: pytest.fail("unreliable identity must not queue a rename"),
    )

    assert not vault_master_jellyfin.queue_provisional_movie_identity_rename(
        store, asset
    )


def test_jellyfin_refresh_does_not_replace_user_override() -> None:
    store = MemoryVaultMasterStore()
    asset = movie_asset()
    store.catalogued_assets[asset.vault_path] = asset
    corrected = store.update_catalogued_asset_metadata(
        asset.id,
        {"display_title": "My corrected title"},
        "owner",
    )
    assert corrected is not None
    movie, details = movie_provider_records()

    imported = import_jellyfin_movie(store, corrected, movie, details)

    assert imported.display_title == "My corrected title"
    assert imported.user_overrides["display_title"] == "My corrected title"
    assert imported.imported_metadata["display_title"] == "Imported title"
    assert imported.metadata_provenance["display_title"] == "user_override"


class FakeJellyfinClient:
    def __init__(
        self,
        movie: JellyfinMovie,
        details: JellyfinMovieDetails,
        *,
        unavailable: bool = False,
    ) -> None:
        self.movie = movie
        self.details = details
        self.unavailable = unavailable

    def find_movie_by_path(self, path: Path) -> JellyfinMovie | None:
        if self.unavailable:
            raise JellyfinUnavailableError("offline")
        return self.movie if path.name == "example.mkv" else None

    def get_movie_details(
        self,
        movie: JellyfinMovie,
    ) -> JellyfinMovieDetails:
        assert movie == self.movie
        return self.details


class FakeArtworkStream:
    def __init__(
        self,
        content: bytes,
        content_type: str = "image/jpeg",
    ) -> None:
        self.content = content
        self.content_type = content_type

    def iter_bytes(self):
        yield self.content


class FakeArtworkJellyfinClient(FakeJellyfinClient):
    def __init__(
        self,
        movie: JellyfinMovie,
        details: JellyfinMovieDetails,
        *,
        fail_artwork: bool = False,
    ) -> None:
        super().__init__(movie, details)
        self.fail_artwork = fail_artwork

    def get_image_url(
        self,
        item_id: str,
        image_type: str,
        *,
        image_index: int | None = None,
        max_width: int,
    ) -> str:
        del image_index, max_width
        assert item_id in {self.movie.item_id, "person-1"}
        return (
            f"https://jellyfin.invalid/{item_id}/"
            f"{image_type.casefold()}"
        )

    def open_resource(self, url: str) -> FakeArtworkStream:
        if self.fail_artwork:
            raise JellyfinUnavailableError("artwork offline")
        return FakeArtworkStream(
            b"portrait-bytes"
            if "/person-1/" in url
            else b"poster-bytes"
            if url.endswith("/primary")
            else b"backdrop-bytes"
        )


def test_movie_library_import_enriches_only_catalogued_files(
    tmp_path: Path,
) -> None:
    movie_path = tmp_path / "example.mkv"
    movie_path.write_bytes(b"movie")
    (tmp_path / "uncatalogued.mkv").write_bytes(b"other")
    store = MemoryVaultMasterStore()
    asset = movie_asset()
    store.catalogued_assets[asset.vault_path] = asset
    movie, details = movie_provider_records()

    result = import_jellyfin_movie_library(
        store,
        tmp_path,
        FakeJellyfinClient(movie, details),  # type: ignore[arg-type]
    )

    assert result == (1, 0)
    persisted = store.get_catalogued_asset(asset.vault_path)
    assert persisted is not None
    assert persisted.imported_metadata["overview"] == "A durable synopsis."


def test_provider_failure_keeps_last_successful_snapshot(
    tmp_path: Path,
) -> None:
    (tmp_path / "example.mkv").write_bytes(b"movie")
    store = MemoryVaultMasterStore()
    asset = movie_asset()
    store.catalogued_assets[asset.vault_path] = asset
    movie, details = movie_provider_records()
    imported = import_jellyfin_movie(store, asset, movie, details)

    result = import_jellyfin_movie_library(
        store,
        tmp_path,
        FakeJellyfinClient(  # type: ignore[arg-type]
            movie,
            details,
            unavailable=True,
        ),
    )

    assert result == (0, 1)
    persisted = store.get_catalogued_asset(asset.vault_path)
    assert persisted is not None
    assert persisted.imported_metadata == imported.imported_metadata


def test_complete_movie_provider_snapshot_is_exported_to_portable_sidecar(
    tmp_path: Path,
) -> None:
    store = MemoryVaultMasterStore(sidecar_root=tmp_path)
    asset = movie_asset()
    store.catalogued_assets[asset.vault_path] = asset
    movie, details = movie_provider_records()

    imported = import_jellyfin_movie(store, asset, movie, details)

    sidecar = json.loads(
        (tmp_path / "sidecars" / f"{imported.id}.json").read_text(
            encoding="utf-8"
        )
    )
    retained = sidecar["metadata"]["imported"]
    assert retained["edition"] == "Director's Cut"
    assert retained["collections"] == ["Example Collection"]
    assert retained["chapters"][1]["name"] == "Discovery"
    assert retained["subtitles"][0]["language"] == "eng"


def test_movie_library_retains_owned_artwork(
    tmp_path: Path,
) -> None:
    movies_root = tmp_path / "movies"
    artwork_root = tmp_path / "metadata"
    movies_root.mkdir()
    (movies_root / "example.mkv").write_bytes(b"movie")
    store = MemoryVaultMasterStore()
    asset = movie_asset()
    store.catalogued_assets[asset.vault_path] = asset
    movie, details = movie_provider_records()

    result = import_jellyfin_movie_library(
        store,
        movies_root,
        FakeArtworkJellyfinClient(  # type: ignore[arg-type]
            movie,
            details,
        ),
        artwork_root=artwork_root,
    )

    assert result == (1, 0)
    persisted = store.get_catalogued_asset(asset.vault_path)
    assert persisted is not None
    owned = persisted.imported_metadata["artwork"]["owned"]
    assert owned["poster"]["storage_key"] == (
        f"artwork/{asset.id}/poster"
    )
    assert owned["backdrop"]["storage_key"] == (
        f"artwork/{asset.id}/backdrop"
    )
    assert (
        artwork_root / owned["poster"]["storage_key"]
    ).read_bytes() == b"poster-bytes"
    assert (
        artwork_root / owned["backdrop"]["storage_key"]
    ).read_bytes() == b"backdrop-bytes"
    person = persisted.imported_metadata["people"][0]
    portrait = person["owned_image"]
    assert portrait["provider_item_id"] == "person-1"
    assert portrait["storage_key"].startswith(
        f"artwork/{asset.id}/people/"
    )
    assert (
        artwork_root / portrait["storage_key"]
    ).read_bytes() == b"portrait-bytes"


def test_artwork_failure_preserves_last_owned_copy(
    tmp_path: Path,
) -> None:
    movies_root = tmp_path / "movies"
    artwork_root = tmp_path / "metadata"
    movies_root.mkdir()
    (movies_root / "example.mkv").write_bytes(b"movie")
    store = MemoryVaultMasterStore()
    asset = movie_asset()
    store.catalogued_assets[asset.vault_path] = asset
    movie, details = movie_provider_records()
    previous_owned = {
        "poster": {
            "storage_key": f"artwork/{asset.id}/poster",
            "mime_type": "image/jpeg",
            "size_bytes": 8,
        }
    }
    portrait_id = person_portrait_id("person-1")
    previous_portrait = {
        "storage_key": f"artwork/{asset.id}/people/{portrait_id}",
        "mime_type": "image/jpeg",
        "size_bytes": 9,
    }
    asset = import_jellyfin_movie(store, asset, movie, details)
    store.catalogued_assets[asset.vault_path] = asset
    stored = store.import_catalogued_asset_metadata(
        asset.id,
        {
            **asset.imported_metadata,
            "artwork": {
                **asset.imported_metadata["artwork"],
                "owned": previous_owned,
            },
            "people": [
                {
                    **asset.imported_metadata["people"][0],
                    "owned_image": previous_portrait,
                }
            ],
        },
        "jellyfin",
    )
    assert stored is not None

    result = import_jellyfin_movie_library(
        store,
        movies_root,
        FakeArtworkJellyfinClient(  # type: ignore[arg-type]
            movie,
            details,
            fail_artwork=True,
        ),
        artwork_root=artwork_root,
    )

    assert result == (1, 0)
    persisted = store.get_catalogued_asset(asset.vault_path)
    assert persisted is not None
    assert (
        persisted.imported_metadata["artwork"]["owned"]
        == previous_owned
    )
    assert (
        persisted.imported_metadata["people"][0]["owned_image"]
        == previous_portrait
    )
