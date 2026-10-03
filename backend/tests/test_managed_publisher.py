"""Synthetic v1/v2 request compatibility for the generic managed publisher."""

import hashlib
import hmac
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest


PUBLISHER = Path(__file__).parents[2] / "ops/storage/arrival-managed-publisher.py"


def _publisher(tmp_path: Path, monkeypatch):
    monkeypatch.syspath_prepend(str(PUBLISHER.parents[2] / "backend"))
    spec = importlib.util.spec_from_file_location("managed_publisher_candidate", PUBLISHER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.QUEUE = tmp_path / "requests"
    module.RECEIPTS = tmp_path / "receipts"
    module.KEY = tmp_path / "key"
    module.ARRIVAL = tmp_path / "Arrival Hall"
    module.MANIFEST = tmp_path / "active-slots.json"
    module.SLOT_ROOT = tmp_path / "slots"
    for root in (module.QUEUE, module.RECEIPTS, module.ARRIVAL,
                 module.SLOT_ROOT / "PV-DISK-002"):
        root.mkdir(parents=True)
    module.KEY.write_bytes(b"synthetic-publisher-test-key")
    module.MANIFEST.write_text(json.dumps({
        "schema": module.SCHEMA,
        "slots": {"PV-DISK-002": {
            "state": "active", "integration_mode": "slot_managed",
            "hardware_id": "synthetic", "filesystem_uuid": "synthetic",
            "areas": ["Gallery"],
            "logical_mappings": {"Gallery": "/vault/Gallery"},
            "resolver_root": str(module.SLOT_ROOT / "PV-DISK-002"),
        }},
    }))
    return module


def _request(module, name: str, authorization: bool):
    item_id = str(UUID(int=1 if authorization else 2))
    request_id = str(UUID(int=3 if authorization else 4))
    payload = f"synthetic-{name}".encode()
    (module.ARRIVAL / name).write_bytes(payload)
    request = {
        "request_id": request_id, "item_id": item_id,
        "owner_user_id": str(UUID(int=5)), "source_relative_path": name,
        "logical_destination": f"/vault/Gallery/{name}",
        "expected_sha256": hashlib.sha256(payload).hexdigest(),
        "expected_size_bytes": len(payload),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if authorization:
        request["routing_authorization"] = {
            "version": "vm-routing-score-v2", "decision_id": str(UUID(int=6)),
            "authorization_id": str(UUID(int=7)), "policy_id": str(UUID(int=8)),
            "owner_user_id": request["owner_user_id"],
            "semantic_class": "personal_photo", "destination": "Gallery",
            "score": 85, "threshold": 80,
        }
    path = module.QUEUE / f"{request_id}.json"
    path.write_text(json.dumps({
        "request": request,
        "signature": hmac.new(module.KEY.read_bytes(), module.canonical(request), hashlib.sha256).hexdigest(),
    }))
    return path, request


@pytest.mark.parametrize("authorization", [False, True])
def test_legacy_and_scored_requests_publish_with_matching_receipts(tmp_path, monkeypatch, authorization):
    module = _publisher(tmp_path, monkeypatch)
    path, request = _request(module, "Example Photo.jpg", authorization)
    module.process_request(path)
    assert not path.exists()
    assert (module.SLOT_ROOT / "PV-DISK-002/Gallery/Example Photo.jpg").read_bytes() == b"synthetic-Example Photo.jpg"
    document = json.loads((module.RECEIPTS / f"{request['request_id']}.json").read_text())
    receipt = document["receipt"]
    assert receipt.get("routing_authorization") == request.get("routing_authorization")
    assert hmac.compare_digest(document["signature"], hmac.new(
        module.KEY.read_bytes(), module.canonical(receipt), hashlib.sha256).hexdigest())


def test_invalid_scored_authorization_cannot_publish(tmp_path, monkeypatch):
    module = _publisher(tmp_path, monkeypatch)
    path, request = _request(module, "Example Photo.jpg", True)
    request["routing_authorization"]["destination"] = "Documents"
    path.write_text(json.dumps({"request": request, "signature": hmac.new(
        module.KEY.read_bytes(), module.canonical(request), hashlib.sha256).hexdigest()}))
    with pytest.raises(ValueError, match="routing authorization"):
        module.process_request(path)
    assert not list(module.RECEIPTS.iterdir())
    assert not list((module.SLOT_ROOT / "PV-DISK-002").iterdir())


def test_recovery_cannot_be_combined_with_scored_authorization(tmp_path, monkeypatch):
    module = _publisher(tmp_path, monkeypatch)
    path, request = _request(module, "Example Photo.jpg", True)
    request["recovery"] = {"schema": "personal-vault.tv-resolver-relocation.v1"}
    path.write_text(json.dumps({"request": request, "signature": hmac.new(
        module.KEY.read_bytes(), module.canonical(request), hashlib.sha256).hexdigest()}))
    with pytest.raises(ValueError, match="invalid request"):
        module.process_request(path)
