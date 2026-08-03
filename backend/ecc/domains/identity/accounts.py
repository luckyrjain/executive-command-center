"""Phase 8 Task 1: account/membership/session framework -- `POST /identity/
accounts`, `POST /identity/auth/login`, `POST /identity/auth/select-
workspace`, `GET|POST /identity/workspaces`, `GET|PATCH /identity/
workspaces/{id}` (`docs/phases/phase-008/API-SCHEMAS.md`; that document's
own shorthand omits the `/api/v1/identity` prefix, matching every prior
phase's own `API-SCHEMAS.md` convention -- `ecc.domains.identity.person_
organizations` already claims `/api/v1/identity` for an unrelated PKOS
concern, this module adds sibling routes under the same domain prefix).

`docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md` Decision 1.
`accounts` (new, workspace-independent) is what a person authenticates as;
`users` (unchanged FK role, migration `0061`) anchors a specific account's
presence within one workspace; `workspace_memberships` carries that
presence's mutable role/status. Argon2id (`RFC-005.md`'s already-approved
algorithm, `argon2-cffi`, present in `pyproject.toml` since before this
task but never yet exercised by any code path) is this module's own first
real caller.

**Invitation-gated as of Task 2.** `POST /accounts` now requires a valid,
matching, unresolved `invitations.token` (query parameter) -- see
`ecc.domains.identity.invitations`' own module docstring for the full
acceptance-path reasoning (why this single call registers the account
*and* completes acceptance in one transaction, rather than requiring a
separate authenticated `/accept` call a brand-new recipient could not yet
make). Self-registration with no invitation at all is closed; creating a
workspace with no invitation still works, but only for an already-
authenticated account (`POST /workspaces`), which now itself requires
having gone through an invitation first.

**No endpoint in this module carries `Idempotency-Key`, unlike every
workspace-scoped mutating route elsewhere in this codebase -- for two
different reasons, not one.** `POST /accounts` and `POST /auth/login` are
pre-auth: `idempotency_records` is keyed on `(workspace_id, actor_id)`,
which does not exist yet at either call site, and unlike a domain resource,
a repeated login is not a duplication bug to guard against -- creating a
second valid session for the same account is ordinary multi-device use, not
an error (`accounts.email`'s own `UNIQUE` constraint is what makes
`POST /accounts` safe to retry). `POST /workspaces` is authenticated but
still skips it deliberately (see its own docstring below -- a mistaken
second workspace is a visible, correctable user action, not a silent
duplicate-side-effect risk); `PATCH /workspaces/{id}` needs it least of
all -- a field-level `UPDATE` is naturally idempotent. `POST /workspaces`
and `PATCH /workspaces/{id}` **do** carry CSRF protection, like every other
authenticated mutation in this codebase.

**The post-login workspace-selection step is a short-lived, stateless,
signed token, not server-side session state.** An account with more than
one active membership cannot be routed straight into a session (which
workspace?) -- `POST /auth/login` returns a `pending_login_token`
(HMAC-SHA256 over `account_id` + a 5-minute expiry, using the same
`session_secret` `require_csrf`'s own double-submit HMAC already uses,
reused rather than reinvented) instead of setting a cookie. `POST /auth/
select-workspace` accepts either that token (finishing an in-progress
login) or an already-established session (switching an existing session to
a different workspace the same account belongs to, CSRF-protected like any
other authenticated mutation) -- one endpoint, two ways to prove "which
account," the same shape `require_auth_context` already tolerates a plain
function call outside its own FastAPI dependency wiring.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import (
    AuthContext,
    AuthDep,
    CsrfDep,
    CsrfHeader,
    SessionCookie,
    require_auth_context,
    require_csrf,
)
from ecc.config import get_settings
from ecc.database import get_session
from ecc.observability import queue_lifecycle_event, record_audit_outbox_failure

router = APIRouter(prefix="/api/v1/identity", tags=["identity"])
SessionDep = Annotated[Session, Depends(get_session)]

_password_hasher = PasswordHasher()
_SESSION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_PENDING_LOGIN_TOKEN_MAX_AGE_SECONDS = 5 * 60
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# A fixed hash to verify a login attempt against when no account matches the
# submitted email, so a nonexistent-email attempt pays the same Argon2id
# verify cost as a wrong-password attempt against a real account -- without
# this, the two cases are distinguishable by response timing alone (a
# nonexistent email short-circuits to ~instant), letting a caller enumerate
# registered emails despite both returning the identical 401 body.
_DUMMY_PASSWORD_HASH = _password_hasher.hash(secrets.token_urlsafe(32))


def _normalize_email(email: str) -> str:
    return email.strip().casefold()


def _hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def _verify_password(password_hash: str, password: str) -> bool:
    try:
        _password_hasher.verify(password_hash, password)
    except VerifyMismatchError:
        return False
    return True


def _make_pending_login_token(account_id: UUID) -> str:
    expires_at = int(
        (datetime.now(UTC) + timedelta(seconds=_PENDING_LOGIN_TOKEN_MAX_AGE_SECONDS)).timestamp()
    )
    material = f"{account_id}:{expires_at}"
    signature = new(
        get_settings().session_secret.encode("utf-8"), material.encode("utf-8"), "sha256"
    ).hexdigest()
    return f"{material}:{signature}"


def _verify_pending_login_token(token: str) -> UUID | None:
    parts = token.split(":")
    if len(parts) != 3:
        return None
    account_id_raw, expires_at_raw, signature = parts
    material = f"{account_id_raw}:{expires_at_raw}"
    expected = new(
        get_settings().session_secret.encode("utf-8"), material.encode("utf-8"), "sha256"
    ).hexdigest()
    if not compare_digest(signature, expected):
        return None
    try:
        expires_at = int(expires_at_raw)
        account_id = UUID(account_id_raw)
    except ValueError:
        return None
    if datetime.now(UTC).timestamp() > expires_at:
        return None
    return account_id


def _set_session_cookies(response: Response, session_token: str, csrf_token: str) -> None:
    secure = get_settings().environment.strip().casefold() != "development"
    response.set_cookie(
        "ecc_session",
        session_token,
        max_age=_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        "ecc_csrf",
        csrf_token,
        max_age=_SESSION_MAX_AGE_SECONDS,
        httponly=False,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _create_session_for(
    session: Session, *, workspace_id: UUID, users_id: UUID, now: datetime
) -> tuple[str, str]:
    session_token = secrets.token_urlsafe(32)
    session_hash = sha256(session_token.encode("utf-8")).hexdigest()
    session.execute(
        text(
            """
            INSERT INTO sessions (id, workspace_id, user_id, token_hash, expires_at, last_seen_at)
            VALUES (:id, :workspace_id, :users_id, :token_hash, :expires_at, :now)
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "users_id": users_id,
            "token_hash": session_hash,
            "expires_at": now + timedelta(seconds=_SESSION_MAX_AGE_SECONDS),
            "now": now,
        },
    )
    csrf_token = new(
        get_settings().session_secret.encode("utf-8"), session_token.encode("utf-8"), "sha256"
    ).hexdigest()
    return session_token, csrf_token


