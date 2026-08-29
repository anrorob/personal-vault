import hashlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.incoming import require_intake_admission
from app.auth import get_authentication_store
from app.main import app
from app.vault_control_intake import get_gallery_screenshot_findings
from app.vault_master_intake import IntakeRejected, MemoryIntakeStore, get_intake_store
import app.vault_master_intake as intake_module
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, elevate_vault_control


def _source(store: MemoryIntakeStore):
    source, token = store.create_source(TEST_USERNAME, "Stage 3 source")
    assert store.set_source_status(source.id, TEST_USERNAME, "enabled")
    return source, token


def test_pause_waits_for_current_transfer_then_blocks_next_file():
    store = MemoryIntakeStore()
    source, token = _source(store)
    receipt, replayed = store.reserve(
        source.id, token, "stage-three-transfer-0001", "current.jpg", 3,
        hashlib.sha256(b"one").hexdigest(),
    )
    assert replayed is False
    assert store.request_gate("pause") == {"state": "pausing", "active_transfers": 1}
    with pytest.raises(IntakeRejected) as blocked:
        store.reserve(source.id, token, "stage-three-transfer-0002", "next.jpg", 3, hashlib.sha256(b"two").hexdigest())
    assert blocked.value.code == 503
    store.complete(receipt.id, "current.jpg", 3, hashlib.sha256(b"one").hexdigest())
    assert store.gate_status() == {"state": "paused", "active_transfers": 0}
    with pytest.raises(IntakeRejected):
        store.begin_transfer()
    assert store.request_gate("resume") == {"state": "open", "active_transfers": 0}


def test_ordinary_add_to_vault_admission_uses_the_same_gate(monkeypatch):
    store = MemoryIntakeStore()
    monkeypatch.setattr(intake_module, "get_intake_store", lambda: store)
    require_intake_admission()
    assert store.gate_status()["active_transfers"] == 1
    store.request_gate("pause")
    store.finish_transfer()
    with pytest.raises(HTTPException) as blocked:
        require_intake_admission()
    assert blocked.value.status_code == 503


def test_intake_control_page_is_private_and_limits_receipts(client: TestClient):
    store = MemoryIntakeStore()
    app.dependency_overrides[get_intake_store] = lambda: store
    app.dependency_overrides[get_gallery_screenshot_findings] = lambda: []
    try:
        assert client.get("/api/vault-control/intake").status_code == 401
        assert client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}).status_code == 200
        elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())
        response = client.get("/api/vault-control/intake?limit=20")
        assert response.status_code == 200
        body = response.json()
        assert body["gate"] == {"state": "open", "active_transfers": 0}
        assert body["arrival_hall"]["needs_review"] is None
        assert body["audits"] == [{"name": "Gallery Screenshot Audit", "status": "clear", "findings": 0}]
        assert client.get("/api/vault-control/intake?limit=21").status_code == 422
        assert client.post("/api/vault-control/intake/gate/pause").json()["state"] == "paused"
    finally:
        app.dependency_overrides.pop(get_intake_store, None)
        app.dependency_overrides.pop(get_gallery_screenshot_findings, None)
