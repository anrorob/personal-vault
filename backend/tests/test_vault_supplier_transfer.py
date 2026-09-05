from base64 import urlsafe_b64decode, urlsafe_b64encode
import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app.auth_store import MemoryAuthenticationStore
import app.incoming as incoming_module
from app.incoming import get_arrival_hall_file_source_context, get_incoming_path
from app.main import app
from app.vault_master import INCOMING_SOURCE, MemoryVaultMasterStore, enqueue_root, process_next_batch
from app.vault_supplier import get_vault_supplier_store
from app.vault_supplier_transfer import backfill_arrival_hall_source_context, get_transfer_store
from tests.conftest import pairing_secret


def _login(client: TestClient) -> None:
    assert client.post("/api/auth/login", json={"username": "owner", "password": "correct-horse-battery-staple"}).status_code == 200


def _authorized_headers(client: TestClient, authentication_store: MemoryAuthenticationStore) -> dict[str, str]:
    _login(client)
    user = authentication_store.get_account("owner")
    assert user is not None
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    code = pairing_secret(client.post("/api/vault-supplier/pairing-code").json())
    installation_id = uuid4()
    paired = client.post("/api/vault-supplier/pair", json={"pairing_secret": code, "installation_id": str(installation_id), "installation_public_key": urlsafe_b64encode(public).rstrip(b"=").decode("ascii"), "key_algorithm": "ECDSA_P256", "supplier_version": "test", "protocol_version": 1})
    assert paired.status_code == 200, paired.text
    challenge = client.post(f"/api/vault-supplier/installations/{installation_id}/challenge", json={"requested_user_id": str(user.user_id)}).json()
    raw = challenge["challenge"].encode("ascii") + b"=" * (-len(challenge["challenge"]) % 4)
    signature = urlsafe_b64encode(private.sign(urlsafe_b64decode(raw), ec.ECDSA(hashes.SHA256()))).rstrip(b"=").decode("ascii")
    authenticated = client.post(f"/api/vault-supplier/installations/{installation_id}/authenticate", json={"challenge_id": challenge["challenge_id"], "signature": signature})
    assert authenticated.status_code == 200, authenticated.text
    token = authenticated.json()["authorization_token"]
    assert isinstance(token, str)
    return {"Authorization": f"Bearer {token}", "X-PV-Supplier-Installation-ID": str(installation_id), "X-PV-Supplier-User-ID": str(user.user_id)}


def _configure_arrival_hall(tmp_path: Path) -> None:
    arrival = tmp_path / "Arrival Hall"
    arrival.mkdir()
    app.dependency_overrides[get_incoming_path] = lambda: arrival


