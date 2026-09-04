"""Vault Supplier pairing and installation-bound authentication.

This module deliberately stops at device pairing/authentication.  It exposes no
content-transfer or storage-writing capability.
"""

from __future__ import annotations

from base64 import b64decode, b64encode, urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import re
import secrets
import threading
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import psycopg
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, StrictInt

from app.auth import AuthenticatedUser, get_authentication_store
from app.auth_store import AuthenticationStore
from app.config import get_database_conninfo, get_webauthn_origin
from app.vault_supplier_lan import (
    LAN_PROTOCOL_VERSION,
    VERIFY_PATH,
    LanServerIdentity,
    ServerIdentityUnavailable,
    canonical_payload,
    get_lan_server_identity,
    lan_port,
    validate_nonce,
)


PROTOCOL_VERSION = 1
PAIRING_CODE_LIFETIME = timedelta(minutes=10)
CHALLENGE_LIFETIME = timedelta(minutes=2)
AUTHORIZATION_LIFETIME = timedelta(minutes=15)
PAIRING_PREFIX = "PVPAIR1."

router = APIRouter(prefix="/api/vault-supplier", tags=["vault-supplier"])


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _b64encode(value: bytes) -> str:
    return urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        return urlsafe_b64decode(value.encode("ascii") + b"=" * (-len(value) % 4))
    except Exception as error:
        raise ValueError("invalid base64") from error


def _domain_error(code: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": code.replace("_", " ").capitalize() + "."})


@dataclass(frozen=True)
class PairingBinding:
    protocol_version: int
    vault_id: UUID
    management_origin: str
    server_key_id: str
    server_public_key_spki_der_base64: str


def get_pairing_origin() -> str:
    """Use the existing canonical application origin; never trust request headers."""
    try:
        raw = os.environ["PV_WEBAUTHN_ORIGIN"]
        if any(ord(c) <= 32 or ord(c) >= 127 for c in raw) or any(c in raw for c in "@?#\\"):
            raise ValueError("ambiguous origin")
        parsed = urlsplit(get_webauthn_origin())
        host = parsed.hostname or ""
        if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
            raise ValueError("invalid host")
        if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in host.split(".")):
            raise ValueError("invalid host")
        port = parsed.port
        if parsed.netloc.endswith(":") or (port is not None and not 1 <= port <= 65535):
            raise ValueError("invalid port")
        origin = "https://" + host.lower() + (f":{port}" if port not in (None, 443) else "")
    except (KeyError, RuntimeError, ValueError):
        raise _domain_error("invalid_pairing_origin") from None
    return origin


def pairing_binding(vault_id: UUID, identity: LanServerIdentity) -> PairingBinding:
    origin = get_pairing_origin()
    try:
        if not isinstance(vault_id, UUID) or vault_id.int == 0:
            raise ValueError("invalid vault identity")
        der = identity.public_key_spki_der
        key = serialization.load_der_public_key(der)
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
            raise ValueError("invalid server key")
        canonical_der = identity.private_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        if der != canonical_der or identity.key_id_sha256 != hashlib.sha256(der).hexdigest():
            raise ValueError("invalid server identity")
    except (ValueError, TypeError, AttributeError):
        raise _domain_error("invalid_pairing_descriptor") from None
    return PairingBinding(PROTOCOL_VERSION, vault_id, origin, identity.key_id_sha256, b64encode(der).decode("ascii"))


def get_pairing_server_identity() -> LanServerIdentity:
    try:
        return get_lan_server_identity()
    except ServerIdentityUnavailable:
        raise _domain_error("invalid_pairing_descriptor") from None


def encode_pairing_credential(binding: PairingBinding, secret: str) -> str:
    descriptor = {"v": binding.protocol_version, "vault_id": str(binding.vault_id),
                  "origin": binding.management_origin, "server_key_id": binding.server_key_id,
                  "server_public_key_spki_der_base64": binding.server_public_key_spki_der_base64,
                  "pairing_secret": secret}
    return PAIRING_PREFIX + _b64encode(json.dumps(descriptor, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))


def _lan_error(code: str, message: str, status_code: int) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _public_key(value: str, algorithm: str) -> bytes:
    if algorithm != "ECDSA_P256":
        raise ValueError("invalid_installation_identity")
    try:
        raw = value.encode("utf-8")
        key = serialization.load_pem_public_key(raw) if b"BEGIN" in raw else serialization.load_der_public_key(_b64decode(value))
    except Exception as error:
        raise ValueError("invalid_installation_identity") from error
    if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("invalid_installation_identity")
    return key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def _verify_signature(public_key: bytes, challenge: bytes, signature: str) -> bool:
    try:
        key = serialization.load_der_public_key(public_key)
        assert isinstance(key, ec.EllipticCurvePublicKey)
        key.verify(_b64decode(signature), challenge, ec.ECDSA(hashes.SHA256()))
        return True
    except (ValueError, InvalidSignature, TypeError):
        return False


