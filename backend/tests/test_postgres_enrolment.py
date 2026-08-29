from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest

from app.auth_store import Account, PostgresAuthenticationStore
from app.enrolment import INVITATION_LIFETIME, PostgresEnrolmentStore
from app.passkeys import PasskeyCredential, PostgresPasskeyStore


@pytest.fixture
def postgres_conninfo() -> str:
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    return conninfo


def test_postgres_invite_consumes_with_first_credential_atomically(postgres_conninfo: str) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    authentication.initialize()
    passkeys = PostgresPasskeyStore(postgres_conninfo)
    passkeys.initialize()
    invites = PostgresEnrolmentStore(postgres_conninfo)
    invites.initialize()
    account = Account(
        "enrolment-atomic@example.test", "Enrolment Atomic", "enrolment-atomic@example.test",
        None, "user", True, False, datetime.now(timezone.utc), None, password_login_enabled=False,
    )
    authentication.create_account(account)
    try:
        invite, token = invites.create(account.user_id, account.user_id)
        assert invite.expires_at - invite.created_at == INVITATION_LIFETIME
        credential = PasskeyCredential(
            uuid4(), account.user_id, b"enrolment-atomic", b"public-key", 0,
            ("internal",), "platform", "Windows Hello", datetime.now(timezone.utc), None,
        )
        assert invites.consume_and_create_credential(token, credential)
        assert passkeys.get_credential(b"enrolment-atomic") is not None
        assert invites.validate(token) is None
        assert not invites.consume_and_create_credential(token, credential)

        rollback_account = Account(
            "enrolment-rollback@example.test", "Enrolment Rollback", "enrolment-rollback@example.test",
            None, "user", True, False, datetime.now(timezone.utc), None, password_login_enabled=False,
        )
        authentication.create_account(rollback_account)
        _, second_token = invites.create(rollback_account.user_id, account.user_id)
        duplicate = PasskeyCredential(
            uuid4(), rollback_account.user_id, b"enrolment-atomic", b"other-key", 0,
            (), None, None, datetime.now(timezone.utc), None,
        )
        with pytest.raises(Exception):
            invites.consume_and_create_credential(second_token, duplicate)
        # Credential insert failure rolls back the invite consumption too.
        assert invites.validate(second_token) is not None
    finally:
        with passkeys._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM auth_enrolment_invites WHERE user_id IN (%s, %s)", (account.user_id, locals().get("rollback_account", account).user_id))
            cursor.execute("DELETE FROM auth_passkey_credentials WHERE user_id IN (%s, %s)", (account.user_id, locals().get("rollback_account", account).user_id))
            cursor.execute("DELETE FROM auth_accounts WHERE user_id IN (%s, %s)", (account.user_id, locals().get("rollback_account", account).user_id))


def test_postgres_recovery_revokes_only_target_access_and_consumes_atomically(postgres_conninfo: str) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    authentication.initialize()
    passkeys = PostgresPasskeyStore(postgres_conninfo)
    passkeys.initialize()
    invites = PostgresEnrolmentStore(postgres_conninfo)
    invites.initialize()
    target = Account("recovery-target@example.test", "Recovery Target", "recovery-target@example.test", "hash", "user", True, False, datetime.now(timezone.utc), None)
    other = Account("recovery-other@example.test", "Recovery Other", "recovery-other@example.test", "hash", "user", True, False, datetime.now(timezone.utc), None)
    authentication.create_account(target)
    authentication.create_account(other)
    old = PasskeyCredential(uuid4(), target.user_id, b"recovery-old", b"public", 0, (), None, None, datetime.now(timezone.utc), None)
    untouched = PasskeyCredential(uuid4(), other.user_id, b"recovery-other", b"public", 0, (), None, None, datetime.now(timezone.utc), None)
    passkeys.create_credential(old)
    passkeys.create_credential(untouched)
    authentication.create_session("recovery-target-session", target.user_id, target.username, datetime.now(timezone.utc) + timedelta(hours=1))
    authentication.create_session("recovery-other-session", other.user_id, other.username, datetime.now(timezone.utc) + timedelta(hours=1))
    try:
        recovery = invites.create_recovery(target.user_id, other.user_id)
        assert recovery is not None
        invite, token = recovery
        assert invite.purpose == "recovery_enrolment"
        assert invite.expires_at - invite.created_at == INVITATION_LIFETIME
        assert token not in invite.token_hash
        assert not passkeys.list_credentials(target.user_id)
        assert passkeys.list_credentials(other.user_id)
        assert authentication.get_session_user_id("recovery-target-session") is None
        assert authentication.get_session_user_id("recovery-other-session") == other.user_id
        credential = PasskeyCredential(uuid4(), target.user_id, b"recovery-new", b"public", 0, (), None, None, datetime.now(timezone.utc), None)
        assert invites.consume_and_create_credential(token, credential, "recovery_enrolment")
        assert passkeys.get_credential(b"recovery-new") is not None
        assert not invites.consume_and_create_credential(token, credential, "recovery_enrolment")
    finally:
        with passkeys._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM auth_enrolment_invites WHERE user_id IN (%s, %s)", (target.user_id, other.user_id))
            cursor.execute("DELETE FROM auth_passkey_credentials WHERE user_id IN (%s, %s)", (target.user_id, other.user_id))
            cursor.execute("DELETE FROM auth_sessions WHERE user_id IN (%s, %s)", (target.user_id, other.user_id))
            cursor.execute("DELETE FROM auth_accounts WHERE user_id IN (%s, %s)", (target.user_id, other.user_id))
