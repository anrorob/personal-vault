from pathlib import Path
from uuid import uuid4

import pytest

from app.jellyfin import JellyfinMovie, JellyfinUnavailableError
from app.tv_jellyfin_import import _retain_episode_artwork, import_pending_tv_metadata
from app.tv_shows import PendingTvEpisode


class Store:
    def __init__(self) -> None:
        self.pending = [PendingTvEpisode(uuid4(), uuid4(), "/vault/Theatre/TV Shows/Foundation/Season 01/Foundation - S01E01.mp4", 1)]
        self.imported = []
        self.failed = []
    def pending_metadata_episodes(self): return self.pending
    def import_episode_metadata(self, *args, **kwargs): self.imported.append((args, kwargs))
    def import_hierarchy_provider_ids(self, *args): self.hierarchy = args
    def mark_metadata_import_failed(self, ids): self.failed = ids


class Client:
    def __init__(self, answers): self.answers = iter(answers); self.refreshes = 0
    def find_episode_by_path(self, path: Path):
        answer = next(self.answers)
        return answer
    def refresh_library(self): self.refreshes += 1
    def get_tv_item_metadata(self, item_id): return {"Name": "The Emperor's Peace", "IndexNumber": 1, "SeriesId": "series", "SeasonId": "season", "ProviderIds": {"Tmdb": "123"}}


def _indexed() -> JellyfinMovie:
    return JellyfinMovie("episode", "source", "/media/tv/Foundation/Season 01/Foundation - S01E01.mp4", "mp4", "h264", ("aac",))


def test_tv_import_uses_filesystem_discovery_without_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PV_TV_SHOWS_PATH", "/media/tv")
    store, client = Store(), Client([_indexed()])
    assert import_pending_tv_metadata(store, client, discovery_wait_seconds=0, sleep=lambda _: None) == 1
    assert client.refreshes == 0 and len(store.imported) == 1


def test_tv_import_uses_exactly_one_refresh_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PV_TV_SHOWS_PATH", "/media/tv")
    store, client = Store(), Client([None, _indexed()])
    assert import_pending_tv_metadata(store, client, discovery_wait_seconds=0, sleep=lambda _: None) == 1
    assert client.refreshes == 1 and len(store.imported) == 1


def test_tv_import_fails_closed_after_single_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PV_TV_SHOWS_PATH", "/media/tv")
    store, client = Store(), Client([None, None])
    with pytest.raises(JellyfinUnavailableError):
        import_pending_tv_metadata(store, client, discovery_wait_seconds=0, sleep=lambda _: None)
    assert client.refreshes == 1 and not store.imported and store.failed == [store.pending[0].id]


def test_episode_still_is_retained_only_when_jellyfin_supplies_a_primary_image() -> None:
    episode_id = uuid4()

    class ArtworkStore:
        def __init__(self): self.retained = []
        def retain_episode_artwork(self, *args): self.retained.append(args)

    class Response:
        def close(self): pass

    class Stream:
        content_type = "image/jpeg; charset=binary"
        response = Response()
        def iter_bytes(self): return iter((b"owned-", b"still"))

    class ArtworkClient:
        def get_image_url(self, item_id, kind, max_width):
            assert (item_id, kind, max_width) == ("episode", "Primary", 1000)
            return "https://jellyfin.invalid/image"
        def open_resource(self, url):
            assert url == "https://jellyfin.invalid/image"
            return Stream()

    store = ArtworkStore()
    assert _retain_episode_artwork(store, ArtworkClient(), episode_id, "episode", {"ImageTags": {"Primary": "tag"}}) == {
        "storage_key": f"tv-artwork/episodes/{episode_id}/primary",
        "mime_type": "image/jpeg",
    }
    assert store.retained == [(episode_id, "primary", b"owned-still", "image/jpeg")]
    assert not _retain_episode_artwork(store, ArtworkClient(), episode_id, "episode", {"ImageTags": {}})
    assert len(store.retained) == 1
