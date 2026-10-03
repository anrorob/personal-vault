"""Development-only Theatre playback facts with an explicit observation boundary."""
from __future__ import annotations

import os
from typing import Protocol, Sequence

from fastapi import HTTPException


class IndexedSubtitle(Protocol):
    index: int


class PlaybackItem(Protocol):
    item_id: str
    media_source_id: str
    subtitle_tracks: Sequence[IndexedSubtitle]


def require_development() -> None:
    if os.getenv("PV_ENVIRONMENT") != "development":
        raise HTTPException(status_code=404, detail="Not found")


def playback_info(
    movie: PlaybackItem, source: dict[str, object], subtitle_index: int | None,
    decision: dict[str, object] | None = None,
    h264_supported: bool | None = None,
) -> dict[str, object]:
    selected = next((track for track in movie.subtitle_tracks if track.index == subtitle_index), None)
    if subtitle_index is not None and selected is None:
        raise HTTPException(status_code=400, detail="Subtitle track is not available")
    return {
        "source": source,
        "client_capability": {
            "h264_mp4": h264_supported,
            "basis": "browser canPlayType probe" if h264_supported is not None else "not supplied",
            "limits": "Codec string support does not prove source profile, level, HDR, audio or container compatibility.",
        },
        "jellyfin_decision": decision,
        "actual_session": {
            "status": "UNOBSERVED",
            "delivered_mode": None,
            "video_codec": None,
            "audio_codec": None,
            "width": None,
            "height": None,
            "bitrate": None,
        },
        "playback": {
            "service": "Jellyfin",
            "requested_mode": "Transcode",
            "confirmed_mode": None,
            "reason": "PV requests H.264 video and AAC stereo audio with video and audio stream copy disabled.",
            "requested_video_codec": "h264",
            "requested_audio_codec": "aac",
            "requested_max_width": 1920,
            "requested_max_height": 1080,
            "requested_max_video_bitrate": 12000000,
            "delivered_video_codec": None,
            "delivered_audio_codec": None,
            "delivered_bitrate": None,
            "delivered_width": None,
            "delivered_height": None,
            "container": "MPEG-TS segments over HLS",
            "stream_url_type": "PV authenticated HLS proxy",
            "jellyfin_item_id": movie.item_id,
            "jellyfin_media_source_id": movie.media_source_id,
            "selected_subtitle_index": selected.index if selected else None,
            "selected_audio_index": None,
        },
        "observation": "Source fields come from Jellyfin. Requested delivery comes from the PV HLS adapter. Confirmed session and delivered stream fields require live Jellyfin session evidence.",
    }
