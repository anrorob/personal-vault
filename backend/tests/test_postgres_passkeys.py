from datetime import datetime, timezone
import os
from uuid import uuid4

import pytest

from app.auth_store import PostgresAuthenticationStore
from app.passkeys import PasskeyCredential, PostgresPasskeyStore


@pytest.fixture
def postgres_conninfo() -> str:
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    return conninfo


def test_postgres_passkey_schema_is_idempotent_uuid_owned_and_restart_safe(postgres_conninfo: str) -> None:
    authentication = PostgresAuthenticationStore(postgres_conninfo)
    authentication.initialize()
    store = PostgresPasskeyStore(postgres_conninfo)
    store.initialize()
    store.initialize()
    account = authentication.ensure_initial_administrator("passkey-owner", "test-hash")
    assert account.password_login_enabled is True
    assert authentication.set_password_login_enabled(account.user_id, False)
    assert authentication.get_account_by_user_id(account.user_id).password_login_enabled is False
    # Reinitialisation must preserve the explicit policy rather than resetting it.
    authentication.initialize()
    assert authentication.get_account_by_user_id(account.user_id).password_login_enabled is False

    try:
        assert store.list_credentials(account.user_id) == []
        challenge = store.create_challenge("registration", account.user_id, b"registration-challenge")
        assert store.consume_challenge(challenge.id, "registration", account.user_id) is not None
        assert store.consume_challenge(challenge.id, "registration", account.user_id) is None
        step_up = store.create_challenge(
            "authentication", account.user_id, b"step-up-challenge", purpose="vault_control_step_up"
        )
        assert store.consume_challenge(step_up.id, "authentication", account.user_id) is None
        assert store.consume_challenge(
            step_up.id, "authentication", account.user_id, purpose="vault_control_step_up"
        ) is not None
        credential = PasskeyCredential(
            id=uuid4(), user_id=account.user_id, credential_id=b"postgres-credential",
            public_key=b"public-key", sign_count=0, transports=("internal",),
            authenticator_attachment="platform", label="Windows Hello",
            created_at=datetime.now(timezone.utc), last_used_at=None,
        )
        store.create_credential(credential)
        recreated = PostgresPasskeyStore(postgres_conninfo)
        persisted = recreated.get_credential(b"postgres-credential")
        assert persisted is not None and persisted.user_id == account.user_id
        recreated.record_authentication(credential.id, 0)
        assert recreated.revoke_credential(credential.id, account.user_id)
        assert recreated.get_credential(b"postgres-credential") is None
    finally:
        with store._connect() as connection, connection.cursor() as cursor:  # test-only disposable cleanup
            cursor.execute("DELETE FROM auth_passkey_challenges")
            cursor.execute("DELETE FROM auth_passkey_credentials")
