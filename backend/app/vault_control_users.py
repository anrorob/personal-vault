"""Stage 5 account administration.  This intentionally contains no sharing model."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from app.auth import AuthenticatedAdministrator, get_authentication_store, get_enrolment_store, get_passkey_store, require_vault_control_elevated_administrator
from app.auth_store import Account, AuthenticationStore
from app.enrolment import MemoryEnrolmentStore, PostgresEnrolmentStore
from app.passkeys import PasskeyStore
from app.config import get_database_conninfo
from app.security import hash_password

router = APIRouter(prefix="/api/vault-control/users", tags=["vault-control-users"], dependencies=[Depends(require_vault_control_elevated_administrator)])


def _email(value: str) -> str:
    value = value.strip().casefold()
    if "@" not in value or value.startswith("@") or value.endswith("@"):
        raise ValueError("A valid email address is required")
    return value


class CreateUserRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    email: str
    role: str
    temporary_password: str | None = Field(default=None, min_length=8, max_length=256)
    passkey_first: bool = True

    _normalise_email = field_validator("email")(_email)

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        if value not in {"administrator", "user"}:
            raise ValueError("Role must be administrator or user")
        return value


class UpdateUserRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    email: str
    role: str
    active: bool

    _normalise_email = field_validator("email")(_email)

    @field_validator("role")
    @classmethod
    def valid_role(cls, value: str) -> str:
        if value not in {"administrator", "user"}:
            raise ValueError("Role must be administrator or user")
        return value


class ResetPasswordRequest(BaseModel):
    temporary_password: str = Field(min_length=8, max_length=256)


class AuthenticationPolicyRequest(BaseModel):
    password_login_enabled: bool


class SessionActionResponse(BaseModel):
    status: str


def _usage(user_id: UUID) -> int | None:
    """Asset ownership is the only accepted storage attribution source."""
    try:
        with psycopg.connect(get_database_conninfo()) as connection, connection.cursor() as cursor:
            # vault_assets has no physical byte count, so it cannot safely answer this.
            # The legacy vault_files table has a size only when it is reliably linked to owner metadata.
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'vault_files' AND column_name = 'size_bytes'
                )
            """)
            if not bool(cursor.fetchone()[0]):
                return None
            cursor.execute("""
                SELECT COALESCE(SUM(file.size_bytes), 0)::bigint
                FROM vault_files AS file
                JOIN vault_assets AS asset ON asset.id = file.asset_id
                WHERE asset.owner_user_id = %s
            """, (user_id,))
            row = cursor.fetchone()
            return int(row[0]) if row else None
    except (OSError, RuntimeError, psycopg.Error):
        return None


def _item(account: Account, passkeys: PasskeyStore | None = None, enrolment: object | None = None) -> dict[str, object]:
    credentials = passkeys.list_credentials(account.user_id) if passkeys else []
    recovery = getattr(enrolment, "active_for_user", lambda *_: None)(account.user_id, "recovery_enrolment") if enrolment else None
    return {
        "user_id": str(account.user_id),
        "username": account.username,
        "display_name": account.display_name,
        "email": account.email,
        "role": account.role,
        "active": account.active,
        "authentication_methods": (
            (["Passkey"] if credentials else [])
            + (["Password"] if account.password_login_enabled else [])
        ),
        "passkeys_available": bool(credentials),
        "passkeys_active_count": len(credentials),
        "password_login_enabled": account.password_login_enabled,
        "created_at": account.created_at.isoformat(),
        "last_sign_in_at": account.last_sign_in_at.isoformat() if account.last_sign_in_at else None,
        "password_change_required": account.password_change_required,
        "storage_used_bytes": _usage(account.user_id),
        "recovery_pending": recovery is not None,
        "recovery_expires_at": recovery.expires_at.isoformat() if recovery else None,
    }


def _invite_url(token: str) -> str:
    from app.config import get_webauthn_origin
    return f"{get_webauthn_origin()}/enrol/{token}"


def _recovery_url(token: str) -> str:
    from app.config import get_webauthn_origin
    return f"{get_webauthn_origin()}/recover/{token}"


def _account_or_404(store: AuthenticationStore, username: str) -> Account:
    account = store.get_account(username)
    if account is None:
        raise HTTPException(status_code=404, detail="User not found")
    return account


def _would_remove_final_admin(store: AuthenticationStore, account: Account, role: str, active: bool) -> bool:
    if account.role != "administrator" or not account.active or (role == "administrator" and active):
        return False
    return sum(item.active and item.role == "administrator" for item in store.list_accounts()) <= 1


@router.get("")
def list_users(response: Response, _: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store), passkeys: PasskeyStore = Depends(get_passkey_store), enrolment=Depends(get_enrolment_store)) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    accounts = store.list_accounts()
    return {"users": [_item(account, passkeys, enrolment) for account in accounts]}


@router.post("")
def create_user(body: CreateUserRequest, admin: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store), enrolment=Depends(get_enrolment_store)) -> dict[str, object]:
    username = body.email
    passkey_first = body.passkey_first and body.temporary_password is None
    if not passkey_first and body.temporary_password is None:
        raise HTTPException(status_code=422, detail="A temporary password is required for password-capable users")
    try:
        store.create_account(Account(username, body.display_name.strip(), body.email, None if passkey_first else hash_password(body.temporary_password), body.role, True, not passkey_first, datetime.now(timezone.utc), None, password_login_enabled=not passkey_first))
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    account = _account_or_404(store, username)
    if passkey_first:
        invite, token = enrolment.create(account.user_id, admin.user_id)
        return {**_item(account), "enrolment_url": _invite_url(token), "enrolment_expires_at": invite.expires_at.isoformat()}
    return _item(account)


