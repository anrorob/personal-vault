from fastapi import Request
import pytest

from app.request_security import client_ip, trusted_proxy_networks
from app.auth import _passkey_rate_limit_key
from app.config import get_webauthn_origin


def request_from(peer: str, headers: dict[str, str] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [
                (name.lower().encode(), value.encode())
                for name, value in (headers or {}).items()
            ],
            "client": (peer, 43210),
        }
    )


def test_trusted_proxy_uses_only_caddy_normalized_canonical_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PV_TRUSTED_PROXY_CIDRS", "172.22.0.3/32")

    assert client_ip(request_from("172.22.0.3", {"X-PV-Client-IP": "2001:db8::7"})) == "2001:db8::7"
    assert _passkey_rate_limit_key(
        request_from("172.22.0.3", {"X-PV-Client-IP": "203.0.113.7"})
    ) == "passkey:203.0.113.7"


def test_untrusted_peer_cannot_spoof_forwarding_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PV_TRUSTED_PROXY_CIDRS", "172.22.0.3/32")
    request = request_from(
        "198.51.100.8",
        {
            "CF-Connecting-IP": "203.0.113.200",
            "X-Forwarded-For": "203.0.113.201, 10.0.0.1",
            "X-PV-Client-IP": "203.0.113.202",
        },
    )

    assert client_ip(request) == "198.51.100.8"


@pytest.mark.parametrize("header", ["not-an-ip", "203.0.113.7, 10.0.0.1", ""])
def test_malformed_or_ambiguous_canonical_client_ip_fails_to_peer(
    monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    monkeypatch.setenv("PV_TRUSTED_PROXY_CIDRS", "172.22.0.3/32")

    assert client_ip(request_from("172.22.0.3", {"X-PV-Client-IP": header})) == "172.22.0.3"


def test_no_proxy_configuration_never_trusts_forwarded_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PV_TRUSTED_PROXY_CIDRS", raising=False)

    assert client_ip(request_from("192.0.2.9", {"X-PV-Client-IP": "203.0.113.9"})) == "192.0.2.9"
    assert client_ip(request_from("2001:db8::99", {"X-Forwarded-For": "203.0.113.9"})) == "2001:db8::99"


def test_malformed_trusted_proxy_configuration_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PV_TRUSTED_PROXY_CIDRS", "not-a-cidr")

    with pytest.raises(RuntimeError, match="PV_TRUSTED_PROXY_CIDRS"):
        trusted_proxy_networks()


def test_webauthn_origin_must_match_its_configured_rp_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PV_WEBAUTHN_RP_ID", "vault.pv-hq.com")
    monkeypatch.setenv("PV_WEBAUTHN_ORIGIN", "https://other.pv-hq.com")

    with pytest.raises(RuntimeError, match="must match"):
        get_webauthn_origin()
