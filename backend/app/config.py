import os
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
