"""Browser-observed, source-specific Theatre negotiation behind PV authorization."""
from __future__ import annotations

import os
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.jellyfin import JellyfinUnavailableError
from app.theatre_quality import DecodeTarget, QualityMode, quality_target


class BrowserCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")
    direct: bool = False
    video_copy: bool = False
    audio_copy: bool = False
    h264: bool = False
    aac: bool = False


class PlaybackPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capabilities: BrowserCapabilities | None = None
    subtitle_index: int | None = Field(default=None, ge=0)
    audio_index: int | None = Field(default=None, ge=0)
    quality_mode: QualityMode = "Auto"
    bandwidth_bps: int | None = Field(default=None, gt=0, le=2147483647)
    decode_targets: list[DecodeTarget] = Field(default_factory=list, max_length=8)
    evaluate_only: bool = False


def require_playback_enabled() -> None:
    if os.getenv("PV_JELLYFIN_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        raise HTTPException(status_code=503, detail="Playback service is disabled")


def negotiate(client, item, source: dict, capabilities: BrowserCapabilities, subtitle_index: int | None,
              quality_mode: QualityMode = "Auto", bandwidth_bps: int | None = None,
              decode_targets: list[DecodeTarget] | None = None, audio_index: int | None = None):
    """The capability report is a delivery hint, never authorization or a URL."""
    if subtitle_index is not None and subtitle_index not in {s.index for s in item.subtitle_tracks}:
        raise HTTPException(status_code=400, detail="Subtitle track is not available")
    video = source.get("video", {})
    audios = source.get("audio_tracks", [])
    audio = next((a for a in audios if a.get("IsDefault")), audios[0] if audios else {})
    default_audio_index = audio.get("Index")
    if audio_index is not None:
        audio = next((a for a in audios if a.get("Index") == audio_index), None)
        if audio is None:
            raise HTTPException(status_code=400, detail="Audio track is not available")
    # These are provider metadata, not browser-supplied codec strings.
    video_codec = video.get("Codec")
    audio_codec = audio.get("Codec")
    video_copy = capabilities.video_copy and video_codec in {"h264", "hevc", "av1", "vp9"}
    audio_copy = capabilities.audio_copy and audio_codec in {"aac", "mp3", "ac3", "eac3", "opus", "flac", "alac"}
    quality = quality_target(source, quality_mode, (video_copy or capabilities.direct) and subtitle_index is None,
                             bandwidth_bps, decode_targets or [])
    target = quality["target_resolution"]
    scaled = (target["width"], target["height"]) != (video.get("Width"), video.get("Height"))
    video_copy = video_copy and not scaled
    direct = capabilities.direct and subtitle_index is None and not scaled and audio.get("Index") == default_audio_index
    video_codecs = [video_codec] if video_copy else []
    audio_codecs = [audio_codec] if audio_copy else []
    if capabilities.h264 and "h264" not in video_codecs:
        video_codecs.append("h264")
    if capabilities.aac and "aac" not in audio_codecs:
        audio_codecs.append("aac")
    profile = {
        "Name": "Personal Vault source-specific browser capabilities",
        # Jellyfin otherwise substitutes an 8 Mbps default. Int32 maximum is
        # the API's unbounded-bandwidth representation, not a quality tier.
        "MaxStreamingBitrate": 2147483647,
        "MaxStaticBitrate": 2147483647,
        "DirectPlayProfiles": [{"Type": "Video", "Container": source["container"],
                                "VideoCodec": video_codec, "AudioCodec": audio_codec or ""}] if direct else [],
        "TranscodingProfiles": [{"Type": "Video", "Container": "mp4", "Protocol": "hls",
                                  "VideoCodec": ",".join(video_codecs), "AudioCodec": ",".join(audio_codecs),
                                  "MinSegments": 1, "SegmentLength": 3,
                                  "BreakOnNonKeyFrames": False}] if video_codecs and (audio_codecs or not audios) else [],
        # Retain existing subtitle burn-in behavior; do not introduce text delivery here.
        "SubtitleProfiles": [{"Format": s, "Method": "Encode"} for s in ("pgssub", "dvdsub", "srt", "ass", "ssa", "subrip", "webvtt")],
        "CodecProfiles": [{"Type": "Video", "Codec": ",".join(video_codecs), "Conditions": [
            {"Condition": "LessThanEqual", "Property": "Width", "Value": str(target["width"]), "IsRequired": True},
            {"Condition": "LessThanEqual", "Property": "Height", "Value": str(target["height"]), "IsRequired": True},
        ]}] if scaled else [],
    }
    payload = {
        "MediaSourceId": item.media_source_id, "DeviceProfile": profile,
        "EnableDirectPlay": direct, "EnableDirectStream": True, "EnableTranscoding": True,
        "AllowVideoStreamCopy": video_copy and subtitle_index is None,
        "AllowAudioStreamCopy": audio_copy,
        "SubtitleStreamIndex": subtitle_index if subtitle_index is not None else -1,
        "AudioStreamIndex": audio.get("Index"),
        "MaxStreamingBitrate": 2147483647,
    }
    body = client.request_playback_info(item, payload)
    media = next((s for s in body.get("MediaSources", []) if s.get("Id") == item.media_source_id), None)
    if media is None:
        raise JellyfinUnavailableError("Playback decision media source does not match")
    if direct and media.get("SupportsDirectPlay") is True:
        mode = "Direct Play"
        url = f"{client._base_url}/Videos/{item.item_id}/stream?" + urlencode({"Static": "true", "MediaSourceId": item.media_source_id})
        source_type = "file"
    else:
        candidate = media.get("TranscodingUrl")
        if not isinstance(candidate, str) or not candidate or media.get("SupportsTranscoding") is not True:
            raise JellyfinUnavailableError("No compatible browser playback path")
        url = urljoin(client._base_url + "/", candidate.lstrip("/"))
        parsed, base = urlsplit(url), urlsplit(client._base_url)
        path_parts = parsed.path.removeprefix(base.path).strip("/").split("/")
        def same_item(value):
            try:
                return UUID(value) == UUID(item.item_id)
            except ValueError:
                return value == item.item_id
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc) or len(path_parts) != 3 or path_parts[0].lower() != "videos" or not same_item(path_parts[1]) or path_parts[2] != "master.m3u8":
            raise JellyfinUnavailableError("Invalid playback decision URL")
        query = {k.lower(): v for k, v in parse_qsl(parsed.query)}
        # Preserve the negotiated VideoBitrate: Jellyfin treats an omitted value
        # as zero and ResolutionNormalizer then selects its smallest rendition.
        # The provider applies source-aware bitrate limits; PV adds no fixed cap.
        for key in list(query):
            if key in {"api_key", "apikey", "token", "width", "height", "maxwidth", "maxheight", "maxstreamingbitrate", "framerate", "maxframerate"}:
                query.pop(key)
        try:
            video_bitrate = int(query["videobitrate"]) if "videobitrate" in query else None
        except ValueError:
            raise JellyfinUnavailableError("Invalid negotiated video bitrate") from None
        if not (video_copy and subtitle_index is None) and (video_bitrate is None or video_bitrate <= 0):
            raise JellyfinUnavailableError("Missing positive negotiated video bitrate")
        if quality_mode == "Original" and not (video_copy and subtitle_index is None) and video_bitrate < (video.get("BitRate") or 0):
            reason = "Provider bitrate restriction would risk downscaling Original; playback refused"
            raise HTTPException(status_code=409, detail={"message": reason, "quality": {**quality, "fallback_reason": reason}})
        if scaled:
            # Coherent quality budget plus an aspect-preserving, no-upscale box.
            # Keep bitrate positive: missing/zero reintroduces the 416px defect.
            video_bitrate = quality["target_video_bitrate"]
            query.update({"maxwidth": str(target["width"]), "maxheight": str(target["height"]),
                          "videobitrate": str(video_bitrate)})
        query.update({"allowvideostreamcopy": str(video_copy and subtitle_index is None).lower(),
                      "allowaudiostreamcopy": str(audio_copy).lower(),
                      "subtitlestreamindex": str(subtitle_index if subtitle_index is not None else -1),
                      "segmentlength": "3", "minsegments": "1", "breakonnonkeyframes": "false"})
        if audio.get("Index") is not None:
            query["audiostreamindex"] = str(audio["Index"])
        if subtitle_index is not None:
            query["subtitlemethod"] = "Encode"
        if audio_copy:
            for key in list(query):
                if key.lower() in {"audiobitrate", "audiochannels", "maxaudiochannels", "transcodingmaxaudiochannels"}:
                    query.pop(key)
        mode = "Remux / Direct Stream" if media.get("SupportsDirectStream") is True and video_copy and audio_copy and subtitle_index is None else "Transcode"
        url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))
        source_type = "hls"
    reasons = media.get("TranscodeReasons") or dict(parse_qsl(urlsplit(url).query)).get("transcodereasons", "")
    if isinstance(reasons, str):
        reasons = reasons.split(",")
    allowed_reasons = {"DirectPlayError", "ContainerNotSupported", "VideoCodecNotSupported", "AudioCodecNotSupported", "SubtitleCodecNotSupported", "VideoProfileNotSupported", "VideoLevelNotSupported", "VideoBitDepthNotSupported", "VideoRangeTypeNotSupported", "AudioChannelsNotSupported", "ContainerBitrateExceedsLimit"}
    diagnostics = {
        "quality": quality,
        "source": source, "client_capability": capabilities.model_dump(),
        "client_constraints": [name for name, supported in (("source_container_or_tracks_not_native", direct),
                               ("source_video_not_supported_for_hls", video_copy),
                               ("source_audio_not_supported_for_hls", audio_copy)) if not supported],
        "jellyfin_decision": {"direct_play_supported": media.get("SupportsDirectPlay"),
                              "direct_stream_supported": media.get("SupportsDirectStream"),
                              "selected_mode": mode, "reasons": [r for r in reasons if r in allowed_reasons]},
        "playback": {"requested_mode": mode, "video_stream_copy_allowed": video_copy and subtitle_index is None,
                     "audio_stream_copy_allowed": audio_copy,
                     "requested_max_video_bitrate": video_bitrate if source_type == "hls" else None,
                     "requested_max_width": target["width"] if scaled else None,
                     "requested_max_height": target["height"] if scaled else None,
                     "selected_audio_index": audio.get("Index"), "selected_subtitle_index": subtitle_index,
                     "subtitle_method": "Encode" if subtitle_index is not None else "off",
                     "quality": "Source-aware positive bitrate and configured high-quality encoder; explicit mode target"},
        "actual_session": {"status": "UNOBSERVED", "delivered_mode": None, "bitrate": None},
    }
    return url, source_type, diagnostics


def create_plan(client, item, request, resources, user_id, scope, resource_prefix):
    require_playback_enabled()
    try:
        source = client.playback_source_info(item)
        if request.capabilities is None:
            return {"source": source}
        if request.evaluate_only:
            return {"quality": quality_target(source, request.quality_mode,
                    (request.capabilities.video_copy or request.capabilities.direct) and request.subtitle_index is None,
                    request.bandwidth_bps, request.decode_targets)}
        url, source_type, diagnostics = negotiate(client, item, source, request.capabilities, request.subtitle_index,
                                                  request.quality_mode, request.bandwidth_bps, request.decode_targets, request.audio_index)
    except JellyfinUnavailableError:
        raise HTTPException(status_code=503, detail={"message": "Playback negotiation is unavailable",
            "quality": {"selected_mode": request.quality_mode,
                        "fallback_reason": "Provider could not supply a valid compatible plan with a positive video bitrate; no lower-quality fallback was applied"}}) from None
    token = resources.issue(user_id, url, scope=scope)
    return {"url": f"{resource_prefix}/{token}", "source_type": source_type, "diagnostics": diagnostics}
