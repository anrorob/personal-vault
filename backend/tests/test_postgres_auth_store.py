from datetime import datetime, timedelta, timezone
import os
from collections.abc import Iterator

import pytest
import psycopg

from app.auth_store import (
    GalleryState,
    MovieProgress,
    PostgresAuthenticationStore,
)


@pytest.fixture
def postgres_conninfo() -> str:
    conninfo = os.getenv("PV_TEST_DATABASE_URL")

    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")

    return conninfo


@pytest.fixture
def postgres_store(
    postgres_conninfo: str,
) -> Iterator[PostgresAuthenticationStore]:
    store = PostgresAuthenticationStore(postgres_conninfo)
    store.initialize()
    store.reset()
    yield store
    store.reset()


def test_session_survives_store_recreation(
    postgres_store: PostgresAuthenticationStore,
    postgres_conninfo: str,
) -> None:
    token = "persistent-session-token"
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    account = postgres_store.ensure_initial_administrator("owner", "test-hash")
    postgres_store.create_session(token, account.user_id, account.username, expires_at)

    recreated_store = PostgresAuthenticationStore(postgres_conninfo)

    assert recreated_store.get_session_user_id(token) == account.user_id


def test_expired_postgres_session_is_removed(
    postgres_store: PostgresAuthenticationStore,
) -> None:
    token = "expired-session-token"
    expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    account = postgres_store.ensure_initial_administrator("owner", "test-hash")
    postgres_store.create_session(token, account.user_id, account.username, expires_at)

    assert postgres_store.get_session_user_id(token) is None


def test_postgres_vault_control_elevation_is_session_bound_and_expires(
    postgres_store: PostgresAuthenticationStore,
) -> None:
    account = postgres_store.ensure_initial_administrator("owner", "test-hash")
    first, second = "first-vc-session", "second-vc-session"
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    postgres_store.create_session(first, account.user_id, account.username, expiry)
    postgres_store.create_session(second, account.user_id, account.username, expiry)
    elevated_until = datetime.now(timezone.utc) + timedelta(minutes=30)
    assert postgres_store.elevate_vault_control_session(first, account.user_id, elevated_until)
    assert postgres_store.has_vault_control_elevation(first, account.user_id)
    assert not postgres_store.has_vault_control_elevation(second, account.user_id)
    assert postgres_store.refresh_vault_control_elevation(first, account.user_id, elevated_until)
    postgres_store.delete_session(first)
    assert not postgres_store.has_vault_control_elevation(first, account.user_id)


def test_postgres_session_metadata_public_id_and_event_ledger_are_uuid_scoped(
    postgres_store: PostgresAuthenticationStore,
) -> None:
    account = postgres_store.ensure_initial_administrator("owner", "test-hash")
    other = postgres_store.ensure_initial_administrator("other", "test-hash")
    postgres_store.create_session("current", account.user_id, account.username, datetime.now(timezone.utc) + timedelta(hours=1), authentication_method="passkey", client_ip="203.0.113.10", user_agent="Test Browser")
    postgres_store.create_session("other", other.user_id, other.username, datetime.now(timezone.utc) + timedelta(hours=1))
    sessions = postgres_store.list_active_sessions(account.user_id, "current")
    assert len(sessions) == 1 and sessions[0].current
    assert sessions[0].authentication_method == "passkey"
    assert sessions[0].client_ip == "203.0.113.10"
    assert sessions[0].user_agent == "Test Browser"
    assert sessions[0].last_seen_at is not None
    assert postgres_store.revoke_session_for_user_id(other.user_id, sessions[0].id) is False
    postgres_store.record_security_event("sign_in_succeeded", user_id=account.user_id, authentication_method="passkey", client_ip="203.0.113.10")
    events = postgres_store.list_security_events(account.user_id)
    assert len(events) == 1 and events[0].event_type == "sign_in_succeeded"
    assert postgres_store.list_security_events(other.user_id) == []


def test_lockout_survives_store_recreation(
    postgres_store: PostgresAuthenticationStore,
    postgres_conninfo: str,
) -> None:
    rate_limit_key = "owner:203.0.113.10"

    for _ in range(4):
        assert postgres_store.record_failed_attempt(rate_limit_key) == 0

    assert postgres_store.record_failed_attempt(rate_limit_key) > 0

    recreated_store = PostgresAuthenticationStore(postgres_conninfo)

    assert recreated_store.get_lockout_seconds(rate_limit_key) > 0


