"""One-time initial-passkey enrolment invitations; never sessions."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
import threading
from typing import Literal
from uuid import UUID, uuid4

import psycopg

from app.passkeys import PasskeyCredential

INVITATION_LIFETIME = timedelta(minutes=60)
EnrolmentPurpose = Literal["initial_enrolment", "recovery_enrolment"]

@dataclass(frozen=True)
class EnrolmentInvite:
    id: UUID
    user_id: UUID
    token_hash: str
    issued_by_user_id: UUID
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    invalidated_at: datetime | None = None
    purpose: EnrolmentPurpose = "initial_enrolment"

class MemoryEnrolmentStore:
    def __init__(self) -> None:
        self.invites: dict[UUID, EnrolmentInvite] = {}
        self._lock = threading.Lock()

    def create(
        self, user_id: UUID, issued_by: UUID, purpose: EnrolmentPurpose = "initial_enrolment"
    ) -> tuple[EnrolmentInvite, str]:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        record = EnrolmentInvite(
            uuid4(), user_id, _token_hash(token), issued_by, now, now + INVITATION_LIFETIME,
            purpose=purpose,
        )
        with self._lock:
            for key, item in self.invites.items():
                if item.user_id == user_id and item.purpose == purpose and item.consumed_at is None and item.invalidated_at is None:
                    self.invites[key] = replace(item, invalidated_at=now)
            self.invites[record.id] = record
        return record, token

    def validate(self, token: str, purpose: EnrolmentPurpose = "initial_enrolment") -> EnrolmentInvite | None:
        digest = _token_hash(token)
        now = datetime.now(timezone.utc)
        with self._lock:
            return next(
                (
                    item
                    for item in self.invites.values()
                    if secrets.compare_digest(item.token_hash, digest)
                    and item.purpose == purpose
                    and item.consumed_at is None
                    and item.invalidated_at is None
                    and item.expires_at > now
                ),
                None,
            )

    def consume(self, invite_id: UUID) -> bool:
        with self._lock:
            item = self.invites.get(invite_id)
            now = datetime.now(timezone.utc)
            if not item or item.consumed_at or item.invalidated_at or item.expires_at <= now:
                return False
            self.invites[invite_id] = replace(item, consumed_at=now)
            return True

    def active_for_user(self, user_id: UUID, purpose: EnrolmentPurpose) -> EnrolmentInvite | None:
        now = datetime.now(timezone.utc)
        with self._lock:
            return next((item for item in self.invites.values() if item.user_id == user_id and item.purpose == purpose and item.consumed_at is None and item.invalidated_at is None and item.expires_at > now), None)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

class PostgresEnrolmentStore:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def initialize(self) -> None:
        with psycopg.connect(self.conninfo) as c, c.cursor() as x:
            x.execute("CREATE TABLE IF NOT EXISTS auth_enrolment_invites (id UUID PRIMARY KEY,user_id UUID NOT NULL REFERENCES auth_accounts(user_id),token_hash CHAR(64) NOT NULL UNIQUE,issued_by_user_id UUID NOT NULL REFERENCES auth_accounts(user_id),created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,expires_at TIMESTAMPTZ NOT NULL,consumed_at TIMESTAMPTZ,invalidated_at TIMESTAMPTZ,purpose TEXT NOT NULL DEFAULT 'initial_enrolment')")
            x.execute("ALTER TABLE auth_enrolment_invites ADD COLUMN IF NOT EXISTS purpose TEXT NOT NULL DEFAULT 'initial_enrolment'")
            x.execute("CREATE INDEX IF NOT EXISTS auth_enrolment_invites_user_idx ON auth_enrolment_invites(user_id,created_at)")

    def create(
        self, user_id: UUID, issued_by: UUID, purpose: EnrolmentPurpose = "initial_enrolment"
    ) -> tuple[EnrolmentInvite, str]:
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        record = EnrolmentInvite(uuid4(), user_id, _token_hash(token), issued_by, now, now + INVITATION_LIFETIME, purpose=purpose)
        with psycopg.connect(self.conninfo) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE auth_enrolment_invites SET invalidated_at=CURRENT_TIMESTAMP WHERE user_id=%s AND purpose=%s AND consumed_at IS NULL AND invalidated_at IS NULL", (user_id, purpose))
            cursor.execute("INSERT INTO auth_enrolment_invites(id,user_id,token_hash,issued_by_user_id,created_at,expires_at,purpose) VALUES(%s,%s,%s,%s,%s,%s,%s)", (record.id, record.user_id, record.token_hash, record.issued_by_user_id, record.created_at, record.expires_at, purpose))
        return record, token

    def create_recovery(self, user_id: UUID, issued_by: UUID) -> tuple[EnrolmentInvite, str] | None:
        """Atomically revoke the target's access and issue a recovery-only authority."""
        token = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc)
        record = EnrolmentInvite(uuid4(), user_id, _token_hash(token), issued_by, now, now + INVITATION_LIFETIME, purpose="recovery_enrolment")
        with psycopg.connect(self.conninfo) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT active,role FROM auth_accounts WHERE user_id=%s FOR UPDATE", (user_id,))
            account = cursor.fetchone()
            if account is None or not bool(account[0]) or str(account[1]) == "administrator":
                return None
            cursor.execute("UPDATE auth_passkey_credentials SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=%s AND revoked_at IS NULL", (user_id,))
            cursor.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user_id,))
            cursor.execute("UPDATE auth_enrolment_invites SET invalidated_at=CURRENT_TIMESTAMP WHERE user_id=%s AND purpose='recovery_enrolment' AND consumed_at IS NULL AND invalidated_at IS NULL", (user_id,))
            cursor.execute("INSERT INTO auth_enrolment_invites(id,user_id,token_hash,issued_by_user_id,created_at,expires_at,purpose) VALUES(%s,%s,%s,%s,%s,%s,%s)", (record.id, record.user_id, record.token_hash, record.issued_by_user_id, record.created_at, record.expires_at, record.purpose))
        return record, token

    def validate(self, token: str, purpose: EnrolmentPurpose = "initial_enrolment") -> EnrolmentInvite | None:
        with psycopg.connect(self.conninfo) as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT i.id,i.user_id,i.token_hash,i.issued_by_user_id,i.created_at,i.expires_at,i.consumed_at,i.invalidated_at,i.purpose
                FROM auth_enrolment_invites i JOIN auth_accounts a ON a.user_id=i.user_id
                WHERE i.token_hash=%s AND i.purpose=%s AND i.consumed_at IS NULL AND i.invalidated_at IS NULL AND i.expires_at>CURRENT_TIMESTAMP AND a.active
                AND ((i.purpose='initial_enrolment' AND a.password_hash IS NULL AND a.password_login_enabled = FALSE)
                  OR (i.purpose='recovery_enrolment' AND a.role <> 'administrator'))
                AND NOT EXISTS(SELECT 1 FROM auth_passkey_credentials p WHERE p.user_id=i.user_id AND p.revoked_at IS NULL)""", (_token_hash(token), purpose))
            row = cursor.fetchone()
        return EnrolmentInvite(UUID(str(row[0])), UUID(str(row[1])), str(row[2]), UUID(str(row[3])), row[4], row[5], row[6], row[7], row[8]) if row else None

    def consume_and_create_credential(self, token: str, credential: PasskeyCredential, purpose: EnrolmentPurpose = "initial_enrolment") -> bool:
        """Atomically claim a still-valid invite and persist its first credential."""
        with psycopg.connect(self.conninfo) as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT i.id,i.user_id FROM auth_enrolment_invites i JOIN auth_accounts a ON a.user_id=i.user_id
                WHERE i.token_hash=%s AND i.purpose=%s AND i.consumed_at IS NULL AND i.invalidated_at IS NULL AND i.expires_at>CURRENT_TIMESTAMP AND a.active
                AND ((i.purpose='initial_enrolment' AND a.password_hash IS NULL AND a.password_login_enabled = FALSE)
                  OR (i.purpose='recovery_enrolment' AND a.role <> 'administrator'))
                AND NOT EXISTS(SELECT 1 FROM auth_passkey_credentials p WHERE p.user_id=i.user_id AND p.revoked_at IS NULL) FOR UPDATE""", (_token_hash(token), purpose))
            row = cursor.fetchone()
            if row is None or UUID(str(row[1])) != credential.user_id:
                return False
            cursor.execute("""INSERT INTO auth_passkey_credentials(id,user_id,credential_id,public_key,sign_count,transports,authenticator_attachment,label,created_at)
                VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s)""", (credential.id, credential.user_id, credential.credential_id, credential.public_key, credential.sign_count, psycopg.types.json.Jsonb(list(credential.transports)), credential.authenticator_attachment, credential.label, credential.created_at))
            cursor.execute("UPDATE auth_enrolment_invites SET consumed_at=CURRENT_TIMESTAMP WHERE id=%s AND consumed_at IS NULL", (row[0],))
            if cursor.rowcount != 1:
                # Raising rolls back the credential insert with this transaction.
                raise RuntimeError("Invitation could not be consumed")
        return True

    def active_for_user(self, user_id: UUID, purpose: EnrolmentPurpose) -> EnrolmentInvite | None:
        with psycopg.connect(self.conninfo) as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT id,user_id,token_hash,issued_by_user_id,created_at,expires_at,consumed_at,invalidated_at,purpose
                FROM auth_enrolment_invites WHERE user_id=%s AND purpose=%s AND consumed_at IS NULL
                AND invalidated_at IS NULL AND expires_at>CURRENT_TIMESTAMP ORDER BY created_at DESC LIMIT 1""", (user_id, purpose))
            row = cursor.fetchone()
        return EnrolmentInvite(UUID(str(row[0])), UUID(str(row[1])), str(row[2]), UUID(str(row[3])), row[4], row[5], row[6], row[7], row[8]) if row else None
