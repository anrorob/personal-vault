from copy import deepcopy
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from app.playback_policy import BrowserCapabilities, PlaybackPlanRequest, create_plan, negotiate
from app.theatre_quality import DecodeTarget, quality_target
from tests.test_playback_policy import SOURCE, ITEM, Client


def source(width, height, codec="vc1"):
    data = deepcopy(SOURCE)
    data["video"].update(Width=width, Height=height, Codec=codec, BitRate=20000000, AverageFrameRate=24)
    return data


@pytest.mark.parametrize("mode,native,target", [
    ("Original", (3840,2160), (3840,2160)), ("Original", (1920,1080), (1920,1080)),
    ("Original", (854,480), (854,480)), ("Auto", (3840,2160), (3840,2160)),
    ("FHD", (3840,2160), (1920,1080)), ("FHD", (1920,1080), (1920,1080)),
    ("FHD", (1280,720), (1280,720)), ("720p", (3840,2160), (1280,720)),
    ("720p", (1920,1080), (1280,720)), ("720p", (854,480), (854,480)),
    ("FHD", (3840,1600), (1920,800)),
])
def test_resolution_contract_through_provider_negotiation(mode, native, target):
    client = Client()
    url, kind, info = negotiate(client, ITEM, source(*native), BrowserCapabilities(h264=True,aac=True), None, mode)
    query = parse_qs(urlsplit(url).query)
    assert kind == "hls"
    assert info["quality"]["target_resolution"] == dict(zip(("width", "height"), target))
    assert int(query["videobitrate"][0]) > 0
    assert query["allowvideostreamcopy"] == ["false"]
    assert "framerate" not in query and "maxframerate" not in query
    if target == native:
        assert "maxwidth" not in query and "maxheight" not in query
        assert query["videobitrate"] == ["2147003647"]
    else:
        assert (int(query["maxwidth"][0]), int(query["maxheight"][0])) == target
        assert int(query["videobitrate"][0]) >= target[0]*target[1]*30*.24
        assert client.payload["EnableDirectPlay"] is False


def test_auto_only_reduces_on_real_evidence_and_reports_reason():
    data = source(3840,2160)
    result = quality_target(data, "Auto", False, 25000000, [])
    assert result["target_resolution"] == {"width":1920,"height":1080}
    assert "throughput" in result["fallback_reason"]
    result = quality_target(data, "Auto", False, None, [DecodeTarget(width=3840,height=2160,supported=False)])
    assert result["target_resolution"] == {"width":1920,"height":1080}
    assert "decode" in result["fallback_reason"]
    with pytest.raises(HTTPException) as error:
        quality_target(data, "Auto", False, 1, [])
    assert error.value.status_code == 409


def test_original_refuses_known_client_or_provider_limit():
    data = source(3840,2160)
    with pytest.raises(HTTPException) as error:
        quality_target(data, "Original", False, None, [DecodeTarget(width=3840,height=2160,supported=False)])
    assert "decode" in error.value.detail["quality"]["fallback_reason"]
    client = Client()
    client.media["TranscodingUrl"] = "/Videos/example/master.m3u8?VideoBitrate=1000000"
    with pytest.raises(HTTPException) as error:
        negotiate(client, ITEM, data, BrowserCapabilities(h264=True,aac=True), None, "Original")
    assert "bitrate restriction" in error.value.detail["quality"]["fallback_reason"]


def test_original_retains_direct_and_remux_without_video_conversion():
    caps = BrowserCapabilities(direct=True,video_copy=True,audio_copy=True,h264=True,aac=True)
    assert negotiate(Client(True), ITEM, source(3840,2160,"h264"), caps, None, "Original")[1] == "file"
    info = negotiate(Client(remux=True), ITEM, source(3840,2160,"h264"), caps, None, "Original")[2]
    assert info["playback"]["video_stream_copy_allowed"] is True


def test_mode_switch_pins_audio_and_subtitle_indices():
    data = source(1920,1080)
    data["audio_tracks"].append({"Index":3,"Codec":"aac"})
    client = Client(True)
    url, _, info = negotiate(client, ITEM, data, BrowserCapabilities(direct=True,h264=True,aac=True), 2, "720p", audio_index=3)
    query = parse_qs(urlsplit(url).query)
    assert query["audiostreamindex"] == ["3"]
    assert query["subtitlestreamindex"] == ["2"]
    assert query["subtitlemethod"] == ["Encode"]
    assert info["playback"]["selected_audio_index"] == 3
    with pytest.raises(HTTPException):
        negotiate(client, ITEM, data, BrowserCapabilities(h264=True,aac=True), None, audio_index=99)


def test_evaluation_does_not_start_provider_session_or_issue_resource():
    client = Client()
    plan = create_plan(client, ITEM, PlaybackPlanRequest(capabilities=BrowserCapabilities(h264=True,aac=True), evaluate_only=True),
                       None, uuid4(), "example", "/api/example")
    assert plan["quality"]["selected_mode"] == "Auto"
    assert not hasattr(client, "payload")
    assert "url" not in plan


@pytest.mark.parametrize("body", [{"quality_mode":"4K"}, {"bandwidth_bps":0}, {"bandwidth_bps":-1}])
def test_invalid_or_zero_quality_hints_are_rejected(body):
    with pytest.raises(ValidationError):
        PlaybackPlanRequest(**body)
