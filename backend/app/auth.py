from datetime import datetime, timedelta, timezone
from functools import lru_cache
import json
import logging
import re
import secrets
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse

from app.auth_store import Account, AuthenticationStore, PostgresAuthenticationStore
from app.config import get_admin_password_hash, get_admin_username, get_database_conninfo, get_webauthn_origin, get_webauthn_rp_id
from app.passkeys import MemoryPasskeyStore, PasskeyCredential, PasskeyStore, PostgresPasskeyStore
from app.enrolment import EnrolmentPurpose, MemoryEnrolmentStore, PostgresEnrolmentStore
from app.security import hash_password, verify_password
from app.request_security import client_ip

router = APIRouter(prefix="/api/auth", tags=["authentication"])
logger = logging.getLogger("pv.auth")
SESSION_COOKIE_NAME = "pv_session"
SESSION_DURATION = timedelta(hours=12)
VAULT_CONTROL_ELEVATION_DURATION = timedelta(minutes=30)
PASSKEY_TIMEOUT_MS = 60_000
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=256)


class PasskeyVerifyRequest(BaseModel):
    challenge_id: UUID
    credential: dict[str, object]
    label: str | None = Field(default=None, max_length=80)


class EnrolmentTokenRequest(BaseModel):
    # This value is deliberately accepted only in the request body.  It is
    # never logged, returned, or persisted in its raw form.
    token: str = Field(min_length=32, max_length=512)


class EnrolmentPasskeyVerifyRequest(PasskeyVerifyRequest):
    token: str = Field(min_length=32, max_length=512)


class PasskeyCredentialSummary(BaseModel):
    id: UUID
    label: str | None
    created_at: datetime
    last_used_at: datetime | None
    authenticator_attachment: str | None


class SessionSummaryResponse(BaseModel):
    id: UUID
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime
    authentication_method: str | None
    client_ip: str | None
    user_agent: str | None
    vault_control_elevated: bool
    current: bool


class SecurityEventResponse(BaseModel):
    id: UUID
    event_type: str
    occurred_at: datetime
    authentication_method: str | None
    client_ip: str | None
    user_agent: str | None


@lru_cache
def get_authentication_store() -> AuthenticationStore:
    return PostgresAuthenticationStore(get_database_conninfo())


@lru_cache
def get_passkey_store() -> PasskeyStore:
    return PostgresPasskeyStore(get_database_conninfo())


@lru_cache
def get_enrolment_store():
    return PostgresEnrolmentStore(get_database_conninfo())


def _bootstrap(store: AuthenticationStore) -> Account:
    return store.ensure_initial_administrator(get_admin_username(), get_admin_password_hash())


class AuthenticatedIdentity(str):
    """Authenticated account context with legacy username string compatibility.

    Its string value is transitional audit/display data. Owner authorization
    must use ``user_id``.
    """

    user_id: UUID
    display_name: str
    role: str
    active: bool

    def __new__(cls, account: Account) -> "AuthenticatedIdentity":
        value = super().__new__(cls, account.username)
        value.user_id = account.user_id
        value.display_name = account.display_name
        value.role = account.role
        value.active = account.active
        return value

    def __reduce__(self):
        # Copies used in audit/request payloads retain only the legacy string
        # representation; authorization has already used the live UUID.
        return (str, (str(self),))


def require_authenticated_user(request: Request, store: AuthenticationStore = Depends(get_authentication_store)) -> AuthenticatedIdentity:
    _bootstrap(store)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user_id = store.get_session_user_id(token) if token else None
    account = store.get_account_by_user_id(user_id) if user_id else None
    if not account or not account.active:
        if token:
            store.delete_session(token)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if account.password_change_required and request.url.path != "/api/auth/change-password":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="A password change is required before accessing Personal Vault",
        )
    return AuthenticatedIdentity(account)


AuthenticatedUsername = Annotated[AuthenticatedIdentity, Depends(require_authenticated_user)]


def authenticated_user_id(identity: object) -> UUID:
    """Return a usable immutable identity; compatibility strings never authorize."""
    user_id = getattr(identity, "user_id", None)
    if not isinstance(user_id, UUID):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return user_id


