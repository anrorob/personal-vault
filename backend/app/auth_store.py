from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
from typing import Protocol
from uuid import UUID, NAMESPACE_URL, uuid4, uuid5

import psycopg


MAX_FAILED_ATTEMPTS = 5
ATTEMPT_WINDOW = timedelta(minutes=15)
LOCKOUT_DURATION = timedelta(minutes=15)
SESSION_LAST_SEEN_INTERVAL = timedelta(minutes=5)


class AuthenticationStore(Protocol):
    def healthcheck(self) -> None: ...

    def get_lockout_seconds(self, key: str) -> int: ...

    def record_failed_attempt(self, key: str) -> int: ...

    def clear_failed_attempts(self, key: str) -> None: ...

    def create_session(
        self,
        token: str,
        user_id: UUID,
        username: str,
        expires_at: datetime,
        *,
        authentication_method: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None: ...

    def get_session_user_id(self, token: str) -> UUID | None: ...

    def elevate_vault_control_session(self, token: str, user_id: UUID, expires_at: datetime) -> bool: ...
    def refresh_vault_control_elevation(self, token: str, user_id: UUID, expires_at: datetime) -> bool: ...
    def has_vault_control_elevation(self, token: str, user_id: UUID) -> bool: ...

    def delete_session(self, token: str) -> None: ...
    def delete_sessions_for_user_id(self, user_id: UUID) -> None: ...
    def delete_other_sessions_for_user_id(self, user_id: UUID, token: str) -> None: ...
    def list_active_sessions(self, user_id: UUID, current_token: str | None = None) -> list["SessionSummary"]: ...
    def revoke_session_for_user_id(self, user_id: UUID, session_id: UUID) -> bool: ...
    def record_security_event(self, event_type: str, *, user_id: UUID | None = None, session_id: UUID | None = None, authentication_method: str | None = None, client_ip: str | None = None, user_agent: str | None = None, actor_user_id: UUID | None = None, metadata: dict[str, str] | None = None) -> None: ...
    def list_security_events(self, user_id: UUID, limit: int = 50) -> list["SecurityEvent"]: ...

    def ensure_initial_administrator(self, username: str, password_hash: str) -> "Account": ...

    def get_account(self, username: str) -> "Account | None": ...
    def get_account_by_user_id(self, user_id: UUID) -> "Account | None": ...

    def get_account_by_identity(self, identity: str) -> "Account | None": ...

    def list_accounts(self) -> list["Account"]: ...

    def create_account(self, account: "Account") -> None: ...

    def update_account(self, username: str, *, display_name: str, email: str | None, role: str, active: bool) -> "Account": ...

    def set_account_password(self, username: str, password_hash: str, password_change_required: bool) -> None: ...
    def set_password_login_enabled(self, user_id: UUID, enabled: bool) -> "Account | None": ...

    def record_sign_in(self, username: str) -> None: ...

    def get_gallery_state(self, user_id: UUID) -> "GalleryState | None": ...

    def save_gallery_state(self, user_id: UUID, state: "GalleryState") -> None: ...

    def get_movie_progress(self, user_id: UUID, movie_id: str) -> "MovieProgress | None": ...

    def save_movie_progress(self, user_id: UUID, progress: "MovieProgress") -> None: ...

    def get_episode_progress(self, user_id: UUID, episode_id: UUID) -> "EpisodeProgress | None": ...

    def save_episode_progress(self, user_id: UUID, progress: "EpisodeProgress") -> None: ...


@dataclass(frozen=True)
class Account:
    username: str
    display_name: str
    email: str | None
    password_hash: str | None
    role: str
    active: bool
    password_change_required: bool
    created_at: datetime
    last_sign_in_at: datetime | None
    user_id: UUID = field(default_factory=uuid4)
    password_login_enabled: bool = True


@dataclass
class LoginAttempt:
    failed_attempts: int
    first_failure_at: datetime
    locked_until: datetime | None = None


@dataclass
class SessionRecord:
    id: UUID
    user_id: UUID
    username: str
    expires_at: datetime
    created_at: datetime
    last_seen_at: datetime | None = None
    authentication_method: str | None = None
    client_ip: str | None = None
    user_agent: str | None = None
    vault_control_elevated_until: datetime | None = None


@dataclass(frozen=True)
class SessionSummary:
    id: UUID
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime
    authentication_method: str | None
    client_ip: str | None
    user_agent: str | None
    vault_control_elevated: bool
    current: bool


@dataclass(frozen=True)
class SecurityEvent:
    id: UUID
    user_id: UUID | None
    event_type: str
    occurred_at: datetime
    authentication_method: str | None
    session_id: UUID | None
    client_ip: str | None
    user_agent: str | None
    actor_user_id: UUID | None
    metadata: dict[str, str]


@dataclass(frozen=True)
class GalleryState:
    sort: str
    anchor_id: str | None = None
    anchor_offset: int = 0


@dataclass(frozen=True)
class MovieProgress:
    movie_id: str
    position_seconds: float
    duration_seconds: float
    completed: bool = False


@dataclass(frozen=True)
class EpisodeProgress:
    episode_id: UUID
    position_seconds: float
    duration_seconds: float
    completed: bool = False


class MemoryAuthenticationStore:
    def __init__(self) -> None:
        self.attempts: dict[str, LoginAttempt] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.gallery_states: dict[UUID, GalleryState] = {}
        self.movie_progress: dict[tuple[UUID, str], MovieProgress] = {}
        self.episode_progress: dict[tuple[UUID, UUID], EpisodeProgress] = {}
        self.accounts: dict[str, Account] = {}
        self.security_events: list[SecurityEvent] = []
        self._lock = threading.Lock()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _remove_stale_attempts(self, now: datetime) -> None:
        stale_keys = [
            key
            for key, attempt in self.attempts.items()
            if (
                attempt.locked_until is None
                and now - attempt.first_failure_at >= ATTEMPT_WINDOW
            )
            or (
                attempt.locked_until is not None
                and attempt.locked_until <= now
            )
        ]

        for key in stale_keys:
            self.attempts.pop(key, None)

    def healthcheck(self) -> None:
        return None

    def get_lockout_seconds(self, key: str) -> int:
        now = self._now()

        with self._lock:
            self._remove_stale_attempts(now)
            attempt = self.attempts.get(key)

            if not attempt or not attempt.locked_until:
                return 0

            remaining = int((attempt.locked_until - now).total_seconds())
            return max(remaining, 1)

    def record_failed_attempt(self, key: str) -> int:
        now = self._now()

        with self._lock:
            self._remove_stale_attempts(now)
            attempt = self.attempts.get(key)

            if attempt is None:
                attempt = LoginAttempt(
                    failed_attempts=0,
                    first_failure_at=now,
                )
                self.attempts[key] = attempt

            attempt.failed_attempts += 1

            if attempt.failed_attempts >= MAX_FAILED_ATTEMPTS:
                attempt.locked_until = now + LOCKOUT_DURATION
                return int(LOCKOUT_DURATION.total_seconds())

            return 0

    def clear_failed_attempts(self, key: str) -> None:
        with self._lock:
            self.attempts.pop(key, None)

    def create_session(
        self,
        token: str,
        user_id: UUID,
        username: str,
        expires_at: datetime,
        *,
        authentication_method: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        with self._lock:
            self.sessions[token] = SessionRecord(
                id=uuid4(),
                user_id=user_id,
                username=username,
                expires_at=expires_at,
                created_at=self._now(),
                last_seen_at=self._now(),
                authentication_method=authentication_method,
                client_ip=client_ip,
                user_agent=user_agent,
            )

    def get_session_user_id(self, token: str) -> UUID | None:
        now = self._now()

        with self._lock:
            expired_tokens = [
                stored_token
                for stored_token, session in self.sessions.items()
                if session.expires_at <= now
            ]

            for expired_token in expired_tokens:
                self.sessions.pop(expired_token, None)

            session = self.sessions.get(token)
            if session and (session.last_seen_at is None or now - session.last_seen_at >= SESSION_LAST_SEEN_INTERVAL):
                session.last_seen_at = now
            return session.user_id if session else None

    def elevate_vault_control_session(self, token: str, user_id: UUID, expires_at: datetime) -> bool:
        with self._lock:
            session = self.sessions.get(token)
            if not session or session.user_id != user_id or session.expires_at <= self._now():
                return False
            self.sessions[token] = replace(session, vault_control_elevated_until=expires_at)
            return True

    def refresh_vault_control_elevation(self, token: str, user_id: UUID, expires_at: datetime) -> bool:
        with self._lock:
            session = self.sessions.get(token)
            if (not session or session.user_id != user_id or session.expires_at <= self._now()
                    or not session.vault_control_elevated_until or session.vault_control_elevated_until <= self._now()):
                return False
            self.sessions[token] = replace(session, vault_control_elevated_until=expires_at)
            return True

    def has_vault_control_elevation(self, token: str, user_id: UUID) -> bool:
        with self._lock:
            session = self.sessions.get(token)
            return bool(session and session.user_id == user_id and session.expires_at > self._now()
                        and session.vault_control_elevated_until and session.vault_control_elevated_until > self._now())

    def delete_session(self, token: str) -> None:
        with self._lock:
            self.sessions.pop(token, None)

    def delete_sessions_for_user_id(self, user_id: UUID) -> None:
        with self._lock:
            self.sessions = {token: item for token, item in self.sessions.items() if item.user_id != user_id}

    def delete_other_sessions_for_user_id(self, user_id: UUID, token: str) -> None:
        with self._lock:
            self.sessions = {
                stored_token: item for stored_token, item in self.sessions.items()
                if item.user_id != user_id or stored_token == token
            }

    def list_active_sessions(self, user_id: UUID, current_token: str | None = None) -> list[SessionSummary]:
        now = self._now()
        with self._lock:
            return [
                SessionSummary(item.id, item.created_at, item.last_seen_at, item.expires_at,
                    item.authentication_method, item.client_ip, item.user_agent,
                    bool(item.vault_control_elevated_until and item.vault_control_elevated_until > now), token == current_token)
                for token, item in self.sessions.items()
                if item.user_id == user_id and item.expires_at > now
            ]

    def revoke_session_for_user_id(self, user_id: UUID, session_id: UUID) -> bool:
        with self._lock:
            token = next((token for token, item in self.sessions.items() if item.id == session_id and item.user_id == user_id), None)
            if token is None:
                return False
            self.sessions.pop(token, None)
            return True

    def record_security_event(self, event_type: str, *, user_id: UUID | None = None, session_id: UUID | None = None, authentication_method: str | None = None, client_ip: str | None = None, user_agent: str | None = None, actor_user_id: UUID | None = None, metadata: dict[str, str] | None = None) -> None:
        with self._lock:
            self.security_events.append(SecurityEvent(uuid4(), user_id, event_type, self._now(), authentication_method, session_id, client_ip, user_agent, actor_user_id, metadata or {}))

    def list_security_events(self, user_id: UUID, limit: int = 50) -> list[SecurityEvent]:
        with self._lock:
            return [event for event in reversed(self.security_events) if event.user_id == user_id][:limit]

    def ensure_initial_administrator(self, username: str, password_hash: str) -> Account:
        username = username.casefold()
        with self._lock:
            account = self.accounts.get(username)
            if account is None:
                account = Account(username, username, None, password_hash, "administrator", True, False, self._now(), None, uuid5(NAMESPACE_URL, f"personal-vault-test:{username}"))
                self.accounts[username] = account
            return account

    def get_account(self, username: str) -> Account | None:
        with self._lock:
            return self.accounts.get(username.casefold())

    def get_account_by_user_id(self, user_id: UUID) -> Account | None:
        with self._lock:
            return next((account for account in self.accounts.values() if account.user_id == user_id), None)

    def get_account_by_identity(self, identity: str) -> Account | None:
        key = identity.strip().casefold()
        with self._lock:
            return next((account for account in self.accounts.values() if account.username == key or account.email == key), None)

    def list_accounts(self) -> list[Account]:
        with self._lock:
            return sorted(self.accounts.values(), key=lambda account: (account.display_name.casefold(), account.username))

    def create_account(self, account: Account) -> None:
        with self._lock:
            if account.username in self.accounts or (account.email and any(item.email == account.email for item in self.accounts.values())):
                raise ValueError("An account with that email already exists")
            self.accounts[account.username] = account

    def update_account(self, username: str, *, display_name: str, email: str | None, role: str, active: bool) -> Account:
        with self._lock:
            account = self.accounts[username.casefold()]
            if email and any(item.username != account.username and item.email == email for item in self.accounts.values()):
                raise ValueError("An account with that email already exists")
            updated = replace(account, display_name=display_name, email=email, role=role, active=active)
            self.accounts[updated.username] = updated
            return updated

    def set_account_password(self, username: str, password_hash: str, password_change_required: bool) -> None:
        with self._lock:
            account = self.accounts[username.casefold()]
            self.accounts[account.username] = replace(account, password_hash=password_hash, password_change_required=password_change_required)

    def set_password_login_enabled(self, user_id: UUID, enabled: bool) -> Account | None:
        with self._lock:
            account = next((item for item in self.accounts.values() if item.user_id == user_id), None)
            if account is None:
                return None
            updated = replace(account, password_login_enabled=enabled)
            self.accounts[updated.username] = updated
            return updated

    def record_sign_in(self, username: str) -> None:
        with self._lock:
            account = self.accounts[username.casefold()]
            self.accounts[account.username] = replace(account, last_sign_in_at=self._now())

    def get_gallery_state(self, user_id: UUID) -> GalleryState | None:
        return self.gallery_states.get(user_id)

    def save_gallery_state(self, user_id: UUID, state: GalleryState) -> None:
        self.gallery_states[user_id] = state

    def get_movie_progress(self, user_id: UUID, movie_id: str) -> MovieProgress | None:
        return self.movie_progress.get((user_id, movie_id))

    def save_movie_progress(self, user_id: UUID, progress: MovieProgress) -> None:
        self.movie_progress[(user_id, progress.movie_id)] = progress

    def get_episode_progress(self, user_id: UUID, episode_id: UUID) -> EpisodeProgress | None:
        return self.episode_progress.get((user_id, episode_id))

    def save_episode_progress(self, user_id: UUID, progress: EpisodeProgress) -> None:
        self.episode_progress[(user_id, progress.episode_id)] = progress


class PostgresAuthenticationStore:
    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    @staticmethod
    def _digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo)

    def initialize(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                CREATE TABLE IF NOT EXISTS auth_sessions (
                        id UUID NOT NULL DEFAULT gen_random_uuid(),
                        token_hash CHAR(64) PRIMARY KEY,
                        username TEXT NOT NULL,
                        user_id UUID,
                        expires_at TIMESTAMPTZ NOT NULL
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auth_accounts (
                        username TEXT PRIMARY KEY,
                        user_id UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
                        display_name TEXT NOT NULL,
                        email TEXT UNIQUE,
                        password_hash TEXT,
                        role TEXT NOT NULL CHECK (role IN ('administrator','user')),
                        active BOOLEAN NOT NULL DEFAULT TRUE,
                        password_change_required BOOLEAN NOT NULL DEFAULT FALSE,
                        password_login_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_sign_in_at TIMESTAMPTZ
                    )
                    """
                )
                cursor.execute("ALTER TABLE auth_accounts ADD COLUMN IF NOT EXISTS user_id UUID")
                cursor.execute("ALTER TABLE auth_accounts ADD COLUMN IF NOT EXISTS password_login_enabled BOOLEAN NOT NULL DEFAULT TRUE")
                cursor.execute("ALTER TABLE auth_accounts ALTER COLUMN password_hash DROP NOT NULL")
                cursor.execute("ALTER TABLE auth_accounts ALTER COLUMN user_id SET DEFAULT gen_random_uuid()")
                cursor.execute("SELECT username FROM auth_accounts WHERE user_id IS NULL")
                for (username,) in cursor.fetchall():
                    cursor.execute("UPDATE auth_accounts SET user_id = %s WHERE username = %s AND user_id IS NULL", (uuid4(), username))
                cursor.execute("SELECT COUNT(*) FROM auth_accounts WHERE user_id IS NULL")
                if int(cursor.fetchone()[0]):
                    raise RuntimeError("Authentication identity migration could not resolve every account")
                cursor.execute("ALTER TABLE auth_accounts ALTER COLUMN user_id SET NOT NULL")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS auth_accounts_user_id_idx ON auth_accounts (user_id)")
                cursor.execute("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS user_id UUID")
                cursor.execute("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS vault_control_elevated_until TIMESTAMPTZ")
                cursor.execute("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS id UUID")
                cursor.execute("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP")
                cursor.execute("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ")
                cursor.execute("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS authentication_method TEXT")
                cursor.execute("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS client_ip TEXT")
                cursor.execute("ALTER TABLE auth_sessions ADD COLUMN IF NOT EXISTS user_agent TEXT")
                cursor.execute("UPDATE auth_sessions SET id=gen_random_uuid() WHERE id IS NULL")
                cursor.execute("ALTER TABLE auth_sessions ALTER COLUMN id SET NOT NULL")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS auth_sessions_id_idx ON auth_sessions (id)")
                cursor.execute("UPDATE auth_sessions session SET user_id = account.user_id FROM auth_accounts account WHERE session.username = account.username AND session.user_id IS NULL")
                # A legacy row without an exact account match cannot establish an
                # identity safely, so invalidate it rather than guessing.
                cursor.execute("DELETE FROM auth_sessions WHERE user_id IS NULL")
                cursor.execute("ALTER TABLE auth_sessions ALTER COLUMN user_id SET NOT NULL")
                cursor.execute("ALTER TABLE auth_sessions DROP CONSTRAINT IF EXISTS auth_sessions_user_id_fkey")
                cursor.execute("ALTER TABLE auth_sessions ADD CONSTRAINT auth_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth_accounts(user_id)")
                cursor.execute("CREATE INDEX IF NOT EXISTS auth_sessions_user_id_idx ON auth_sessions (user_id)")
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_gallery_state (
                        username TEXT PRIMARY KEY,
                        user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                        sort_order TEXT NOT NULL CHECK (sort_order IN ('newest', 'oldest')),
                        anchor_id TEXT,
                        anchor_offset INTEGER NOT NULL DEFAULT 0,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS auth_security_events (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        user_id UUID REFERENCES auth_accounts(user_id),
                        event_type TEXT NOT NULL,
                        occurred_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        authentication_method TEXT,
                        session_id UUID,
                        client_ip TEXT,
                        user_agent TEXT,
                        actor_user_id UUID REFERENCES auth_accounts(user_id),
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS auth_security_events_user_time_idx ON auth_security_events (user_id, occurred_at DESC)")
                cursor.execute("ALTER TABLE user_gallery_state ADD COLUMN IF NOT EXISTS user_id UUID")
                cursor.execute("UPDATE user_gallery_state state SET user_id = account.user_id FROM auth_accounts account WHERE state.username = account.username AND state.user_id IS NULL")
                cursor.execute("SELECT COUNT(*) FROM user_gallery_state WHERE user_id IS NULL")
                if int(cursor.fetchone()[0]):
                    raise RuntimeError("Gallery state identity migration found an unknown account")
                cursor.execute("ALTER TABLE user_gallery_state ALTER COLUMN user_id SET NOT NULL")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS user_gallery_state_user_id_idx ON user_gallery_state (user_id)")
                cursor.execute("ALTER TABLE user_gallery_state DROP CONSTRAINT IF EXISTS user_gallery_state_user_id_fkey")
                cursor.execute("ALTER TABLE user_gallery_state ADD CONSTRAINT user_gallery_state_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth_accounts(user_id)")
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_movie_progress (
                        username TEXT NOT NULL,
                        user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                        movie_id TEXT NOT NULL,
                        position_seconds DOUBLE PRECISION NOT NULL CHECK (position_seconds >= 0),
                        duration_seconds DOUBLE PRECISION NOT NULL CHECK (duration_seconds >= 0),
                        completed BOOLEAN NOT NULL DEFAULT FALSE,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (username, movie_id)
                    )
                    """
                )
                cursor.execute("ALTER TABLE user_movie_progress ADD COLUMN IF NOT EXISTS user_id UUID")
                cursor.execute("UPDATE user_movie_progress state SET user_id = account.user_id FROM auth_accounts account WHERE state.username = account.username AND state.user_id IS NULL")
                cursor.execute("SELECT COUNT(*) FROM user_movie_progress WHERE user_id IS NULL")
                if int(cursor.fetchone()[0]):
                    raise RuntimeError("Movie progress identity migration found an unknown account")
                cursor.execute("ALTER TABLE user_movie_progress ALTER COLUMN user_id SET NOT NULL")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS user_movie_progress_user_id_movie_id_idx ON user_movie_progress (user_id, movie_id)")
                cursor.execute("ALTER TABLE user_movie_progress DROP CONSTRAINT IF EXISTS user_movie_progress_user_id_fkey")
                cursor.execute("ALTER TABLE user_movie_progress ADD CONSTRAINT user_movie_progress_user_id_fkey FOREIGN KEY (user_id) REFERENCES auth_accounts(user_id)")
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS user_episode_progress (
                        user_id UUID NOT NULL REFERENCES auth_accounts(user_id),
                        episode_id UUID NOT NULL,
                        position_seconds DOUBLE PRECISION NOT NULL CHECK (position_seconds >= 0),
                        duration_seconds DOUBLE PRECISION NOT NULL CHECK (duration_seconds >= 0),
                        completed BOOLEAN NOT NULL DEFAULT FALSE,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (user_id, episode_id)
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS auth_sessions_expires_at_idx
                    ON auth_sessions (expires_at)
                    """
                )
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS auth_login_attempts (
                        key_hash CHAR(64) PRIMARY KEY,
                        failed_attempts INTEGER NOT NULL
                            CHECK (failed_attempts >= 0),
                        first_failure_at TIMESTAMPTZ NOT NULL,
                        locked_until TIMESTAMPTZ
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS
                        auth_login_attempts_locked_until_idx
                    ON auth_login_attempts (locked_until)
                    """
                )

    def healthcheck(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

    def _remove_stale_attempt(
        self,
        cursor: psycopg.Cursor,
        key_hash: str,
    ) -> None:
        cursor.execute(
            """
            DELETE FROM auth_login_attempts
            WHERE key_hash = %s
              AND (
                (
                    locked_until IS NULL
                    AND first_failure_at
                        <= CURRENT_TIMESTAMP - (%s * INTERVAL '1 second')
                )
                OR (
                    locked_until IS NOT NULL
                    AND locked_until <= CURRENT_TIMESTAMP
                )
              )
            """,
            (key_hash, int(ATTEMPT_WINDOW.total_seconds())),
        )

    def get_lockout_seconds(self, key: str) -> int:
        key_hash = self._digest(key)

        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._remove_stale_attempt(cursor, key_hash)
                cursor.execute(
                    """
                    SELECT GREATEST(
                        1,
                        CEIL(
                            EXTRACT(
                                EPOCH FROM (locked_until - CURRENT_TIMESTAMP)
                            )
                        )::INTEGER
                    )
                    FROM auth_login_attempts
                    WHERE key_hash = %s
                      AND locked_until > CURRENT_TIMESTAMP
                    """,
                    (key_hash,),
                )
                row = cursor.fetchone()

        return int(row[0]) if row else 0

    def record_failed_attempt(self, key: str) -> int:
        key_hash = self._digest(key)
        lockout_seconds = int(LOCKOUT_DURATION.total_seconds())

        with self._connect() as connection:
            with connection.cursor() as cursor:
                self._remove_stale_attempt(cursor, key_hash)
                cursor.execute(
                    """
                    INSERT INTO auth_login_attempts (
                        key_hash,
                        failed_attempts,
                        first_failure_at,
                        locked_until
                    )
                    VALUES (%s, 1, CURRENT_TIMESTAMP, NULL)
                    ON CONFLICT (key_hash) DO UPDATE
                    SET
                        failed_attempts = CASE
                            WHEN auth_login_attempts.locked_until
                                    > CURRENT_TIMESTAMP
                            THEN auth_login_attempts.failed_attempts
                            ELSE auth_login_attempts.failed_attempts + 1
                        END,
                        locked_until = CASE
                            WHEN auth_login_attempts.locked_until
                                    > CURRENT_TIMESTAMP
                            THEN auth_login_attempts.locked_until
                            WHEN auth_login_attempts.failed_attempts + 1 >= %s
                            THEN CURRENT_TIMESTAMP
                                + (%s * INTERVAL '1 second')
                            ELSE NULL
                        END
                    RETURNING CASE
                        WHEN locked_until IS NULL THEN 0
                        ELSE GREATEST(
                            1,
                            CEIL(
                                EXTRACT(
                                    EPOCH FROM (
                                        locked_until - CURRENT_TIMESTAMP
                                    )
                                )
                            )::INTEGER
                        )
                    END
                    """,
                    (
                        key_hash,
                        MAX_FAILED_ATTEMPTS,
                        lockout_seconds,
                    ),
                )
                row = cursor.fetchone()

        return int(row[0]) if row else 0

    def clear_failed_attempts(self, key: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM auth_login_attempts WHERE key_hash = %s",
                    (self._digest(key),),
                )

    def create_session(
        self,
        token: str,
        user_id: UUID,
        username: str,
        expires_at: datetime,
        *,
        authentication_method: str | None = None,
        client_ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO auth_sessions (
                        id,
                        token_hash,
                        username,
                        user_id,
                        expires_at, last_seen_at, authentication_method, client_ip, user_agent
                    )
                    VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s, %s)
                    """,
                    (uuid4(), self._digest(token), username, user_id, expires_at, authentication_method, client_ip, user_agent),
                )

    def get_session_user_id(self, token: str) -> UUID | None:
        token_hash = self._digest(token)

        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM auth_sessions
                    WHERE expires_at <= CURRENT_TIMESTAMP
                    """
                )
                cursor.execute(
                    """UPDATE auth_sessions SET last_seen_at = CURRENT_TIMESTAMP
                    WHERE token_hash = %s
                      AND (last_seen_at IS NULL OR last_seen_at <= CURRENT_TIMESTAMP - INTERVAL '5 minutes')""",
                    (token_hash,),
                )
                cursor.execute(
                    """
                    SELECT user_id
                    FROM auth_sessions
                    WHERE token_hash = %s
                    """,
                    (token_hash,),
                )
                row = cursor.fetchone()

        return UUID(str(row[0])) if row else None

    def elevate_vault_control_session(self, token: str, user_id: UUID, expires_at: datetime) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE auth_sessions SET vault_control_elevated_until=%s
                   WHERE token_hash=%s AND user_id=%s AND expires_at>CURRENT_TIMESTAMP""",
                (expires_at, self._digest(token), user_id),
            )
            return cursor.rowcount == 1

    def refresh_vault_control_elevation(self, token: str, user_id: UUID, expires_at: datetime) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """UPDATE auth_sessions SET vault_control_elevated_until=%s
                   WHERE token_hash=%s AND user_id=%s AND expires_at>CURRENT_TIMESTAMP
                     AND vault_control_elevated_until>CURRENT_TIMESTAMP""",
                (expires_at, self._digest(token), user_id),
            )
            return cursor.rowcount == 1

    def has_vault_control_elevation(self, token: str, user_id: UUID) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT 1 FROM auth_sessions WHERE token_hash=%s AND user_id=%s
                   AND expires_at>CURRENT_TIMESTAMP AND vault_control_elevated_until>CURRENT_TIMESTAMP""",
                (self._digest(token), user_id),
            )
            return cursor.fetchone() is not None

    def delete_session(self, token: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM auth_sessions WHERE token_hash = %s",
                    (self._digest(token),),
                )

    def delete_sessions_for_user_id(self, user_id: UUID) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM auth_sessions WHERE user_id = %s", (user_id,))

    def delete_other_sessions_for_user_id(self, user_id: UUID, token: str) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM auth_sessions WHERE user_id = %s AND token_hash <> %s",
                    (user_id, self._digest(token)),
                )

    def list_active_sessions(self, user_id: UUID, current_token: str | None = None) -> list[SessionSummary]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""SELECT id,created_at,last_seen_at,expires_at,authentication_method,client_ip,user_agent,
                vault_control_elevated_until>CURRENT_TIMESTAMP,token_hash=%s
                FROM auth_sessions WHERE user_id=%s AND expires_at>CURRENT_TIMESTAMP ORDER BY created_at DESC""", (self._digest(current_token) if current_token else "", user_id))
            return [SessionSummary(UUID(str(row[0])), row[1], row[2], row[3], row[4], row[5], row[6], bool(row[7]), bool(row[8])) for row in cursor.fetchall()]

    def revoke_session_for_user_id(self, user_id: UUID, session_id: UUID) -> bool:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("DELETE FROM auth_sessions WHERE id=%s AND user_id=%s", (session_id, user_id))
            return cursor.rowcount == 1

    def record_security_event(self, event_type: str, *, user_id: UUID | None = None, session_id: UUID | None = None, authentication_method: str | None = None, client_ip: str | None = None, user_agent: str | None = None, actor_user_id: UUID | None = None, metadata: dict[str, str] | None = None) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("""INSERT INTO auth_security_events (id,user_id,event_type,authentication_method,session_id,client_ip,user_agent,actor_user_id,metadata)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)""", (uuid4(), user_id, event_type, authentication_method, session_id, client_ip, user_agent, actor_user_id, json.dumps(metadata or {})))

    def list_security_events(self, user_id: UUID, limit: int = 50) -> list[SecurityEvent]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id,user_id,event_type,occurred_at,authentication_method,session_id,client_ip,user_agent,actor_user_id,metadata FROM auth_security_events WHERE user_id=%s ORDER BY occurred_at DESC LIMIT %s", (user_id, min(max(limit, 1), 50)))
            return [SecurityEvent(UUID(str(row[0])), UUID(str(row[1])) if row[1] else None, str(row[2]), row[3], row[4], UUID(str(row[5])) if row[5] else None, row[6], row[7], UUID(str(row[8])) if row[8] else None, dict(row[9] or {})) for row in cursor.fetchall()]

    @staticmethod
    def _account(row: tuple[object, ...] | None) -> Account | None:
        if row is None:
            return None
        return Account(
            username=str(row[0]), display_name=str(row[1]),
            email=str(row[2]) if row[2] is not None else None,
            password_hash=str(row[3]) if row[3] is not None else None, role=str(row[4]), active=bool(row[5]),
            password_change_required=bool(row[6]), created_at=row[7],
            last_sign_in_at=row[8], user_id=UUID(str(row[9])), password_login_enabled=bool(row[10]),
        )

    def ensure_initial_administrator(self, username: str, password_hash: str) -> Account:
        username = username.casefold()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO auth_accounts (username, user_id, display_name, email, password_hash, role)
                VALUES (%s, %s, %s, NULL, %s, 'administrator')
                ON CONFLICT (username) DO NOTHING
                """, (username, uuid4(), username, password_hash),
            )
        account = self.get_account(username)
        assert account is not None
        return account

    def get_account(self, username: str) -> Account | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT username, display_name, email, password_hash, role, active, password_change_required, created_at, last_sign_in_at, user_id, password_login_enabled FROM auth_accounts WHERE username = %s", (username.casefold(),))
            return self._account(cursor.fetchone())

    def get_account_by_user_id(self, user_id: UUID) -> Account | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT username, display_name, email, password_hash, role, active, password_change_required, created_at, last_sign_in_at, user_id, password_login_enabled FROM auth_accounts WHERE user_id = %s", (user_id,))
            return self._account(cursor.fetchone())

    def get_account_by_identity(self, identity: str) -> Account | None:
        identity = identity.strip().casefold()
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT username, display_name, email, password_hash, role, active, password_change_required, created_at, last_sign_in_at, user_id, password_login_enabled FROM auth_accounts WHERE username = %s OR email = %s", (identity, identity))
            return self._account(cursor.fetchone())

    def list_accounts(self) -> list[Account]:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT username, display_name, email, password_hash, role, active, password_change_required, created_at, last_sign_in_at, user_id, password_login_enabled FROM auth_accounts ORDER BY lower(display_name), username")
            return [self._account(row) for row in cursor.fetchall() if row is not None]

    def create_account(self, account: Account) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO auth_accounts (username, user_id, display_name, email, password_hash, role, active, password_change_required, password_login_enabled, created_at, last_sign_in_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", (account.username, account.user_id, account.display_name, account.email, account.password_hash, account.role, account.active, account.password_change_required, account.password_login_enabled, account.created_at, account.last_sign_in_at))
        except psycopg.errors.UniqueViolation as error:
            raise ValueError("An account with that email already exists") from error

    def update_account(self, username: str, *, display_name: str, email: str | None, role: str, active: bool) -> Account:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE auth_accounts SET display_name = %s, email = %s, role = %s, active = %s WHERE username = %s RETURNING username, display_name, email, password_hash, role, active, password_change_required, created_at, last_sign_in_at, user_id, password_login_enabled", (display_name, email, role, active, username.casefold()))
                account = self._account(cursor.fetchone())
        except psycopg.errors.UniqueViolation as error:
            raise ValueError("An account with that email already exists") from error
        if account is None:
            raise KeyError(username)
        return account

    def set_account_password(self, username: str, password_hash: str, password_change_required: bool) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE auth_accounts SET password_hash = %s, password_change_required = %s WHERE username = %s", (password_hash, password_change_required, username.casefold()))

    def set_password_login_enabled(self, user_id: UUID, enabled: bool) -> Account | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE auth_accounts SET password_login_enabled=%s WHERE user_id=%s RETURNING username, display_name, email, password_hash, role, active, password_change_required, created_at, last_sign_in_at, user_id, password_login_enabled", (enabled, user_id))
            return self._account(cursor.fetchone())

    def record_sign_in(self, username: str) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE auth_accounts SET last_sign_in_at = CURRENT_TIMESTAMP WHERE username = %s", (username.casefold(),))

    def get_gallery_state(self, user_id: UUID) -> GalleryState | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT sort_order, anchor_id, anchor_offset FROM user_gallery_state WHERE user_id = %s",
                    (user_id,),
                )
                row = cursor.fetchone()
        return GalleryState(str(row[0]), str(row[1]) if row[1] else None, int(row[2])) if row else None

    def save_gallery_state(self, user_id: UUID, state: GalleryState) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO user_gallery_state (username, user_id, sort_order, anchor_id, anchor_offset)
                    SELECT username, user_id, %s, %s, %s FROM auth_accounts WHERE user_id = %s
                    ON CONFLICT (username) DO UPDATE SET
                        sort_order = EXCLUDED.sort_order,
                        anchor_id = EXCLUDED.anchor_id,
                        anchor_offset = EXCLUDED.anchor_offset,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (state.sort, state.anchor_id, state.anchor_offset, user_id),
                )

    def get_movie_progress(self, user_id: UUID, movie_id: str) -> MovieProgress | None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT position_seconds, duration_seconds, completed
                    FROM user_movie_progress WHERE user_id = %s AND movie_id = %s
                    """,
                    (user_id, movie_id),
                )
                row = cursor.fetchone()
        return MovieProgress(movie_id, float(row[0]), float(row[1]), bool(row[2])) if row else None

    def save_movie_progress(self, user_id: UUID, progress: MovieProgress) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO user_movie_progress
                        (username, user_id, movie_id, position_seconds, duration_seconds, completed)
                    SELECT username, user_id, %s, %s, %s, %s FROM auth_accounts WHERE user_id = %s
                    ON CONFLICT (username, movie_id) DO UPDATE SET
                        position_seconds = EXCLUDED.position_seconds,
                        duration_seconds = EXCLUDED.duration_seconds,
                        completed = EXCLUDED.completed,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        progress.movie_id, progress.position_seconds,
                        progress.duration_seconds, progress.completed, user_id,
                    ),
                )

    def get_episode_progress(self, user_id: UUID, episode_id: UUID) -> EpisodeProgress | None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """SELECT position_seconds, duration_seconds, completed FROM user_episode_progress
                   WHERE user_id=%s AND episode_id=%s""", (user_id, episode_id)
            )
            row = cursor.fetchone()
        return EpisodeProgress(episode_id, float(row[0]), float(row[1]), bool(row[2])) if row else None

    def save_episode_progress(self, user_id: UUID, progress: EpisodeProgress) -> None:
        with self._connect() as connection, connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO user_episode_progress
                   (user_id, episode_id, position_seconds, duration_seconds, completed)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (user_id, episode_id) DO UPDATE SET
                   position_seconds=EXCLUDED.position_seconds, duration_seconds=EXCLUDED.duration_seconds,
                   completed=EXCLUDED.completed, updated_at=CURRENT_TIMESTAMP""",
                (user_id, progress.episode_id, progress.position_seconds, progress.duration_seconds, progress.completed),
            )

    def reset(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM auth_security_events")
                cursor.execute("DELETE FROM auth_sessions")
                cursor.execute("DELETE FROM auth_login_attempts")