@dataclass(frozen=True)
class PairingCode:
    id: UUID
    vault_id: UUID
    user_id: UUID
    code_hash: str
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    invalidated_at: datetime | None = None
    binding: PairingBinding | None = None
    replaced_previous: bool = False


def validate_pairing_record(record: PairingCode | None, binding: PairingBinding) -> PairingCode:
    if record is None:
        raise ValueError("invalid_pairing_code")
    if record.invalidated_at:
        raise ValueError("pairing_code_replaced")
    if record.consumed_at:
        raise ValueError("pairing_code_used")
    if record.expires_at <= _now():
        raise ValueError("pairing_code_expired")
    if record.binding is None:
        raise ValueError("invalid_pairing_code")  # Legacy rows never acquire new authority.
    if record.binding.protocol_version != binding.protocol_version:
        raise ValueError("protocol_mismatch")
    if record.vault_id != binding.vault_id or record.binding != binding:
        raise ValueError("pairing_identity_mismatch")
    return record


@dataclass(frozen=True)
class SupplierInstallation:
    installation_id: UUID
    vault_id: UUID
    public_key: bytes
    key_algorithm: str
    protocol_version: int
    supplier_version: str
    created_at: datetime
    last_seen_at: datetime | None = None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class SupplierChallenge:
    id: UUID
    installation_id: UUID
    vault_id: UUID
    requested_user_id: UUID | None
    challenge: bytes
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(frozen=True)
class InstallationSummary:
    installation_id: UUID
    supplier_version: str
    protocol_version: int
    created_at: datetime
    last_seen_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class SupplierAuthorization:
    """Short-lived bearer authority minted only after key challenge proof."""

    installation_id: UUID
    user_id: UUID
    token_hash: str
    expires_at: datetime


class VaultSupplierStore(Protocol):
    def initialize(self) -> None: ...
    def create_pairing_code(self, binding: PairingBinding, user_id: UUID, code: str) -> PairingCode: ...
    def get_pairing_code(self, code: str) -> PairingCode | None: ...
    def complete_pairing(self, code: str, binding: PairingBinding, installation: SupplierInstallation, user_id: UUID) -> SupplierInstallation: ...
    def local_vault(self) -> tuple[UUID, str]: ...
    def register_installation(self, installation: SupplierInstallation, user_id: UUID) -> SupplierInstallation: ...
    def list_installations(self, user_id: UUID) -> list[InstallationSummary]: ...
    def revoke_installation(self, installation_id: UUID, user_id: UUID) -> bool: ...
    def create_challenge(self, installation_id: UUID, requested_user_id: UUID | None) -> SupplierChallenge | None: ...
    def consume_challenge(self, challenge_id: UUID, installation_id: UUID) -> SupplierChallenge | None: ...
    def challenge_error(self, challenge_id: UUID, installation_id: UUID) -> str: ...
    def get_installation(self, installation_id: UUID) -> SupplierInstallation | None: ...
    def record_installation_authentication(self, installation_id: UUID, user_id: UUID | None) -> None: ...
    def create_authorization(self, installation_id: UUID, user_id: UUID | None) -> tuple[str, datetime]: ...
    def authorize_request(self, installation_id: UUID, user_id: UUID, token: str) -> SupplierInstallation | None: ...


