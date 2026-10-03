from copy import deepcopy

import pytest

from app import ken_service as lab
from tests.test_ken_service import ken_api_fixture, mock_preparation, result_binding


@pytest.mark.parametrize("change", ["missing", "hash", "size", "nonce", "decoded", "runtime"])
def test_worker_rejects_ambiguous_handoff_and_persists_only_technical_metadata(ken_api_fixture, monkeypatch, change):
    _, store, vault, asset, _ = ken_api_fixture
    mock_preparation(monkeypatch)
    run = store.queue(asset.id, asset.owner_user_id, lab.configuration())

    def analyse(payload):
        saved = store.get(run["id"])["configuration"]
        assert saved["expected_input_sha256"] == payload["expected_input_sha256"]
        assert saved["expected_input_size"] == payload["expected_input_size"] > 0
        assert saved["request_nonce"] == payload["request_nonce"]
        result = result_binding(payload)
        attestation = result["input_attestation"]
        if change == "missing":
            result.pop("input_attestation")
        elif change == "hash":
            attestation["resolved_input_sha256"] = "0" * 64
        elif change == "size":
            attestation["resolved_input_size"] += 1
        elif change == "nonce":
            attestation["request_nonce"] = "stale"
        elif change == "decoded":
            attestation["decoded_manifest"]["frame_count"] = 0
        else:
            attestation["runtime_opened_sha256"] = "0" * 64
        return {**result, "description": "synthetic response must be discarded", "raw_response": "discard this fixture"}

    monkeypatch.setattr(lab.ADAPTER, "analyse", analyse)
    lab.process_next(store, vault)
    assert run["status"] == "failed"
    assert run["error"] == "Input integrity verification failed"
    assert run["result"]["description"] is None
    assert run["result"]["raw_response"] == ""
    if change == "hash":
        assert run["result"]["input_attestation"]["resolved_input_sha256"] == "0" * 64
