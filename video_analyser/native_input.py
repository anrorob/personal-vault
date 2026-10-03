"""Resolve only backend-created native-video capabilities; never request paths."""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess

INPUT_ROOT = Path(os.getenv("PV_KEN_INPUT_ROOT", "/inputs"))
VIDEO_ROOT = Path(os.getenv("PV_KEN_VIDEO_ROOT", "/videos"))


def resolve_native(body, attestation=None):
    token = body.get("native_job", "")
    if not isinstance(token, str) or not re.fullmatch(r"pv-ken-[a-z0-9_-]{8,100}", token):
        raise ValueError("Invalid native capability")
    work = (INPUT_ROOT / token).resolve()
    if work.parent != INPUT_ROOT.resolve() or (INPUT_ROOT / token).is_symlink():
        raise ValueError("Invalid native capability")
    record_path = work / "native.json"
    if record_path.is_symlink() or record_path.stat().st_size > 65536:
        raise ValueError("Invalid native manifest")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    manifest = record["integrity_manifest"]
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()
    if (record.get("run_id") != body.get("run_id") or not body.get("run_id")
            or record["asset_id"] != body.get("asset_id") or manifest["asset_id"] != body.get("asset_id")
            or manifest["input_mode"] != "native_video" or record["input_fingerprint"] != digest
            or body.get("input_fingerprint") != digest or record["runtime_video"] != manifest["native_parameters"]):
        raise ValueError("Native input binding mismatch")
    reference = record["reference"]
    if reference["kind"] == "canonical_video":
        root = VIDEO_ROOT.resolve()
    elif reference["kind"] == "prepared_native_video" and reference["relative_path"] == "prepared.mp4":
        root = work
    else:
        raise ValueError("Unknown native input reference")
    relative = Path(reference["relative_path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid native path")
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Native input unavailable")
    params = record["runtime_video"]
    try:
        import ken_config as ken
    except ModuleNotFoundError:
        from app import ken_config as ken
    ken_video = params.get('version') == ken.VERSION
    if not ken_video: raise ValueError('Unsupported KEN input policy')
    if ken_video:
        expected = ken.policy(params['prepared_stream']['duration_ms'])
        if (any(params.get(key) != value for key, value in expected.items())
                or body.get('native_parameters') != params
                or body.get('correction_context') != manifest.get('correction_context')):
            raise ValueError('KEN input policy or correction context mismatch')
    if params["prepared_kind"] != reference["kind"]:
        raise ValueError("Native preparation kind mismatch")
    before = path.stat()
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if attestation is not None:
        attestation.update(resolved_input_sha256=actual, resolved_input_size=before.st_size)
    if actual != manifest["prepared_video_sha256"] or actual != params["prepared_sha256"] or actual != body.get("native_sha256"):
        raise ValueError("Native video checksum mismatch")
    if attestation is not None and (actual != body.get("expected_input_sha256") or before.st_size != body.get("expected_input_size")):
        raise ValueError("Native service checksum mismatch")
    if params['fps'] != 1 or params['max_decoded_frames'] != expected['max_decoded_frames'] or params['max_dimension'] != 384:
        raise ValueError('Invalid KEN processing budget')
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=width,height,duration:format=duration", "-of", "json", str(path)],
                            capture_output=True, text=True, check=True, timeout=60)
    probe = json.loads(result.stdout)
    video = probe["streams"][0]
    if attestation is not None and any(video[key] != params["prepared_stream"].get(key) for key in ("width", "height")):
        raise ValueError("Native decoded dimensions mismatch")
    duration = float(video.get("duration") if video.get("duration") not in (None, "N/A", "") else probe["format"]["duration"])
    if not 0 < max(video["width"], video["height"]) <= params["max_dimension"] or not math.isfinite(duration) or duration <= 0:
        raise ValueError("Native stream exceeds effective input budget")
    if abs(duration * 1000 - params["prepared_stream"]["duration_ms"]) > 100:
        raise ValueError("Native stream duration mismatch")
    if (before.st_size, before.st_mtime_ns) != (path.stat().st_size, path.stat().st_mtime_ns):
        raise ValueError("Native input changed")
    return path, params, (before.st_size, before.st_mtime_ns)