class MemoryVaultSupplierStore:
    def __init__(self, vault_id: UUID | None = None) -> None:
        self.vault_id = vault_id or uuid4()
        self.codes: dict[UUID, PairingCode] = {}
        self.installations: dict[UUID, SupplierInstallation] = {}
        self.authorized_users: set[tuple[UUID, UUID]] = set()
        self.challenges: dict[UUID, SupplierChallenge] = {}
        self.authorizations: dict[str, SupplierAuthorization] = {}
        self._lock = threading.RLock()

    def initialize(self) -> None:
        return None

    def local_vault(self) -> tuple[UUID, str]:
        return self.vault_id, os.getenv("PV_VAULT_DISPLAY_NAME", "Personal Vault")

    def create_pairing_code(self, binding: PairingBinding, user_id: UUID, code: str) -> PairingCode:
        with self._lock:
            now = _now()
            replaced = False
            for item_id, item in self.codes.items():
                if item.vault_id == binding.vault_id and item.user_id == user_id and not item.consumed_at and not item.invalidated_at and item.expires_at > now:
                    self.codes[item_id] = replace(item, invalidated_at=now)
                    replaced = True
            record = PairingCode(uuid4(), binding.vault_id, user_id, _digest(code), now, now + PAIRING_CODE_LIFETIME, binding=binding, replaced_previous=replaced)
            self.codes[record.id] = record
            return record

    def get_pairing_code(self, code: str) -> PairingCode | None:
        with self._lock:
            return next((value for value in self.codes.values() if secrets.compare_digest(value.code_hash, _digest(code))), None)

    def complete_pairing(self, code: str, binding: PairingBinding, installation: SupplierInstallation, user_id: UUID) -> SupplierInstallation:
        with self._lock:
            record = validate_pairing_record(self.get_pairing_code(code), binding)
            if record.user_id != user_id or self.vault_id != binding.vault_id:
                raise ValueError("pairing_identity_mismatch")
            result = self.register_installation(installation, user_id)
            self.codes[record.id] = replace(record, consumed_at=_now())
            return result

    def register_installation(self, installation: SupplierInstallation, user_id: UUID) -> SupplierInstallation:
        with self._lock:
            existing = self.installations.get(installation.installation_id)
            if existing:
                if existing.vault_id != installation.vault_id or existing.public_key != installation.public_key or existing.key_algorithm != installation.key_algorithm:
                    raise ValueError("invalid_installation_identity")
                if existing.revoked_at:
                    raise ValueError("installation_revoked")
                installation = existing
            else:
                self.installations[installation.installation_id] = installation
            self.authorized_users.add((installation.installation_id, user_id))
            return installation

    def list_installations(self, user_id: UUID) -> list[InstallationSummary]:
        with self._lock:
            return [InstallationSummary(item.installation_id, item.supplier_version, item.protocol_version, item.created_at, item.last_seen_at, item.revoked_at)
                    for item in self.installations.values() if (item.installation_id, user_id) in self.authorized_users]

    def revoke_installation(self, installation_id: UUID, user_id: UUID) -> bool:
        with self._lock:
            item = self.installations.get(installation_id)
            if not item or (installation_id, user_id) not in self.authorized_users or item.revoked_at:
                return False
            self.installations[installation_id] = replace(item, revoked_at=_now())
            return True

    def create_challenge(self, installation_id: UUID, requested_user_id: UUID | None) -> SupplierChallenge | None:
        with self._lock:
            item = self.installations.get(installation_id)
            if not item or item.revoked_at or (requested_user_id and (installation_id, requested_user_id) not in self.authorized_users):
                return None
            result = SupplierChallenge(uuid4(), installation_id, item.vault_id, requested_user_id, secrets.token_bytes(32), _now() + CHALLENGE_LIFETIME)
            self.challenges[result.id] = result
            return result

    def consume_challenge(self, challenge_id: UUID, installation_id: UUID) -> SupplierChallenge | None:
        with self._lock:
            item = self.challenges.get(challenge_id)
            if not item or item.installation_id != installation_id or item.consumed_at or item.expires_at <= _now():
                return None
            self.challenges[item.id] = replace(item, consumed_at=_now())
            return item

    def challenge_error(self, challenge_id: UUID, installation_id: UUID) -> str:
        with self._lock:
            item = self.challenges.get(challenge_id)
            if item and item.installation_id == installation_id and item.expires_at <= _now(): return "challenge_expired"
            return "challenge_used"

    def get_installation(self, installation_id: UUID) -> SupplierInstallation | None:
        with self._lock:
            return self.installations.get(installation_id)

    def record_installation_authentication(self, installation_id: UUID, user_id: UUID | None) -> None:
        with self._lock:
            item = self.installations[installation_id]
            self.installations[installation_id] = replace(item, last_seen_at=_now())

    def create_authorization(self, installation_id: UUID, user_id: UUID | None) -> tuple[str, datetime]:
        if user_id is None:
            raise ValueError("user_not_allowed")
        token = secrets.token_urlsafe(32)
        expires_at = _now() + AUTHORIZATION_LIFETIME
        with self._lock:
            if (installation_id, user_id) not in self.authorized_users:
                raise ValueError("user_not_allowed")
            self.authorizations[_digest(token)] = SupplierAuthorization(
                installation_id, user_id, _digest(token), expires_at
            )
        return token, expires_at

    def authorize_request(self, installation_id: UUID, user_id: UUID, token: str) -> SupplierInstallation | None:
        with self._lock:
            authorization = self.authorizations.get(_digest(token))
            installation = self.installations.get(installation_id)
            if (
                authorization is None
                or authorization.installation_id != installation_id
                or authorization.user_id != user_id
                or authorization.expires_at <= _now()
                or installation is None
                or installation.revoked_at is not None
                or (installation_id, user_id) not in self.authorized_users
            ):
                return None
            self.installations[installation_id] = replace(installation, last_seen_at=_now())
            return self.installations[installation_id]


class PostgresVaultSupplierStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_supplier_pairing_codes (
                id UUID PRIMARY KEY, vault_id UUID NOT NULL REFERENCES vaults(vault_id), user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                code_hash CHAR(64) NOT NULL UNIQUE, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ, invalidated_at TIMESTAMPTZ,
                protocol_version INTEGER NOT NULL CHECK(protocol_version = 1))""")
            # Existing legacy rows remain intact and unbound: never backfill authority.
            cursor.execute("ALTER TABLE vault_supplier_pairing_codes ADD COLUMN IF NOT EXISTS management_origin TEXT")
            cursor.execute("ALTER TABLE vault_supplier_pairing_codes ADD COLUMN IF NOT EXISTS server_key_id TEXT")
            cursor.execute("ALTER TABLE vault_supplier_pairing_codes ADD COLUMN IF NOT EXISTS server_public_key_spki_der_base64 TEXT")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_supplier_installations (
                installation_id UUID PRIMARY KEY, vault_id UUID NOT NULL REFERENCES vaults(vault_id), public_key BYTEA NOT NULL,
                key_algorithm TEXT NOT NULL CHECK(key_algorithm = 'ECDSA_P256'), protocol_version INTEGER NOT NULL CHECK(protocol_version = 1),
                supplier_version TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen_at TIMESTAMPTZ, revoked_at TIMESTAMPTZ)""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_supplier_authorized_users (
                installation_id UUID NOT NULL REFERENCES vault_supplier_installations(installation_id), user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                authorized_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP, revoked_at TIMESTAMPTZ, last_used_at TIMESTAMPTZ,
                PRIMARY KEY(installation_id,user_id))""")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_supplier_challenges (
                id UUID PRIMARY KEY, installation_id UUID NOT NULL REFERENCES vault_supplier_installations(installation_id), vault_id UUID NOT NULL REFERENCES vaults(vault_id),
                requested_user_id UUID REFERENCES auth_accounts(user_id), challenge BYTEA NOT NULL, expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ)""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_supplier_pairing_codes_active_idx ON vault_supplier_pairing_codes(vault_id,user_id,expires_at) WHERE consumed_at IS NULL AND invalidated_at IS NULL")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_supplier_challenges_expiry_idx ON vault_supplier_challenges(expires_at)")
            cursor.execute("""CREATE TABLE IF NOT EXISTS vault_supplier_authorizations (
                token_hash CHAR(64) PRIMARY KEY,
                installation_id UUID NOT NULL REFERENCES vault_supplier_installations(installation_id),
                user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                expires_at TIMESTAMPTZ NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            cursor.execute("CREATE INDEX IF NOT EXISTS vault_supplier_authorizations_expiry_idx ON vault_supplier_authorizations(expires_at)")

    def local_vault(self) -> tuple[UUID, str]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT vault_id FROM vaults WHERE is_local=TRUE")
            rows = cursor.fetchall()
        if len(rows) != 1:
            raise RuntimeError("Vault Supplier requires exactly one local Vault identity")
        return UUID(str(rows[0][0])), os.getenv("PV_VAULT_DISPLAY_NAME", "Personal Vault")

    def create_pairing_code(self, binding: PairingBinding, user_id: UUID, code: str) -> PairingCode:
        with self._connect() as connection, connection.cursor() as cursor:
            # Serialize issuance even when there is no previous active row.
            cursor.execute("SELECT user_id FROM auth_accounts WHERE user_id=%s FOR UPDATE", (user_id,))
            now = _now()
            cursor.execute("UPDATE vault_supplier_pairing_codes SET invalidated_at=%s WHERE vault_id=%s AND user_id=%s AND consumed_at IS NULL AND invalidated_at IS NULL AND expires_at>%s", (now, binding.vault_id, user_id, now))
            replaced = cursor.rowcount > 0
            record = PairingCode(uuid4(), binding.vault_id, user_id, _digest(code), now, now + PAIRING_CODE_LIFETIME, binding=binding, replaced_previous=replaced)
            cursor.execute("""INSERT INTO vault_supplier_pairing_codes(id,vault_id,user_id,code_hash,created_at,expires_at,protocol_version,management_origin,server_key_id,server_public_key_spki_der_base64)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""", (record.id, record.vault_id, user_id, record.code_hash, now, record.expires_at, binding.protocol_version, binding.management_origin, binding.server_key_id, binding.server_public_key_spki_der_base64))
        return record

    @staticmethod
    def _pairing_record(row) -> PairingCode | None:
        if row is None:
            return None
        binding = PairingBinding(row[8], UUID(str(row[1])), row[9], row[10], row[11]) if all(value is not None for value in row[9:12]) else None
        return PairingCode(UUID(str(row[0])), UUID(str(row[1])), UUID(str(row[2])), str(row[3]), row[4], row[5], row[6], row[7], binding)

    def get_pairing_code(self, code: str) -> PairingCode | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,vault_id,user_id,code_hash,created_at,expires_at,consumed_at,invalidated_at,protocol_version,management_origin,server_key_id,server_public_key_spki_der_base64 FROM vault_supplier_pairing_codes WHERE code_hash=%s", (_digest(code),))
            return self._pairing_record(cursor.fetchone())

    def complete_pairing(self, code: str, binding: PairingBinding, installation: SupplierInstallation, user_id: UUID) -> SupplierInstallation:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,vault_id,user_id,code_hash,created_at,expires_at,consumed_at,invalidated_at,protocol_version,management_origin,server_key_id,server_public_key_spki_der_base64 FROM vault_supplier_pairing_codes WHERE code_hash=%s FOR UPDATE", (_digest(code),))
            record = validate_pairing_record(self._pairing_record(cursor.fetchone()), binding)
            cursor.execute("SELECT vault_id FROM vaults WHERE is_local=TRUE FOR SHARE")
            if cursor.fetchall() != [(binding.vault_id,)] or record.user_id != user_id:
                raise ValueError("pairing_identity_mismatch")
            result = self._register_installation(cursor, installation, user_id)
            cursor.execute("UPDATE vault_supplier_pairing_codes SET consumed_at=%s WHERE id=%s", (_now(), record.id))
            return result

    def register_installation(self, installation: SupplierInstallation, user_id: UUID) -> SupplierInstallation:
        with self._connect() as connection, connection.cursor() as cursor:
            return self._register_installation(cursor, installation, user_id)

    @staticmethod
    def _register_installation(cursor, installation: SupplierInstallation, user_id: UUID) -> SupplierInstallation:
        cursor.execute("""INSERT INTO vault_supplier_installations(installation_id,vault_id,public_key,key_algorithm,protocol_version,supplier_version,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(installation_id) DO NOTHING""", (installation.installation_id, installation.vault_id, installation.public_key, installation.key_algorithm, installation.protocol_version, installation.supplier_version, installation.created_at))
        cursor.execute("SELECT installation_id,vault_id,public_key,key_algorithm,protocol_version,supplier_version,created_at,last_seen_at,revoked_at FROM vault_supplier_installations WHERE installation_id=%s FOR UPDATE", (installation.installation_id,))
        row = cursor.fetchone()
        assert row is not None
        existing = SupplierInstallation(UUID(str(row[0])), UUID(str(row[1])), bytes(row[2]), str(row[3]), int(row[4]), str(row[5]), row[6], row[7], row[8])
        if existing.vault_id != installation.vault_id or existing.public_key != installation.public_key or existing.key_algorithm != installation.key_algorithm:
            raise ValueError("invalid_installation_identity")
        if existing.revoked_at:
            raise ValueError("installation_revoked")
        cursor.execute("INSERT INTO vault_supplier_authorized_users(installation_id,user_id) VALUES(%s,%s) ON CONFLICT(installation_id,user_id) DO UPDATE SET revoked_at=NULL", (installation.installation_id, user_id))
        return existing

    def list_installations(self, user_id: UUID) -> list[InstallationSummary]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT i.installation_id,i.supplier_version,i.protocol_version,i.created_at,i.last_seen_at,i.revoked_at
                FROM vault_supplier_installations i JOIN vault_supplier_authorized_users u ON u.installation_id=i.installation_id
                WHERE u.user_id=%s AND u.revoked_at IS NULL ORDER BY i.created_at DESC""", (user_id,))
            return [InstallationSummary(UUID(str(r[0])), str(r[1]), int(r[2]), r[3], r[4], r[5]) for r in cursor.fetchall()]

    def revoke_installation(self, installation_id: UUID, user_id: UUID) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE vault_supplier_installations SET revoked_at=CURRENT_TIMESTAMP WHERE installation_id=%s AND revoked_at IS NULL
                AND EXISTS(SELECT 1 FROM vault_supplier_authorized_users WHERE installation_id=%s AND user_id=%s AND revoked_at IS NULL)""", (installation_id, installation_id, user_id))
            return cursor.rowcount == 1

    def create_challenge(self, installation_id: UUID, requested_user_id: UUID | None) -> SupplierChallenge | None:
        result = SupplierChallenge(uuid4(), installation_id, UUID(int=0), requested_user_id, secrets.token_bytes(32), _now() + CHALLENGE_LIFETIME)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT vault_id FROM vault_supplier_installations WHERE installation_id=%s AND revoked_at IS NULL", (installation_id,)); row = cursor.fetchone()
            if not row: return None
            result = replace(result, vault_id=UUID(str(row[0])))
            if requested_user_id:
                cursor.execute("SELECT 1 FROM vault_supplier_authorized_users WHERE installation_id=%s AND user_id=%s AND revoked_at IS NULL", (installation_id, requested_user_id))
                if not cursor.fetchone(): return None
            cursor.execute("INSERT INTO vault_supplier_challenges(id,installation_id,vault_id,requested_user_id,challenge,expires_at) VALUES(%s,%s,%s,%s,%s,%s)", (result.id,result.installation_id,result.vault_id,result.requested_user_id,result.challenge,result.expires_at))
        return result

    def consume_challenge(self, challenge_id: UUID, installation_id: UUID) -> SupplierChallenge | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""UPDATE vault_supplier_challenges SET consumed_at=CURRENT_TIMESTAMP WHERE id=%s AND installation_id=%s
                AND consumed_at IS NULL AND expires_at>CURRENT_TIMESTAMP RETURNING id,installation_id,vault_id,requested_user_id,challenge,expires_at,consumed_at""", (challenge_id, installation_id)); row = cursor.fetchone()
        return SupplierChallenge(UUID(str(row[0])),UUID(str(row[1])),UUID(str(row[2])),UUID(str(row[3])) if row[3] else None,bytes(row[4]),row[5],row[6]) if row else None

    def challenge_error(self, challenge_id: UUID, installation_id: UUID) -> str:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT expires_at FROM vault_supplier_challenges WHERE id=%s AND installation_id=%s", (challenge_id, installation_id)); row = cursor.fetchone()
        return "challenge_expired" if row and row[0] <= _now() else "challenge_used"

    def get_installation(self, installation_id: UUID) -> SupplierInstallation | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT installation_id,vault_id,public_key,key_algorithm,protocol_version,supplier_version,created_at,last_seen_at,revoked_at FROM vault_supplier_installations WHERE installation_id=%s", (installation_id,)); row = cursor.fetchone()
        return SupplierInstallation(UUID(str(row[0])),UUID(str(row[1])),bytes(row[2]),str(row[3]),int(row[4]),str(row[5]),row[6],row[7],row[8]) if row else None

    def record_installation_authentication(self, installation_id: UUID, user_id: UUID | None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE vault_supplier_installations SET last_seen_at=CURRENT_TIMESTAMP WHERE installation_id=%s", (installation_id,))
            if user_id: cursor.execute("UPDATE vault_supplier_authorized_users SET last_used_at=CURRENT_TIMESTAMP WHERE installation_id=%s AND user_id=%s", (installation_id, user_id))

    def create_authorization(self, installation_id: UUID, user_id: UUID | None) -> tuple[str, datetime]:
        if user_id is None:
            raise ValueError("user_not_allowed")
        token = secrets.token_urlsafe(32)
        expires_at = _now() + AUTHORIZATION_LIFETIME
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT 1 FROM vault_supplier_authorized_users
                WHERE installation_id=%s AND user_id=%s AND revoked_at IS NULL""", (installation_id, user_id))
            if cursor.fetchone() is None:
                raise ValueError("user_not_allowed")
            cursor.execute("""INSERT INTO vault_supplier_authorizations(token_hash,installation_id,user_id,expires_at)
                VALUES(%s,%s,%s,%s)""", (_digest(token), installation_id, user_id, expires_at))
        return token, expires_at

    def authorize_request(self, installation_id: UUID, user_id: UUID, token: str) -> SupplierInstallation | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT i.installation_id,i.vault_id,i.public_key,i.key_algorithm,i.protocol_version,i.supplier_version,i.created_at,i.last_seen_at,i.revoked_at
                FROM vault_supplier_authorizations a
                JOIN vault_supplier_installations i ON i.installation_id=a.installation_id
                JOIN vault_supplier_authorized_users u ON u.installation_id=i.installation_id AND u.user_id=a.user_id
                WHERE a.token_hash=%s AND a.installation_id=%s AND a.user_id=%s
                  AND a.expires_at>CURRENT_TIMESTAMP AND i.revoked_at IS NULL AND u.revoked_at IS NULL""", (_digest(token), installation_id, user_id))
            row = cursor.fetchone()
            if row is None:
                return None
            cursor.execute("UPDATE vault_supplier_installations SET last_seen_at=CURRENT_TIMESTAMP WHERE installation_id=%s", (installation_id,))
            cursor.execute("UPDATE vault_supplier_authorized_users SET last_used_at=CURRENT_TIMESTAMP WHERE installation_id=%s AND user_id=%s", (installation_id, user_id))
        return SupplierInstallation(UUID(str(row[0])),UUID(str(row[1])),bytes(row[2]),str(row[3]),int(row[4]),str(row[5]),row[6],row[7],row[8])