def test_clearing_postgres_failures_is_persistent(
    postgres_store: PostgresAuthenticationStore,
    postgres_conninfo: str,
) -> None:
    rate_limit_key = "owner:203.0.113.10"

    for _ in range(5):
        postgres_store.record_failed_attempt(rate_limit_key)

    assert postgres_store.get_lockout_seconds(rate_limit_key) > 0

    recreated_store = PostgresAuthenticationStore(postgres_conninfo)
    recreated_store.clear_failed_attempts(rate_limit_key)

    assert postgres_store.get_lockout_seconds(rate_limit_key) == 0


def test_legacy_identity_migration_is_idempotent(
    postgres_conninfo: str,
) -> None:
    with psycopg.connect(postgres_conninfo) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DROP TABLE IF EXISTS auth_sessions, auth_login_attempts, "
                "user_movie_progress, user_gallery_state, auth_accounts CASCADE"
            )
            cursor.execute(
                """
                CREATE TABLE auth_accounts (
                    username TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    email TEXT UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('administrator','user')),
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    password_change_required BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_sign_in_at TIMESTAMPTZ
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE user_gallery_state (
                    username TEXT PRIMARY KEY,
                    sort_order TEXT NOT NULL CHECK (sort_order IN ('newest', 'oldest')),
                    anchor_id TEXT,
                    anchor_offset INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE user_movie_progress (
                    username TEXT NOT NULL,
                    movie_id TEXT NOT NULL,
                    position_seconds DOUBLE PRECISION NOT NULL CHECK (position_seconds >= 0),
                    duration_seconds DOUBLE PRECISION NOT NULL CHECK (duration_seconds >= 0),
                    completed BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (username, movie_id)
                )
                """
            )
            cursor.execute(
                """
                INSERT INTO auth_accounts
                    (username, display_name, email, password_hash, role)
                VALUES ('legacy-user', 'Legacy User', 'legacy@example.test', 'hash', 'user')
                """
            )
            cursor.execute(
                """
                INSERT INTO user_gallery_state (username, sort_order, anchor_id, anchor_offset)
                VALUES ('legacy-user', 'oldest', 'photo-42', -18)
                """
            )
            cursor.execute(
                """
                INSERT INTO user_movie_progress
                    (username, movie_id, position_seconds, duration_seconds, completed)
                VALUES ('legacy-user', 'movie-42', 125, 600, FALSE)
                """
            )

    try:
        migrated = PostgresAuthenticationStore(postgres_conninfo)
        migrated.initialize()
        account = migrated.get_account('legacy-user')

        assert account is not None
        assert migrated.get_account_by_user_id(account.user_id) == account
        assert migrated.get_gallery_state(account.user_id) == GalleryState(
            'oldest', 'photo-42', -18
        )
        assert migrated.get_movie_progress(account.user_id, 'movie-42') == MovieProgress(
            'movie-42', 125, 600, False
        )

        # This matches an account insert from the pre-Stage-1A backend, which
        # does not know about user_id.
        with psycopg.connect(postgres_conninfo) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auth_accounts
                        (username, display_name, email, password_hash, role)
                    VALUES ('rollback-user', 'Rollback User', 'rollback@example.test', 'hash', 'user')
                    """
                )
        rollback_account = migrated.get_account('rollback-user')
        assert rollback_account is not None

        recreated = PostgresAuthenticationStore(postgres_conninfo)
        assert recreated.get_account('legacy-user').user_id == account.user_id
        assert recreated.get_account('rollback-user').user_id == rollback_account.user_id
        assert recreated.get_gallery_state(account.user_id) == GalleryState(
            'oldest', 'photo-42', -18
        )
        assert recreated.get_movie_progress(account.user_id, 'movie-42') == MovieProgress(
            'movie-42', 125, 600, False
        )
    finally:
        with psycopg.connect(postgres_conninfo) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DROP TABLE IF EXISTS auth_sessions, auth_login_attempts, "
                    "user_movie_progress, user_gallery_state, auth_accounts CASCADE"
                )
