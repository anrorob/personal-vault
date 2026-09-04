from collections.abc import Iterator
from datetime import datetime, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from app.vault_supplier import MemoryVaultSupplierStore, get_vault_supplier_store
from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE_NAME, VAULT_CONTROL_ELEVATION_DURATION, get_authentication_store, get_enrolment_store, get_passkey_store
from app.auth_store import MemoryAuthenticationStore
from app.passkeys import MemoryPasskeyStore
from app.enrolment import MemoryEnrolmentStore
import app.main as main_module
from app.main import app
from app.security import hash_password
from app.vault_master_ingestion_ai import MemoryIngestionAiStore, get_ingestion_ai_store


TEST_USERNAME = "owner"
TEST_PASSWORD = "correct-horse-battery-staple"
TEST_PASSWORD_HASH = hash_password(TEST_PASSWORD)


def elevate_vault_control(client: TestClient, store: MemoryAuthenticationStore) -> None:
    """Test-only setup for legacy VC tests unrelated to the WebAuthn ceremony."""
    token = client.cookies.get(SESSION_COOKIE_NAME)
    assert token is not None
    user_id = store.get_session_user_id(token)
    assert user_id is not None
    assert store.elevate_vault_control_session(
        token, user_id, datetime.now(timezone.utc) + VAULT_CONTROL_ELEVATION_DURATION
    )


@pytest.fixture
def authentication_store() -> MemoryAuthenticationStore:
    return MemoryAuthenticationStore()


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch,
    authentication_store: MemoryAuthenticationStore,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[TestClient]:
    monkeypatch.setenv("PV_ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("PV_ADMIN_PASSWORD_HASH", TEST_PASSWORD_HASH)
    monkeypatch.setenv("PV_SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("PV_WEBAUTHN_RP_ID", "testserver")
    monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", "https://testserver")
    monkeypatch.setenv("PV_VAULT_MASTER_WORKER_ENABLED", "false")
    monkeypatch.setenv("PV_ENVIRONMENT", "test")
    monkeypatch.setenv("PV_COMMIT", "test")

    server_key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path_factory.mktemp("vault-supplier-lan-key") / "server-key.pem"
    key_path.write_bytes(server_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv("PV_VAULT_SUPPLIER_SERVER_IDENTITY_KEY_PATH", str(key_path))

    app.dependency_overrides[get_authentication_store] = (
        lambda: authentication_store
    )
    passkey_store = MemoryPasskeyStore()
    app.dependency_overrides[get_passkey_store] = lambda: passkey_store
    enrolment_store = MemoryEnrolmentStore()
    app.dependency_overrides[get_enrolment_store] = lambda: enrolment_store
    supplier_store = MemoryVaultSupplierStore()
    app.dependency_overrides[get_vault_supplier_store] = lambda: supplier_store
    ingestion_ai_store = MemoryIngestionAiStore()
    app.dependency_overrides[get_ingestion_ai_store] = lambda: ingestion_ai_store
    # Unit API tests deliberately replace every persistent dependency with a
    # memory store.  Production lifespan bootstrap is covered separately with
    # disposable PostgreSQL; it must not attempt an unrelated local database.
    monkeypatch.setattr(main_module, "bootstrap_application_schema", lambda: None)

    with TestClient(
        app,
        base_url="https://testserver",
        headers={"Origin": "https://testserver"},
    ) as test_client:
        yield test_client

    app.dependency_overrides.clear()


def pairing_secret(response: dict) -> str:
    import base64
    import json
    credential = response["pairing_credential"]
    assert credential.startswith("PVPAIR1.")
    payload = credential.split(".", 1)[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["pairing_secret"]
