from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.jellyfin import JellyfinMovie
from app.tv_playback import _playback_episode, _source_path, private_resources
from app.tv_shows import TvEpisodeSource


class Store:
    def __init__(self, allowed: set[tuple[object, object]]):
        self.allowed = allowed
    def visible_episode_source(self, episode_id, user_id):
        if (episode_id, user_id) not in self.allowed:
            return None
        return TvEpisodeSource(episode_id, uuid4(), "/vault/Theatre/TV Shows/Foundation/Season 01/Foundation - S01E01.mp4", user_id, "vault-wide")


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
