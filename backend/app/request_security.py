"""Request-boundary helpers for the public Personal Vault deployment path."""

from __future__ import annotations

import ipaddress
import os

from fastapi import Request


CANONICAL_CLIENT_IP_HEADER = "X-PV-Client-IP"


def trusted_proxy_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the optional, explicit proxy boundary without unsafe fallbacks.

    An absent value is safe for direct local development: forwarded headers are
    ignored. A supplied malformed value is a deployment error rather than a
    reason to trust a wider network.
    """
    raw_value = os.getenv("PV_TRUSTED_PROXY_CIDRS", "").strip()
    if not raw_value:
        return ()
    values = [item.strip() for item in raw_value.split(",") if item.strip()]
    if not values:
        raise RuntimeError("PV_TRUSTED_PROXY_CIDRS must contain comma-separated CIDRs")
    try:
        return tuple(
            ipaddress.ip_network(item, strict=False)
            for item in values
        )
    except ValueError as error:
        raise RuntimeError("PV_TRUSTED_PROXY_CIDRS must contain comma-separated CIDRs") from error


def _peer_ip(request: Request) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    peer = request.client.host if request.client else None
    if not peer:
        return None
    try:
        return ipaddress.ip_address(peer)
    except ValueError:
        return None


def client_ip(request: Request) -> str:
    """Return canonical metadata IP only when Caddy's peer boundary is trusted.

    Caddy overwrites ``X-PV-Client-IP`` after applying its own Cloudflare Tunnel
    trust rule. Direct peers and malformed/ambiguous values fall back to the
    immediate socket peer; legacy forwarding headers never participate here.
    """
    peer = _peer_ip(request)
    if peer is None:
        return "unknown"
    if any(peer in network for network in trusted_proxy_networks()):
        candidate = request.headers.get(CANONICAL_CLIENT_IP_HEADER)
        if candidate:
            try:
                return str(ipaddress.ip_address(candidate.strip()))
            except ValueError:
                pass
    return str(peer)
