import hashlib
import hmac
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import storage_slot_integrations, vault_master_jellyfin, vault_storage_control
from app.auth import get_authentication_store
from app.auth_store import Account, MemoryAuthenticationStore
from app.security import hash_password
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, elevate_vault_control


def login(client: TestClient) -> None:
    response = client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD})
    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())


def test_storage_inventory_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/vault-control/storage").status_code == 401


def test_storage_controls_require_administrator_role(
    client: TestClient, authentication_store: MemoryAuthenticationStore,
) -> None:
    authentication_store.create_account(
        Account(
            username="member", display_name="Member", email="member@example.test",
            password_hash=hash_password("member-password"), role="user", active=True,
            password_change_required=False, created_at=datetime.now(timezone.utc),
            last_sign_in_at=None,
        )
    )
    assert client.post(
        "/api/auth/login",
        json={"username": "member", "password": "member-password"},
    ).status_code == 200
    assert client.get("/api/vault-control/storage").status_code == 403
    assert client.post(
        "/api/vault-control/storage/operations",
        json={"operation": "verify", "target_hardware_id": "serial:test"},
    ).status_code == 403


def test_storage_inventory_returns_host_snapshot(client: TestClient, monkeypatch, tmp_path) -> None:
    root = tmp_path / "metadata"
    snapshot = root / "storage-control" / "inventory.json"; snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({"schema":"personal-vault.storage-inventory.v1","status":"available","summary":{"total_bytes":10,"used_bytes":1,"free_bytes":9,"health":"Healthy","active_disk_count":4,"unassigned_device_count":0},"disks":[],"unassigned_devices":[],"verification":None}))
    monkeypatch.setattr(vault_storage_control, "get_metadata_storage_root", lambda: root)
    login(client)
    response = client.get("/api/vault-control/storage")
    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json()["summary"]["total_bytes"] == 10


def test_destructive_operation_uses_fixed_signed_schema(client: TestClient, monkeypatch, tmp_path) -> None:
    root = tmp_path / "metadata"; key = tmp_path / "key"; key.write_bytes(b"test-key")
    monkeypatch.setattr(vault_storage_control, "get_metadata_storage_root", lambda: root)
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    login(client)
    rejected = client.post("/api/vault-control/storage/operations", json={"operation":"erase","source_disk_id":"PV-DISK-003","target_hardware_id":"serial:retired","confirmation":"ERASE"})
    assert rejected.status_code == 422
    queued = client.post("/api/vault-control/storage/operations", json={"operation":"erase","source_disk_id":"PV-DISK-003","target_hardware_id":"serial:retired","confirmation":"PV-DISK-003"})
    assert queued.status_code == 200
    request = json.loads(next((root / "storage-control-requests").glob("*.json")).read_text())
    assert set(request["request"]) == {"schema", "request_id", "operation", "target_hardware_id", "source_disk_id", "operation_id", "requested_by", "created_at"}
    assert "command" not in request["request"]


def test_storage_operation_status_reads_structured_executor_state(client: TestClient, monkeypatch, tmp_path) -> None:
    root = tmp_path / "metadata"; operation = root / "storage-control" / "operations" / "replace.json"; operation.parent.mkdir(parents=True)
    operation.write_text(json.dumps({"schema":"personal-vault.storage-operation.v1","operation_id":"replace","operation":"start_replacement","state":"copying","progress":{"files_copied":1}}))
    monkeypatch.setattr(vault_storage_control, "get_metadata_storage_root", lambda: root)
    login(client)
    response = client.get("/api/vault-control/storage/operations")
    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())
    assert response.json()[0]["state"] == "copying"


