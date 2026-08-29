from fastapi.testclient import TestClient
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.auth import get_authentication_store, get_passkey_store
from app.auth_store import MemoryAuthenticationStore
from app.passkeys import PasskeyCredential
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, elevate_vault_control


def _login(client: TestClient, username: str = TEST_USERNAME, password: str = TEST_PASSWORD) -> None:
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    if username == TEST_USERNAME:
        elevate_vault_control(client, client.app.dependency_overrides[get_authentication_store]())


def test_existing_account_is_admin_and_users_can_be_administered(client: TestClient, authentication_store: MemoryAuthenticationStore) -> None:
    _login(client)
    response = client.get("/api/vault-control/users")
    assert response.status_code == 200
    assert response.json()["users"][0]["role"] == "administrator"
    created = client.post("/api/vault-control/users", json={"display_name": "Ada", "email": "ada@example.test", "role": "user", "temporary_password": "temporary-password"})
    assert created.status_code == 200
    assert created.json()["password_change_required"] is True
    assert client.post("/api/auth/logout").status_code == 200
    _login(client, "ada@example.test", "temporary-password")
    assert client.get("/api/auth/me").status_code == 403
    assert client.get("/api/vault-control/users").status_code == 403
    assert client.post("/api/auth/change-password", json={"password": "new-temporary-password"}).status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    _login(client)
    assert client.post("/api/vault-control/users", json={"display_name": "Again", "email": "ada@example.test", "role": "user", "temporary_password": "temporary-password"}).status_code == 409
    assert client.post("/api/vault-control/users/ada%40example.test/reset-password", json={"temporary_password": "reset-password"}).status_code == 200
    assert not any(record.username == "ada@example.test" for record in authentication_store.sessions.values())


def test_duplicate_display_names_keep_distinct_permanent_identities(client: TestClient) -> None:
    _login(client)
    first = client.post("/api/vault-control/users", json={"display_name": "Owner", "email": "first@example.test", "role": "user", "temporary_password": "temporary-password"})
    second = client.post("/api/vault-control/users", json={"display_name": "Owner", "email": "second@example.test", "role": "user", "temporary_password": "temporary-password"})
    assert first.status_code == second.status_code == 200
    assert first.json()["display_name"] == second.json()["display_name"] == "Owner"
    assert first.json()["user_id"] != second.json()["user_id"]


def test_disable_and_last_administrator_protection(client: TestClient) -> None:
    _login(client)
    assert client.put("/api/vault-control/users/owner", json={"display_name": "owner", "email": "owner@example.test", "role": "user", "active": True}).status_code == 409
    assert client.put("/api/vault-control/users/owner", json={"display_name": "owner", "email": "owner@example.test", "role": "administrator", "active": False}).status_code == 409
    created = client.post("/api/vault-control/users", json={"display_name": "Ada", "email": "ada@example.test", "role": "user", "temporary_password": "temporary-password"})
    assert created.status_code == 200
    assert client.put("/api/vault-control/users/ada%40example.test", json={"display_name": "Ada", "email": "ada@example.test", "role": "user", "active": False}).status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/auth/login", json={"username": "ada@example.test", "password": "temporary-password"}).status_code == 401


def test_password_login_policy_is_uuid_addressed_admin_only_and_requires_passkey(client: TestClient, authentication_store: MemoryAuthenticationStore) -> None:
    _login(client)
    created = client.post("/api/vault-control/users", json={"display_name": "Ada", "email": "ada@example.test", "role": "user", "temporary_password": "temporary-password"}).json()
    user_id = created["user_id"]
    policy = f"/api/vault-control/users/{user_id}/authentication-policy"
    assert client.put(policy, json={"password_login_enabled": False}).status_code == 409
    passkeys = client.app.dependency_overrides[get_passkey_store]()
    passkeys.create_credential(PasskeyCredential(uuid4(), UUID(user_id), b"ada", b"public", 0, (), None, None, datetime.now(timezone.utc), None))
    assert client.put(policy, json={"password_login_enabled": False}).status_code == 200
    assert authentication_store.get_account_by_user_id(UUID(user_id)).password_login_enabled is False
    assert client.put(policy, json={"password_login_enabled": True}).status_code == 200


def test_elevated_administrator_can_revoke_only_target_uuid_sessions(client: TestClient, authentication_store: MemoryAuthenticationStore) -> None:
    _login(client)
    created = client.post("/api/vault-control/users", json={"display_name": "Ada", "email": "ada@example.test", "role": "user", "temporary_password": "temporary-password"}).json()
    target = UUID(created["user_id"])
    account = authentication_store.get_account_by_user_id(target)
    assert account is not None
    authentication_store.create_session("ada-device", target, account.username, datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1))
    response = client.get(f"/api/vault-control/users/{target}/sessions")
    assert response.status_code == 200
    session_id = response.json()["sessions"][0]["id"]
    assert client.delete(f"/api/vault-control/users/{target}/sessions/{session_id}").json() == {"status": "revoked"}
    assert "ada-device" not in authentication_store.sessions


def test_user_session_management_requires_existing_vault_control_elevation(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}).status_code == 200
    assert client.get("/api/vault-control/users/00000000-0000-0000-0000-000000000000/sessions").status_code == 403
