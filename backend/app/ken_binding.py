"""Identity-only validation; never interpret KEN response content."""

RESULT_BINDING_VERSION = "ken-result-binding-v1"


def verify_result(asset_id, run_id, config, result):
    if config.get('correction_version') and result.get('correction_fingerprint') != config.get('correction_fingerprint'):
        raise ValueError('KEN correction binding mismatch')
    if (result.get("asset_id") != str(asset_id) or result.get("run_id") != str(run_id)
            or not config.get("input_integrity", {}).get("input_fingerprint")
            or result.get("input_fingerprint") != config["input_integrity"]["input_fingerprint"]):
        raise ValueError("KEN result binding mismatch")


def verify_stored_run(run, asset_id, owner_id):
    if run["asset_id"] != asset_id or run["owner_user_id"] != owner_id:
        raise ValueError("KEN history asset mismatch")
    config = run["configuration"]
    source = config.get("source", {})
    manifest = config.get("input_integrity", {}).get("manifest", {})
    if any(value is not None and value != str(asset_id) for value in (source.get("asset_id"), manifest.get("asset_id"))):
        raise ValueError("KEN source binding mismatch")
    result = run.get("result")
    if result is not None and (config.get("result_binding_version") or any(key in result for key in ("asset_id", "run_id", "input_fingerprint"))):
        verify_result(asset_id, run["id"], config, result)
    return run