def require_current_user(identity: AuthenticatedUsername, store: AuthenticationStore = Depends(get_authentication_store)) -> Account:
    """Re-load the active account by its immutable authenticated identity."""
    account = store.get_account_by_user_id(authenticated_user_id(identity))
    if not account or not account.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    return account


AuthenticatedUser = Annotated[Account, Depends(require_current_user)]


def require_vault_control_administrator(
    identity: AuthenticatedUsername,
    store: AuthenticationStore = Depends(get_authentication_store),
) -> AuthenticatedIdentity:
    # The session's username is a transitional lookup key only. Re-check the
    # current role and active state against the immutable authenticated ID.
    try:
        user_id = authenticated_user_id(identity)
    except HTTPException as error:
        raise HTTPException(status_code=403, detail="Vault Control administrator access is required") from error
    account = store.get_account_by_user_id(user_id)
    if not account or not account.active or account.role != "administrator":
        raise HTTPException(status_code=403, detail="Vault Control administrator access is required")
    return AuthenticatedIdentity(account)


AuthenticatedAdministrator = Annotated[
    AuthenticatedIdentity,
    Depends(require_vault_control_administrator),
]


def _vault_control_session_token(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=403, detail="Vault Control re-authentication is required")
    return token


def require_vault_control_elevated_administrator(
    request: Request,
    administrator: AuthenticatedAdministrator,
    store: AuthenticationStore = Depends(get_authentication_store),
) -> AuthenticatedIdentity:
    """Require current admin authority plus session-bound, idle-expiring VC elevation."""
    token = _vault_control_session_token(request)
    if not store.refresh_vault_control_elevation(
        token,
        administrator.user_id,
        datetime.now(timezone.utc) + VAULT_CONTROL_ELEVATION_DURATION,
    ):
        raise HTTPException(status_code=403, detail="Vault Control re-authentication is required")
    return administrator


ElevatedVaultControlAdministrator = Annotated[
    AuthenticatedIdentity,
    Depends(require_vault_control_elevated_administrator),
]


def _session_payload(account: Account) -> dict[str, object]:
    return {
        "authenticated": True,
        "username": account.username,
        "user_id": str(account.user_id),
        "display_name": account.display_name,
        "role": account.role,
        "password_change_required": account.password_change_required,
        "password_login_enabled": account.password_login_enabled,
    }


def _client_ip(request: Request) -> str:
    return client_ip(request)


def create_authenticated_session(
    account: Account,
    response: Response,
    store: AuthenticationStore,
    *,
    method: str,
    client_ip: str,
    user_agent: str | None = None,
) -> dict[str, object]:
    """Issue the normal Stage 2 session after an authentication method verifies a UUID."""
    if not account.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    store.record_sign_in(account.username)
    current = store.get_account_by_user_id(account.user_id)
    if current is None or not current.active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    token = secrets.token_urlsafe(32)
    store.create_session(token, current.user_id, current.username, datetime.now(timezone.utc) + SESSION_DURATION,
        authentication_method=method, client_ip=client_ip, user_agent=user_agent)
    store.record_security_event("sign_in_succeeded", user_id=current.user_id, authentication_method=method,
        client_ip=client_ip, user_agent=user_agent)
    response.set_cookie(key=SESSION_COOKIE_NAME, value=token, httponly=True, secure=True, samesite="lax", max_age=int(SESSION_DURATION.total_seconds()), path="/")
    logger.info("Successful %s authentication for user_id=%s ip=%s", method, current.user_id, client_ip)
    return {"status": "authenticated", **_session_payload(current)}


def _passkey_rate_limit_key(request: Request) -> str:
    return f"passkey:{_client_ip(request)}"


def _passkey_options(value: object, challenge_id: UUID) -> dict[str, object]:
    return {"challenge_id": str(challenge_id), "publicKey": json.loads(options_to_json(value))}


def _credential_raw_id(credential: dict[str, object]) -> bytes:
    raw_id = credential.get("rawId") or credential.get("id")
    if not isinstance(raw_id, str):
        raise ValueError("Passkey credential is malformed")
    return base64url_to_bytes(raw_id)


def _credential_user_handle(credential: dict[str, object]) -> bytes | None:
    response = credential.get("response")
    if not isinstance(response, dict):
        return None
    user_handle = response.get("userHandle")
    return base64url_to_bytes(user_handle) if isinstance(user_handle, str) and user_handle else None


