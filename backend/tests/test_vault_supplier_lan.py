from tests.conftest import pairing_secret
from base64 import b64decode, urlsafe_b64encode
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.vault_supplier_lan import LanServerIdentity, canonical_payload, verify_signature


def _login(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"username": "owner", "password": "correct-horse-battery-staple"}).status_code == 200


def _pair(client: TestClient) -> dict[str, object]:
    _login(client)
    code = pairing_secret(client.post("/api/vault-supplier/pairing-code").json())
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    response = client.post("/api/vault-supplier/pair", json={
        "pairing_secret": code,
        "installation_id": "11111111-2222-3333-4444-555555555555",
        "installation_public_key": urlsafe_b64encode(public).rstrip(b"=").decode("ascii"),
        "key_algorithm": "ECDSA_P256",
        "supplier_version": "test",
        "protocol_version": 1,
    })
    assert response.status_code == 200, response.text
    return response.json()


def test_pairing_returns_pinnable_server_identity(client: TestClient) -> None:
    paired = _pair(client)
    identity = paired["server_identity"]
    assert identity["key_algorithm"] == "ECDSA_P256_SHA256"
    assert len(identity["key_id_sha256"]) == 64
    assert identity["key_id_sha256"] == identity["key_id_sha256"].lower()
    assert b64decode(identity["public_key_spki_der_base64"], validate=True)


def test_canonical_payload_matches_the_published_byte_vector() -> None:
    assert canonical_payload(
        vault_id=UUID("11111111-2222-3333-4444-555555555555"),
        nonce="A" * 43,
        key_id="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        port=8000,
        receiver_available=False,
        resumable_upload_supported=False,
    ) == (
        b"PV-VS-LAN-1\n"
        b"vault_id=11111111-2222-3333-4444-555555555555\n"
        b"nonce=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
        b"server_key_id=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef\n"
        b"port=8000\n"
        b"receiver_available=0\n"
        b"resumable_upload_supported=0\n"
    )


def test_server_identity_is_stable_for_a_persistent_key(monkeypatch, tmp_path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "persistent-server-key.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setenv("PV_VAULT_SUPPLIER_SERVER_IDENTITY_KEY_PATH", str(key_path))
    assert LanServerIdentity.load().pairing_response() == LanServerIdentity.load().pairing_response()


def test_identity_and_verify_match_the_exact_signed_contract(client: TestClient) -> None:
    paired = _pair(client)
    identity = client.get("/api/vault-supplier/lan/identity")
    assert identity.status_code == 200
    assert identity.json() == {
        "protocol_version": 1,
        "vault_id": paired["vault_id"],
        "server_key_id": paired["server_identity"]["key_id_sha256"],
        "verify_path": "/api/vault-supplier/lan/verify",
        "capabilities": {"receiver_available": True, "resumable_upload_supported": True},
    }
    nonce = "A" * 43
    response = client.post("/api/vault-supplier/lan/verify", json={"protocol_version": 1, "nonce": nonce})
    assert response.status_code == 200, response.text
    verified = response.json()
    payload = b64decode(verified["signed_payload_base64"], validate=True)
    expected = canonical_payload(
        vault_id=UUID(paired["vault_id"]), nonce=nonce,
        key_id=paired["server_identity"]["key_id_sha256"], port=9443,
        receiver_available=False, resumable_upload_supported=False,
    )
    assert payload == expected
    assert verify_signature(b64decode(paired["server_identity"]["public_key_spki_der_base64"], validate=True), payload, verified["signature_der_base64"])
    wrong_public = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    assert not verify_signature(wrong_public, payload, verified["signature_der_base64"])
    assert not verify_signature(b64decode(paired["server_identity"]["public_key_spki_der_base64"], validate=True), payload + b"x", verified["signature_der_base64"])
    assert verified["nonce"] == nonce and verified["vault_id"] == paired["vault_id"]


def test_verify_rejects_invalid_nonce_and_protocol(client: TestClient) -> None:
    assert client.post("/api/vault-supplier/lan/verify", json={"protocol_version": 1, "nonce": "A" * 42}).json() == {
        "detail": {"code": "invalid_nonce", "message": "Nonce must be an unpadded Base64URL encoding of exactly 32 bytes."}
    }
    response = client.post("/api/vault-supplier/lan/verify", json={"protocol_version": 2, "nonce": "A" * 43})
    assert response.status_code == 400
    assert response.json() == {"detail": {"code": "protocol_mismatch", "message": "Unsupported Vault Supplier LAN protocol version."}}
