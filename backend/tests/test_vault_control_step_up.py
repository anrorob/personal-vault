from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import app.auth as auth_module
from app.auth import SESSION_COOKIE_NAME, VAULT_CONTROL_ELEVATION_DURATION, get_passkey_store
from app.auth_store import MemoryAuthenticationStore
from app.passkeys import MemoryPasskeyStore, PasskeyCredential
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def _login(client) -> None:
    assert client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}).status_code == 200


def _credential(store: MemoryAuthenticationStore, client, raw_id: bytes = b"vc-step-up") -> None:
    account = store.get_account(TEST_USERNAME)
    assert account is not None
    passkeys = client.app.dependency_overrides[get_passkey_store]()
    assert isinstance(passkeys, MemoryPasskeyStore)
    passkeys.create_credential(PasskeyCredential(
        uuid4(), account.user_id, raw_id, b"public-key", 0, (), "platform", "Windows",
        datetime.now(timezone.utc), None,
    ))


def test_vault_control_requires_session_scoped_step_up(client, authentication_store: MemoryAuthenticationStore, monkeypatch) -> None:
    _login(client)
    assert client.get("/api/vault-control/users").status_code == 403
    _credential(authentication_store, client)
    monkeypatch.setattr(auth_module, "verify_authentication_response", lambda **_: SimpleNamespace(new_sign_count=1))

    options = client.post("/api/auth/vault-control/elevation/options")
    assert options.status_code == 200
    response = client.post("/api/auth/vault-control/elevation/verify", json={
        "challenge_id": options.json()["challenge_id"],
        "credential": {"id": "dmMtc3RlcC11cA", "rawId": "dmMtc3RlcC11cA", "response": {"userHandle": ""}},
    })
    assert response.status_code == 200
    assert response.json() == {"elevated": True}
    assert client.get("/api/auth/vault-control/elevation").json() == {"elevated": True}
    assert client.get("/api/vault-control/users").status_code == 200

    # A second normal session for the same immutable user is not elevated.
    first_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert first_token is not None
    client.post("/api/auth/logout")
    _login(client)
    second_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert second_token and second_token != first_token
    assert client.get("/api/vault-control/users").status_code == 403


def test_step_up_rejects_other_users_passkey_and_replay(client, authentication_store: MemoryAuthenticationStore, monkeypatch) -> None:
    _login(client)
    administrator = authentication_store.get_account(TEST_USERNAME)
    assert administrator is not None
    from app.auth_store import Account
    from app.security import hash_password
    other = Account("other", "Other", "other@example.test", hash_password("password-value"), "user", True, False, datetime.now(timezone.utc), None)
    authentication_store.create_account(other)
    passkeys = client.app.dependency_overrides[get_passkey_store]()
    assert isinstance(passkeys, MemoryPasskeyStore)
    passkeys.create_credential(PasskeyCredential(uuid4(), other.user_id, b"other-key", b"public-key", 0, (), None, None, datetime.now(timezone.utc), None))
    monkeypatch.setattr(auth_module, "verify_authentication_response", lambda **_: SimpleNamespace(new_sign_count=1))
    options = client.post("/api/auth/vault-control/elevation/options")
    assert options.status_code == 400  # The current administrator has no eligible credential.
    _credential(authentication_store, client, b"admin-key")
    options = client.post("/api/auth/vault-control/elevation/options")
    body = {"challenge_id": options.json()["challenge_id"], "credential": {"id": "b3RoZXIta2V5", "rawId": "b3RoZXIta2V5", "response": {}}}
    assert client.post("/api/auth/vault-control/elevation/verify", json=body).status_code == 401
    assert client.post("/api/auth/vault-control/elevation/verify", json=body).status_code == 400


def test_elevation_expires_and_admin_demotion_invalidates_it(client, authentication_store: MemoryAuthenticationStore) -> None:
    _login(client)
    account = authentication_store.get_account(TEST_USERNAME)
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert account and token
    assert authentication_store.elevate_vault_control_session(token, account.user_id, datetime.now(timezone.utc) + VAULT_CONTROL_ELEVATION_DURATION)
    assert client.get("/api/vault-control/users").status_code == 200
    authentication_store.sessions[token].vault_control_elevated_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert client.get("/api/vault-control/users").status_code == 403
    assert authentication_store.elevate_vault_control_session(token, account.user_id, datetime.now(timezone.utc) + VAULT_CONTROL_ELEVATION_DURATION)
    authentication_store.update_account(TEST_USERNAME, display_name=account.display_name, email=account.email, role="user", active=True)
    assert client.get("/api/vault-control/users").status_code == 403