def get_vault_supplier_store() -> VaultSupplierStore:
    return PostgresVaultSupplierStore(get_database_conninfo())


class PairingCodeResponse(BaseModel):
    pairing_credential: str
    expires_at: datetime
    protocol_version: int = PROTOCOL_VERSION


class PairRequest(BaseModel):
    pairing_secret: str = Field(max_length=128)
    installation_id: UUID
    installation_public_key: str = Field(max_length=16384)
    key_algorithm: str
    supplier_version: str = Field(min_length=1, max_length=100)
    protocol_version: StrictInt

    model_config = {"extra": "forbid"}


class ChallengeRequest(BaseModel):
    requested_user_id: UUID | None = None


class ChallengeVerifyRequest(BaseModel):
    challenge_id: UUID
    signature: str = Field(min_length=16, max_length=2048)


@router.post("/pairing-code", response_model=PairingCodeResponse)
def create_pairing_code(user: AuthenticatedUser, request: Request, store: VaultSupplierStore = Depends(get_vault_supplier_store), auth: AuthenticationStore = Depends(get_authentication_store), server_identity: LanServerIdentity = Depends(get_pairing_server_identity)) -> PairingCodeResponse:
    vault_id, _ = store.local_vault()
    binding = pairing_binding(vault_id, server_identity)
    secret = secrets.token_urlsafe(32)
    credential = encode_pairing_credential(binding, secret)
    result = store.create_pairing_code(binding, user.user_id, secret)
    auth.record_security_event("vault_supplier_pairing_code_generated", user_id=user.user_id, actor_user_id=user.user_id, client_ip=request.client.host if request.client else None)
    if result.replaced_previous:
        auth.record_security_event("vault_supplier_pairing_code_replaced", user_id=user.user_id, actor_user_id=user.user_id, client_ip=request.client.host if request.client else None)
    return PairingCodeResponse(pairing_credential=credential, expires_at=result.expires_at)