def _optional_auth_context(
    request: Request,
    session: SessionDep,
    ecc_session: SessionCookie = None,
) -> AuthContext | None:
    if not ecc_session:
        return None
    try:
        return require_auth_context(request, session, ecc_session)
    except HTTPException:
        return None


OptionalAuthDep = Annotated[AuthContext | None, Depends(_optional_auth_context)]


class _EmailPasswordField:
    @staticmethod
    def validate_email(value: str) -> str:
        normalized = _normalize_email(value)
        if not _EMAIL_PATTERN.match(normalized) or len(normalized) > 320:
            raise ValueError("EMAIL_INVALID")
        return normalized


class AccountCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str = Field(min_length=8, max_length=256)
    display_name: str = Field(min_length=1, max_length=200)

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _EmailPasswordField.validate_email(value)


class AccountResponse(BaseModel):
    id: UUID
    email: str
    display_name: str
    created_at: datetime
    workspace_id: UUID
    role: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        return _EmailPasswordField.validate_email(value)


class WorkspaceOption(BaseModel):
    workspace_id: UUID
    name: str
    role: str


class LoginAuthenticated(BaseModel):
    status: str = "authenticated"
    workspace_id: UUID


class LoginSelectWorkspace(BaseModel):
    status: str = "select_workspace"
    pending_login_token: str
    workspaces: list[WorkspaceOption]


class SelectWorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workspace_id: UUID
    pending_login_token: str | None = None


class WorkspaceCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=64)


class WorkspacePatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    timezone: str | None = Field(default=None, min_length=1, max_length=64)


class WorkspaceResponse(BaseModel):
    id: UUID
    name: str
    timezone: str
    role: str
    created_at: datetime
    # Phase 8 Task 9's own addition -- `GET /workspaces` is the only call a
    # multi-workspace account's frontend can make to discover its own
    # `auth.workspace_id` (no `/me`/`/whoami` endpoint exists, and every
    # other Task 9 panel's URL needs that id). Computed server-side by
    # comparing each row's own `id` against the session's `auth.workspace_id`
    # -- never trust a client-supplied value for "which one is current."
    current: bool


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceResponse]


# ---------------------------------------------------------------------------
# POST /identity/accounts
# ---------------------------------------------------------------------------


@router.post("/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED)
def create_account_endpoint(
    payload: AccountCreateRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    token: str = Query(...),
) -> AccountResponse:
    """Registration and invitation acceptance in one transaction -- see this
    module's own top docstring and `ecc.domains.identity.invitations`' for
    why a brand-new recipient has no other way to reach an authenticated
    `/accept` call. `email` must case-insensitively match the invitation's
    own recipient email; a token for someone else's inbox never lets a
    caller register their own different address against it.
    """
    now = datetime.now(UTC)
    token_hash = sha256(token.encode("utf-8")).hexdigest()
    account_id = uuid4()
    with session.begin():
        invitation = (
            session.execute(
                text(
                    """
                    SELECT id, workspace_id, email, role, invited_by, expires_at,
                           accepted_at, rejected_at, revoked_at
                    FROM invitations
                    WHERE token_hash = :token_hash
                    FOR UPDATE
                    """
                ),
                {"token_hash": token_hash},
            )
            .mappings()
            .one_or_none()
        )
        if invitation is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="INVITATION_TOKEN_INVALID"
            )
        if invitation["expires_at"] <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="INVITATION_EXPIRED"
            )
        if (
            invitation["accepted_at"] is not None
            or invitation["rejected_at"] is not None
            or invitation["revoked_at"] is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="INVITATION_ALREADY_RESOLVED"
            )
        if payload.email != invitation["email"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="INVITATION_EMAIL_MISMATCH"
            )

        # See `invitations.py`'s own `accept_invitation_endpoint` for why:
        # found in the third whole-phase review, the identical stale-
        # authority gap exists on this brand-new-recipient path too -- an
        # `owner`-role invitation is never re-verified against its inviter's
        # current membership at this, the *other* accept call site.
        if invitation["role"] == "owner":
            inviter_membership = (
                session.execute(
                    text(
                        "SELECT role FROM workspace_memberships "
                        "WHERE workspace_id = :workspace_id AND users_id = :users_id "
                        "AND status = 'active'"
                    ),
                    {
                        "workspace_id": invitation["workspace_id"],
                        "users_id": invitation["invited_by"],
                    },
                )
                .mappings()
                .one_or_none()
            )
            if inviter_membership is None or inviter_membership["role"] != "owner":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="INVITER_NO_LONGER_AUTHORIZED"
                )

        try:
            session.execute(
                text(
                    """
                    INSERT INTO accounts (id, email, password_hash, display_name, created_at)
                    VALUES (:id, :email, :password_hash, :display_name, :created_at)
                    """
                ),
                {
                    "id": account_id,
                    "email": payload.email,
                    "password_hash": _hash_password(payload.password),
                    "display_name": payload.display_name,
                    "created_at": now,
                },
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="EMAIL_ALREADY_REGISTERED"
            ) from exc

        workspace_id = invitation["workspace_id"]
        users_id = uuid4()
        session.execute(
            text(
                "INSERT INTO users (id, workspace_id, account_id, created_at) "
                "VALUES (:id, :workspace_id, :account_id, :now)"
            ),
            {"id": users_id, "workspace_id": workspace_id, "account_id": account_id, "now": now},
        )
        session.execute(
            text(
                """
                INSERT INTO workspace_memberships (
                    id, workspace_id, account_id, users_id, role, status,
                    invited_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :account_id, :users_id, :role, 'active',
                    :invited_by, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "account_id": account_id,
                "users_id": users_id,
                "role": invitation["role"],
                "invited_by": invitation["invited_by"],
                "now": now,
            },
        )
        session.execute(
            text("UPDATE invitations SET accepted_at = :now WHERE id = :id"),
            {"now": now, "id": invitation["id"]},
        )
        _write_identity_audit_event(
            session,
            request,
            workspace_id=workspace_id,
            actor_users_id=users_id,
            event_type="invitation.accepted",
            aggregate_type="invitation",
            aggregate_id=invitation["id"],
            now=now,
        )
        session_token, csrf_token = _create_session_for(
            session, workspace_id=workspace_id, users_id=users_id, now=now
        )
    _set_session_cookies(response, session_token, csrf_token)
    return AccountResponse(
        id=account_id,
        email=payload.email,
        display_name=payload.display_name,
        created_at=now,
        workspace_id=workspace_id,
        role=invitation["role"],
    )


# ---------------------------------------------------------------------------
# POST /identity/auth/login, POST /identity/auth/select-workspace
# ---------------------------------------------------------------------------


@router.post("/auth/login", response_model=LoginAuthenticated | LoginSelectWorkspace)
def login_endpoint(
    payload: LoginRequest, request: Request, response: Response, session: SessionDep
) -> LoginAuthenticated | LoginSelectWorkspace:
    account = (
        session.execute(
            text("SELECT id, password_hash, disabled_at FROM accounts WHERE email = :email"),
            {"email": payload.email},
        )
        .mappings()
        .one_or_none()
    )
    session.rollback()
    password_hash = account["password_hash"] if account is not None else _DUMMY_PASSWORD_HASH
    password_ok = _verify_password(password_hash, payload.password)
    if account is None or account["disabled_at"] is not None or not password_ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="INVALID_CREDENTIALS")

    memberships = (
        session.execute(
            text(
                """
                SELECT wm.workspace_id, wm.users_id, wm.role, w.name
                FROM workspace_memberships AS wm
                JOIN workspaces AS w ON w.id = wm.workspace_id
                WHERE wm.account_id = :account_id AND wm.status = 'active'
                ORDER BY w.created_at ASC
                """
            ),
            {"account_id": account["id"]},
        )
        .mappings()
        .all()
    )
    session.rollback()

    if not memberships:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="NO_ACTIVE_MEMBERSHIP")

    if len(memberships) == 1:
        chosen = memberships[0]
        now = datetime.now(UTC)
        with session.begin():
            session_token, csrf_token = _create_session_for(
                session, workspace_id=chosen["workspace_id"], users_id=chosen["users_id"], now=now
            )
        _set_session_cookies(response, session_token, csrf_token)
        return LoginAuthenticated(workspace_id=chosen["workspace_id"])

    return LoginSelectWorkspace(
        pending_login_token=_make_pending_login_token(account["id"]),
        workspaces=[
            WorkspaceOption(workspace_id=m["workspace_id"], name=m["name"], role=m["role"])
            for m in memberships
        ],
    )


@router.post("/auth/select-workspace", response_model=LoginAuthenticated)
def select_workspace_endpoint(
    payload: SelectWorkspaceRequest,
    request: Request,
    response: Response,
    session: SessionDep,
    auth: OptionalAuthDep,
    csrf_token: CsrfHeader = None,
    ecc_session: SessionCookie = None,
) -> LoginAuthenticated:
    if auth is not None:
        require_csrf(ecc_session=ecc_session, csrf_token=csrf_token)
        account_id_row = (
            session.execute(
                text(
                    "SELECT account_id FROM users "
                    "WHERE workspace_id = :workspace_id AND id = :users_id"
                ),
                {"workspace_id": auth.workspace_id, "users_id": auth.user_id},
            )
            .mappings()
            .one()
        )
        session.rollback()
        account_id = account_id_row["account_id"]
    elif payload.pending_login_token is not None:
        account_id = _verify_pending_login_token(payload.pending_login_token)
        if account_id is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="PENDING_LOGIN_TOKEN_INVALID"
            )
    else:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
        )

    membership = (
        session.execute(
            text(
                """
                SELECT users_id FROM workspace_memberships
                WHERE account_id = :account_id AND workspace_id = :workspace_id
                  AND status = 'active'
                """
            ),
            {"account_id": account_id, "workspace_id": payload.workspace_id},
        )
        .mappings()
        .one_or_none()
    )
    session.rollback()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")

    now = datetime.now(UTC)
    with session.begin():
        session_token, csrf_token_out = _create_session_for(
            session, workspace_id=payload.workspace_id, users_id=membership["users_id"], now=now
        )
    _set_session_cookies(response, session_token, csrf_token_out)
    return LoginAuthenticated(workspace_id=payload.workspace_id)


# ---------------------------------------------------------------------------
# GET|POST /identity/workspaces, GET|PATCH /identity/workspaces/{id}
# ---------------------------------------------------------------------------


def _write_identity_audit_event(
    session: Session,
    request: Request,
    *,
    workspace_id: UUID,
    actor_users_id: UUID | None,
    event_type: str,
    aggregate_id: UUID,
    now: datetime,
    aggregate_type: str = "workspace",
    version: int = 1,
) -> None:
    """Shared by every mutation in this `identity` domain package that
    writes an audit trail entry -- `aggregate_type` defaults to
    `'workspace'` for this module's own `workspace.created`/`workspace.
    updated` events, and `ecc.domains.identity.invitations` passes
    `aggregate_type='invitation'` for its own events rather than this
    module carrying a second, near-duplicate copy hardcoded to a different
    aggregate type. `actor_users_id` is `None` for the one case where no
    workspace-scoped actor identity exists yet -- `invitations.py`'s own
    `reject_invitation_endpoint`, where the caller is responding from a
    session scoped to a *different* workspace and has no `users` row in
    this event's own `workspace_id` at all; `audit_events.actor_id` is
    nullable and its composite FK is skipped when NULL for exactly this.
    """
    try:
        request_id = UUID(request.state.request_id)
        correlation_id = UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        request_id, correlation_id = uuid4(), uuid4()
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, :aggregate_type, :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "aggregate_version": version,
                "actor_id": actor_users_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "occurred_at": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_id, workspace_id, event_type, event_version,
                    correlation_id, payload, occurred_at, attempt_count
                ) VALUES (
                    :event_id, :workspace_id, :event_type_v1, 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": workspace_id,
                "event_type_v1": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps({"aggregate_id": str(aggregate_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("identity")
        raise
    queue_lifecycle_event(session, "identity", event_type, "allowed")


class MeResponse(BaseModel):
    account_id: UUID
    users_id: UUID
    workspace_id: UUID
    email: str
    display_name: str


@router.get("/me", response_model=MeResponse)
def get_me_endpoint(auth: AuthDep, session: SessionDep) -> MeResponse:
    """Phase 8 Task 9's own addition -- the frontend has no other way to
    learn the logged-in account's own `account_id` (needed by `Delegations
    Panel.tsx` to tell delegator from recipient, since `Delegation`
    responses only ever name both parties by `account_id`, never "is this
    me"). No prior task needed this: every earlier Phase 8 UI surface
    either has no notion of "which party am I" (`SharingReview.tsx`) or
    only needs the coarser `workspace_id`/`role` `WorkspaceResponse.current`
    already carries.
    """
    row = (
        session.execute(
            text(
                "SELECT u.account_id, a.email, a.display_name FROM users u "
                "JOIN accounts a ON a.id = u.account_id "
                "WHERE u.workspace_id = :workspace_id AND u.id = :users_id"
            ),
            {"workspace_id": auth.workspace_id, "users_id": auth.user_id},
        )
        .mappings()
        .one()
    )
    session.rollback()
    return MeResponse(
        account_id=row["account_id"],
        users_id=auth.user_id,
        workspace_id=auth.workspace_id,
        email=row["email"],
        display_name=row["display_name"],
    )


@router.get("/workspaces", response_model=WorkspaceListResponse)
def list_workspaces_endpoint(auth: AuthDep, session: SessionDep) -> WorkspaceListResponse:
    account_id_row = (
        session.execute(
            text(
                "SELECT account_id FROM users WHERE workspace_id = :workspace_id AND id = :users_id"
            ),
            {"workspace_id": auth.workspace_id, "users_id": auth.user_id},
        )
        .mappings()
        .one()
    )
    session.rollback()
    rows = (
        session.execute(
            text(
                """
                SELECT w.id, w.name, w.timezone, wm.role, w.created_at
                FROM workspace_memberships AS wm
                JOIN workspaces AS w ON w.id = wm.workspace_id
                WHERE wm.account_id = :account_id AND wm.status = 'active'
                ORDER BY w.created_at ASC
                """
            ),
            {"account_id": account_id_row["account_id"]},
        )
        .mappings()
        .all()
    )
    session.rollback()
    return WorkspaceListResponse(
        workspaces=[
            WorkspaceResponse(**dict(r), current=(r["id"] == auth.workspace_id)) for r in rows
        ]
    )


@router.get("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
def get_workspace_endpoint(
    workspace_id: UUID, auth: AuthDep, session: SessionDep
) -> WorkspaceResponse:
    if workspace_id != auth.workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
    row = (
        session.execute(
            text(
                """
                SELECT w.id, w.name, w.timezone, wm.role, w.created_at
                FROM workspace_memberships AS wm
                JOIN workspaces AS w ON w.id = wm.workspace_id
                WHERE w.id = :workspace_id AND wm.users_id = :users_id AND wm.status = 'active'
                """
            ),
            {"workspace_id": workspace_id, "users_id": auth.user_id},
        )
        .mappings()
        .one_or_none()
    )
    session.rollback()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
    return WorkspaceResponse(**dict(row), current=True)


@router.post("/workspaces", response_model=WorkspaceResponse, status_code=status.HTTP_201_CREATED)
def create_workspace_endpoint(
    payload: WorkspaceCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> WorkspaceResponse:
    """No `Idempotency-Key`/replay protection is required here beyond the
    caller's own retry judgement -- creating a second workspace by mistake
    is a visible, correctable user action (rename/delete), not a silent
    duplicate-side-effect risk the way a domain-resource create is.
    """
    account_id_row = (
        session.execute(
            text(
                "SELECT account_id FROM users WHERE workspace_id = :workspace_id AND id = :users_id"
            ),
            {"workspace_id": auth.workspace_id, "users_id": auth.user_id},
        )
        .mappings()
        .one()
    )
    session.rollback()
    now = datetime.now(UTC)
    new_workspace_id = uuid4()
    new_users_id = uuid4()
    with session.begin():
        session.execute(
            text(
                "INSERT INTO workspaces (id, name, created_at, timezone) "
                "VALUES (:id, :name, :now, :timezone)"
            ),
            {
                "id": new_workspace_id,
                "name": payload.name,
                "now": now,
                "timezone": payload.timezone,
            },
        )
        session.execute(
            text(
                "INSERT INTO users (id, workspace_id, account_id, created_at) "
                "VALUES (:id, :workspace_id, :account_id, :now)"
            ),
            {
                "id": new_users_id,
                "workspace_id": new_workspace_id,
                "account_id": account_id_row["account_id"],
                "now": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO workspace_memberships (
                    id, workspace_id, account_id, users_id, role, status,
                    invited_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :account_id, :users_id, 'owner', 'active',
                    :users_id, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": new_workspace_id,
                "account_id": account_id_row["account_id"],
                "users_id": new_users_id,
                "now": now,
            },
        )
        _write_identity_audit_event(
            session,
            request,
            workspace_id=new_workspace_id,
            actor_users_id=new_users_id,
            event_type="workspace.created",
            aggregate_id=new_workspace_id,
            version=1,
            now=now,
        )
    return WorkspaceResponse(
        id=new_workspace_id,
        name=payload.name,
        timezone=payload.timezone,
        role="owner",
        created_at=now,
        # The caller's session stays scoped to their existing
        # `auth.workspace_id` -- creating a workspace does not switch to it;
        # `POST /auth/select-workspace` is the only thing that does.
        current=False,
    )


@router.patch("/workspaces/{workspace_id}", response_model=WorkspaceResponse)
def patch_workspace_endpoint(
    workspace_id: UUID,
    payload: WorkspacePatchRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> WorkspaceResponse:
    """No `Idempotency-Key` needed either -- a field-level `UPDATE` is
    naturally idempotent (replaying the same payload twice leaves the same
    end state), unlike a `POST` that would otherwise double-create.
    """
    if workspace_id != auth.workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
    now = datetime.now(UTC)
    with session.begin():
        membership = (
            session.execute(
                text(
                    "SELECT role FROM workspace_memberships "
                    "WHERE workspace_id = :workspace_id AND users_id = :users_id "
                    "AND status = 'active'"
                ),
                {"workspace_id": workspace_id, "users_id": auth.user_id},
            )
            .mappings()
            .one_or_none()
        )
        if membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
        if membership["role"] not in {"owner", "admin"}:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")

        updates: dict[str, Any] = {}
        if payload.name is not None:
            updates["name"] = payload.name
        if payload.timezone is not None:
            updates["timezone"] = payload.timezone
        if updates:
            set_clause = ", ".join(f"{column} = :{column}" for column in updates)
            session.execute(
                text(f"UPDATE workspaces SET {set_clause} WHERE id = :workspace_id"),  # noqa: S608
                {**updates, "workspace_id": workspace_id},
            )
            _write_identity_audit_event(
                session,
                request,
                workspace_id=workspace_id,
                actor_users_id=auth.user_id,
                event_type="workspace.updated",
                aggregate_id=workspace_id,
                version=1,
                now=now,
            )
        row = (
            session.execute(
                text(
                    """
                    SELECT w.id, w.name, w.timezone, wm.role, w.created_at
                    FROM workspace_memberships AS wm
                    JOIN workspaces AS w ON w.id = wm.workspace_id
                    WHERE w.id = :workspace_id AND wm.users_id = :users_id
                    """
                ),
                {"workspace_id": workspace_id, "users_id": auth.user_id},
            )
            .mappings()
            .one()
        )
    return WorkspaceResponse(**dict(row), current=True)