def test_swap_root_predicate_failure_persists_exact_evidence_before_503(monkeypatch, tmp_path) -> None:
    root = tmp_path / "metadata"; key = tmp_path / "key"; key.write_bytes(b"test-key")
    operation = {
        "operation_id": "swap-root-evidence", "state": "integrating",
        "receipt": {"slot_id": "PV-DISK-001", "integration_pending": "swap"},
        "safe_to_disconnect": False,
    }
    evidence = {
        "stage": "root_set_preflight", "predicate": "current_equals_before_or_desired",
        "root_sets": {
            "raw": {"current": ["/media/movies/"], "desired": ["/media/movies"], "managed": ["/media/movies"]},
            "normalised": {"current": ["/media/movies"], "desired": ["/media/movies"], "managed": ["/media/movies"]},
            "comparisons": {"current_equals_desired": False, "accepted": False},
        },
    }
    monkeypatch.setattr(vault_storage_control, "get_metadata_storage_root", lambda: root)
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    monkeypatch.setattr(vault_storage_control, "get_operations", lambda: [operation])
    monkeypatch.setattr(vault_master_jellyfin, "get_jellyfin_metadata_client", lambda: object())
    monkeypatch.setattr(
        storage_slot_integrations, "reconcile_movies_slot",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(storage_slot_integrations.SlotIntegrationError("root mismatch", evidence=evidence)),
    )

    with pytest.raises(HTTPException, match="root mismatch") as raised:
        vault_storage_control._reconcile_operation("swap-root-evidence")

    assert raised.value.status_code == 503
    diagnostic = json.loads((root / "storage-swap-acks" / "swap-root-evidence.error.json").read_text())
    assert diagnostic["error"] == "root mismatch"
    assert diagnostic["evidence"] == evidence
    assert diagnostic["schema"] == "personal-vault.storage-slot-integration-error.v1"
    signed = {name: value for name, value in diagnostic.items() if name != "signature"}
    assert hmac.compare_digest(
        diagnostic["signature"], hmac.new(b"test-key", vault_storage_control._canonical(signed), hashlib.sha256).hexdigest(),
    )
    assert operation["state"] == "integrating"
    assert operation["safe_to_disconnect"] is False
    assert not (root / "storage-swap-acks" / "swap-root-evidence.json").exists()


def test_add_drive_requires_a_current_safe_host_candidate(client: TestClient, monkeypatch, tmp_path) -> None:
    root = tmp_path / "metadata"; key = tmp_path / "key"; key.write_bytes(b"test-key")
    snapshot = root / "storage-control" / "inventory.json"; snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({"schema":"personal-vault.storage-inventory.v1","status":"available","operations_enabled":True,"eligible_areas":["Theatre / Movies"],"summary":None,"disks":[],"verification":None,"unassigned_devices":[{"hardware_id":"serial:new","device_path":"/dev/test","safety":"ready"}]}))
    monkeypatch.setattr(vault_storage_control, "get_metadata_storage_root", lambda: root)
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    login(client)
    response = client.get("/api/vault-control/storage/add-drive")
    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())
    queued = client.post("/api/vault-control/storage/operations", json={"operation":"commission_add","target_hardware_id":"serial:new","vault_area":"Theatre / Movies","confirmation":"PREPARE DRIVE"})
    assert queued.status_code == 200
    request = json.loads(next((root / "storage-control-requests").glob("*.json")).read_text())["request"]
    assert request["candidate"] == {"hardware_id":"serial:new","device_path":"/dev/test","safety":"ready"}
    assert request["vault_area"] == "Theatre / Movies"


def test_add_drive_context_excludes_system_and_commissioned_drives_from_selection(client: TestClient, monkeypatch, tmp_path) -> None:
    root = tmp_path / "metadata"
    snapshot = root / "storage-control" / "inventory.json"; snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({
        "schema": "personal-vault.storage-inventory.v1", "status": "available",
        "eligible_areas": ["Theatre / Movies"], "summary": None,
        "disks": [{"id": "PV-DISK-001", "serial": "pv-disk-001", "device_path": "/dev/sda"}],
        "verification": None,
        "unassigned_devices": [
            {"hardware_id": "serial:system", "device_path": "/dev/nvme0n1", "safety": "blocked", "reason": "System disk is protected; Drive or partition is mounted/in use"},
            {"hardware_id": "serial:pv-disk-001", "device_path": "/dev/sda", "safety": "ready"},
            {"hardware_id": "serial:spare", "device_path": "/dev/sde", "safety": "ready"},
        ],
    }))
    monkeypatch.setattr(vault_storage_control, "get_metadata_storage_root", lambda: root)
    login(client)
    response = client.get("/api/vault-control/storage/add-drive")
    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())
    assert response.json()["candidates"] == [{"hardware_id": "serial:spare", "device_path": "/dev/sde", "safety": "ready"}]


