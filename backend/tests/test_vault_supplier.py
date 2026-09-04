from tests.conftest import pairing_secret
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import timedelta
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.vault_supplier import MemoryVaultSupplierStore, _now, get_vault_supplier_store


def _login(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"username": "owner", "password": "correct-horse-battery-staple"}).status_code == 200


def _key() -> tuple[ec.EllipticCurvePrivateKey, str]:
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return private, urlsafe_b64encode(public).rstrip(b"=").decode("ascii")


def _pair(client: TestClient, code: str, installation_id: str | None = None) -> tuple[str, ec.EllipticCurvePrivateKey]:
    private, public = _key(); installation_id = installation_id or str(uuid4())
    response = client.post("/api/vault-supplier/pair", json={"pairing_secret": code, "installation_id": installation_id, "installation_public_key": public, "key_algorithm": "ECDSA_P256", "supplier_version": "1.0.0-test", "protocol_version": 1})
    assert response.status_code == 200, response.text
    return installation_id, private


def test_pairing_code_is_authenticated_hash_only_replaced_and_single_use(client: TestClient) -> None:
    assert client.post("/api/vault-supplier/pairing-code").status_code == 401
    _login(client)
    first = client.post("/api/vault-supplier/pairing-code").json()
    second = client.post("/api/vault-supplier/pairing-code").json()
    store = client.app.dependency_overrides[get_vault_supplier_store]()
    assert isinstance(store, MemoryVaultSupplierStore)
    assert all(pairing_secret(first) not in code.code_hash and pairing_secret(second) not in code.code_hash for code in store.codes.values())
    private, public = _key()
    request = {"pairing_secret": pairing_secret(first), "installation_id": str(uuid4()), "installation_public_key": public, "key_algorithm": "ECDSA_P256", "supplier_version": "1.0", "protocol_version": 1}
    assert client.post("/api/vault-supplier/pair", json=request).status_code == 400
    request["pairing_secret"] = pairing_secret(second)
    assert client.post("/api/vault-supplier/pair", json=request).status_code == 200
    assert client.post("/api/vault-supplier/pair", json=request).status_code == 400


def test_pairing_rejects_bad_protocol_and_identity(client: TestClient) -> None:
    _login(client); code = pairing_secret(client.post("/api/vault-supplier/pairing-code").json())
    bad_protocol = {"pairing_secret": code, "installation_id": str(uuid4()), "installation_public_key": "x" * 32, "key_algorithm": "ECDSA_P256", "supplier_version": "1.0", "protocol_version": 2}
    assert client.post("/api/vault-supplier/pair", json=bad_protocol).json()["detail"]["code"] == "protocol_mismatch"
    bad_protocol["protocol_version"] = 1
    assert client.post("/api/vault-supplier/pair", json=bad_protocol).json()["detail"]["code"] == "invalid_installation_key"


def test_challenge_signature_replay_and_revocation(client: TestClient) -> None:
    _login(client); code = pairing_secret(client.post("/api/vault-supplier/pairing-code").json())
    installation_id, private = _pair(client, code)
    challenge = client.post(f"/api/vault-supplier/installations/{installation_id}/challenge", json={}).json()
    raw = challenge["challenge"].encode("ascii") + b"=" * (-len(challenge["challenge"]) % 4)
    signature = urlsafe_b64encode(private.sign(urlsafe_b64decode(raw), ec.ECDSA(hashes.SHA256()))).rstrip(b"=").decode("ascii")
    verified = client.post(f"/api/vault-supplier/installations/{installation_id}/authenticate", json={"challenge_id": challenge["challenge_id"], "signature": signature})
    assert verified.status_code == 200
    assert client.post(f"/api/vault-supplier/installations/{installation_id}/authenticate", json={"challenge_id": challenge["challenge_id"], "signature": signature}).json()["detail"]["code"] == "challenge_used"
    assert client.delete(f"/api/vault-supplier/installations/{installation_id}").status_code == 200
    assert client.post(f"/api/vault-supplier/installations/{installation_id}/challenge", json={}).json()["detail"]["code"] == "installation_revoked"


def test_expired_challenge_and_wrong_key_fail_closed(client: TestClient) -> None:
    _login(client); code = pairing_secret(client.post("/api/vault-supplier/pairing-code").json())
    installation_id, _ = _pair(client, code)
    challenge = client.post(f"/api/vault-supplier/installations/{installation_id}/challenge", json={}).json()
    _, wrong_public = _key()
    del wrong_public
    wrong_private = ec.generate_private_key(ec.SECP256R1())
    raw = challenge["challenge"].encode("ascii") + b"=" * (-len(challenge["challenge"]) % 4)
    signature = urlsafe_b64encode(wrong_private.sign(urlsafe_b64decode(raw), ec.ECDSA(hashes.SHA256()))).rstrip(b"=").decode("ascii")
    assert client.post(f"/api/vault-supplier/installations/{installation_id}/authenticate", json={"challenge_id": challenge["challenge_id"], "signature": signature}).json()["detail"]["code"] == "invalid_signature"
    expired = client.post(f"/api/vault-supplier/installations/{installation_id}/challenge", json={}).json()
    store = client.app.dependency_overrides[get_vault_supplier_store]()
    record = store.challenges[UUID(expired["challenge_id"])]
    store.challenges[record.id] = record.__class__(record.id, record.installation_id, record.vault_id, record.requested_user_id, record.challenge, _now() - timedelta(seconds=1))
    assert client.post(f"/api/vault-supplier/installations/{installation_id}/authenticate", json={"challenge_id": str(record.id), "signature": signature}).json()["detail"]["code"] == "challenge_expired"
