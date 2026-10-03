"""KEN service-boundary input attestation (also copied to gateway)."""
import hashlib
import json
import re
from uuid import uuid4

ATTESTATION_VERSION = "ken-service-input-v1"


def expect_input(config, payload, digest, size):
    evidence = {"attestation_version": ATTESTATION_VERSION, "request_nonce": str(uuid4()),
                "expected_input_sha256": digest, "expected_input_size": size}
    config.update(evidence)
    payload.update(evidence)


def digest_json(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")).hexdigest()


def decoded_manifest(frames, timestamps_ms, width_heights):
    return {"version": "rgb24-decoded-v1", "frame_count": len(frames),
            "frames": [{"sha256": hashlib.sha256(frame).hexdigest(), "timestamp_ms": timestamp,
                        "width": size[0], "height": size[1]}
                       for frame, timestamp, size in zip(frames, timestamps_ms, width_heights, strict=True)]}


def valid_attestation(payload, value):
    if not isinstance(value, dict) or value.get("version") != ATTESTATION_VERSION or value.get("status") != "verified":
        return False
    for key in ("asset_id", "run_id", "input_fingerprint", "request_nonce"):
        if not payload.get(key) or value.get(key) != payload[key]:
            return False
    if (value.get("resolved_input_sha256") != payload.get("expected_input_sha256")
            or value.get("resolved_input_size") != payload.get("expected_input_size")):
        return False
    decoded = value.get("decoded_manifest")
    version = payload.get('native_parameters', {}).get('version')
    if payload.get('input_mode') != 'native_video' or version not in ('ken-full-video-v1','ken-chunk-v1'):
        return False
    ken_video = version == 'ken-full-video-v1'
    frame_limit, dimension_limit = (1802 if ken_video else 44), 384
    if (not isinstance(decoded, dict) or type(decoded.get("frame_count")) is not int
            or not 1 <= decoded["frame_count"] <= min(frame_limit, payload.get("native_parameters", {}).get("max_decoded_frames", 16))):
        return False
    frames = decoded.get("frames", [])
    if not isinstance(frames, list) or len(frames) != decoded["frame_count"]:
        return False
    for frame in frames:
        if (not isinstance(frame, dict) or not isinstance(frame.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", frame["sha256"])
                or any(type(frame.get(k)) is not int or not 1 <= frame[k] <= dimension_limit for k in ("width", "height"))
                or type(frame.get("timestamp_ms")) is not int or frame["timestamp_ms"] < 0):
            return False
    if payload.get("input_mode") == "native_video" and (
            value.get("runtime_opened_sha256") != payload.get("expected_input_sha256")
            or value.get("runtime_opened_size") != payload.get("expected_input_size")):
        return False
    if ken_video and not valid_ken_chunks(payload, value):
        return False
    return value.get("decoded_input_fingerprint") == digest_json(decoded)


def valid_ken_chunks(payload, value):
    policy = payload['native_parameters']
    records = value.get('chunks')
    if (not isinstance(records, list) or len(records) != len(policy['chunks'])
            or value.get('correction_fingerprint') != payload.get('correction_fingerprint')
            or payload.get('correction_fingerprint') != digest_json(payload.get('correction_context'))):
        return False
    frames = {}
    for expected, record in zip(policy['chunks'], records, strict=True):
        if any(record.get(k) != v for k,v in expected.items()):
            return False
        child_fp = digest_json({'parent_input_fingerprint': payload['input_fingerprint'],
                               'chunk': expected, 'prepared_sha256': record.get('prepared_sha256')})
        child = {**payload, 'input_fingerprint': child_fp, 'expected_input_sha256': record.get('prepared_sha256'),
                 'expected_input_size': record.get('prepared_size'),
                 'native_parameters': {**policy, 'version': 'ken-chunk-v1', 'max_decoded_frames': 44}}
        proof = record.get('input_attestation')
        if record.get('input_fingerprint') != child_fp or not valid_attestation(child, proof):
            return False
        decoded = proof['decoded_manifest']['frames']
        if abs(len(decoded) - (expected['end_ms']-expected['start_ms'])/1000) > 1:
            return False
        for index, frame in enumerate(decoded):
            if frame['timestamp_ms'] != index*1000:
                return False
            timestamp = frame['timestamp_ms'] + expected['start_ms']
            frames.setdefault(timestamp, {**frame, 'timestamp_ms': timestamp})
    if not frames or min(frames) != 0 or max(frames) < policy['prepared_stream']['duration_ms'] - 1500:
        return False
    return value['decoded_manifest']['frames'] == [frames[t] for t in sorted(frames)]


def technical_attestation(value):
    """Whitelist metadata on failure; never carry descriptions/raw diagnostics."""
    if not isinstance(value, dict):
        return {}
    keys = ("version", "status", "asset_id", "run_id", "input_fingerprint", "request_nonce",
            "resolved_input_sha256", "resolved_input_size", "runtime_opened_sha256", "runtime_opened_size",
            "decoded_input_fingerprint", "decoded_manifest", "decoder", "error", "correction_fingerprint")
    clean = {key: value[key] for key in keys if key in value}
    if "error" in clean:
        clean["error"] = "Input or decoded-frame verification failed"
    if isinstance(clean.get("decoded_manifest"), dict):
        manifest = clean["decoded_manifest"]
        clean["decoded_manifest"] = {key: manifest[key] for key in ("version", "frame_count") if key in manifest}
        frames = manifest.get("frames", [])
        clean["decoded_manifest"]["frames"] = [
            {key: frame[key] for key in ("sha256", "timestamp_ms", "width", "height") if key in frame}
            for frame in (frames if isinstance(frames, list) else []) if isinstance(frame, dict)]
    return clean


def enforce_attestation(payload, result):
    if valid_attestation(payload, result.get("input_attestation")):
        return result
    return {**{key: payload[key] for key in ("asset_id", "run_id", "input_fingerprint")},
            **({'correction_fingerprint': payload['correction_fingerprint']} if 'correction_fingerprint' in payload else {}),
            "error": "Input integrity verification failed", "description": None, "raw_response": "",
            "input_attestation": technical_attestation(result.get("input_attestation"))}
