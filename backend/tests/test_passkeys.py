from datetime import datetime, timedelta, timezone
from dataclasses import replace
import hashlib
from types import SimpleNamespace
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

import app.auth as auth_module
from app.auth import get_enrolment_store, get_passkey_store
from app.auth_store import Account, MemoryAuthenticationStore
from app.enrolment import MemoryEnrolmentStore
from app.passkeys import MemoryPasskeyStore, PasskeyCredential
from tests.conftest import TEST_PASSWORD, TEST_USERNAME, elevate_vault_control


def login(client: TestClient, username: str = TEST_USERNAME, password: str = TEST_PASSWORD) -> None:
    assert client.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
    if username == TEST_USERNAME:
        elevate_vault_control(client, client.app.dependency_overrides[auth_module.get_authentication_store]())


def test_registration_requires_an_authenticated_user(client: TestClient) -> None:
    assert client.post("/api/auth/passkeys/registration/options").status_code == 401


def test_memory_challenge_is_user_bound_expiring_and_one_time() -> None:
    store = MemoryPasskeyStore()
    owner, other = uuid4(), uuid4()
    challenge = store.create_challenge("registration", owner, b"challenge")

    assert store.consume_challenge(challenge.id, "registration", other) is None
    assert store.consume_challenge(challenge.id, "authentication", owner) is None
    assert store.consume_challenge(challenge.id, "registration", owner) is not None
    assert store.consume_challenge(challenge.id, "registration", owner) is None


