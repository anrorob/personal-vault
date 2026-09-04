"""PVPAIR1 contract and the same security behavior on memory and PostgreSQL."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import os
import re
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
import pytest

from app.auth import get_authentication_store
from app.auth_store import PostgresAuthenticationStore
from app.vault_supplier import (
    PAIRING_CODE_LIFETIME, PostgresVaultSupplierStore, SupplierInstallation,
    _now, get_pairing_server_identity, get_vault_supplier_store, pairing_binding,
)
from app.vault_supplier_lan import LanServerIdentity
from tests.test_vault_supplier import _key, _login


@pytest.fixture(params=["memory", "postgres"])
def pairing_client(request, client):
    _login(client)
    if request.param == "memory":
        yield client
        return
    conninfo = os.getenv("PV_TEST_DATABASE_URL")
    if not conninfo:
        pytest.skip("PV_TEST_DATABASE_URL is not configured")
    schema = "pair010_" + uuid4().hex
    with psycopg.connect(conninfo) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(conninfo, options=f"-csearch_path={schema}")
    try:
        auth = PostgresAuthenticationStore(scoped)
        auth.initialize()
        account = client.app.dependency_overrides[get_authentication_store]().get_account("owner")
        auth.create_account(account)
        with psycopg.connect(scoped) as connection:
            connection.execute("CREATE TABLE vaults(vault_id UUID PRIMARY KEY, is_local BOOLEAN NOT NULL)")
            connection.execute("INSERT INTO vaults VALUES(%s,TRUE)", (uuid4(),))
        store = PostgresVaultSupplierStore(scoped)
        store.initialize()
        client.app.dependency_overrides[get_vault_supplier_store] = lambda: store
        yield client
    finally:
        with psycopg.connect(conninfo) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def issue(client):
    response = client.post("/api/vault-supplier/pairing-code")
    assert response.status_code == 200, response.text
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"pairing_credential", "expires_at", "protocol_version"}
    credential = body["pairing_credential"]
    assert re.fullmatch(r"PVPAIR1\.[A-Za-z0-9_-]+", credential)
    assert len(credential) < 1024
    payload = credential.split(".")[1]
    return body, json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def pair_request(descriptor):
    _, public = _key()
    return {"pairing_secret": descriptor["pairing_secret"], "installation_id": str(uuid4()),
            "installation_public_key": public, "key_algorithm": "ECDSA_P256",
            "supplier_version": "pair010-test", "protocol_version": 1}


def assert_error(response, code):
    assert response.status_code in (400, 403, 422), response.text
    assert response.json()["detail"]["code"] == code
    assert isinstance(response.json()["detail"]["message"], str)


def store_for(client):
    return client.app.dependency_overrides[get_vault_supplier_store]()


def mutate_record(store, secret, **updates):
    record = store.get_pairing_code(secret)
    if isinstance(store, PostgresVaultSupplierStore):
        with store._connect() as connection:
            for key, value in updates.items():
                connection.execute(sql.SQL("UPDATE vault_supplier_pairing_codes SET {}=%s WHERE id=%s").format(sql.Identifier(key)), (value, record.id))
    else:
        binding_updates = {k: v for k, v in updates.items() if k in {"protocol_version", "management_origin", "server_key_id", "server_public_key_spki_der_base64"}}
        if binding_updates:
            updates = {k: v for k, v in updates.items() if k not in binding_updates}
            updates["binding"] = replace(record.binding, **binding_updates)
        store.codes[record.id] = replace(record, **updates)


def test_exact_descriptor_roundtrip_hash_storage_and_audit(pairing_client):
    client = pairing_client
    body, descriptor = issue(client)
    store = store_for(client)
    secret = descriptor["pairing_secret"]
    record = store.get_pairing_code(secret)
    assert set(descriptor) == {"v", "vault_id", "origin", "server_key_id", "server_public_key_spki_der_base64", "pairing_secret"}
    assert descriptor["v"] == 1
    assert descriptor["vault_id"] == str(store.local_vault()[0]) == str(UUID(descriptor["vault_id"]))
    assert descriptor["origin"] == "https://testserver"
    der = base64.b64decode(descriptor["server_public_key_spki_der_base64"], validate=True)
    assert base64.b64encode(der).decode() == descriptor["server_public_key_spki_der_base64"]
    assert hashlib.sha256(der).hexdigest() == descriptor["server_key_id"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", secret)
    assert record.code_hash == hashlib.sha256(secret.encode()).hexdigest()
    assert record.expires_at - record.created_at == PAIRING_CODE_LIFETIME == timedelta(minutes=10)
    stored = repr(record)
    if isinstance(store, PostgresVaultSupplierStore):
        with store._connect() as connection:
            stored = str(connection.execute("SELECT row_to_json(c) FROM vault_supplier_pairing_codes c WHERE id=%s", (record.id,)).fetchone()[0])
    assert secret not in stored and body["pairing_credential"] not in stored and "PVPAIR1." not in stored
    request = pair_request(descriptor)
    response = client.post("/api/vault-supplier/pair", json=request)
    assert response.status_code == 200, response.text
    paired = response.json()
    assert paired["vault_id"] == descriptor["vault_id"]
    assert paired["user_id"] == str(record.user_id)
    assert paired["installation_id"] == request["installation_id"]
    assert paired["protocol_version"] == 1
    assert paired["server_identity"] == {"key_algorithm": "ECDSA_P256_SHA256", "key_id_sha256": descriptor["server_key_id"], "public_key_spki_der_base64": descriptor["server_public_key_spki_der_base64"]}
    assert_error(client.post("/api/vault-supplier/pair", json=request), "pairing_code_used")
    events = client.get("/api/auth/security-events").json()
    assert {event["event_type"] for event in events} >= {"vault_supplier_pairing_code_generated", "vault_supplier_paired", "vault_supplier_user_authorized", "vault_supplier_installation_registered"}
    assert secret not in json.dumps(events) and body["pairing_credential"] not in json.dumps(events)


@pytest.mark.parametrize("origin,expected", [
    ("https://another.example.net", "https://another.example.net"),
    ("https://vault.example.net", "https://vault.example.net"),
    ("https://VAULT.EXAMPLE.NET:443/", "https://vault.example.net"),
    ("https://vault.example.net:8443/", "https://vault.example.net:8443"),
])
def test_configured_origin(pairing_client, monkeypatch, origin, expected):
    from urllib.parse import urlsplit
    monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", origin)
    monkeypatch.setenv("PV_WEBAUTHN_RP_ID", urlsplit(origin).hostname)
    pairing_client.headers["Origin"] = expected
    pairing_client.headers["Host"] = urlsplit(expected).netloc
    _, descriptor = issue(pairing_client)
    assert descriptor["origin"] == expected
    response = pairing_client.post("/api/vault-supplier/pair", json=pair_request(descriptor))
    assert response.status_code == 200, response.text
    assert response.json()["vault_id"] == descriptor["vault_id"]
    assert response.json()["server_identity"]["key_id_sha256"] == descriptor["server_key_id"]


@pytest.mark.parametrize("origin", ["", "http://testserver", "https://testserver/path", "https://user:test@testserver", "https://@testserver", "https://testserver?", "https://testserver#", "https://testserver?q=1", "https://testserver/#a", " https://testserver", "https://testserver\n", "https://testserver:bad", "https://testserver:0", "https://testserver:", "https://testserver:65536", "https://testserver\\bad"])
def test_unsafe_origin_never_replaces_active_credential(pairing_client, monkeypatch, origin):
    _, descriptor = issue(pairing_client)
    monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", origin)
    assert_error(pairing_client.post("/api/vault-supplier/pairing-code"), "invalid_pairing_origin")
    record = store_for(pairing_client).get_pairing_code(descriptor["pairing_secret"])
    assert record.invalidated_at is None


@pytest.mark.parametrize("field,value", [("vault_id", uuid4()), ("management_origin", "https://old.example.net"), ("server_key_id", "0" * 64), ("server_public_key_spki_der_base64", "bad-spki"), ("protocol_version", 2)])
def test_stored_binding_is_authoritative(pairing_client, field, value):
    store = store_for(pairing_client)
    if isinstance(store, PostgresVaultSupplierStore) and field == "protocol_version":
        # Production CHECK(protocol_version=1) forbids this corrupted state.
        with store._connect() as connection:
            with pytest.raises(psycopg.errors.CheckViolation):
                _, descriptor = issue(pairing_client)
                connection.execute("UPDATE vault_supplier_pairing_codes SET protocol_version=2")
        return
    _, descriptor = issue(pairing_client)
    if isinstance(store, PostgresVaultSupplierStore) and field == "vault_id":
        with store._connect() as connection:
            connection.execute("INSERT INTO vaults VALUES(%s,FALSE)", (value,))
    mutate_record(store, descriptor["pairing_secret"], **{field: value})
    assert_error(pairing_client.post("/api/vault-supplier/pair", json=pair_request(descriptor)), "protocol_mismatch" if field == "protocol_version" else "pairing_identity_mismatch")
    assert store.get_pairing_code(descriptor["pairing_secret"]).consumed_at is None


@pytest.mark.parametrize("change", ["vault", "origin", "identity"])
def test_current_identity_change_fails_without_consuming(pairing_client, monkeypatch, change):
    _, descriptor = issue(pairing_client)
    store = store_for(pairing_client)
    if change == "vault":
        if isinstance(store, PostgresVaultSupplierStore):
            with store._connect() as connection:
                connection.execute("UPDATE vaults SET is_local=FALSE")
                connection.execute("INSERT INTO vaults VALUES(%s,TRUE)", (uuid4(),))
        else:
            store.vault_id = uuid4()
    elif change == "origin":
        monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", "https://new.example.net")
        monkeypatch.setenv("PV_WEBAUTHN_RP_ID", "new.example.net")
        pairing_client.headers["Origin"] = "https://new.example.net"
        pairing_client.headers["Host"] = "new.example.net"
    else:
        private, _ = _key()
        from cryptography.hazmat.primitives import serialization
        der = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        identity = LanServerIdentity(private, der, hashlib.sha256(der).hexdigest())
        pairing_client.app.dependency_overrides[get_pairing_server_identity] = lambda: identity
    assert_error(pairing_client.post("/api/vault-supplier/pair", json=pair_request(descriptor)), "pairing_identity_mismatch")
    assert store.get_pairing_code(descriptor["pairing_secret"]).consumed_at is None


@pytest.mark.parametrize("change", ["key_id", "spki", "missing"])
def test_malformed_server_identity_blocks_issuance(pairing_client, monkeypatch, change):
    identity = LanServerIdentity.load()
    if change == "missing":
        monkeypatch.setenv("PV_VAULT_SUPPLIER_SERVER_IDENTITY_KEY_PATH", "/nonexistent/pair010-key")
    else:
        identity = replace(identity, **({"key_id_sha256": "A" * 64} if change == "key_id" else {"public_key_spki_der": b"bad"}))
        pairing_client.app.dependency_overrides[get_pairing_server_identity] = lambda: identity
    assert_error(pairing_client.post("/api/vault-supplier/pairing-code"), "invalid_pairing_descriptor")


def test_expired_replaced_unknown_and_legacy(pairing_client):
    _, first = issue(pairing_client)
    _, second = issue(pairing_client)
    assert first["pairing_secret"] != second["pairing_secret"]
    assert_error(pairing_client.post("/api/vault-supplier/pair", json=pair_request(first)), "pairing_code_replaced")
    mutate_record(store_for(pairing_client), second["pairing_secret"], expires_at=_now() - timedelta(seconds=1))
    assert_error(pairing_client.post("/api/vault-supplier/pair", json=pair_request(second)), "pairing_code_expired")
    for secret in ["A" * 43, "SHORTLEGACYCODE123456", "", "PVPAIR1.fake"]:
        assert_error(pairing_client.post("/api/vault-supplier/pair", json=pair_request({"pairing_secret": secret})), "invalid_pairing_code")


def test_failed_registration_rolls_back_consumption(pairing_client):
    _, descriptor = issue(pairing_client)
    request = pair_request(descriptor)
    assert pairing_client.post("/api/vault-supplier/pair", json=request).status_code == 200
    _, next_descriptor = issue(pairing_client)
    bad = pair_request(next_descriptor)
    bad["installation_id"] = request["installation_id"]
    assert_error(pairing_client.post("/api/vault-supplier/pair", json=bad), "invalid_installation_identity")
    assert store_for(pairing_client).get_pairing_code(next_descriptor["pairing_secret"]).consumed_at is None
    request["pairing_secret"] = next_descriptor["pairing_secret"]
    assert pairing_client.post("/api/vault-supplier/pair", json=request).status_code == 200


@pytest.mark.parametrize("field,value,error", [
    ("protocol_version", 2, "protocol_mismatch"),
    ("protocol_version", True, "invalid_pairing_descriptor"),
    ("installation_public_key", "bad", "invalid_installation_key"),
    ("key_algorithm", "RSA", "invalid_installation_key"),
    ("user_id", str(uuid4()), "invalid_pairing_descriptor"),
])
def test_bad_request_does_not_consume_or_echo_secret(pairing_client, field, value, error):
    _, descriptor = issue(pairing_client)
    request = pair_request(descriptor)
    request[field] = value
    response = pairing_client.post("/api/vault-supplier/pair", json=request)
    assert_error(response, error)
    assert descriptor["pairing_secret"] not in response.text
    assert store_for(pairing_client).get_pairing_code(descriptor["pairing_secret"]).consumed_at is None


def test_inactive_bound_user_cannot_pair(pairing_client):
    _, descriptor = issue(pairing_client)
    auth = pairing_client.app.dependency_overrides[get_authentication_store]()
    auth.update_account("owner", display_name="Owner", email=None, role="admin", active=False)
    pairing_client.cookies.clear()
    assert_error(pairing_client.post("/api/vault-supplier/pair", json=pair_request(descriptor)), "user_not_allowed")
    assert store_for(pairing_client).get_pairing_code(descriptor["pairing_secret"]).consumed_at is None


def test_concurrent_issuance_and_consume_once(pairing_client):
    store = store_for(pairing_client)
    binding = pairing_binding(store.local_vault()[0], LanServerIdentity.load())
    user = pairing_client.app.dependency_overrides[get_authentication_store]().get_account("owner")
    import secrets
    secrets_list = [secrets.token_urlsafe(32) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(lambda secret: store.create_pairing_code(binding, user.user_id, secret), secrets_list))
    assert sum(record.replaced_previous for record in records) == 7
    active = [secret for secret in secrets_list if not store.get_pairing_code(secret).invalidated_at]
    assert len(active) == 1
    _, public = _key()
    installation = SupplierInstallation(uuid4(), binding.vault_id, base64.urlsafe_b64decode(public + "=" * (-len(public) % 4)), "ECDSA_P256", 1, "concurrent-test", _now())
    def consume(_):
        try:
            store.complete_pairing(active[0], binding, installation, user.user_id)
            return "success"
        except ValueError as error:
            return str(error)
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(consume, range(8)))
    assert results.count("success") == 1
    assert results.count("pairing_code_used") == 7


def test_bootstrap_additive_idempotent_and_legacy_rows_fail_closed(pairing_client):
    store = store_for(pairing_client)
    if not isinstance(store, PostgresVaultSupplierStore):
        return
    _, descriptor = issue(pairing_client)
    original = store.get_pairing_code(descriptor["pairing_secret"])
    # Simulate the exact pre-upgrade table in this disposable schema only.
    with store._connect() as connection:
        connection.execute("ALTER TABLE vault_supplier_pairing_codes DROP COLUMN management_origin, DROP COLUMN server_key_id, DROP COLUMN server_public_key_spki_der_base64")
    store.initialize()
    store.initialize()
    restored = store.get_pairing_code(descriptor["pairing_secret"])
    assert restored.id == original.id and restored.code_hash == original.code_hash
    assert restored.consumed_at is None and restored.invalidated_at is None and restored.binding is None
    assert_error(pairing_client.post("/api/vault-supplier/pair", json=pair_request(descriptor)), "invalid_pairing_code")
