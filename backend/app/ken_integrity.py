"""KEN binding of actual prepared media to one canonical asset."""

import hashlib
import json


def input_manifest(asset_id, config: dict, payload: dict) -> dict:
    source = config["source"]
    if source["asset_id"] != str(asset_id) or payload["asset_id"] != str(asset_id):
        raise ValueError("Prepared input belongs to a different asset")
    mode = config["input_mode"]
    if payload["input_mode"] != mode:
        raise ValueError("Prepared input mode mismatch")
    manifest = {
        "version": "ken-input-integrity-v1", "asset_id": str(asset_id),
        "source_size_bytes": source["size_bytes"], "source_duration_ms": source["duration_ms"],
        "authoritative_source_sha256": source.get("catalogue_sha256"),
        "input_mode": mode,
    }
    if config.get('correction_version'):
        manifest['correction_version'] = config['correction_version']
        manifest['correction_context'] = config.get('correction_context', {})
    if source.get("verified_sha256"):
        manifest["verified_source_sha256"] = source["verified_sha256"]
    if mode == "native_video":
        manifest.update(native_parameters=config["runtime_video"], frame_count=None)
        if source.get("verified_sha256"):
            manifest["verified_source_sha256"] = source["verified_sha256"]
        if config["runtime_video"].get("prepared_sha256"):
            if payload.get("native_sha256") != config["runtime_video"]["prepared_sha256"]:
                raise ValueError("Native prepared input checksum mismatch")
            manifest["prepared_video_sha256"] = payload["native_sha256"]
        if not source.get("catalogue_sha256") and not source.get("verified_sha256"):
            manifest["source_mtime_ns"] = source["mtime_ns"]
    else:
        raise ValueError("Unknown prepared input mode")
    return manifest


def fingerprint(manifest: dict) -> str:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def seal_input(asset_id, config: dict, payload: dict) -> None:
    manifest = input_manifest(asset_id, config, payload)
    digest = fingerprint(manifest)
    config["input_integrity"] = {"manifest": manifest, "input_fingerprint": digest}
    payload["input_fingerprint"] = digest


def verify_input(asset_id, config: dict, payload: dict) -> None:
    manifest = input_manifest(asset_id, config, payload)
    saved = config["input_integrity"]
    if manifest != saved["manifest"] or fingerprint(manifest) != saved["input_fingerprint"] or payload["input_fingerprint"] != saved["input_fingerprint"]:
        raise ValueError("Prepared input fingerprint mismatch")