@router.post("/pair")
def pair(body: PairRequest, store: VaultSupplierStore = Depends(get_vault_supplier_store), auth: AuthenticationStore = Depends(get_authentication_store), server_identity: LanServerIdentity = Depends(get_pairing_server_identity)) -> dict[str, object]:
    if body.protocol_version != PROTOCOL_VERSION:
        raise _domain_error("protocol_mismatch")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", body.pairing_secret):
        raise _domain_error("invalid_pairing_code")
    try:
        public_key = _public_key(body.installation_public_key, body.key_algorithm)
    except ValueError:
        raise _domain_error("invalid_installation_key") from None
    vault_id, display_name = store.local_vault()
    binding = pairing_binding(vault_id, server_identity)
    try:
        code = validate_pairing_record(store.get_pairing_code(body.pairing_secret), binding)
        account = auth.get_account_by_user_id(code.user_id)
        if not account or not account.active:
            raise _domain_error("user_not_allowed", status.HTTP_403_FORBIDDEN)
        installation = store.complete_pairing(body.pairing_secret, binding, SupplierInstallation(body.installation_id, code.vault_id, public_key, body.key_algorithm, body.protocol_version, body.supplier_version, _now()), code.user_id)
    except ValueError as error:
        raise _domain_error(str(error)) from error
    auth.record_security_event("vault_supplier_paired", user_id=code.user_id, actor_user_id=code.user_id, metadata={"installation_id":str(installation.installation_id)})
    auth.record_security_event("vault_supplier_user_authorized", user_id=code.user_id, actor_user_id=code.user_id, metadata={"installation_id":str(installation.installation_id)})
    auth.record_security_event("vault_supplier_installation_registered", user_id=code.user_id, actor_user_id=code.user_id, metadata={"installation_id":str(installation.installation_id)})
    return {"vault_id":str(code.vault_id), "vault_display_name":display_name, "user_id":str(code.user_id), "user_display_name":account.display_name, "installation_id":str(installation.installation_id), "protocol_version":PROTOCOL_VERSION, "lan_connection_metadata":{"available":False,"mode":"unavailable"}, "server_identity":server_identity.pairing_response()}