@router.post("/login")
def login(credentials: LoginRequest, request: Request, response: Response, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    _bootstrap(store)
    client_ip = _client_ip(request)
    rate_limit_key = f"{credentials.username.casefold()}:{client_ip}"
    lockout_seconds = store.get_lockout_seconds(rate_limit_key)
    if lockout_seconds > 0:
        raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.", headers={"Retry-After": str(lockout_seconds)})
    account = store.get_account_by_identity(credentials.username)
    valid_password = bool(account and account.password_hash and verify_password(credentials.password, account.password_hash))
    if not account or not account.active or not valid_password or not account.password_login_enabled:
        if account is not None:
            store.record_security_event("sign_in_failed", user_id=account.user_id, authentication_method="password", client_ip=client_ip, user_agent=request.headers.get("user-agent"))
        retry_after = store.record_failed_attempt(rate_limit_key)
        if retry_after:
            raise HTTPException(status_code=429, detail="Too many failed login attempts. Try again later.", headers={"Retry-After": str(retry_after)})
        raise HTTPException(status_code=401, detail="Invalid username or password")
    store.clear_failed_attempts(rate_limit_key)
    return create_authenticated_session(account, response, store, method="password", client_ip=client_ip, user_agent=request.headers.get("user-agent"))


@router.post("/passkeys/registration/options")
def begin_passkey_registration(
    user: AuthenticatedUser,
    passkeys: PasskeyStore = Depends(get_passkey_store),
) -> dict[str, object]:
    challenge = secrets.token_bytes(32)
    ceremony = passkeys.create_challenge("registration", user.user_id, challenge)
    exclusions = [PublicKeyCredentialDescriptor(id=item.credential_id) for item in passkeys.list_credentials(user.user_id)]
    options = generate_registration_options(
        rp_id=get_webauthn_rp_id(), rp_name="Personal Vault", user_name=user.username,
        user_display_name=user.display_name, user_id=user.user_id.bytes, challenge=challenge,
        timeout=PASSKEY_TIMEOUT_MS, exclude_credentials=exclusions,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    logger.info("Passkey registration challenge issued for user_id=%s", user.user_id)
    return _passkey_options(options, ceremony.id)


@router.post("/passkeys/registration/verify")
def finish_passkey_registration(
    body: PasskeyVerifyRequest,
    user: AuthenticatedUser,
    passkeys: PasskeyStore = Depends(get_passkey_store),
    store: AuthenticationStore = Depends(get_authentication_store),
) -> PasskeyCredentialSummary:
    challenge = passkeys.consume_challenge(body.challenge_id, "registration", user.user_id)
    if challenge is None:
        logger.warning("Passkey registration rejected: missing, expired, or replayed challenge")
        raise HTTPException(status_code=400, detail="Passkey registration challenge is invalid or expired")
    try:
        verified = verify_registration_response(
            credential=body.credential, expected_challenge=challenge.challenge,
            expected_rp_id=get_webauthn_rp_id(), expected_origin=get_webauthn_origin(),
            require_user_verification=True,
        )
        transports = body.credential.get("response", {})
        transports = transports.get("transports", []) if isinstance(transports, dict) else []
        credential = PasskeyCredential(
            id=uuid4(), user_id=user.user_id, credential_id=verified.credential_id,
            public_key=verified.credential_public_key, sign_count=verified.sign_count,
            transports=tuple(item for item in transports if isinstance(item, str)),
            authenticator_attachment=str(body.credential.get("authenticatorAttachment")) if body.credential.get("authenticatorAttachment") else None,
            label=body.label.strip() if body.label and body.label.strip() else None,
            created_at=datetime.now(timezone.utc), last_used_at=None,
        )
        passkeys.create_credential(credential)
    except (InvalidRegistrationResponse, ValueError, TypeError) as error:
        logger.warning("Passkey registration verification failed for user_id=%s: %s", user.user_id, type(error).__name__)
        raise HTTPException(status_code=400, detail="Passkey registration could not be verified") from error
    logger.info("Passkey registered for user_id=%s credential_id=%s", user.user_id, credential.id)
    store.record_security_event("passkey_added", user_id=user.user_id, actor_user_id=user.user_id)
    return PasskeyCredentialSummary(id=credential.id, label=credential.label, created_at=credential.created_at, last_used_at=None, authenticator_attachment=credential.authenticator_attachment)


def _enrolment_invite_or_400(token: str, enrolment: object, purpose: EnrolmentPurpose = "initial_enrolment"):
    invite = getattr(enrolment, "validate")(token, purpose)
    if invite is None:
        # Keep all invalid states intentionally indistinguishable to an
        # unauthenticated caller (expired, replaced, consumed, or unknown).
        raise HTTPException(status_code=400, detail="This enrolment link is invalid or expired")
    return invite


def _enrolment_account_or_400(store: AuthenticationStore, user_id: UUID, purpose: EnrolmentPurpose = "initial_enrolment") -> Account:
    account = store.get_account_by_user_id(user_id)
    initial_ineligible = account is None or not account.active or account.password_hash is not None or account.password_login_enabled
    recovery_ineligible = account is None or not account.active or account.role == "administrator"
    if (purpose == "initial_enrolment" and initial_ineligible) or (purpose == "recovery_enrolment" and recovery_ineligible):
        raise HTTPException(status_code=400, detail="This enrolment link is invalid or expired")
    return account


def _registration_credential(
    body: PasskeyVerifyRequest,
    user_id: UUID,
    challenge: bytes,
) -> PasskeyCredential:
    verified = verify_registration_response(
        credential=body.credential,
        expected_challenge=challenge,
        expected_rp_id=get_webauthn_rp_id(),
        expected_origin=get_webauthn_origin(),
        require_user_verification=True,
    )
    response = body.credential.get("response", {})
    transports = response.get("transports", []) if isinstance(response, dict) else []
    return PasskeyCredential(
        id=uuid4(), user_id=user_id, credential_id=verified.credential_id,
        public_key=verified.credential_public_key, sign_count=verified.sign_count,
        transports=tuple(item for item in transports if isinstance(item, str)),
        authenticator_attachment=str(body.credential.get("authenticatorAttachment")) if body.credential.get("authenticatorAttachment") else None,
        label=body.label.strip() if body.label and body.label.strip() else None,
        created_at=datetime.now(timezone.utc), last_used_at=None,
    )


@router.post("/enrolment/status")
def enrolment_status(
    body: EnrolmentTokenRequest,
    store: AuthenticationStore = Depends(get_authentication_store),
    enrolment=Depends(get_enrolment_store),
) -> dict[str, object]:
    invite = _enrolment_invite_or_400(body.token, enrolment)
    account = _enrolment_account_or_400(store, invite.user_id)
    return {
        "display_name": account.display_name,
        "expires_at": invite.expires_at.isoformat(),
        "status": "pending_passkey_enrolment",
    }


@router.post("/enrolment/registration/options")
def begin_enrolment_passkey_registration(
    body: EnrolmentTokenRequest,
    store: AuthenticationStore = Depends(get_authentication_store),
    passkeys: PasskeyStore = Depends(get_passkey_store),
    enrolment=Depends(get_enrolment_store),
) -> dict[str, object]:
    invite = _enrolment_invite_or_400(body.token, enrolment)
    account = _enrolment_account_or_400(store, invite.user_id)
    challenge = secrets.token_bytes(32)
    ceremony = passkeys.create_challenge("registration", invite.user_id, challenge)
    options = generate_registration_options(
        rp_id=get_webauthn_rp_id(), rp_name="Personal Vault", user_name=account.username,
        user_display_name=account.display_name, user_id=invite.user_id.bytes, challenge=challenge,
        timeout=PASSKEY_TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    logger.info("Enrolment passkey registration challenge issued for user_id=%s", invite.user_id)
    return _passkey_options(options, ceremony.id)


@router.post("/enrolment/registration/verify")
def finish_enrolment_passkey_registration(
    body: EnrolmentPasskeyVerifyRequest,
    passkeys: PasskeyStore = Depends(get_passkey_store),
    enrolment=Depends(get_enrolment_store),
    store: AuthenticationStore = Depends(get_authentication_store),
) -> PasskeyCredentialSummary:
    invite = _enrolment_invite_or_400(body.token, enrolment)
    challenge = passkeys.consume_challenge(body.challenge_id, "registration", invite.user_id)
    if challenge is None:
        raise HTTPException(status_code=400, detail="Passkey registration challenge is invalid or expired")
    try:
        credential = _registration_credential(body, invite.user_id, challenge.challenge)
        if isinstance(enrolment, PostgresEnrolmentStore):
            if not enrolment.consume_and_create_credential(body.token, credential):
                raise ValueError("Enrolment link is invalid")
        elif isinstance(enrolment, MemoryEnrolmentStore):
            # The in-memory implementation is a test double only.  Production
            # persistence always uses the atomic PostgreSQL path above.
            if not enrolment.consume(invite.id):
                raise ValueError("Enrolment link is invalid")
            passkeys.create_credential(credential)
        else:
            raise ValueError("Unsupported enrolment store")
    except (InvalidRegistrationResponse, ValueError, TypeError, RuntimeError) as error:
        logger.warning("Enrolment passkey registration rejected for user_id=%s: %s", invite.user_id, type(error).__name__)
        raise HTTPException(status_code=400, detail="Passkey enrolment could not be completed") from error
    logger.info("Initial passkey enrolled for user_id=%s credential_id=%s", invite.user_id, credential.id)
    store.record_security_event("initial_passkey_enrolled", user_id=invite.user_id)
    return PasskeyCredentialSummary(id=credential.id, label=credential.label, created_at=credential.created_at, last_used_at=None, authenticator_attachment=credential.authenticator_attachment)


@router.post("/recovery/status")
def recovery_status(
    body: EnrolmentTokenRequest,
    store: AuthenticationStore = Depends(get_authentication_store),
    enrolment=Depends(get_enrolment_store),
) -> dict[str, object]:
    invite = _enrolment_invite_or_400(body.token, enrolment, "recovery_enrolment")
    account = _enrolment_account_or_400(store, invite.user_id, "recovery_enrolment")
    return {"display_name": account.display_name, "expires_at": invite.expires_at.isoformat(), "status": "pending_passkey_recovery"}


@router.post("/recovery/registration/options")
def begin_recovery_passkey_registration(
    body: EnrolmentTokenRequest,
    store: AuthenticationStore = Depends(get_authentication_store),
    passkeys: PasskeyStore = Depends(get_passkey_store),
    enrolment=Depends(get_enrolment_store),
) -> dict[str, object]:
    invite = _enrolment_invite_or_400(body.token, enrolment, "recovery_enrolment")
    account = _enrolment_account_or_400(store, invite.user_id, "recovery_enrolment")
    challenge = secrets.token_bytes(32)
    ceremony = passkeys.create_challenge("registration", invite.user_id, challenge, "recovery_enrolment")
    options = generate_registration_options(
        rp_id=get_webauthn_rp_id(), rp_name="Personal Vault", user_name=account.username,
        user_display_name=account.display_name, user_id=invite.user_id.bytes, challenge=challenge,
        timeout=PASSKEY_TIMEOUT_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
    )
    logger.info("Recovery passkey registration challenge issued for user_id=%s", invite.user_id)
    return _passkey_options(options, ceremony.id)


@router.post("/recovery/registration/verify")
def finish_recovery_passkey_registration(
    body: EnrolmentPasskeyVerifyRequest,
    passkeys: PasskeyStore = Depends(get_passkey_store),
    enrolment=Depends(get_enrolment_store),
    store: AuthenticationStore = Depends(get_authentication_store),
) -> PasskeyCredentialSummary:
    invite = _enrolment_invite_or_400(body.token, enrolment, "recovery_enrolment")
    challenge = passkeys.consume_challenge(body.challenge_id, "registration", invite.user_id, "recovery_enrolment")
    if challenge is None:
        raise HTTPException(status_code=400, detail="Passkey registration challenge is invalid or expired")
    try:
        credential = _registration_credential(body, invite.user_id, challenge.challenge)
        if isinstance(enrolment, PostgresEnrolmentStore):
            if not enrolment.consume_and_create_credential(body.token, credential, "recovery_enrolment"):
                raise ValueError("Recovery link is invalid")
        elif isinstance(enrolment, MemoryEnrolmentStore):
            if not enrolment.consume(invite.id):
                raise ValueError("Recovery link is invalid")
            passkeys.create_credential(credential)
        else:
            raise ValueError("Unsupported enrolment store")
    except (InvalidRegistrationResponse, ValueError, TypeError, RuntimeError) as error:
        logger.warning("Recovery passkey registration rejected for user_id=%s: %s", invite.user_id, type(error).__name__)
        raise HTTPException(status_code=400, detail="Passkey recovery could not be completed") from error
    logger.info("Recovery passkey enrolled for user_id=%s credential_id=%s", invite.user_id, credential.id)
    store.record_security_event("recovery_completed", user_id=invite.user_id)
    return PasskeyCredentialSummary(id=credential.id, label=credential.label, created_at=credential.created_at, last_used_at=None, authenticator_attachment=credential.authenticator_attachment)


@router.get("/passkeys", response_model=list[PasskeyCredentialSummary])
def list_passkeys(user: AuthenticatedUser, passkeys: PasskeyStore = Depends(get_passkey_store)) -> list[PasskeyCredentialSummary]:
    return [PasskeyCredentialSummary(id=item.id, label=item.label, created_at=item.created_at, last_used_at=item.last_used_at, authenticator_attachment=item.authenticator_attachment) for item in passkeys.list_credentials(user.user_id)]


@router.delete("/passkeys/{credential_id}")
def revoke_passkey(credential_id: UUID, user: AuthenticatedUser, passkeys: PasskeyStore = Depends(get_passkey_store), store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    credentials = passkeys.list_credentials(user.user_id)
    if not any(item.id == credential_id for item in credentials):
        raise HTTPException(status_code=404, detail="Passkey not found")
    if not user.password_login_enabled and len(credentials) <= 1:
        raise HTTPException(status_code=409, detail="Add another passkey before removing your final passkey. Account recovery is administrator-assisted.")
    if not passkeys.revoke_credential(credential_id, user.user_id):
        raise HTTPException(status_code=404, detail="Passkey not found")
    logger.info("Passkey revoked for user_id=%s credential_id=%s", user.user_id, credential_id)
    store.record_security_event("passkey_removed", user_id=user.user_id, actor_user_id=user.user_id)
    return {"status": "revoked"}


@router.post("/passkeys/authentication/options")
def begin_passkey_authentication(
    request: Request,
    store: AuthenticationStore = Depends(get_authentication_store),
    passkeys: PasskeyStore = Depends(get_passkey_store),
) -> dict[str, object]:
    _bootstrap(store)
    rate_key = _passkey_rate_limit_key(request)
    lockout = store.get_lockout_seconds(rate_key)
    if lockout:
        raise HTTPException(status_code=429, detail="Too many passkey attempts. Try again later.", headers={"Retry-After": str(lockout)})
    challenge = secrets.token_bytes(32)
    ceremony = passkeys.create_challenge("authentication", None, challenge)
    options = generate_authentication_options(rp_id=get_webauthn_rp_id(), challenge=challenge, timeout=PASSKEY_TIMEOUT_MS, user_verification=UserVerificationRequirement.REQUIRED)
    logger.info("Passkey authentication challenge issued ip=%s", _client_ip(request))
    return _passkey_options(options, ceremony.id)


@router.post("/passkeys/authentication/verify")
def finish_passkey_authentication(
    body: PasskeyVerifyRequest,
    request: Request,
    response: Response,
    store: AuthenticationStore = Depends(get_authentication_store),
    passkeys: PasskeyStore = Depends(get_passkey_store),
) -> dict[str, object]:
    _bootstrap(store)
    rate_key = _passkey_rate_limit_key(request)
    if store.get_lockout_seconds(rate_key):
        raise HTTPException(status_code=429, detail="Too many passkey attempts. Try again later.")
    challenge = passkeys.consume_challenge(body.challenge_id, "authentication", None)
    if challenge is None:
        raise HTTPException(status_code=400, detail="Passkey authentication challenge is invalid or expired")
    try:
        credential = passkeys.get_credential(_credential_raw_id(body.credential))
        if credential is None:
            raise ValueError("Unknown passkey")
        user_handle = _credential_user_handle(body.credential)
        if user_handle is not None and user_handle != credential.user_id.bytes:
            raise ValueError("Passkey user handle does not match credential owner")
        account = store.get_account_by_user_id(credential.user_id)
        if account is None or not account.active:
            raise ValueError("Passkey account is unavailable")
        verified = verify_authentication_response(
            credential=body.credential, expected_challenge=challenge.challenge,
            expected_rp_id=get_webauthn_rp_id(), expected_origin=get_webauthn_origin(),
            credential_public_key=credential.public_key, credential_current_sign_count=credential.sign_count,
            require_user_verification=True,
        )
        passkeys.record_authentication(credential.id, verified.new_sign_count)
    except (InvalidAuthenticationResponse, ValueError, TypeError) as error:
        retry_after = store.record_failed_attempt(rate_key)
        logger.warning("Passkey authentication verification failed ip=%s reason=%s", _client_ip(request), type(error).__name__)
        if retry_after:
            raise HTTPException(status_code=429, detail="Too many passkey attempts. Try again later.", headers={"Retry-After": str(retry_after)}) from error
        raise HTTPException(status_code=401, detail="Passkey authentication failed") from error
    store.clear_failed_attempts(rate_key)
    return create_authenticated_session(account, response, store, method="passkey", client_ip=_client_ip(request), user_agent=request.headers.get("user-agent"))


@router.get("/vault-control/elevation")
def vault_control_elevation_status(
    request: Request,
    administrator: AuthenticatedAdministrator,
    store: AuthenticationStore = Depends(get_authentication_store),
) -> dict[str, bool]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return {"elevated": bool(token and store.has_vault_control_elevation(token, administrator.user_id))}


@router.post("/vault-control/elevation/options")
def begin_vault_control_elevation(
    request: Request,
    administrator: AuthenticatedAdministrator,
    store: AuthenticationStore = Depends(get_authentication_store),
    passkeys: PasskeyStore = Depends(get_passkey_store),
) -> dict[str, object]:
    rate_key = _passkey_rate_limit_key(request)
    lockout = store.get_lockout_seconds(rate_key)
    if lockout:
        raise HTTPException(status_code=429, detail="Too many passkey attempts. Try again later.", headers={"Retry-After": str(lockout)})
    credentials = passkeys.list_credentials(administrator.user_id)
    if not credentials:
        raise HTTPException(status_code=400, detail="An active passkey is required for Vault Control")
    challenge = secrets.token_bytes(32)
    ceremony = passkeys.create_challenge(
        "authentication", administrator.user_id, challenge, purpose="vault_control_step_up"
    )
    options = generate_authentication_options(
        rp_id=get_webauthn_rp_id(), challenge=challenge, timeout=PASSKEY_TIMEOUT_MS,
        allow_credentials=[PublicKeyCredentialDescriptor(id=item.credential_id) for item in credentials],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    logger.info("Vault Control step-up challenge issued for user_id=%s", administrator.user_id)
    return _passkey_options(options, ceremony.id)


@router.post("/vault-control/elevation/verify")
def finish_vault_control_elevation(
    body: PasskeyVerifyRequest,
    request: Request,
    administrator: AuthenticatedAdministrator,
    store: AuthenticationStore = Depends(get_authentication_store),
    passkeys: PasskeyStore = Depends(get_passkey_store),
) -> dict[str, bool]:
    rate_key = _passkey_rate_limit_key(request)
    if store.get_lockout_seconds(rate_key):
        raise HTTPException(status_code=429, detail="Too many passkey attempts. Try again later.")
    challenge = passkeys.consume_challenge(
        body.challenge_id, "authentication", administrator.user_id, purpose="vault_control_step_up"
    )
    if challenge is None:
        raise HTTPException(status_code=400, detail="Vault Control authentication challenge is invalid or expired")
    try:
        credential = passkeys.get_credential(_credential_raw_id(body.credential))
        if credential is None or credential.user_id != administrator.user_id:
            raise ValueError("Passkey does not belong to the current administrator")
        user_handle = _credential_user_handle(body.credential)
        if user_handle is not None and user_handle != administrator.user_id.bytes:
            raise ValueError("Passkey user handle does not match current administrator")
        verified = verify_authentication_response(
            credential=body.credential, expected_challenge=challenge.challenge,
            expected_rp_id=get_webauthn_rp_id(), expected_origin=get_webauthn_origin(),
            credential_public_key=credential.public_key, credential_current_sign_count=credential.sign_count,
            require_user_verification=True,
        )
        passkeys.record_authentication(credential.id, verified.new_sign_count)
        if not store.elevate_vault_control_session(
            _vault_control_session_token(request), administrator.user_id,
            datetime.now(timezone.utc) + VAULT_CONTROL_ELEVATION_DURATION,
        ):
            raise ValueError("Normal session is no longer valid")
    except (InvalidAuthenticationResponse, ValueError, TypeError) as error:
        retry_after = store.record_failed_attempt(rate_key)
        logger.warning("Vault Control step-up failed for user_id=%s reason=%s", administrator.user_id, type(error).__name__)
        if retry_after:
            raise HTTPException(status_code=429, detail="Too many passkey attempts. Try again later.", headers={"Retry-After": str(retry_after)}) from error
        raise HTTPException(status_code=401, detail="Vault Control identity confirmation failed") from error
    store.clear_failed_attempts(rate_key)
    logger.info("Vault Control session elevated for user_id=%s", administrator.user_id)
    return {"elevated": True}


@router.get("/session")
def get_session(request: Request, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    _bootstrap(store)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user_id = store.get_session_user_id(token) if token else None
    account = store.get_account_by_user_id(user_id) if user_id else None
    if not account or not account.active:
        return {
            "authenticated": False,
            "username": None,
            "display_name": None,
            "role": None,
            "password_change_required": False,
        }
    return _session_payload(account)


@router.get("/me")
def get_current_user(username: AuthenticatedUsername, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, object]:
    account = store.get_account(username)
    assert account is not None
    return _session_payload(account)


@router.get("/sessions", response_model=list[SessionSummaryResponse])
def list_my_sessions(request: Request, user: AuthenticatedUser, store: AuthenticationStore = Depends(get_authentication_store)) -> list[SessionSummaryResponse]:
    return [SessionSummaryResponse(**item.__dict__) for item in store.list_active_sessions(user.user_id, request.cookies.get(SESSION_COOKIE_NAME))]


@router.delete("/sessions/{session_id}")
def revoke_my_session(session_id: UUID, request: Request, user: AuthenticatedUser, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    current = next((item for item in store.list_active_sessions(user.user_id, request.cookies.get(SESSION_COOKIE_NAME)) if item.id == session_id and item.current), None)
    if current:
        raise HTTPException(status_code=409, detail="Use normal sign out for this session")
    if not store.revoke_session_for_user_id(user.user_id, session_id):
        raise HTTPException(status_code=404, detail="Session not found")
    store.record_security_event("session_revoked", user_id=user.user_id, actor_user_id=user.user_id)
    return {"status": "revoked"}


@router.post("/sessions/sign-out-others")
def sign_out_other_sessions(request: Request, user: AuthenticatedUser, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    store.delete_other_sessions_for_user_id(user.user_id, token)
    store.record_security_event("other_sessions_revoked", user_id=user.user_id, actor_user_id=user.user_id)
    return {"status": "revoked"}


@router.get("/security-events", response_model=list[SecurityEventResponse])
def list_my_security_events(user: AuthenticatedUser, store: AuthenticationStore = Depends(get_authentication_store)) -> list[SecurityEventResponse]:
    return [SecurityEventResponse(id=item.id, event_type=item.event_type, occurred_at=item.occurred_at,
        authentication_method=item.authentication_method, client_ip=item.client_ip, user_agent=item.user_agent)
        for item in store.list_security_events(user.user_id)]


@router.post("/change-password")
def change_password(body: ChangePasswordRequest, request: Request, username: AuthenticatedUsername, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    store.set_account_password(username, hash_password(body.password), False)
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        store.delete_other_sessions_for_user_id(username.user_id, token)
    store.record_security_event("password_changed", user_id=username.user_id, actor_user_id=username.user_id)
    return {"status": "password_changed"}


@router.post("/logout")
def logout(request: Request, response: Response, store: AuthenticationStore = Depends(get_authentication_store)) -> dict[str, str]:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        user_id = store.get_session_user_id(token)
        store.delete_session(token)
        if user_id:
            store.record_security_event("signed_out", user_id=user_id)
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/", secure=True, httponly=True, samesite="lax")
    return {"status": "logged_out"}
