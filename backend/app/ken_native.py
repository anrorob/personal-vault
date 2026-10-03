"""Shared native-video preparation. Runtime chooses temporal frames from video."""

import hashlib
import json
import math
from pathlib import Path
import re
import shutil

from app import ken_config as ken
from app.video_intelligence import probe_video_stream, prepare_compatible_video


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_native(source: Path, root: Path, config: dict, work: Path) -> dict:
    stream = probe_video_stream(source)
    config["source"]["duration_ms"] = stream["duration_ms"]
    source_hash = file_sha256(source)
    if config["source"].get("catalogue_sha256") and source_hash != config["source"]["catalogue_sha256"]:
        raise ValueError("Native source checksum does not match catalogue")
    config["source"]["verified_sha256"] = source_hash
    dimension = 384
    ken.policy(stream['duration_ms'])
    reference = {"kind": "canonical_video", "relative_path": source.relative_to(root).as_posix()}
    prepared = source
    # Inaccessible originals and oversized/less-portable codecs use a rebuildable
    # continuous H.264 video. No frame-rate reduction or JPEG sampler is involved.
    readable = bool(source.stat().st_mode & 0o004) and all(p.stat().st_mode & 0o001 for p in [source.parent, *source.parents] if p.is_relative_to(root))
    if max(stream["width"], stream["height"]) > dimension or stream["codec"] not in {"h264", "hevc", "vp8", "vp9", "av1", "mpeg4", "mjpeg"} or stream.get("rotation", 0) % 360 or not readable:
        prepared = work / "prepared.mp4"
        if prepared.exists():
            raise ValueError("Native work directory is not fresh")
        prepare_compatible_video(source, prepared, max_dimension=dimension)
        reference = {"kind": "prepared_native_video", "relative_path": "prepared.mp4"}
    prepared_stream = probe_video_stream(prepared) if prepared != source else stream
    if max(prepared_stream["width"], prepared_stream["height"]) > dimension:
        raise ValueError("Native input resolution exceeds runtime budget")
    # Timing must remain that of the full video, not a sparse-image timeline.
    if abs(prepared_stream["duration_ms"] - stream["duration_ms"]) > max(1000, stream["duration_ms"] * 0.01):
        raise ValueError("Prepared video timing does not match the source")
    config['runtime_video'] = {**ken.policy(prepared_stream['duration_ms']),
        'prepared_kind':reference['kind'], 'source_stream':stream, 'prepared_stream':prepared_stream,
        'decoder':'llama.cpp FFmpeg video input with Qwen temporal merging',
        'prepared_sha256':file_sha256(prepared) if prepared!=source else source_hash,
        'derivative_policy':'continuous H.264/yuv420p, original timing, CRF23' if prepared!=source else None}
    return {"native_parameters": config["runtime_video"], "native_job": work.name, "_native_reference": reference,
            "native_sha256": config["runtime_video"]["prepared_sha256"]}


def publish_native(config: dict, payload: dict, work: Path) -> None:
    reference = payload.pop("_native_reference")
    record = {"asset_id": payload["asset_id"], "run_id": payload.get("run_id"), "input_fingerprint": payload["input_fingerprint"],
              "integrity_manifest": config["input_integrity"]["manifest"], "reference": reference,
              "runtime_video": config["runtime_video"]}
    destination = work / "native.json"
    with destination.open("x", encoding="utf-8") as stream:
        json.dump(record, stream)
    # Backend owns this short-lived capability directory; analyser only mounts it read-only.
    work.chmod(0o755)
    destination.chmod(0o444)


def cleanup_abandoned_inputs(root: Path) -> None:
    """Called only under the KEN global lock with the analyser confirmed idle."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for child in root.iterdir():
        if (re.fullmatch(r"pv-ken-[0-9a-f-]{36}-[a-z0-9_]{8}", child.name)
                and not child.is_symlink() and child.is_dir() and child.resolve().parent == root):
            shutil.rmtree(child)