def test_commissioned_hardware_tracks_serial_and_wwn_without_rewriting_either_identity() -> None:
    identities, paths = vault_storage_control._commissioned_hardware({
        "disks": [{"id": "PV-DISK-001", "serial": "legacy-001", "wwn": "modern-001", "device_path": "/dev/sda"}],
    })
    assert identities == {"serial:legacy-001", "wwn:modern-001"}
    assert paths == {"/dev/sda"}


def test_add_drive_refuses_a_stale_or_blocked_candidate(client: TestClient, monkeypatch, tmp_path) -> None:
    root = tmp_path / "metadata"; key = tmp_path / "key"; key.write_bytes(b"test-key")
    snapshot = root / "storage-control" / "inventory.json"; snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps({"schema":"personal-vault.storage-inventory.v1","status":"available","operations_enabled":True,"eligible_areas":["Theatre / Movies"],"summary":None,"disks":[],"verification":None,"unassigned_devices":[{"hardware_id":"serial:new","safety":"blocked"}]}))
    monkeypatch.setattr(vault_storage_control, "get_metadata_storage_root", lambda: root)
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    login(client)
    response = client.post("/api/vault-control/storage/operations", json={"operation":"commission_add","target_hardware_id":"serial:new","vault_area":"Theatre / Movies","confirmation":"PREPARE DRIVE"})
    assert response.status_code == 422


def test_retire_slot_requires_current_empty_authoritative_preflight(client: TestClient, monkeypatch, tmp_path) -> None:
    root = tmp_path / "metadata"; key = tmp_path / "key"; key.write_bytes(b"test-key")
    monkeypatch.setattr(vault_storage_control, "get_metadata_storage_root", lambda: root)
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    monkeypatch.setattr(vault_storage_control, "get_retire_preflight", lambda slot: {"slot_id": slot, "state": "eligible", "canonical_file_count": 0, "filesystem_uuid": "empty", "hardware_id": "serial:empty"})
    login(client)
    response = client.post("/api/vault-control/storage/operations", json={"operation":"retire_slot","source_disk_id":"PV-DISK-005","confirmation":"RETIRE DRIVE"})
    assert response.status_code == 200
    elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())
    request = json.loads(next((root / "storage-control-requests").glob("*.json")).read_text())["request"]
    assert request["operation"] == "retire_slot"
    assert request["retirement"]["canonical_file_count"] == 0
    assert request["requested_by_user_id"]


