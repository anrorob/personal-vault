from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi.testclient import TestClient
from httpx import Response

from app.auth import SESSION_COOKIE_NAME
from app.auth_store import MemoryAuthenticationStore
from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def login(
    client: TestClient,
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
) -> Response:
    return client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


def test_health_reports_database_ready(client: TestClient) -> None:
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "pv-backend",
        "database": "ok",
    }


def test_successful_login_creates_secure_session(client: TestClient) -> None:
    response = login(client)

    assert response.status_code == 200
    payload = response.json()
    assert UUID(payload.pop("user_id"))
    assert payload == {
        "status": "authenticated",
        "username": TEST_USERNAME,
        "authenticated": True,
        "role": "administrator",
        "display_name": TEST_USERNAME,
        "password_change_required": False,
        "password_login_enabled": True,
    }

    set_cookie = response.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE_NAME}=" in set_cookie
    assert "httponly" in set_cookie
    assert "secure" in set_cookie
    assert "samesite=lax" in set_cookie

    session_response = client.get("/api/auth/session")
    assert session_response.status_code == 200
    session_payload = session_response.json()
    assert session_payload.pop("user_id") == response.json()["user_id"]
    assert session_payload == {
        "authenticated": True,
        "username": TEST_USERNAME,
        "display_name": TEST_USERNAME,
        "role": "administrator",
        "password_change_required": False,
        "password_login_enabled": True,
    }

    protected_response = client.get("/api/auth/me")
    assert protected_response.status_code == 200
    current_payload = protected_response.json()
    assert current_payload.pop("user_id") == response.json()["user_id"]
    assert current_payload == {
        "authenticated": True,
        "username": TEST_USERNAME,
        "display_name": TEST_USERNAME,
        "role": "administrator",
        "password_change_required": False,
        "password_login_enabled": True,
    }


def test_authenticated_mutation_requires_canonical_origin_and_sensitive_auth_responses_do_not_cache(
    client: TestClient,
) -> None:
    assert login(client).status_code == 200
    assert client.get("/api/auth/session").headers["cache-control"] == "no-store"
    assert client.post("/api/auth/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/auth/logout").status_code == 200


