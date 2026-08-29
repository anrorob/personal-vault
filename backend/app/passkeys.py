"""Durable WebAuthn credential and ceremony state.

This module intentionally stores only public WebAuthn material.  The browser
and authenticator retain every private key and biometric/PIN operation.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import threading
from typing import Literal, Protocol
from uuid import UUID, uuid4

import psycopg


PASSKEY_CHALLENGE_LIFETIME = timedelta(minutes=5)
PasskeyCeremony = Literal["registration", "authentication"]
PasskeyPurpose = Literal["normal", "vault_control_step_up", "recovery_enrolment"]


@dataclass(frozen=True)
class PasskeyCredential:
    id: UUID
    user_id: UUID
    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    authenticator_attachment: str | None
    label: str | None
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None = None


@dataclass(frozen=True)
class PasskeyChallenge:
    id: UUID
    ceremony: PasskeyCeremony
    user_id: UUID | None
    challenge: bytes
    expires_at: datetime
    consumed_at: datetime | None = None
    purpose: PasskeyPurpose = "normal"


class PasskeyStore(Protocol):
    def initialize(self) -> None: ...
    def create_challenge(self, ceremony: PasskeyCeremony, user_id: UUID | None, challenge: bytes, purpose: PasskeyPurpose = "normal") -> PasskeyChallenge: ...
    def consume_challenge(self, challenge_id: UUID, ceremony: PasskeyCeremony, user_id: UUID | None, purpose: PasskeyPurpose = "normal") -> PasskeyChallenge | None: ...
    def create_credential(self, credential: PasskeyCredential) -> None: ...
    def get_credential(self, credential_id: bytes) -> PasskeyCredential | None: ...
    def list_credentials(self, user_id: UUID) -> list[PasskeyCredential]: ...
    def record_authentication(self, credential_id: UUID, sign_count: int) -> None: ...
    def revoke_credential(self, credential_id: UUID, user_id: UUID) -> bool: ...


class MemoryPasskeyStore:
    def __init__(self) -> None:
        self.challenges: dict[UUID, PasskeyChallenge] = {}
        self.credentials: dict[UUID, PasskeyCredential] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def initialize(self) -> None:
        return None

    def create_challenge(self, ceremony: PasskeyCeremony, user_id: UUID | None, challenge: bytes, purpose: PasskeyPurpose = "normal") -> PasskeyChallenge:
        record = PasskeyChallenge(uuid4(), ceremony, user_id, challenge, self._now() + PASSKEY_CHALLENGE_LIFETIME, purpose=purpose)
        with self._lock:
            self.challenges[record.id] = record
        return record

    def consume_challenge(self, challenge_id: UUID, ceremony: PasskeyCeremony, user_id: UUID | None, purpose: PasskeyPurpose = "normal") -> PasskeyChallenge | None:
        with self._lock:
            record = self.challenges.get(challenge_id)
            if not record or record.ceremony != ceremony or record.purpose != purpose or record.user_id != user_id or record.consumed_at or record.expires_at <= self._now():
                return None
            self.challenges[challenge_id] = replace(record, consumed_at=self._now())
            return record

    def create_credential(self, credential: PasskeyCredential) -> None:
        with self._lock:
            if any(item.credential_id == credential.credential_id for item in self.credentials.values()):
                raise ValueError("Passkey credential is already registered")
            self.credentials[credential.id] = credential

    def get_credential(self, credential_id: bytes) -> PasskeyCredential | None:
        with self._lock:
            return next((item for item in self.credentials.values() if item.credential_id == credential_id and item.revoked_at is None), None)

    def list_credentials(self, user_id: UUID) -> list[PasskeyCredential]:
        with self._lock:
            return sorted((item for item in self.credentials.values() if item.user_id == user_id and item.revoked_at is None), key=lambda item: item.created_at)

    def record_authentication(self, credential_id: UUID, sign_count: int) -> None:
        with self._lock:
            record = self.credentials[credential_id]
            self.credentials[credential_id] = replace(record, sign_count=sign_count, last_used_at=self._now())

    def revoke_credential(self, credential_id: UUID, user_id: UUID) -> bool:
        with self._lock:
            record = self.credentials.get(credential_id)
            if not record or record.user_id != user_id or record.revoked_at is not None:
                return False
            self.credentials[credential_id] = replace(record, revoked_at=self._now())
            return True


class PostgresPasskeyStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo)

    def initialize(self) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS auth_passkey_credentials (
                    id UUID PRIMARY KEY,
                    user_id UUID NOT NULL REFERENCES auth_accounts(user_id) ON DELETE RESTRICT,
                    credential_id BYTEA NOT NULL UNIQUE,
                    public_key BYTEA NOT NULL,
                    sign_count BIGINT NOT NULL DEFAULT 0 CHECK (sign_count >= 0),
                    transports JSONB NOT NULL DEFAULT '[]'::jsonb,
                    authenticator_attachment TEXT,
                    label TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMPTZ,
                    revoked_at TIMESTAMPTZ
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS auth_passkey_credentials_user_idx ON auth_passkey_credentials (user_id, created_at)")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS auth_passkey_challenges (
                    id UUID PRIMARY KEY,
                    ceremony TEXT NOT NULL CHECK (ceremony IN ('registration', 'authentication')),
                    user_id UUID REFERENCES auth_accounts(user_id) ON DELETE CASCADE,
                    challenge BYTEA NOT NULL,
                    expires_at TIMESTAMPTZ NOT NULL,
                    consumed_at TIMESTAMPTZ,
                    purpose TEXT NOT NULL DEFAULT 'normal'
                )
            """)
            cursor.execute("ALTER TABLE auth_passkey_challenges ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'normal'")
            cursor.execute("CREATE INDEX IF NOT EXISTS auth_passkey_challenges_expiry_idx ON auth_passkey_challenges (expires_at)")

    @staticmethod
    def _credential(row: tuple[object, ...] | None) -> PasskeyCredential | None:
        if row is None:
            return None
        return PasskeyCredential(
            id=UUID(str(row[0])), user_id=UUID(str(row[1])),
            credential_id=bytes(row[2]), public_key=bytes(row[3]),
            sign_count=int(row[4]), transports=tuple(row[5]),
            authenticator_attachment=str(row[6]) if row[6] else None,
            label=str(row[7]) if row[7] else None,
            created_at=row[8], last_used_at=row[9], revoked_at=row[10],
        )

    def create_challenge(self, ceremony: PasskeyCeremony, user_id: UUID | None, challenge: bytes, purpose: PasskeyPurpose = "normal") -> PasskeyChallenge:
        record = PasskeyChallenge(uuid4(), ceremony, user_id, challenge, datetime.now(timezone.utc) + PASSKEY_CHALLENGE_LIFETIME, purpose=purpose)
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM auth_passkey_challenges WHERE expires_at <= CURRENT_TIMESTAMP OR consumed_at IS NOT NULL")
            cursor.execute("INSERT INTO auth_passkey_challenges (id, ceremony, user_id, challenge, expires_at, purpose) VALUES (%s, %s, %s, %s, %s, %s)", (record.id, record.ceremony, record.user_id, record.challenge, record.expires_at, record.purpose))
        return record

    def consume_challenge(self, challenge_id: UUID, ceremony: PasskeyCeremony, user_id: UUID | None, purpose: PasskeyPurpose = "normal") -> PasskeyChallenge | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                UPDATE auth_passkey_challenges SET consumed_at = CURRENT_TIMESTAMP
                WHERE id = %s AND ceremony = %s AND purpose = %s AND user_id IS NOT DISTINCT FROM %s
                  AND consumed_at IS NULL AND expires_at > CURRENT_TIMESTAMP
                RETURNING id, ceremony, user_id, challenge, expires_at, consumed_at, purpose
            """, (challenge_id, ceremony, purpose, user_id))
            row = cursor.fetchone()
        if row is None:
            return None
        return PasskeyChallenge(UUID(str(row[0])), row[1], UUID(str(row[2])) if row[2] else None, bytes(row[3]), row[4], row[5], row[6])

    def create_credential(self, credential: PasskeyCredential) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("""INSERT INTO auth_passkey_credentials
                    (id, user_id, credential_id, public_key, sign_count, transports, authenticator_attachment, label, created_at)
                    VALUES (%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""", (credential.id, credential.user_id, credential.credential_id, credential.public_key, credential.sign_count, psycopg.types.json.Jsonb(list(credential.transports)), credential.authenticator_attachment, credential.label, credential.created_at))
        except psycopg.errors.UniqueViolation as error:
            raise ValueError("Passkey credential is already registered") from error

    def get_credential(self, credential_id: bytes) -> PasskeyCredential | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,user_id,credential_id,public_key,sign_count,transports,authenticator_attachment,label,created_at,last_used_at,revoked_at FROM auth_passkey_credentials WHERE credential_id=%s AND revoked_at IS NULL", (credential_id,))
            return self._credential(cursor.fetchone())

    def list_credentials(self, user_id: UUID) -> list[PasskeyCredential]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,user_id,credential_id,public_key,sign_count,transports,authenticator_attachment,label,created_at,last_used_at,revoked_at FROM auth_passkey_credentials WHERE user_id=%s AND revoked_at IS NULL ORDER BY created_at", (user_id,))
            return [item for row in cursor.fetchall() if (item := self._credential(row))]

    def record_authentication(self, credential_id: UUID, sign_count: int) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE auth_passkey_credentials SET sign_count=%s,last_used_at=CURRENT_TIMESTAMP WHERE id=%s AND revoked_at IS NULL", (sign_count, credential_id))

    def revoke_credential(self, credential_id: UUID, user_id: UUID) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE auth_passkey_credentials SET revoked_at=CURRENT_TIMESTAMP WHERE id=%s AND user_id=%s AND revoked_at IS NULL", (credential_id, user_id))
            return cursor.rowcount == 1