def test_resumable_transfer_finalizes_once_and_hidden_part_is_not_scanned(client: TestClient, authentication_store: MemoryAuthenticationStore, tmp_path: Path, monkeypatch) -> None:
    signals: list[str] = []
    monkeypatch.setattr(
        incoming_module,
        "signal_arrival_hall_work_available",
        lambda: signals.append("arrival_hall"),
    )
    _configure_arrival_hall(tmp_path)
    headers = _authorized_headers(client, authentication_store)
    payload = b"supplier-resume-payload"
    digest = hashlib.sha256(payload).hexdigest()
    created = client.post("/api/vault-supplier/transfers", headers=headers, json={"protocol_version": 1, "filename": "video.mp4", "total_size": len(payload), "sha256": digest, "source_context": {"source_kind": "automatic_source", "source_id": "sample-series-rips", "source_label": "Sample Series", "relative_path": "Season 1\\video.mp4"}})
    assert created.status_code == 201, created.text
    transfer_id = created.json()["transfer_id"]
    first = payload[:7]
    uploaded = client.put(f"/api/vault-supplier/transfers/{transfer_id}/data", headers={**headers, "X-PV-Upload-Offset": "0", "Content-Length": str(len(first)), "Content-Type": "application/octet-stream"}, content=first)
    assert uploaded.status_code == 200, uploaded.text
    status_response = client.get(f"/api/vault-supplier/transfers/{transfer_id}", headers=headers)
    assert status_response.json()["bytes_received"] == len(first)
    bad_offset = client.put(f"/api/vault-supplier/transfers/{transfer_id}/data", headers={**headers, "X-PV-Upload-Offset": "0", "Content-Length": str(len(payload) - len(first))}, content=payload[len(first):])
    assert bad_offset.status_code == 409
    resumed = client.put(f"/api/vault-supplier/transfers/{transfer_id}/data", headers={**headers, "X-PV-Upload-Offset": str(len(first)), "Content-Length": str(len(payload) - len(first))}, content=payload[len(first):])
    assert resumed.status_code == 200, resumed.text
    finalized = client.post(f"/api/vault-supplier/transfers/{transfer_id}/finalize", headers=headers)
    assert finalized.status_code == 200, finalized.text
    body = finalized.json()
    assert body["state"] == "finalized" and body["arrival_hall_receipt_id"] == transfer_id
    arrival = tmp_path / "Arrival Hall"
    assert (arrival / body["arrival_hall_filename"]).read_bytes() == payload
    assert get_arrival_hall_file_source_context(arrival, arrival / body["arrival_hall_filename"]) == {
        "transfer_id": transfer_id,
        "source_kind": "automatic_source",
        "source_id": "sample-series-rips",
        "source_label": "Sample Series",
        "relative_path": "Season 1/video.mp4",
    }
    user = authentication_store.get_account("owner")
    assert user is not None
    incoming_module.record_arrival_hall_file_owner(arrival, arrival / body["arrival_hall_filename"], SimpleNamespace(user_id=user.user_id))
    transfer_store = client.app.dependency_overrides[get_transfer_store]()
    assert backfill_arrival_hall_source_context(arrival, transfer_store) == 1
    assert backfill_arrival_hall_source_context(arrival, transfer_store) == 1
    assert get_arrival_hall_file_source_context(arrival, arrival / body["arrival_hall_filename"])["source_label"] == "Sample Series"
    vault_store = MemoryVaultMasterStore(default_asset_owner="owner")
    batch_id = enqueue_root(vault_store, arrival, INCOMING_SOURCE)
    assert process_next_batch(
        vault_store,
        owner_lookup=lambda _: user.user_id,
        source_context_lookup=lambda path: get_arrival_hall_file_source_context(arrival, path),
    ) == batch_id
    assert vault_store.list_items()[0].metadata["source_context"]["source_id"] == "sample-series-rips"
    assert not list((arrival / ".pv-vault-supplier-transfers").glob("*.part"))
    assert signals == ["arrival_hall"]
    assert client.post("/api/vault-supplier/intake/check-hashes", headers=headers, json={"protocol_version": 1, "sha256": [digest]}).json()["hashes"] == [{"sha256": digest, "duplicate": True}]
    duplicate = client.post("/api/vault-supplier/transfers", headers=headers, json={"protocol_version": 1, "filename": "renamed.mp4", "total_size": len(payload), "sha256": digest})
    assert duplicate.status_code == 409 and duplicate.json()["detail"]["code"] == "duplicate_content"


def test_transfer_auth_revocation_checksum_failure_and_abort_are_fail_closed(client: TestClient, authentication_store: MemoryAuthenticationStore, tmp_path: Path, monkeypatch) -> None:
    signals: list[str] = []
    monkeypatch.setattr(
        incoming_module,
        "signal_arrival_hall_work_available",
        lambda: signals.append("arrival_hall"),
    )
    _configure_arrival_hall(tmp_path)
    headers = _authorized_headers(client, authentication_store)
    assert client.get("/api/vault-supplier/intake/state").status_code == 401
    payload = b"bad-checksum"
    created = client.post("/api/vault-supplier/transfers", headers=headers, json={"protocol_version": 1, "filename": "safe.bin", "total_size": len(payload), "sha256": "0" * 64})
    transfer_id = created.json()["transfer_id"]
    assert client.put(f"/api/vault-supplier/transfers/{transfer_id}/data", headers={**headers, "X-PV-Upload-Offset": "0", "Content-Length": str(len(payload))}, content=payload).status_code == 200
    failed = client.post(f"/api/vault-supplier/transfers/{transfer_id}/finalize", headers=headers)
    assert failed.status_code == 422 and failed.json()["detail"]["code"] == "checksum_mismatch"
    assert not list((tmp_path / "Arrival Hall").glob("*.bin"))
    aborted = client.delete(f"/api/vault-supplier/transfers/{transfer_id}", headers=headers)
    assert aborted.status_code == 200 and aborted.json()["state"] == "aborted"
    assert signals == []
    installation_id = headers["X-PV-Supplier-Installation-ID"]
    assert client.delete(f"/api/vault-supplier/installations/{installation_id}").status_code == 200
    assert client.get("/api/vault-supplier/intake/state", headers=headers).status_code == 403


