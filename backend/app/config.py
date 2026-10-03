import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from psycopg.conninfo import make_conninfo


def require_environment_variable(name: str) -> str:
    value = os.getenv(name)

    if not value:
        raise RuntimeError(f"{name} is not configured")

    return value


def get_admin_username() -> str:
    return require_environment_variable("PV_ADMIN_USERNAME")


def get_admin_password_hash() -> str:
    return require_environment_variable("PV_ADMIN_PASSWORD_HASH")


def get_session_secret() -> str:
    return require_environment_variable("PV_SESSION_SECRET")


def get_webauthn_rp_id() -> str:
    rp_id = require_environment_variable("PV_WEBAUTHN_RP_ID")
    if "://" in rp_id or "/" in rp_id or "@" in rp_id or not rp_id.strip():
        raise RuntimeError("PV_WEBAUTHN_RP_ID must be a hostname")
    return rp_id.casefold()


def get_webauthn_origin() -> str:
    origin = require_environment_variable("PV_WEBAUTHN_ORIGIN")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("PV_WEBAUTHN_ORIGIN must be an HTTPS origin without a path")
    if get_webauthn_rp_id() != parsed.hostname.casefold():
        raise RuntimeError("PV_WEBAUTHN_RP_ID must match the PV_WEBAUTHN_ORIGIN hostname")
    return f"https://{parsed.netloc}".rstrip("/")


_HOSTNAME_RE = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z",
    re.IGNORECASE,
)


def get_allowed_hosts() -> frozenset[str]:
    """Return the explicit browser/API Host allowlist.

    WebAuthn retains one canonical origin. A deployment may additionally allow
    exact non-browser API hostnames through PV_ALLOWED_HOSTS. Wildcards, ports
    and URLs are rejected.
    """
    configured = os.getenv("PV_ALLOWED_HOSTS")
    if configured is None:
        hostname = urlsplit(get_webauthn_origin()).hostname
        assert hostname is not None
        return frozenset({hostname.casefold()})

    hosts = frozenset(host.strip().casefold() for host in configured.split(",") if host.strip())
    if not hosts or any(not _HOSTNAME_RE.fullmatch(host) for host in hosts):
        raise RuntimeError("PV_ALLOWED_HOSTS must be a comma-separated list of exact hostnames")
    return hosts


def request_host_name(value: str) -> str | None:
    """Parse a Host header to its hostname without admitting malformed hosts."""
    try:
        parsed = urlsplit(f"//{value}")
        _ = parsed.port
    except ValueError:
        return None
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        return None
    return parsed.hostname.casefold()


def supplier_lan_host_allowed(value: str) -> bool:
    """Require an exact, explicitly configured Supplier receiver hostname.

    This allowlist is separate from browser/API hosts and endpoint discovery.
    Missing or malformed configuration denies every receiver request.
    """
    configured = os.getenv("PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS", "")
    hosts = [host.strip().casefold() for host in configured.split(",")]
    if any(not _HOSTNAME_RE.fullmatch(host) for host in hosts):
        return False
    # urlsplit alone strips some control characters and admits an empty port.
    if not value.isascii() or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return False
    hostname, separator, port = value.partition(":")
    if not _HOSTNAME_RE.fullmatch(hostname):
        return False
    if separator and (not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535):
        return False
    return hostname.casefold() in hosts


def get_jellyfin_url() -> str:
    return require_environment_variable("JELLYFIN_URL")


def get_jellyfin_api_key() -> str:
    return require_environment_variable("JELLYFIN_API_KEY")


def get_metadata_storage_root() -> Path:
    return Path(
        os.getenv(
            "PV_METADATA_STORAGE_PATH",
            "/var/lib/personal-vault/metadata",
        )
    )


def get_upload_max_bytes() -> int:
    raw_value = os.getenv(
        "PV_UPLOAD_MAX_BYTES",
        str(100 * 1024 * 1024 * 1024),
    )

    try:
        value = int(raw_value)
    except ValueError as error:
        raise RuntimeError(
            "PV_UPLOAD_MAX_BYTES must be an integer"
        ) from error

    if value <= 0:
        raise RuntimeError("PV_UPLOAD_MAX_BYTES must be positive")

    return value


def get_database_conninfo() -> str:
    return make_conninfo(
        host=os.getenv("POSTGRES_HOST", "pv-database"),
        port=os.getenv("POSTGRES_PORT", "5432"),
        dbname=os.getenv("POSTGRES_DB", "pv_vault"),
        user=os.getenv("POSTGRES_USER", "pv_vault"),
        password=require_environment_variable("POSTGRES_PASSWORD"),
    )