class LanVerifyRequest(BaseModel):
    protocol_version: int
    nonce: str

    model_config = {"extra": "forbid"}


def _lan_metadata(vault_id: UUID, server_identity: LanServerIdentity) -> dict[str, object]:
    return {
        "protocol_version": LAN_PROTOCOL_VERSION,
        "vault_id": str(vault_id),
        "server_key_id": server_identity.key_id_sha256,
        "verify_path": VERIFY_PATH,
        "capabilities": {"receiver_available": False, "resumable_upload_supported": False},
    }


@router.get("/lan/identity")
def lan_identity(store: VaultSupplierStore = Depends(get_vault_supplier_store)) -> dict[str, object]:
    try:
        server_identity = get_lan_server_identity()
    except ServerIdentityUnavailable:
        raise _lan_error("server_identity_unavailable", "Vault Supplier LAN server identity is unavailable.", status.HTTP_500_INTERNAL_SERVER_ERROR) from None
    vault_id, _ = store.local_vault()
    return _lan_metadata(vault_id, server_identity)


@router.post("/lan/verify")
def lan_verify(body: LanVerifyRequest, store: VaultSupplierStore = Depends(get_vault_supplier_store)) -> dict[str, object]:
    if body.protocol_version != LAN_PROTOCOL_VERSION:
        raise _lan_error("protocol_mismatch", "Unsupported Vault Supplier LAN protocol version.", status.HTTP_400_BAD_REQUEST)
    try:
        nonce = validate_nonce(body.nonce)
    except ValueError:
        raise _lan_error("invalid_nonce", "Nonce must be an unpadded Base64URL encoding of exactly 32 bytes.", status.HTTP_400_BAD_REQUEST) from None
    vault_id, _ = store.local_vault()
    capabilities = {"receiver_available": False, "resumable_upload_supported": False}
    try:
        server_identity = get_lan_server_identity()
        port = lan_port()
        payload = canonical_payload(vault_id=vault_id, nonce=nonce, key_id=server_identity.key_id_sha256, port=port, **capabilities)
        signature = server_identity.sign(payload)
    except ServerIdentityUnavailable:
        raise _lan_error("server_identity_unavailable", "Vault Supplier LAN server identity is unavailable.", status.HTTP_500_INTERNAL_SERVER_ERROR) from None
    return {"protocol_version": LAN_PROTOCOL_VERSION, "vault_id": str(vault_id), "nonce": nonce, "server_key_id": server_identity.key_id_sha256, "port": port, "capabilities": capabilities, "signed_payload_base64": b64encode(payload).decode("ascii"), "signature_der_base64": signature}