def test_authenticated_user_can_register_list_and_revoke_own_passkey(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    monkeypatch.setattr(
        auth_module, "verify_registration_response",
        lambda **_: SimpleNamespace(credential_id=b"credential-one", credential_public_key=b"public-key", sign_count=0),
    )
    options = client.post("/api/auth/passkeys/registration/options")
    assert options.status_code == 200
    created = client.post("/api/auth/passkeys/registration/verify", json={
        "challenge_id": options.json()["challenge_id"],
        "credential": {"id": "Y3JlZGVudGlhbC1vbmU", "rawId": "Y3JlZGVudGlhbC1vbmU", "response": {"transports": ["internal"]}},
        "label": "Windows Hello",
    })
    assert created.status_code == 200
    assert created.json()["label"] == "Windows Hello"
    assert client.get("/api/auth/passkeys").json()[0]["id"] == created.json()["id"]
    assert client.delete(f"/api/auth/passkeys/{created.json()['id']}").json() == {"status": "revoked"}
    assert client.get("/api/auth/passkeys").json() == []


def test_registration_challenge_replay_and_malformed_response_fail_closed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    monkeypatch.setattr(
        auth_module, "verify_registration_response",
        lambda **_: SimpleNamespace(credential_id=b"credential-two", credential_public_key=b"public-key", sign_count=0),
    )
    challenge_id = client.post("/api/auth/passkeys/registration/options").json()["challenge_id"]
    body = {"challenge_id": challenge_id, "credential": {"id": "Y3JlZGVudGlhbC10d28", "rawId": "Y3JlZGVudGlhbC10d28", "response": {}}}
    assert client.post("/api/auth/passkeys/registration/verify", json=body).status_code == 200
    assert client.post("/api/auth/passkeys/registration/verify", json=body).status_code == 400


def test_passkey_authentication_uses_credential_user_id_and_normal_session(
    client: TestClient, authentication_store: MemoryAuthenticationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    owner = authentication_store.get_account(TEST_USERNAME)
    assert owner is not None
    passkeys = client.app.dependency_overrides[get_passkey_store]()
    assert isinstance(passkeys, MemoryPasskeyStore)
    passkeys.create_credential(PasskeyCredential(uuid4(), owner.user_id, b"credential-auth", b"public-key", 0, (), "platform", "Windows", datetime.now(timezone.utc), None))
    client.post("/api/auth/logout")
    monkeypatch.setattr(auth_module, "verify_authentication_response", lambda **_: SimpleNamespace(new_sign_count=0))
    challenge = client.post("/api/auth/passkeys/authentication/options")
    assert challenge.status_code == 200
    response = client.post("/api/auth/passkeys/authentication/verify", json={
        "challenge_id": challenge.json()["challenge_id"],
        "credential": {"id": "Y3JlZGVudGlhbC1hdXRo", "rawId": "Y3JlZGVudGlhbC1hdXRo", "response": {"userHandle": ""}},
    })
    assert response.status_code == 200
    assert response.json()["user_id"] == str(owner.user_id)
    assert client.get("/api/auth/session").json()["authenticated"] is True


def test_passkey_authentication_rejects_mismatched_user_handle(
    client: TestClient, authentication_store: MemoryAuthenticationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = authentication_store.ensure_initial_administrator(TEST_USERNAME, "test")
    passkeys = client.app.dependency_overrides[get_passkey_store]()
    assert isinstance(passkeys, MemoryPasskeyStore)
    passkeys.create_credential(PasskeyCredential(uuid4(), owner.user_id, b"credential-handle", b"public-key", 0, (), None, None, datetime.now(timezone.utc), None))
    monkeypatch.setattr(auth_module, "verify_authentication_response", lambda **_: SimpleNamespace(new_sign_count=0))
    challenge = client.post("/api/auth/passkeys/authentication/options")
    response = client.post("/api/auth/passkeys/authentication/verify", json={
        "challenge_id": challenge.json()["challenge_id"],
        "credential": {"id": "Y3JlZGVudGlhbC1oYW5kbGU", "rawId": "Y3JlZGVudGlhbC1oYW5kbGU", "response": {"userHandle": "AAAAAAAAAAAAAAAAAAAAAA"}},
    })
    assert response.status_code == 401


def test_passkey_credential_cannot_be_shared_between_users() -> None:
    store = MemoryPasskeyStore()
    credential = PasskeyCredential(uuid4(), uuid4(), b"same", b"public", 0, (), None, None, datetime.now(timezone.utc), None)
    store.create_credential(credential)
    with pytest.raises(ValueError, match="already registered"):
        store.create_credential(PasskeyCredential(uuid4(), uuid4(), b"same", b"other", 0, (), None, None, datetime.now(timezone.utc), None))


def test_invitation_scoped_enrolment_registers_only_its_server_bound_user_and_replay_fails(
    client: TestClient, authentication_store: MemoryAuthenticationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    created = client.post("/api/vault-control/users", json={
        "display_name": "Passkey First", "email": "passkey-first@example.test", "role": "user",
    })
    assert created.status_code == 200
    invite_url = created.json()["enrolment_url"]
    token = invite_url.rsplit("/", 1)[-1]
    account = authentication_store.get_account("passkey-first@example.test")
    assert account is not None and account.password_hash is None and not account.password_login_enabled

    # The invitation never authenticates a browser session.
    client.post("/api/auth/logout")
    assert client.get("/api/auth/session").json()["authenticated"] is False
    assert client.post("/api/auth/enrolment/status", json={"token": token}).status_code == 200
    monkeypatch.setattr(
        auth_module, "verify_registration_response",
        lambda **_: SimpleNamespace(credential_id=b"enrolment-one", credential_public_key=b"public-key", sign_count=0),
    )
    options = client.post("/api/auth/enrolment/registration/options", json={"token": token})
    assert options.status_code == 200
    body = {
        "token": token,
        "challenge_id": options.json()["challenge_id"],
        "credential": {"id": "ZW5yb2xtZW50LW9uZQ", "rawId": "ZW5yb2xtZW50LW9uZQ", "response": {}},
    }
    assert client.post("/api/auth/enrolment/registration/verify", json=body).status_code == 200
    assert client.post("/api/auth/enrolment/registration/verify", json=body).status_code == 400
    assert client.post("/api/auth/enrolment/status", json={"token": token}).status_code == 400
    passkeys = client.app.dependency_overrides[get_passkey_store]()
    assert passkeys.list_credentials(account.user_id)
    assert not passkeys.list_credentials(uuid4())


def test_invite_replacement_and_expiry_fail_closed(client: TestClient) -> None:
    login(client)
    first = client.post("/api/vault-control/users", json={
        "display_name": "Invite Test", "email": "invite-test@example.test", "role": "user",
    }).json()
    user_id = first["user_id"]
    first_token = first["enrolment_url"].rsplit("/", 1)[-1]
    second = client.post(f"/api/vault-control/users/{user_id}/enrolment-invite")
    assert second.status_code == 200
    assert client.post("/api/auth/enrolment/status", json={"token": first_token}).status_code == 400
    second_token = second.json()["enrolment_url"].rsplit("/", 1)[-1]
    invites = client.app.dependency_overrides[get_enrolment_store]()
    assert isinstance(invites, MemoryEnrolmentStore)
    pending = invites.validate(second_token)
    assert pending is not None
    invites.invites[pending.id] = replace(pending, expires_at=datetime.now(timezone.utc))
    assert client.post("/api/auth/enrolment/status", json={"token": second_token}).status_code == 400


def test_invitation_persists_only_a_hash_and_fails_closed_for_disabled_or_missing_user(
    client: TestClient, authentication_store: MemoryAuthenticationStore
) -> None:
    login(client)
    created = client.post("/api/vault-control/users", json={
        "display_name": "Invite Boundary", "email": "invite-boundary@example.test", "role": "user",
    }).json()
    token = created["enrolment_url"].rsplit("/", 1)[-1]
    invites = client.app.dependency_overrides[get_enrolment_store]()
    assert isinstance(invites, MemoryEnrolmentStore)
    invitation = invites.validate(token)
    assert invitation is not None
    assert invitation.token_hash == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in invitation.token_hash

    account = authentication_store.get_account("invite-boundary@example.test")
    assert account is not None
    authentication_store.accounts[account.username] = replace(account, active=False)
    assert client.post("/api/auth/enrolment/status", json={"token": token}).status_code == 400
    authentication_store.accounts.pop(account.username)
    assert client.post("/api/auth/enrolment/status", json={"token": token}).status_code == 400


def test_administrator_assisted_recovery_is_user_id_bound_revokes_old_access_and_replays_fail(
    client: TestClient, authentication_store: MemoryAuthenticationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    login(client)
    created = client.post("/api/vault-control/users", json={
        "display_name": "Recovery User", "email": "recovery@example.test", "role": "user",
        "temporary_password": "temporary-password",
    }).json()
    user_id = UUID(created["user_id"])
    passkeys = client.app.dependency_overrides[get_passkey_store]()
    passkeys.create_credential(PasskeyCredential(uuid4(), user_id, b"lost-passkey", b"public", 0, (), None, None, datetime.now(timezone.utc), None))
    authentication_store.create_session("lost-session", user_id, "recovery@example.test", datetime.now(timezone.utc) + timedelta(hours=1))

    recovery = client.post(f"/api/vault-control/users/{user_id}/recovery")
    assert recovery.status_code == 200
    token = recovery.json()["recovery_url"].rsplit("/", 1)[-1]
    assert recovery.json()["recovery_expires_at"]
    assert not passkeys.list_credentials(user_id)
    assert authentication_store.get_session_user_id("lost-session") is None
    invites = client.app.dependency_overrides[get_enrolment_store]()
    invite = invites.validate(token, "recovery_enrolment")
    assert invite is not None and invite.user_id == user_id and invite.token_hash == hashlib.sha256(token.encode()).hexdigest()
    replacement = client.post(f"/api/vault-control/users/{user_id}/recovery")
    assert replacement.status_code == 200
    assert client.post("/api/auth/recovery/status", json={"token": token}).status_code == 400
    token = replacement.json()["recovery_url"].rsplit("/", 1)[-1]
    assert client.post("/api/auth/logout").status_code == 200
    assert client.post("/api/auth/recovery/status", json={"token": token}).status_code == 200
    monkeypatch.setattr(auth_module, "verify_registration_response", lambda **_: SimpleNamespace(credential_id=b"replacement-passkey", credential_public_key=b"public", sign_count=0))
    options = client.post("/api/auth/recovery/registration/options", json={"token": token})
    body = {"token": token, "challenge_id": options.json()["challenge_id"], "credential": {"id": "cmVwbGFjZW1lbnQtcGFzc2tleQ", "rawId": "cmVwbGFjZW1lbnQtcGFzc2tleQ", "response": {}}}
    assert client.post("/api/auth/recovery/registration/verify", json=body).status_code == 200
    assert client.post("/api/auth/recovery/registration/verify", json=body).status_code == 400
    assert len(passkeys.list_credentials(user_id)) == 1


def test_passkey_only_user_cannot_remove_final_passkey(client: TestClient, authentication_store: MemoryAuthenticationStore) -> None:
    account = Account("passkey-only", "Passkey Only", "passkey-only@example.test", None, "user", True, False, datetime.now(timezone.utc), None, password_login_enabled=False)
    authentication_store.create_account(account)
    authentication_store.create_session("passkey-only-session", account.user_id, account.username, datetime.now(timezone.utc) + timedelta(hours=1))
    client.cookies.set("pv_session", "passkey-only-session")
    passkeys = client.app.dependency_overrides[get_passkey_store]()
    credential = PasskeyCredential(uuid4(), account.user_id, b"only-passkey", b"public", 0, (), None, None, datetime.now(timezone.utc), None)
    passkeys.create_credential(credential)
    assert client.delete(f"/api/auth/passkeys/{credential.id}").status_code == 409
    passkeys.create_credential(PasskeyCredential(uuid4(), account.user_id, b"second-passkey", b"public", 0, (), None, None, datetime.now(timezone.utc), None))
    assert client.delete(f"/api/auth/passkeys/{credential.id}").status_code == 200