def test_receiver_validation_errors_and_source_context_is_safely_normalized(client: TestClient, authentication_store: MemoryAuthenticationStore, tmp_path: Path) -> None:
    _configure_arrival_hall(tmp_path)
    headers = _authorized_headers(client, authentication_store)
    digest = hashlib.sha256(b"receiver-validation").hexdigest()
    invalid_filename = client.post("/api/vault-supplier/transfers", headers=headers, json={"protocol_version": 1, "filename": "../unsafe.bin", "total_size": 19, "sha256": digest})
    assert invalid_filename.status_code == 400
    assert invalid_filename.json() == {"detail": {"code": "invalid_filename", "message": "Filename is invalid."}}
    malformed = client.post("/api/vault-supplier/transfers", headers=headers, json={"protocol_version": 1, "filename": "safe.bin", "total_size": 19})
    assert malformed.status_code == 422
    assert malformed.json() == {"detail": {"code": "invalid_request", "message": "Vault Supplier receiver request is malformed."}}
    source_context = {"source_kind": "automatic_source", "source_label": "Sample Series", "relative_path": "Season 1\\Disc 1_t01.mkv"}
    created = client.post("/api/vault-supplier/transfers", headers=headers, json={"protocol_version": 1, "filename": "safe.bin", "total_size": 19, "sha256": digest, "source_context": source_context})
    assert created.status_code == 201, created.text
    transfer = client.app.dependency_overrides[get_transfer_store]().get(UUID(created.json()["transfer_id"]))
    assert transfer is not None and transfer.source_context == {
        "source_kind": "automatic_source", "source_label": "Sample Series", "relative_path": "Season 1/Disc 1_t01.mkv"
    }
    assert client.delete(f"/api/vault-supplier/transfers/{created.json()['transfer_id']}", headers=headers).status_code == 200
    for unsafe in ("../OtherShow/file.mkv", "C:\\OtherShow\\file.mkv", "\\\\server\\share\\file.mkv"):
        rejected = client.post("/api/vault-supplier/transfers", headers=headers, json={"protocol_version": 1, "filename": "different.bin", "total_size": 19, "sha256": hashlib.sha256(unsafe.encode()).hexdigest(), "source_context": {"relative_path": unsafe}})
        assert rejected.status_code == 400
        assert rejected.json()["detail"]["code"] == "invalid_source_context"


def test_large_lan_transfer_over_100mb_streams_and_finalizes(client: TestClient, authentication_store: MemoryAuthenticationStore, tmp_path: Path) -> None:
    _configure_arrival_hall(tmp_path)
    headers = _authorized_headers(client, authentication_store)
    block = b"pv-lan-transfer" * (1024 * 1024)
    payload = (block * 7) + b"x"
    assert len(payload) > 100 * 1024 * 1024
    digest = hashlib.sha256(payload).hexdigest()
    created = client.post("/api/vault-supplier/transfers", headers=headers, json={"protocol_version": 1, "filename": "large.bin", "total_size": len(payload), "sha256": digest})
    assert created.status_code == 201, created.text
    transfer_id = created.json()["transfer_id"]
    offset = 0
    chunk_size = 16 * 1024 * 1024
    while offset < len(payload):
        chunk = payload[offset:offset + chunk_size]
        response = client.put(f"/api/vault-supplier/transfers/{transfer_id}/data", headers={**headers, "X-PV-Upload-Offset": str(offset), "Content-Length": str(len(chunk))}, content=chunk)
        assert response.status_code == 200, response.text
        offset += len(chunk)
    assert client.post(f"/api/vault-supplier/transfers/{transfer_id}/finalize", headers=headers).json()["state"] == "finalized"
