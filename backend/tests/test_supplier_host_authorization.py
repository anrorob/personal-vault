"""Synthetic receiver-host tests; no durable transfer or catalogue state."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import supplier_lan_host_allowed
from app.vault_supplier_transfer import require_lan_receiver_host


@pytest.mark.parametrize("host", ["vault-server.local", "VAULT-SERVER.LOCAL", "vault-server.local:8443", "backup.local:9443"])
def test_explicit_hosts_accepted(monkeypatch, host):
    monkeypatch.setenv("PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS", " vault-server.local, backup.local ")
    assert supplier_lan_host_allowed(host)


@pytest.mark.parametrize("host", [
    "other.local", "evilvault-server.local", "vault-server.local.evil.test",
    "vault-server.local.", "vault-server.local:", "vault-server.local:0",
    "vault-server.local:65536", "vault-server.local:abc", "vault-server.local:8443:80",
    "https://vault-server.local", "user@vault-server.local", "vault-server.local/path",
    "vault-server.local?x", "vault-server.local#x", " vault-server.local",
    "vault-server.local ", "vault-server.local\n", "vault-\tserver.local",
    "vault-server.local,other.local", "", "[::1]", "vаult-server.local",
])
def test_untrusted_or_malformed_hosts_denied(monkeypatch, host):
    monkeypatch.setenv("PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS", "vault-server.local")
    assert not supplier_lan_host_allowed(host)


@pytest.mark.parametrize("configured", [None, "", " ", "*", "*.local", "vault-server.local,", ",vault-server.local", "vault-server.local,,backup.local", "vault-server.local,*.local", "https://vault-server.local", "vault-server.local:8443", "vault-server.local/path"])
def test_absent_or_invalid_allowlist_denies_all(monkeypatch, configured):
    if configured is None:
        monkeypatch.delenv("PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS", raising=False)
    else:
        monkeypatch.setenv("PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS", configured)
    assert not supplier_lan_host_allowed("vault-server.local")


@pytest.mark.parametrize("headers", [[], [(b"host", b"vault-server.local"), (b"host", b"vault-server.local")], [(b"host", b"other.local")]])
def test_missing_duplicate_or_disallowed_header_returns_404(monkeypatch, headers):
    monkeypatch.setenv("PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS", "vault-server.local")
    request = Request({"type": "http", "headers": headers})
    with pytest.raises(HTTPException) as error:
        require_lan_receiver_host(request)
    assert error.value.status_code == 404
    assert error.value.detail["code"] == "receiver_unavailable"


def test_allowed_host_still_requires_supplier_credentials(client):
    response = client.get("/api/vault-supplier/intake/state", headers={"Host": "vault-server.local"})
    assert response.status_code == 401


def test_discovery_and_browser_hosts_do_not_authorize_receiver(monkeypatch):
    monkeypatch.delenv("PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("PV_ALLOWED_HOSTS", "vault-server.local")
    monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", "https://vault-server.local")
    assert not supplier_lan_host_allowed("vault-server.local")
