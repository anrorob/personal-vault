"""Location-only pairing hints from the existing TLS listener configuration."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
import pytest

import app.main as main_module
from app.vault_supplier_lan import LanEndpointHintConfigurationError, lan_endpoint_hint
from tests.test_pairing_credential import issue, pair_request, pairing_client, store_for


def configure_listener(monkeypatch, tmp_path, names=("another-vault.local",), port="9444"):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ignored-cn.local")])
    builder = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(datetime.now(timezone.utc) - timedelta(days=1))
               .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1)))
    if names is not None:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(value) for value in names]), critical=False)
    path = tmp_path / "listener-certificate.pem"
    path.write_bytes(builder.sign(key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))
    monkeypatch.setenv("PV_VAULT_SUPPLIER_LAN_CERTIFICATE_PATH", str(path))
    if port is None:
        monkeypatch.delenv("PV_VAULT_SUPPLIER_LAN_PORT", raising=False)
    else:
        monkeypatch.setenv("PV_VAULT_SUPPLIER_LAN_PORT", port)
    return path


@pytest.mark.parametrize("management,host,port", [
    ("https://another-vault.example.net", "another-vault.local", "9444"),
    ("https://vault.example.net", "vault-lan.local", "9443"),
])
def test_pair_response_preserves_identity_and_separates_locations(pairing_client, monkeypatch, tmp_path, management, host, port):
    client = pairing_client
    configure_listener(monkeypatch, tmp_path, (host,), port)
    monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", management)
    monkeypatch.setenv("PV_WEBAUTHN_RP_ID", management.removeprefix("https://"))
    client.headers["Origin"] = management
    client.headers["Host"] = management.removeprefix("https://")
    _, descriptor = issue(client)
    store = store_for(client)
    record = store.get_pairing_code(descriptor["pairing_secret"])
    body = pair_request(descriptor)
    response = client.post("/api/vault-supplier/pair", json=body)
    assert response.status_code == 200, response.text
    paired = response.json()
    assert set(paired) == {"protocol_version", "vault_id", "vault_display_name", "user_id", "user_display_name", "installation_id", "server_identity", "lan_connection_metadata", "lan_endpoint_hint"}
    assert paired["lan_endpoint_hint"] == f"https://{host}:{port}"
    assert descriptor["origin"] == management != paired["lan_endpoint_hint"]
    assert "lan_endpoint_hint" not in descriptor
    assert paired["lan_connection_metadata"] == {"available": False, "mode": "unavailable"}
    assert paired["protocol_version"] == descriptor["v"] == 1
    assert paired["vault_id"] == descriptor["vault_id"]
    assert paired["user_id"] == str(record.user_id)
    assert paired["installation_id"] == body["installation_id"]
    assert paired["server_identity"] == {"key_algorithm": "ECDSA_P256_SHA256", "key_id_sha256": descriptor["server_key_id"], "public_key_spki_der_base64": descriptor["server_public_key_spki_der_base64"]}
    assert descriptor["pairing_secret"] not in response.text
    assert not any("verified" in key for key in paired)
    assert client.post("/api/vault-supplier/pair", json=body).json()["detail"]["code"] == "pairing_code_used"


def test_unconfigured_listener_returns_null_without_fallback(pairing_client, monkeypatch):
    monkeypatch.delenv("PV_VAULT_SUPPLIER_LAN_CERTIFICATE_PATH", raising=False)
    _, descriptor = issue(pairing_client)
    response = pairing_client.post("/api/vault-supplier/pair", json=pair_request(descriptor))
    assert response.status_code == 200
    assert response.json()["lan_endpoint_hint"] is None


def test_runtime_invalid_configuration_does_not_consume_secret(pairing_client, monkeypatch, tmp_path):
    path = configure_listener(monkeypatch, tmp_path)
    _, descriptor = issue(pairing_client)
    path.write_bytes(b"invalid certificate")
    request = pair_request(descriptor)
    response = pairing_client.post("/api/vault-supplier/pair", json=request)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "invalid_lan_endpoint_hint"
    assert str(path) not in response.text and descriptor["pairing_secret"] not in response.text
    assert store_for(pairing_client).get_pairing_code(descriptor["pairing_secret"]).consumed_at is None
    configure_listener(monkeypatch, tmp_path)
    assert pairing_client.post("/api/vault-supplier/pair", json=request).status_code == 200


def test_location_change_does_not_rebind_credential_identity(pairing_client, monkeypatch, tmp_path):
    configure_listener(monkeypatch, tmp_path)
    _, descriptor = issue(pairing_client)
    store = store_for(pairing_client)
    before = store.get_pairing_code(descriptor["pairing_secret"])
    configure_listener(monkeypatch, tmp_path, ("moved-vault.local",), "9443")
    response = pairing_client.post("/api/vault-supplier/pair", json=pair_request(descriptor))
    assert response.status_code == 200
    assert response.json()["lan_endpoint_hint"] == "https://moved-vault.local:9443"
    after = store.get_pairing_code(descriptor["pairing_secret"])
    assert after.binding == before.binding
    assert after.user_id == before.user_id and after.code_hash == before.code_hash


@pytest.mark.parametrize("names", [None, (), ("one.local", "two.local"), ("",),
    ("http://vault.local",), ("https://vault.local",), ("user:password@vault.local",),
    ("vault.local?secret=abc",), ("vault.local#fragment",), ("vault.local/path",),
    ("vault.local:9444",), ("*.local",), ("-bad.local",), ("bad-.local",),
    ("vault..local",), ("vault.local.",), (" vault.local",), ("vault.local\n",),
    ("vault\\local",), ("a" * 64 + ".local",)])
def test_invalid_or_ambiguous_hostname_is_not_a_hint(monkeypatch, tmp_path, names):
    configure_listener(monkeypatch, tmp_path, names)
    with pytest.raises(LanEndpointHintConfigurationError, match="Invalid Vault Supplier LAN endpoint hint configuration"):
        lan_endpoint_hint()


@pytest.mark.parametrize("port", [None, "", "0", "65536", "-1", "+443", " 443", "443\n", "4_443", "bad", "443/path", "443?token=x"])
def test_explicit_valid_port_required(monkeypatch, tmp_path, port):
    configure_listener(monkeypatch, tmp_path, port=port)
    with pytest.raises(LanEndpointHintConfigurationError):
        lan_endpoint_hint()


def test_normalizes_dns_case_and_keeps_explicit_https_port(monkeypatch, tmp_path):
    configure_listener(monkeypatch, tmp_path, ("VAULT-LAN.LOCAL",), "443")
    assert lan_endpoint_hint() == "https://vault-lan.local:443"


@pytest.mark.parametrize("path", ["", " ", "/nonexistent/pair014-certificate.pem"])
def test_missing_configured_certificate_is_an_error(monkeypatch, path):
    monkeypatch.setenv("PV_VAULT_SUPPLIER_LAN_CERTIFICATE_PATH", path)
    monkeypatch.setenv("PV_VAULT_SUPPLIER_LAN_PORT", "9443")
    with pytest.raises(LanEndpointHintConfigurationError):
        lan_endpoint_hint()


def test_bad_configuration_fails_startup_before_schema_or_workers(monkeypatch, tmp_path):
    configure_listener(monkeypatch, tmp_path, port="bad")
    calls = []
    monkeypatch.setattr(main_module, "bootstrap_application_schema", lambda: calls.append("schema"))
    with pytest.raises(LanEndpointHintConfigurationError):
        with TestClient(main_module.app):
            pytest.fail("malformed configuration started serving")
    assert calls == []


def test_lan_protocol_has_no_product_default_port(monkeypatch):
    from app.vault_supplier_lan import ServerIdentityUnavailable, lan_port
    monkeypatch.delenv("PV_VAULT_SUPPLIER_LAN_PORT", raising=False)
    with pytest.raises(ServerIdentityUnavailable):
        lan_port()