def test_retire_slot_refuses_non_empty_preflight(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(vault_storage_control, "get_retire_preflight", lambda _: {"state": "blocked"})
    login(client)
    response = client.post("/api/vault-control/storage/operations", json={"operation":"retire_slot","source_disk_id":"PV-DISK-005","confirmation":"RETIRE DRIVE"})
    assert response.status_code == 409


def test_legacy_direct_slot_is_not_eligible_for_managed_retirement(monkeypatch) -> None:
    monkeypatch.setattr(vault_storage_control, "get_inventory", lambda: {
        "disks": [{"id": "PV-DISK-001", "state": "Active", "lifecycle_eligible": False}],
    })
    monkeypatch.setattr(vault_storage_control, "get_operations", lambda: [])
    with pytest.raises(HTTPException, match="legacy direct slot") as error:
        vault_storage_control.get_retire_preflight("PV-DISK-001")
    assert error.value.status_code == 409


def test_legacy_direct_slot_is_not_eligible_for_managed_replacement(monkeypatch, tmp_path) -> None:
    key = tmp_path / "key"; key.write_bytes(b"test-key")
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    monkeypatch.setattr(vault_storage_control, "get_inventory", lambda: {
        "disks": [{"id": "PV-DISK-001", "state": "Active", "lifecycle_eligible": False}],
    })
    operation = vault_storage_control.StorageOperation(
        operation="start_replacement", source_disk_id="PV-DISK-001", target_hardware_id="wwn:new", confirmation="PV-DISK-001",
    )
    with pytest.raises(HTTPException, match="Legacy direct topology") as error:
        vault_storage_control.queue_operation(operation, SimpleNamespace(role="administrator", active=True, user_id="admin"))
    assert error.value.status_code == 409


def test_reconciled_managed_slot_queues_a_signed_same_slot_replacement(monkeypatch, tmp_path) -> None:
    key = tmp_path / "key"; key.write_bytes(b"test-key")
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    monkeypatch.setattr(vault_storage_control, "get_inventory", lambda: {
        "swap_drive": {"enabled": True, "generation": "generation-1"},
        "disks": [{"id": "PV-DISK-001", "state": "Active", "lifecycle_eligible": True, "production_lifecycle_eligible": False, "serial": "old", "filesystem_uuid": "old-uuid"}],
        "unassigned_devices": [{"hardware_id": "wwn:new", "device_path": "/dev/new", "capacity_bytes": 1000, "safety": "ready"}],
    })
    monkeypatch.setattr(vault_storage_control, "_swap_snapshot", lambda slot, disk, candidate: {"slot_id": slot, "source_hardware_id": "serial:old", "source_filesystem_uuid": "old-uuid", "target": candidate, "files": [], "canonical_bytes": 0})
    monkeypatch.setattr(vault_storage_control, "_swap_queue_path", lambda: tmp_path / "swap-requests")
    operation = vault_storage_control.StorageOperation(
        operation="start_replacement", source_disk_id="PV-DISK-001", target_hardware_id="wwn:new", confirmation="PV-DISK-001",
    )
    queued = vault_storage_control.queue_operation(operation, SimpleNamespace(role="administrator", active=True, user_id="admin"))
    request = json.loads(next((tmp_path / "swap-requests").glob("*.json")).read_text())["request"]
    assert queued["status"] == "queued"
    assert request["swap"]["slot_id"] == "PV-DISK-001"
    assert request["swap_drive_generation"] == "generation-1"
    assert "physical_mount" not in request["swap"]


def test_swap_context_and_queue_fail_closed_until_root_publishes_enabled_capability(monkeypatch, tmp_path) -> None:
    key = tmp_path / "key"; key.write_bytes(b"test-key")
    inventory = {
        "swap_drive": {"enabled": False, "generation": None},
        "disks": [{"id": "PV-DISK-001", "state": "Active", "lifecycle_eligible": True}],
        "unassigned_devices": [{"hardware_id": "wwn:new", "device_path": "/dev/new", "capacity_bytes": 1000, "safety": "ready"}],
    }
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    monkeypatch.setattr(vault_storage_control, "get_inventory", lambda: inventory)
    monkeypatch.setattr(vault_storage_control, "_swap_queue_path", lambda: tmp_path / "swap-requests")
    monkeypatch.setattr(vault_storage_control, "get_operations", lambda: [])
    assert vault_storage_control.get_swap_context("PV-DISK-001")["swap_enabled"] is False
    operation = vault_storage_control.StorageOperation(operation="start_replacement", source_disk_id="PV-DISK-001", target_hardware_id="wwn:new", confirmation="PV-DISK-001")
    with pytest.raises(HTTPException, match="not operationally enabled") as error:
        vault_storage_control.queue_operation(operation, SimpleNamespace(role="administrator", active=True, user_id="admin"))
    assert error.value.status_code == 503
    assert not (tmp_path / "swap-requests").exists()


def test_swap_queue_refuses_a_second_live_request(monkeypatch, tmp_path) -> None:
    key = tmp_path / "key"; key.write_bytes(b"test-key")
    queue = tmp_path / "swap-requests"; queue.mkdir(); (queue / "already-live.json").write_text("{}")
    monkeypatch.setattr(vault_storage_control, "_key_path", lambda: key)
    monkeypatch.setattr(vault_storage_control, "_swap_queue_path", lambda: queue)
    monkeypatch.setattr(vault_storage_control, "get_inventory", lambda: {
        "swap_drive": {"enabled": True, "generation": "generation-1"},
        "disks": [{"id": "PV-DISK-001", "state": "Active", "lifecycle_eligible": True}],
        "unassigned_devices": [{"hardware_id": "wwn:new", "device_path": "/dev/new", "capacity_bytes": 1000, "safety": "ready"}],
    })
    operation = vault_storage_control.StorageOperation(operation="start_replacement", source_disk_id="PV-DISK-001", target_hardware_id="wwn:new", confirmation="PV-DISK-001")
    with pytest.raises(HTTPException, match="already awaiting") as error:
        vault_storage_control.queue_operation(operation, SimpleNamespace(role="administrator", active=True, user_id="admin"))
    assert error.value.status_code == 409
