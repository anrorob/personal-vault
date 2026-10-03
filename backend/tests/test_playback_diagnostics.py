from types import SimpleNamespace
import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.jellyfin import HlsResourceStore, JellyfinClient, JellyfinMovie, JellyfinSubtitleTrack, JellyfinUnavailableError
from app.playback_diagnostics import playback_info, require_development
from app.tv_playback import episode_playback_info


MOVIE = JellyfinMovie("example-item", "example-source", "/synthetic/example.mp4", "mp4", "h264", ("aac",), subtitle_tracks=(
    JellyfinSubtitleTrack(2, "Example", "Example subtitle", "en", "srt", False, False, False, False),
))


def test_playback_requires_server_side_jellyfin_user_context(monkeypatch):
    client = JellyfinClient("https://example.invalid", "secret-api-key")
    monkeypatch.setattr(client, "_get_json", lambda path: [])
    with pytest.raises(JellyfinUnavailableError, match="no usable user context"):
        client._playback_user_id()


def test_source_info_allowlists_fields_and_omits_provider_secrets(monkeypatch):
    client = JellyfinClient("https://example.invalid", "secret-api-key")
    requests = []
    def fake_get_json(path, parameters=None):
        requests.append((path, parameters))
        if path == "/Users":
            return [{"Id": "synthetic-jellyfin-user"}]
        return {"MediaSources": [{
        "Id": "example-source", "Path": "/secret/source.mkv", "TranscodingUrl": "https://secret.invalid/token",
        "Container": "mkv", "Bitrate": 20000000, "MediaStreams": [
            {"Type": "Video", "Codec": "hevc", "Width": 3840, "Height": 2160, "BitDepth": 10, "Path": "/secret/video"},
            {"Type": "Audio", "Index": 1, "Codec": "eac3", "Language": "en"},
        ],
    }]}
    monkeypatch.setattr(client, "_get_json", fake_get_json)
    result = client.playback_source_info(MOVIE)
    assert requests[1] == ("/Items/example-item", {"userId": "synthetic-jellyfin-user", "Fields": "MediaSources"})
    assert result["video"]["BitDepth"] == 10
    assert result["audio_tracks"][0]["Codec"] == "eac3"
    assert "secret" not in str(result)


def test_requested_playback_is_not_reported_as_confirmed():
    result = playback_info(MOVIE, {"container": "mkv"}, 2)
    assert result["playback"]["requested_mode"] == "Transcode"
    assert result["playback"]["confirmed_mode"] is None
    assert result["playback"]["selected_subtitle_index"] == 2
    assert result["actual_session"]["status"] == "UNOBSERVED"
    assert "secret" not in str(result)


def test_playback_decision_uses_exact_source_and_redacts_urls(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    def fake_open(request, timeout):
        assert request.full_url.endswith("/Items/example-item/PlaybackInfo?UserId=synthetic-jellyfin-user")
        assert request.get_header("X-emby-token") == "secret-api-key"
        payload = json.loads(request.data)
        assert payload["MediaSourceId"] == "example-source"
        assert payload["DeviceProfile"]["DirectPlayProfiles"]
        assert payload["EnableDirectStream"] is True
        return Response()

    monkeypatch.setattr("app.jellyfin.urlopen", fake_open)
    monkeypatch.setattr(JellyfinClient, "_playback_user_id", lambda self: "synthetic-jellyfin-user")
    monkeypatch.setattr("app.jellyfin.json.load", lambda _response: {"MediaSources": [{
        "Id": "example-source", "Path": "/secret/film.mkv",
        "SupportsDirectPlay": False, "SupportsDirectStream": True,
        "SupportsTranscoding": True,
        "TranscodingUrl": "https://secret.invalid/key=secret",
        "TranscodeReasons": ["ContainerNotSupported", "private-secret"],
        "TranscodingContainer": "ts",
    }]})
    result = JellyfinClient("https://example.invalid", "secret-api-key").playback_decision(MOVIE, h264_supported=True)
    assert result["remux_supported"] is True
    assert result["transcode_required"] is False
    assert result["reasons"] == ["ContainerNotSupported"]
    assert "secret" not in str(result)


def test_playback_decision_rejects_different_media_source(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr("app.jellyfin.urlopen", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(JellyfinClient, "_playback_user_id", lambda self: "synthetic-jellyfin-user")
    monkeypatch.setattr("app.jellyfin.json.load", lambda _response: {"MediaSources": [{"Id": "other-source"}]})
    with pytest.raises(JellyfinUnavailableError):
        JellyfinClient("https://example.invalid", "secret-api-key").playback_decision(MOVIE, h264_supported=False)


def test_diagnostics_are_development_only(monkeypatch):
    monkeypatch.setenv("PV_ENVIRONMENT", "production")
    with pytest.raises(HTTPException) as error:
        require_development()
    assert error.value.status_code == 404


def test_hls_resource_cannot_be_replayed_under_another_item():
    store = HlsResourceStore()
    user_id = uuid4()
    token = store.issue(user_id, "https://example.invalid/segment", scope="example-item")
    assert store.resolve(user_id, token, scope="example-item") == "https://example.invalid/segment"
    assert store.resolve(user_id, token, scope="different-item") is None


def test_disabled_episode_diagnostics_still_check_audience(monkeypatch):
    monkeypatch.setenv("PV_ENVIRONMENT", "development")
    monkeypatch.setenv("PV_JELLYFIN_ENABLED", "false")
    episode_id = uuid4()
    user_id = uuid4()

    class Store:
        def visible_episode_source(self, episode, user):
            return None

    with pytest.raises(HTTPException) as error:
        episode_playback_info(episode_id, SimpleNamespace(user_id=user_id), Store(), None)
    assert error.value.status_code == 404
