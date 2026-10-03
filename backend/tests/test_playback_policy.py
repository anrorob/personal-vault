from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.jellyfin import HlsResourceStore, JellyfinUnavailableError
from app.playback_policy import BrowserCapabilities, PlaybackPlanRequest, create_plan, negotiate


ITEM = SimpleNamespace(item_id="example", media_source_id="source", subtitle_tracks=[SimpleNamespace(index=2)])
SOURCE = {"container": "mp4", "video": {"Codec": "h264", "Width": 1920, "Height": 1080},
          "audio_tracks": [{"Index": 1, "Codec": "aac", "IsDefault": True}]}


class Client:
    _base_url = "http://provider.invalid"

    def __init__(self, direct=False, remux=False):
        self.media = {"Id": "source", "SupportsDirectPlay": direct, "SupportsDirectStream": remux,
                      "SupportsTranscoding": True,
                      "TranscodingUrl": "/Videos/example/master.m3u8?VideoCodec=h264&AudioCodec=aac&VideoBitrate=2147003647&MaxWidth=1920&api_key=private-key&TranscodeReasons=VideoCodecNotSupported"}

    def playback_source_info(self, item):
        return SOURCE

    def request_playback_info(self, item, payload):
        self.payload = payload
        return {"MediaSources": [self.media]}


def test_direct_play_preserves_source_bytes_and_omits_credentials():
    client = Client(direct=True)
    url, kind, info = negotiate(client, ITEM, SOURCE, BrowserCapabilities(direct=True), None)
    assert kind == "file"
    assert parse_qs(urlsplit(url).query)["Static"] == ["true"]
    assert info["jellyfin_decision"]["selected_mode"] == "Direct Play"
    assert "private-key" not in str(info)


def test_container_only_adaptation_preserves_both_streams():
    client = Client(remux=True)
    caps = BrowserCapabilities(video_copy=True, audio_copy=True, h264=True, aac=True)
    url, kind, info = negotiate(client, ITEM, SOURCE, caps, None)
    query = parse_qs(urlsplit(url).query)
    assert kind == "hls"
    assert info["jellyfin_decision"]["selected_mode"] == "Remux / Direct Stream"
    assert query["allowvideostreamcopy"] == query["allowaudiostreamcopy"] == ["true"]
    assert not {"maxwidth", "api_key", "apikey"} & query.keys()
    assert query["videobitrate"] == ["2147003647"]


def test_incompatible_audio_does_not_force_video_transcode():
    caps = BrowserCapabilities(video_copy=True, h264=True, aac=True)
    url, _, info = negotiate(Client(), ITEM, SOURCE, caps, None)
    query = parse_qs(urlsplit(url).query)
    assert query["allowvideostreamcopy"] == ["true"]
    assert query["allowaudiostreamcopy"] == ["false"]
    assert info["actual_session"]["status"] == "UNOBSERVED"


def test_vc1_retains_negotiated_rate_to_avoid_zero_bitrate_downscaling():
    source = {**SOURCE, "video": {**SOURCE["video"], "Codec": "vc1"}}
    client = Client()
    url, _, info = negotiate(client, ITEM, source, BrowserCapabilities(h264=True, aac=True), None)
    query = parse_qs(urlsplit(url).query)
    assert query["allowvideostreamcopy"] == ["false"]
    assert query["videobitrate"] == ["2147003647"]
    assert "maxwidth" not in query
    assert info["playback"]["requested_max_video_bitrate"] == 2147003647
    assert client.payload["DeviceProfile"]["MaxStreamingBitrate"] == 2147483647
    assert info["jellyfin_decision"]["reasons"] == ["VideoCodecNotSupported"]


@pytest.mark.parametrize("bitrate", [None, "0", "-1", "invalid"])
def test_transcode_rejects_a_missing_or_invalid_video_bitrate(bitrate):
    client = Client()
    client.media["TranscodingUrl"] = "/Videos/example/master.m3u8?VideoCodec=h264&AudioCodec=aac"
    if bitrate is not None:
        client.media["TranscodingUrl"] += "&VideoBitrate=" + bitrate
    with pytest.raises(JellyfinUnavailableError):
        negotiate(client, ITEM, SOURCE, BrowserCapabilities(h264=True, aac=True), None)


def test_subtitle_selection_preserves_burn_in_and_audio_copy():
    caps = BrowserCapabilities(direct=True, video_copy=True, audio_copy=True, h264=True, aac=True)
    client = Client(direct=True)
    url, kind, _ = negotiate(client, ITEM, SOURCE, caps, 2)
    query = parse_qs(urlsplit(url).query)
    assert kind == "hls"
    assert client.payload["EnableDirectPlay"] is False
    assert query["subtitlemethod"] == ["Encode"]
    assert query["subtitlestreamindex"] == ["2"]
    assert query["allowvideostreamcopy"] == ["false"]
    assert query["allowaudiostreamcopy"] == ["true"]
    assert query["videobitrate"] == ["2147003647"]
    with pytest.raises(HTTPException):
        negotiate(client, ITEM, SOURCE, caps, 999)


@pytest.mark.parametrize("url", ["https://external.invalid/Videos/example/master.m3u8", "/Videos/other/master.m3u8"])
def test_provider_url_cannot_escape_authorized_media(url):
    client = Client()
    client.media["TranscodingUrl"] = url
    with pytest.raises(JellyfinUnavailableError):
        negotiate(client, ITEM, SOURCE, BrowserCapabilities(h264=True, aac=True), None)


def test_missing_or_different_source_fails_closed():
    client = Client()
    client.media["Id"] = "other"
    with pytest.raises(JellyfinUnavailableError):
        negotiate(client, ITEM, SOURCE, BrowserCapabilities(h264=True, aac=True), None)


def test_disabled_plan_never_queries_provider(monkeypatch):
    monkeypatch.setenv("PV_JELLYFIN_ENABLED", "false")
    with pytest.raises(HTTPException) as error:
        create_plan(None, ITEM, PlaybackPlanRequest(), None, uuid4(), "example", "/api/example")
    assert error.value.status_code == 503


def test_plan_issues_only_user_and_item_scoped_private_resource(monkeypatch):
    monkeypatch.setenv("PV_JELLYFIN_ENABLED", "true")
    resources, user = HlsResourceStore(), uuid4()
    plan = create_plan(Client(True), ITEM, PlaybackPlanRequest(capabilities=BrowserCapabilities(direct=True)),
                       resources, user, "example", "/api/example/hls")
    token = plan["url"].split("/")[-1]
    assert resources.resolve(user, token, scope="example")
    assert resources.resolve(uuid4(), token, scope="example") is None
    assert resources.resolve(user, token, scope="other") is None
    assert "provider.invalid" not in str(plan)
