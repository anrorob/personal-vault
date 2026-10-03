from pathlib import Path, PurePosixPath
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.jellyfin import JellyfinMovie
from app.tv_playback import _playback_episode, _source_path, episode_playback_info, private_resources
from app.tv_shows import TvEpisodeSource


@pytest.fixture(autouse=True)
def explicit_jellyfin_path_mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("PV_JELLYFIN_MEDIA_PATH_MAP_JSON", json.dumps([
        {"section": "tv", "pv_root": str(tmp_path), "jellyfin_root": "/provider/tv"}
    ]))


class Store:
    def __init__(self, allowed: set[tuple[object, object]]):
        self.allowed = allowed
    def visible_episode_source(self, episode_id, user_id):
        if (episode_id, user_id) not in self.allowed:
            return None
        return TvEpisodeSource(episode_id, uuid4(), "/vault/Theatre/TV Shows/Example Series/Season 01/Example Series - S01E01.mp4", user_id, "vault-wide")


class Client:
    def find_episode_by_path(self, path):
        return JellyfinMovie("episode", "source-a", str(path), "mp4", "h264", ("aac",))


def test_episode_show_audience_authorization_and_direct_uuid_denial(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PV_TV_SHOWS_PATH", str(tmp_path))
    episode, owner, second, inactive = uuid4(), uuid4(), uuid4(), uuid4()
    store = Store({(episode, owner), (episode, second)})  # vault-wide owner + active second user
    assert _playback_episode(episode, SimpleNamespace(user_id=owner), store, Client()).media_source_id == "source-a"
    assert _playback_episode(episode, SimpleNamespace(user_id=second), store, Client()).item_id == "episode"
    for user in (inactive, uuid4()):  # inactive/Only-me and arbitrary direct UUID access fail closed
        with pytest.raises(HTTPException) as error:
            _source_path(episode, SimpleNamespace(user_id=user), store)
        assert error.value.status_code == 404


def test_hls_resource_tokens_are_user_scoped() -> None:
    owner, other = uuid4(), uuid4()
    token = private_resources.issue(owner, "https://jellyfin.test/resource.ts")
    assert private_resources.resolve(owner, token) == "https://jellyfin.test/resource.ts"
    assert private_resources.resolve(other, token) is None


def test_episode_diagnostics_use_authorized_source_and_playback_decision(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app import tv_playback

    monkeypatch.setenv("PV_ENVIRONMENT", "development")
    monkeypatch.setenv("PV_JELLYFIN_ENABLED", "true")
    monkeypatch.setenv("PV_TV_SHOWS_PATH", str(tmp_path))
    episode, owner = uuid4(), uuid4()
    store = Store({(episode, owner)})

    class Provider(Client):
        def playback_source_info(self, item):
            assert item.media_source_id == "source-a"
            return {"container": "mp4"}

        def playback_decision(self, item, *, h264_supported):
            assert h264_supported is True
            return {"direct_play_supported": True, "reasons": []}

    monkeypatch.setattr(tv_playback, "get_jellyfin_client", Provider)
    result = episode_playback_info(episode, SimpleNamespace(user_id=owner), store, None, True)
    assert result["jellyfin_decision"]["direct_play_supported"] is True
    assert result["actual_session"]["status"] == "UNOBSERVED"
    with pytest.raises(HTTPException) as error:
        episode_playback_info(episode, SimpleNamespace(user_id=uuid4()), store, None, True)
    assert error.value.status_code == 404


def test_episode_source_uses_authoritative_slot_placement(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    slot = tmp_path / "slot"
    relative = "Theatre/TV Shows/Example/Season 01/Example - S01E01.mkv"
    physical = slot / relative
    physical.parent.mkdir(parents=True)
    physical.write_bytes(b"synthetic")
    monkeypatch.setenv("PV_STORAGE_SLOT_ROOTS_JSON", json.dumps({"PV-DEV-DISK-001": str(slot)}))
    episode, owner = uuid4(), uuid4()

    class PlacedStore:
        def visible_episode_source(self, selected_episode, selected_owner):
            if (selected_episode, selected_owner) != (episode, owner):
                return None
            return TvEpisodeSource(episode, uuid4(), "/vault/" + relative, owner, "vault-wide",
                                   {"slot_id": "PV-DEV-DISK-001", "relative_path": relative})

    assert _source_path(episode, SimpleNamespace(user_id=owner), PlacedStore()) == physical
    with pytest.raises(HTTPException):
        _source_path(episode, SimpleNamespace(user_id=uuid4()), PlacedStore())

    class MismatchedStore(PlacedStore):
        def visible_episode_source(self, selected_episode, selected_owner):
            source = super().visible_episode_source(selected_episode, selected_owner)
            return None if source is None else TvEpisodeSource(
                source.id, source.asset_id, source.vault_path, source.owner_user_id,
                source.visibility, {"slot_id": "PV-DEV-DISK-001", "relative_path": "Theatre/TV Shows/other.mkv"})

    with pytest.raises(HTTPException):
        _source_path(episode, SimpleNamespace(user_id=owner), MismatchedStore())


def test_episode_quality_plan_authorizes_before_negotiation(monkeypatch, tmp_path):
    from app.tv_playback import episode_playback_plan
    from app.playback_policy import PlaybackPlanRequest, BrowserCapabilities
    from tests.test_playback_policy import Client as PlanProvider
    monkeypatch.setenv("PV_TV_SHOWS_PATH", str(tmp_path))
    monkeypatch.setenv("PV_JELLYFIN_ENABLED", "true")
    episode, owner = uuid4(), uuid4()
    store = Store({(episode, owner)})
    class Provider(PlanProvider):
        def find_episode_by_path(self, path):
            assert path == PurePosixPath("/provider/tv/Example Series/Season 01/Example Series - S01E01.mp4")
            return JellyfinMovie("example", "source", str(path), "mp4", "h264", ("aac",))
    provider = Provider(direct=True)
    payload = PlaybackPlanRequest(capabilities=BrowserCapabilities(direct=True))
    with pytest.raises(HTTPException) as error:
        episode_playback_plan(episode, payload, SimpleNamespace(user_id=uuid4()), store, provider)
    assert error.value.status_code == 404
    result = episode_playback_plan(episode, payload, SimpleNamespace(user_id=owner), store, provider)
    assert result["source_type"] == "file"
    assert result["url"].startswith(f"/api/tv-shows/episodes/{episode}/hls/")


def test_episode_invalid_mapping_fails_before_provider_and_disabled_is_unchanged(monkeypatch, tmp_path):
    from app.tv_playback import episode_playback_plan
    from app.playback_policy import PlaybackPlanRequest

    monkeypatch.setenv("PV_TV_SHOWS_PATH", str(tmp_path))
    monkeypatch.setenv("PV_ENVIRONMENT", "development")
    monkeypatch.setenv("PV_JELLYFIN_MEDIA_PATH_MAP_JSON", "[]")
    episode, owner = uuid4(), uuid4()
    store = Store({(episode, owner)})
    user = SimpleNamespace(user_id=owner)

    class UnusedProvider:
        def find_episode_by_path(self, path):
            pytest.fail("Unmapped files must not reach Jellyfin")

    monkeypatch.setenv("PV_JELLYFIN_ENABLED", "true")
    with pytest.raises(HTTPException) as error:
        episode_playback_plan(episode, PlaybackPlanRequest(), user, store, UnusedProvider())
    assert error.value.status_code == 503
    monkeypatch.setenv("PV_JELLYFIN_ENABLED", "false")
    assert episode_playback_info(episode, user, store, None, True) == {
        "availability": "jellyfin_disabled", "source": None, "playback": None}
    with pytest.raises(HTTPException) as error:
        episode_playback_plan(episode, PlaybackPlanRequest(), user, store, UnusedProvider())
    assert error.value.status_code == 503
    assert error.value.detail == "Playback service is disabled"
