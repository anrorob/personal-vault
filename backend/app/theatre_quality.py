"""Quality targets derived from authorized source metadata, never delivery URLs."""
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

QualityMode = Literal["Original", "Auto", "FHD", "720p"]


class DecodeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    width: int = Field(gt=0, le=32768)
    height: int = Field(gt=0, le=32768)
    supported: bool | None = None
    smooth: bool | None = None


def fit(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    scale = min(1, max_width / width, max_height / height)
    if scale == 1:
        return width, height
    return max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)


def quality_target(source, mode: QualityMode, video_preserved: bool, bandwidth: int | None,
                   probes: list[DecodeTarget]):
    video = source.get("video", {})
    width, height = video.get("Width"), video.get("Height")
    info = {"selected_mode": mode, "source_resolution": {"width": width, "height": height},
            "target_resolution": None, "fallback_reason": None,
            "bandwidth_bps": bandwidth if mode == "Auto" else None,
            "bandwidth_basis": "Measured HLS delivery throughput" if mode == "Auto" and bandwidth else
                               "Unmeasured; highest-quality local-network default",
            "encoder_policy": "Configured H.264 CRF 18 / medium when video conversion is required",
            "frame_rate_policy": "Preserve source; no requested frame-rate cap"}
    def fail(reason):
        raise HTTPException(status_code=409, detail={"message": reason, "quality": {**info, "fallback_reason": reason}})
    if not width or not height or width <= 0 or height <= 0:
        fail("Source dimensions are unavailable; a quality target cannot be guaranteed")
    native = (width, height)
    candidates = [native] if mode == "Original" else [fit(width, height, 1920, 1080)] if mode == "FHD" else [fit(width, height, 1280, 720)] if mode == "720p" else list(dict.fromkeys([native, *[fit(width, height, w, h) for w, h in [(1920, 1080), (1280, 720), (854, 480), (640, 360)]]]))
    fps = video.get("AverageFrameRate") or video.get("RealFrameRate") or 30
    source_rate = video.get("BitRate") or source.get("bitrate") or 0
    audio_rate = max((a.get("BitRate") or 0 for a in source.get("audio_tracks", [])), default=0) or 640000
    skipped = []
    for target in candidates:
        native_copy = target == native and video_preserved
        probe = next((p for p in probes if (p.width, p.height) == target), None)
        if probe and (probe.supported is False or (mode == "Auto" and probe.smooth is False)):
            skipped.append("Browser decode probe cannot sustain the higher target")
            continue
        # A quality budget, not an assumed measurement; do not starve a chosen
        # resolution. Native requests retain the provider's positive allowance.
        scale = (target[0] * target[1]) / (width * height)
        budget = min(2147483647, max(int(source_rate * scale), int(target[0] * target[1] * max(30, fps) * 0.24), 1000000))
        required = (source_rate if native_copy and source_rate else budget) + audio_rate
        if mode == "Auto" and bandwidth and required > bandwidth * 0.8:
            skipped.append("Measured HLS delivery throughput is insufficient for the higher target with headroom")
            continue
        info.update({"target_resolution": {"width": target[0], "height": target[1]},
                     "estimated_required_bitrate": required,
                     "target_video_bitrate": budget if target != native else None,
                     "fallback_reason": "; ".join(dict.fromkeys(skipped)) or None,
                     "target_basis": "Explicit resolution ceiling without upscaling" if mode in {"FHD", "720p"} else
                                     "Native source target" if target == native else "Auto evidence-based target",
                     "decode_probe": probe.model_dump() if probe else None})
        return info
    fail("; ".join(dict.fromkeys(skipped)) or "No supported quality target")
