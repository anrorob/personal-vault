from fastapi.testclient import TestClient
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from app.main import app
from app.jellyfin import JellyfinUnavailableError
from app.vault_master import MemoryVaultMasterStore, get_vault_master_store
import app.vault_services as services
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, elevate_vault_control
from app.auth import get_authentication_store


class HealthyJellyfin:
    def service_status(self):
        return {"version": "10.11.11", "active_streams": 1, "scan_state": "Unavailable", "last_completed_scan": None}

    def refresh_library(self):
        return None


class OfflineJellyfin:
    def service_status(self):
        raise JellyfinUnavailableError("offline")

    def refresh_library(self):
        raise JellyfinUnavailableError("offline")


def _login(client: TestClient):
    assert client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}).status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())


def test_services_are_private_and_unavailable_metrics_are_explicit(client: TestClient, monkeypatch):
    app.dependency_overrides[get_vault_master_store] = MemoryVaultMasterStore
    monkeypatch.setattr(services, "get_jellyfin_service_status", lambda: OfflineJellyfin().service_status())
    try:
        assert client.get("/api/vault-control/services").status_code == 401
        _login(client)
        response = client.get("/api/vault-control/services")
        assert response.status_code == 200
        body = response.json()
        assert body["jellyfin"]["status"] == "unavailable"
        assert body["database"]["response_ms"] is None
        assert body["backend"]["request_errors"] is None
        assert "secret" not in response.text
    finally:
        app.dependency_overrides.pop(get_vault_master_store, None)


def test_aggregate_uses_worst_service_state():
    result = services._aggregate({
        "vault_master": {"status": "idle"}, "florence": {"status": "ready"},
        "jellyfin": {"status": "unavailable"}, "database": {"status": "offline"},
        "backend": {"status": "healthy"},
    })
    assert result["status"] == "critical"
    assert result["failures"] == 2
    assert result["affected_services"] == ["jellyfin", "database"]


def test_florence_reports_cross_user_activity_without_owner_details(monkeypatch):
    active_job = SimpleNamespace(item_id=uuid4(), status="processing")
    completed_evidence = SimpleNamespace(created_at=datetime.now(timezone.utc))
    store = SimpleNamespace(
        list_all_jobs=lambda: [active_job],
        list_all_evidence=lambda: [completed_evidence],
    )
    monkeypatch.setattr(
        services,
        "get_operational_health",
        lambda _path: {"florence": {"status": "ok", "model": "Florence", "device": "GPU", "active_requests": 1}},
    )
    monkeypatch.setattr(services, "get_ingestion_ai_store", lambda: store)

    result = services._florence()

    assert result["status"] == "busy"
    assert result["gpu_usage"] == "Active inference"
    assert result["current_job"] == {"status": "processing"}
    assert result["queue_length"] == 0


def test_jellyfin_scan_is_admin_only_rate_limited_and_reports_failure(client: TestClient, monkeypatch):
    app.dependency_overrides[get_vault_master_store] = MemoryVaultMasterStore
    monkeypatch.setattr(services, "request_jellyfin_library_scan", HealthyJellyfin().refresh_library)
    services._last_scan_monotonic = 0
    try:
        assert client.post("/api/vault-control/services/jellyfin/scan").status_code == 401
        _login(client)
        assert client.post("/api/vault-control/services/jellyfin/scan").json()["status"] == "triggered"
        assert client.post("/api/vault-control/services/jellyfin/scan").status_code == 429
        services._last_scan_monotonic = 0
        monkeypatch.setattr(services, "request_jellyfin_library_scan", OfflineJellyfin().refresh_library)
        assert client.post("/api/vault-control/services/jellyfin/scan").status_code == 503
    finally:
        app.dependency_overrides.pop(get_vault_master_store, None)
