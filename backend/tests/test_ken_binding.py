from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest

from app import ken_service as lab
from app.ken_binding import verify_result
from tests.test_ken_service import ken_api_fixture, authenticate, mock_preparation, result_binding


def test_api_history_and_run_lookup_cannot_cross_assets(ken_api_fixture, monkeypatch):
    client, store, vault, asset_a, source = ken_api_fixture
    authenticate(client)
    mock_preparation(monkeypatch)
    source_b = source.with_name("other.mp4")
    source_b.write_bytes(b"distinct synthetic source")
    asset_b = replace(asset_a, id=uuid4(), vault_path="/vault/Home Videos/other.mp4", sha256=None)
    vault.catalogued_assets[asset_b.vault_path] = asset_b
    run_a = store.queue(asset_a.id, asset_a.owner_user_id, lab.configuration())
    run_b = store.queue(asset_b.id, asset_b.owner_user_id, lab.configuration())
    lab.process_next(store, vault)
    lab.process_next(store, vault)
    assert run_a["status"] == run_b["status"] == "completed"
    assert run_a["configuration"]["source"]["verified_sha256"] != run_b["configuration"]["source"]["verified_sha256"]
    assert run_a["configuration"]["input_integrity"]["input_fingerprint"] != run_b["configuration"]["input_integrity"]["input_fingerprint"]
    for asset, run in ((asset_a, run_a), (asset_b, run_b)):
        response = client.get(f"/api/personal-videos/ken/assets/{asset.id}/runs")
        assert response.headers["cache-control"] == "private, no-store"
        assert [value["id"] for value in response.json()] == [str(run["id"])]
        assert response.json()[0]["result"]["asset_id"] == str(asset.id)
        assert response.json()[0]["result"]["run_id"] == str(run["id"])
    assert client.get(f"/api/personal-videos/ken/runs/{run_a['id']}?asset_id={asset_b.id}").status_code == 404
    # Even a broken store returning a global result must fail at the API boundary.
    monkeypatch.setattr(store, "history", lambda *_: [run_a])
    assert client.get(f"/api/personal-videos/ken/assets/{asset_b.id}/runs").status_code == 409


@pytest.mark.parametrize("key", ["asset_id", "run_id", "input_fingerprint"])
def test_stale_adapter_response_is_rejected_before_persistence(ken_api_fixture, monkeypatch, key):
    _, store, vault, asset, _ = ken_api_fixture
    mock_preparation(monkeypatch)
    run = store.queue(asset.id, asset.owner_user_id, lab.configuration())
    def analyse(payload):
        return {**result_binding(payload), key: "stale", "description": "synthetic stale fixture"}
    monkeypatch.setattr(lab.ADAPTER, "analyse", analyse)
    lab.process_next(store, vault)
    assert run["status"] == "failed"
    assert run["result"] is None


def test_api_rejects_embedded_result_identity_mismatch(ken_api_fixture, monkeypatch):
    client, store, vault, asset, _ = ken_api_fixture
    authenticate(client)
    mock_preparation(monkeypatch)
    run = store.queue(asset.id, asset.owner_user_id, lab.configuration())
    lab.process_next(store, vault)
    run["result"]["run_id"] = str(uuid4())
    assert client.get(f"/api/personal-videos/ken/assets/{asset.id}/runs").status_code == 409
    assert client.get(f"/api/personal-videos/ken/runs/{run['id']}").status_code == 409


def test_binding_only_api_never_loads_description_bodies(ken_api_fixture, monkeypatch):
    client, store, _, asset, _ = ken_api_fixture
    authenticate(client)
    def forbidden(*args):
        pytest.fail("Diagnostic API loaded result bodies")
    monkeypatch.setattr(store, "history", forbidden)
    monkeypatch.setattr(store, "bindings", lambda asset_id, owner_id: [{"asset_id": asset_id, "run_id": None}], raising=False)
    response = client.get(f"/api/personal-videos/ken/assets/{asset.id}/runs?binding_only=true")
    assert response.status_code == 200
    assert response.json() == [{"asset_id": str(asset.id), "run_id": None}]