def test_unexpected_host_is_rejected_before_authentication(client: TestClient) -> None:
    response = client.get("/api/health", headers={"Host": "evil.example"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid host"}


def test_password_session_persists_available_client_metadata(client: TestClient, authentication_store: MemoryAuthenticationStore) -> None:
    response = client.post("/api/auth/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}, headers={"User-Agent": "Firefox/125.0 Windows NT"})
    assert response.status_code == 200
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token is not None
    record = authentication_store.sessions[token]
    assert record.authentication_method == "password"
    assert record.user_agent == "Firefox/125.0 Windows NT"
    assert record.client_ip is not None
    assert record.last_seen_at is not None
    payload = client.get("/api/auth/sessions").json()[0]
    assert payload["user_agent"] == record.user_agent
    assert payload["authentication_method"] == "password"
    assert payload["current"] is True


def test_protected_endpoint_rejects_missing_session(
    client: TestClient,
) -> None:
    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_protected_endpoint_rejects_invalid_session(
    client: TestClient,
) -> None:
    client.cookies.set(SESSION_COOKIE_NAME, "invalid-session-token")

    response = client.get("/api/auth/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Authentication required"}


def test_invalid_credentials_return_generic_error(client: TestClient) -> None:
    wrong_password_response = login(client, password="wrong-password")
    unknown_username_response = login(
        client,
        username="unknown-user",
        password="wrong-password",
    )

    expected_body = {"detail": "Invalid username or password"}
    assert wrong_password_response.status_code == 401
    assert wrong_password_response.json() == expected_body
    assert unknown_username_response.status_code == 401
    assert unknown_username_response.json() == expected_body


def test_fifth_failed_attempt_starts_lockout(client: TestClient) -> None:
    for _ in range(4):
        response = login(client, password="wrong-password")
        assert response.status_code == 401

    lockout_response = login(client, password="wrong-password")
    assert lockout_response.status_code == 429
    assert lockout_response.json() == {
        "detail": "Too many failed login attempts. Try again later.",
    }
    assert 1 <= int(lockout_response.headers["retry-after"]) <= 900

    blocked_response = login(client, password=TEST_PASSWORD)
    assert blocked_response.status_code == 429
    assert 1 <= int(blocked_response.headers["retry-after"]) <= 900


def test_successful_login_clears_previous_failures(client: TestClient) -> None:
    for _ in range(4):
        assert login(client, password="wrong-password").status_code == 401

    assert login(client).status_code == 200

    for _ in range(4):
        assert login(client, password="wrong-password").status_code == 401

    assert login(client, password="wrong-password").status_code == 429


def test_logout_invalidates_session(client: TestClient) -> None:
    assert login(client).status_code == 200
    assert client.get("/api/auth/session").json()["authenticated"] is True

    logout_response = client.post("/api/auth/logout")

    assert logout_response.status_code == 200
    assert logout_response.json() == {"status": "logged_out"}
    assert client.get("/api/auth/session").json() == {
        "authenticated": False,
        "username": None,
        "display_name": None,
        "role": None,
        "password_change_required": False,
    }
    assert client.get("/api/auth/me").status_code == 401


def test_expired_session_is_rejected_and_removed(
    client: TestClient,
    authentication_store: MemoryAuthenticationStore,
) -> None:
    assert login(client).status_code == 200
    session_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_token is not None

    authentication_store.sessions[session_token].expires_at = (
        datetime.now(timezone.utc) - timedelta(seconds=1)
    )

    protected_response = client.get("/api/auth/me")

    assert protected_response.status_code == 401
    assert protected_response.json() == {"detail": "Authentication required"}
    assert session_token not in authentication_store.sessions
    assert client.get("/api/auth/session").json() == {
        "authenticated": False,
        "username": None,
        "display_name": None,
        "role": None,
        "password_change_required": False,
    }


def test_password_change_keeps_current_session_and_revokes_other_sessions(
    client: TestClient,
    authentication_store: MemoryAuthenticationStore,
) -> None:
    assert login(client).status_code == 200
    current_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert current_token is not None
    account = authentication_store.get_account(TEST_USERNAME)
    assert account is not None
    other_token = "another-valid-device-session"
    authentication_store.create_session(
        other_token, account.user_id, account.username,
        datetime.now(timezone.utc) + timedelta(hours=1),
    )

    response = client.post("/api/auth/change-password", json={"password": "changed-password"})

    assert response.status_code == 200
    assert current_token in authentication_store.sessions
    assert other_token not in authentication_store.sessions
    assert client.get("/api/auth/me").status_code == 200


def test_password_login_policy_is_server_enforced_and_preserved_by_password_change(client: TestClient, authentication_store: MemoryAuthenticationStore) -> None:
    assert login(client).status_code == 200
    account = authentication_store.get_account(TEST_USERNAME)
    assert account is not None
    assert account.password_login_enabled is True
    assert authentication_store.set_password_login_enabled(account.user_id, False)
    assert login(client, password=TEST_PASSWORD).status_code == 401
    # A current passkey-authenticated session would be able to change a stored
    # password, but that operation must not re-enable password login.
    authentication_store.set_account_password(account.username, account.password_hash, False)
    assert authentication_store.get_account_by_user_id(account.user_id).password_login_enabled is False


def test_active_sessions_are_uuid_scoped_and_other_session_revocation_preserves_current(
    client: TestClient, authentication_store: MemoryAuthenticationStore,
) -> None:
    assert login(client).status_code == 200
    current_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert current_token is not None
    account = authentication_store.get_account(TEST_USERNAME)
    assert account is not None
    authentication_store.create_session("other-device", account.user_id, account.username, datetime.now(timezone.utc) + timedelta(hours=1), authentication_method="passkey")
    sessions = client.get("/api/auth/sessions").json()
    assert len(sessions) == 2
    current = next(item for item in sessions if item["current"])
    other = next(item for item in sessions if not item["current"])
    assert client.delete(f"/api/auth/sessions/{current['id']}").status_code == 409
    assert client.delete(f"/api/auth/sessions/{other['id']}").json() == {"status": "revoked"}
    assert current_token in authentication_store.sessions
    assert "other-device" not in authentication_store.sessions


def test_session_events_are_bounded_and_unknown_login_failure_is_not_attributed(client: TestClient) -> None:
    assert login(client, username="unknown@example.test", password="wrong-password").status_code == 401
    assert client.get("/api/auth/security-events").status_code == 401
    assert login(client).status_code == 200
    events = client.get("/api/auth/security-events").json()
    assert events[0]["event_type"] == "sign_in_succeeded"
    assert "token" not in str(events).lower()


def test_sign_out_other_sessions_preserves_current_session(client: TestClient, authentication_store: MemoryAuthenticationStore) -> None:
    assert login(client).status_code == 200
    account = authentication_store.get_account(TEST_USERNAME)
    assert account is not None
    authentication_store.create_session("other-device", account.user_id, account.username, datetime.now(timezone.utc) + timedelta(hours=1))
    assert client.post("/api/auth/sessions/sign-out-others").json() == {"status": "revoked"}
    assert "other-device" not in authentication_store.sessions
    assert client.get("/api/auth/me").status_code == 200