@router.post("/{user_id}/enrolment-invite")
def regenerate_enrolment_invite(user_id: UUID, admin: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store), passkeys: PasskeyStore = Depends(get_passkey_store), enrolment=Depends(get_enrolment_store)) -> dict[str, object]:
    account = store.get_account_by_user_id(user_id)
    if (
        account is None
        or not account.active
        or account.password_hash is not None
        or account.password_login_enabled
        or passkeys.list_credentials(user_id)
    ):
        raise HTTPException(status_code=409, detail="This user is not eligible for initial passkey enrolment")
    invite, token = enrolment.create(user_id, admin.user_id)
    return {"enrolment_url": _invite_url(token), "enrolment_expires_at": invite.expires_at.isoformat()}


@router.post("/{user_id}/recovery")
def begin_recovery(user_id: UUID, admin: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store), passkeys: PasskeyStore = Depends(get_passkey_store), enrolment=Depends(get_enrolment_store)) -> dict[str, object]:
    """Revoke only this ordinary user's active access and issue recovery enrolment authority."""
    account = store.get_account_by_user_id(user_id)
    if account is None or not account.active or account.role == "administrator":
        raise HTTPException(status_code=409, detail="This user is not eligible for administrator-assisted recovery")
    if isinstance(enrolment, PostgresEnrolmentStore):
        result = enrolment.create_recovery(user_id, admin.user_id)
        if result is None:
            raise HTTPException(status_code=409, detail="This user is not eligible for administrator-assisted recovery")
        invite, token = result
    elif isinstance(enrolment, MemoryEnrolmentStore):
        # Test-double equivalent of the production transaction above.
        for credential in passkeys.list_credentials(user_id):
            passkeys.revoke_credential(credential.id, user_id)
        store.delete_sessions_for_user_id(user_id)
        invite, token = enrolment.create(user_id, admin.user_id, "recovery_enrolment")
    else:
        raise HTTPException(status_code=500, detail="Recovery store is unavailable")
    store.record_security_event("recovery_started", user_id=user_id, actor_user_id=admin.user_id)
    return {"recovery_url": _recovery_url(token), "recovery_expires_at": invite.expires_at.isoformat()}


@router.get("/{username}")
def get_user(username: str, response: Response, _: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    response.headers["Cache-Control"] = "private, no-store"
    return _item(_account_or_404(store, username))


@router.put("/{username}")
def update_user(username: str, body: UpdateUserRequest, admin: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    account = _account_or_404(store, username)
    if _would_remove_final_admin(store, account, body.role, body.active):
        raise HTTPException(status_code=409, detail="This user cannot be disabled or demoted because Personal Vault must retain at least one active Administrator.")
    try:
        account = store.update_account(username, display_name=body.display_name.strip(), email=body.email, role=body.role, active=body.active)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    if not account.active or account.role != "administrator":
        store.delete_sessions_for_user_id(account.user_id)
        store.record_security_event("account_sessions_revoked", user_id=account.user_id, actor_user_id=admin.user_id)
    return _item(account)


@router.post("/{username}/reset-password")
def reset_password(username: str, body: ResetPasswordRequest, admin: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    account = _account_or_404(store, username)
    store.set_account_password(account.username, hash_password(body.temporary_password), True)
    store.delete_sessions_for_user_id(account.user_id)
    store.record_security_event("password_reset_by_administrator", user_id=account.user_id, actor_user_id=admin.user_id)
    return {"status": "password_reset"}


@router.put("/{user_id}/authentication-policy")
def update_authentication_policy(user_id: UUID, body: AuthenticationPolicyRequest, admin: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store), passkeys: PasskeyStore = Depends(get_passkey_store)) -> dict[str, object]:
    account = store.get_account_by_user_id(user_id)
    if account is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not body.password_login_enabled and not passkeys.list_credentials(user_id):
        raise HTTPException(status_code=409, detail="Password sign-in cannot be disabled until this user has an active passkey.")
    updated = store.set_password_login_enabled(user_id, body.password_login_enabled)
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    store.record_security_event("password_sign_in_policy_changed", user_id=user_id, actor_user_id=admin.user_id, metadata={"enabled": str(body.password_login_enabled).lower()})
    return _item(updated, passkeys)


@router.get("/{user_id}/sessions")
def list_user_sessions(user_id: UUID, request: Request, _: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    if store.get_account_by_user_id(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"sessions": [
        {"id": str(item.id), "created_at": item.created_at, "last_seen_at": item.last_seen_at,
         "expires_at": item.expires_at, "authentication_method": item.authentication_method,
         "client_ip": item.client_ip, "user_agent": item.user_agent,
         "vault_control_elevated": item.vault_control_elevated, "current": item.current}
        for item in store.list_active_sessions(user_id, request.cookies.get("pv_session"))
    ]}


@router.delete("/{user_id}/sessions/{session_id}", response_model=SessionActionResponse)
def revoke_user_session(user_id: UUID, session_id: UUID, admin: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    if not store.revoke_session_for_user_id(user_id, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    store.record_security_event("session_revoked_by_administrator", user_id=user_id, actor_user_id=admin.user_id)
    return {"status": "revoked"}


@router.post("/{user_id}/sessions/revoke-all", response_model=SessionActionResponse)
def revoke_all_user_sessions(user_id: UUID, admin: AuthenticatedAdministrator, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    if store.get_account_by_user_id(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    store.delete_sessions_for_user_id(user_id)
    store.record_security_event("all_sessions_revoked_by_administrator", user_id=user_id, actor_user_id=admin.user_id)
    return {"status": "revoked"}