@router.get("/installations")
def list_installations(user: AuthenticatedUser, store: VaultSupplierStore = Depends(get_vault_supplier_store)) -> list[InstallationSummary]:
    return store.list_installations(user.user_id)


@router.delete("/installations/{installation_id}")
def revoke_installation(installation_id: UUID, user: AuthenticatedUser, request: Request, store: VaultSupplierStore = Depends(get_vault_supplier_store), auth: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    if not store.revoke_installation(installation_id, user.user_id): raise HTTPException(status_code=404, detail="Installation not found")
    auth.record_security_event("vault_supplier_revoked", user_id=user.user_id, actor_user_id=user.user_id, client_ip=request.client.host if request.client else None, metadata={"installation_id":str(installation_id)})
    return {"status":"revoked"}


@router.post("/installations/{installation_id}/challenge")
def create_challenge(installation_id: UUID, body: ChallengeRequest, store: VaultSupplierStore = Depends(get_vault_supplier_store)) -> dict[str, object]:
    installation = store.get_installation(installation_id)
    if installation and installation.revoked_at: raise _domain_error("installation_revoked", status.HTTP_403_FORBIDDEN)
    result = store.create_challenge(installation_id, body.requested_user_id)
    if not result: raise _domain_error("user_not_allowed", status.HTTP_403_FORBIDDEN)
    return {"challenge_id":str(result.id),"challenge":_b64encode(result.challenge),"expires_at":result.expires_at,"protocol_version":PROTOCOL_VERSION}


@router.post("/installations/{installation_id}/authenticate")
def authenticate_installation(installation_id: UUID, body: ChallengeVerifyRequest, store: VaultSupplierStore = Depends(get_vault_supplier_store), auth: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    installation = store.get_installation(installation_id)
    if not installation: raise _domain_error("invalid_installation_identity", status.HTTP_403_FORBIDDEN)
    if installation.revoked_at: raise _domain_error("installation_revoked", status.HTTP_403_FORBIDDEN)
    challenge = store.consume_challenge(body.challenge_id, installation_id)
    if not challenge: raise _domain_error(store.challenge_error(body.challenge_id, installation_id))
    if not _verify_signature(installation.public_key, challenge.challenge, body.signature):
        auth.record_security_event("vault_supplier_challenge_failed", user_id=challenge.requested_user_id, metadata={"installation_id":str(installation_id)})
        raise _domain_error("invalid_signature", status.HTTP_403_FORBIDDEN)
    store.record_installation_authentication(installation_id, challenge.requested_user_id)
    authorization_token: str | None = None
    authorization_expires_at: datetime | None = None
    if challenge.requested_user_id is not None:
        try:
            authorization_token, authorization_expires_at = store.create_authorization(installation_id, challenge.requested_user_id)
        except ValueError as error:
            raise _domain_error(str(error), status.HTTP_403_FORBIDDEN) from error
    auth.record_security_event("vault_supplier_challenge_succeeded", user_id=challenge.requested_user_id, metadata={"installation_id":str(installation_id)})
    return {"status":"authenticated","installation_id":str(installation_id),"user_id":str(challenge.requested_user_id) if challenge.requested_user_id else None,"authorization_token":authorization_token,"authorization_expires_at":authorization_expires_at,"protocol_version":PROTOCOL_VERSION}
