"""Gmail OAuth initiate/callback endpoints (Phase 10 Gmail Connector Task 1,
design doc Decisions 1 and 3: `docs/superpowers/specs/2026-08-04-phase-10-
gmail-connector-design.md`).

`POST /api/v1/personal/gmail/oauth/start`, `GET /api/v1/personal/gmail/
oauth/callback` are the only two Gmail-specific HTTP routes this task
adds -- every other `connector_accounts` operation (list, sync, disable)
reuses `ecc.domains.engineering.connector_accounts`'s existing generic
endpoints as-is (plan Task 1's own "standard connectors CRUD reused as-is
for everything after the token exchange completes"); a `provider='gmail'`
row shows up in `GET /engineering/connectors` exactly like any other
connector.

**`state`: HMAC-signed, expiring, session-bound -- no new table**, mirroring
design doc Decision 3's own "config-driven check... deliberately not a new
database table" reasoning for the allowlist, applied here to the CSRF-state
problem: `_sign_state`/`_verify_state` below derive a signature from
`session_secret` (already used for `require_csrf`'s identical HMAC-over-
cookie shape in `ecc.auth`) over `(workspace_id, user_id, nonce, expires_at)`
-- forging a valid state without `session_secret` is infeasible, a stale
state expires (`_STATE_TTL_SECONDS`), and binding the signature to the
*current* session's own `workspace_id`/`user_id` means a state minted for
one session can never be replayed against a different one. `GmailAdapter.
handle_oauth_callback` itself does not re-verify `state` (see that module's
own docstring) -- this router's `_verify_state` is the sole enforcement
point, and runs before `handle_oauth_callback` is ever called.

**Allowlist checked twice** (`GmailAdapter.is_account_allowed`'s own
docstring): once here, pre-redirect, against the ECC-authenticated caller's
own `accounts.email` (the fast reject); once inside `GmailAdapter.
handle_oauth_callback` itself, against the actual Google account resolved
post-exchange (the authoritative check -- a caller can authorize a
*different* Google account at Google's own consent screen than their ECC
login implies).

**The callback INSERT does not use the `Idempotency-Key` mechanism**
`ecc.domains.engineering.connector_accounts`'s own mutating endpoints use --
a browser-driven OAuth redirect cannot attach a custom request header, and
Google's own authorization `code` is single-use regardless (a genuine
client-side retry-with-same-key scenario, the mechanism `Idempotency-Key`
exists for, cannot occur here the same way -- a literal same-`code` replay,
e.g. a browser back-button reload, fails earlier, at Google's own token
endpoint, and never reaches the `INSERT` below at all). What *does* reach
`uq_connector_accounts_workspace_provider_external_id` is two genuinely
distinct, successfully-exchanged consent completions for the same Google
account racing each other (two browser tabs; a reconnect attempt started
before a prior one's response arrived) -- handled by returning the
already-connected account's own current state rather than a hard `409`
(see the `IntegrityError` handler's own docstring below for the second,
non-`active`-status case this same branch also handles).
"""

from __future__ import annotations

import hmac
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from hashlib import sha256
from secrets import token_urlsafe
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import SessionFactory, get_session
from ecc.domains.engineering.connector_accounts import (
    ConnectorAccountResponse,
    _to_response,
    _write_side_effects,
    get_connector_account,
)
from ecc.domains.engineering.connectors import AdapterAuthorizationError
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal.gmail_adapter import GmailAdapter
from ecc.platform import authz

router = APIRouter(prefix="/api/v1/personal/gmail", tags=["personal"])
SessionDep = Annotated[Session, Depends(get_session)]

# 10 minutes -- long enough for a real human to complete Google's consent
# screen, short enough that a leaked/logged state value is useless soon
# after.
_STATE_TTL_SECONDS = 600

# Module-level singleton, mirroring `ecc.domains.engineering.connectors.
# registry`'s own "one shared production instance" shape -- constructed
# with no arguments (reads `ECC_GMAIL_OAUTH_*` lazily, per-call, via
# `get_settings()`, exactly like `ecc.domains.engineering.crypto._fernet`
# reads its own key lazily) so importing this module in an environment
# with no Gmail OAuth app configured never itself raises.
_adapter = GmailAdapter()


class OAuthStartResponse(BaseModel):
    authorization_url: str


def _sign_state(auth: AuthContext, nonce: str, expires_at: int) -> str:
    material = f"{auth.workspace_id}:{auth.user_id}:{nonce}:{expires_at}"
    settings = get_settings()
    return hmac.new(
        settings.session_secret.encode("utf-8"), material.encode("utf-8"), sha256
    ).hexdigest()


def _encode_state(auth: AuthContext) -> str:
    nonce = token_urlsafe(16)
    expires_at = int(datetime.now(UTC).timestamp()) + _STATE_TTL_SECONDS
    signature = _sign_state(auth, nonce, expires_at)
    raw = f"{nonce}.{expires_at}.{signature}"
    return urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _verify_state(auth: AuthContext, state: str) -> bool:
    try:
        raw = urlsafe_b64decode(state.encode("ascii")).decode("utf-8")
        nonce, expires_at_str, signature = raw.split(".", 2)
        expires_at = int(expires_at_str)
    except (ValueError, UnicodeDecodeError):
        return False
    if datetime.now(UTC).timestamp() > expires_at:
        return False
    expected = _sign_state(auth, nonce, expires_at)
    return hmac.compare_digest(signature, expected)


def _caller_email(session: Session, auth: AuthContext) -> str | None:
    row = (
        session.execute(
            text(
                "SELECT a.email FROM users AS u "
                "JOIN accounts AS a ON a.id = u.account_id "
                "WHERE u.workspace_id = :workspace_id AND u.id = :users_id"
            ),
            {"workspace_id": auth.workspace_id, "users_id": auth.user_id},
        )
        .mappings()
        .one_or_none()
    )
    session.rollback()
    return row["email"] if row is not None else None


@router.post("/oauth/start", response_model=OAuthStartResponse)
def start_gmail_oauth_endpoint(
    auth: AuthDep, session: SessionDep, _csrf: CsrfDep
) -> OAuthStartResponse:
    authz.require_role_action(session, auth, "write")
    caller_email = _caller_email(session, auth)
    if caller_email is None or not _adapter.is_account_allowed(caller_email):
        raise HTTPException(status_code=403, detail="GMAIL_ACCOUNT_NOT_ALLOWLISTED")
    state = _encode_state(auth)
    try:
        authorization_url = _adapter.get_authorization_url(state)
    except AdapterAuthorizationError as exc:
        raise HTTPException(status_code=422, detail="GMAIL_OAUTH_NOT_CONFIGURED") from exc
    return OAuthStartResponse(authorization_url=authorization_url)


@router.get("/oauth/callback", response_model=ConnectorAccountResponse)
def gmail_oauth_callback_endpoint(
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    code: str = Query(min_length=1),
    state: str = Query(min_length=1),
) -> ConnectorAccountResponse:
    authz.require_role_action(session, auth, "write")
    if not _verify_state(auth, state):
        raise HTTPException(status_code=403, detail="GMAIL_OAUTH_STATE_INVALID")
    session.rollback()

    try:
        authorization = _adapter.handle_oauth_callback(code, state)
    except AdapterAuthorizationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "GMAIL_OAUTH_FAILED", "error": str(exc)[:500]},
        ) from exc
    assert authorization.credential is not None  # handle_oauth_callback always sets it

    now = datetime.now(UTC)
    account_id = uuid4()
    with SessionFactory() as create_session, create_session.begin():
        try:
            with create_session.begin_nested():
                create_session.execute(
                    text(
                        """
                        INSERT INTO connector_accounts (
                            id, workspace_id, provider, external_account_id, display_name,
                            granted_scopes, encrypted_credentials, status, version,
                            created_by, updated_by, created_at, updated_at,
                            owner_id, visibility
                        ) VALUES (
                            :id, :workspace_id, 'gmail', :external_account_id, :display_name,
                            :granted_scopes, :encrypted_credentials, 'active', 1,
                            :actor_id, :actor_id, :now, :now,
                            :actor_id, 'workspace'
                        )
                        """
                    ),
                    {
                        "id": account_id,
                        "workspace_id": auth.workspace_id,
                        "external_account_id": authorization.external_account_id,
                        "display_name": authorization.display_name,
                        "granted_scopes": list(authorization.granted_scopes),
                        "encrypted_credentials": encrypt_credential(authorization.credential),
                        "actor_id": auth.user_id,
                        "now": now,
                    },
                )
        except IntegrityError:
            # `uq_connector_accounts_workspace_provider_external_id` already
            # has a row for this exact Google account -- two real cases,
            # not one. (1) Two distinct, successfully-exchanged consent
            # completions for the same account racing each other (see
            # module docstring for why a literal same-`code` replay cannot
            # reach here at all) while the row is still `active`: return
            # the existing row's current state as-is -- there is nothing
            # new to record.
            # (2) The row is `disconnected`/`permission_lost`/`error`/
            # `rate_limited` -- the user just completed a real Google
            # consent flow and `authorization` above holds a freshly
            # exchanged, valid, allowlist-checked credential. Silently
            # returning the old (broken) row here would discard that
            # credential -- for `disconnected` specifically, one already
            # revoked at Google by `GmailAdapter.disconnect()`, permanently
            # stranding the account with no reactivate/PATCH endpoint
            # anywhere in `connector_accounts.py` to recover it. Reactivate
            # the row with the new credential instead -- the one write path
            # that both (1) and (2) share is "the account exists, make its
            # stored state match what was just proven valid," which is
            # exactly what an UPDATE does for a non-`active` row and a
            # harmless no-op-equivalent read for an already-`active` one.
            existing = (
                create_session.execute(
                    text(
                        "SELECT id, status FROM connector_accounts "
                        "WHERE workspace_id = :workspace_id "
                        "AND provider = 'gmail' AND external_account_id = :external_account_id "
                        "FOR UPDATE"
                    ),
                    {
                        "workspace_id": auth.workspace_id,
                        "external_account_id": authorization.external_account_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
            if existing is None:
                raise HTTPException(
                    status_code=409, detail="GMAIL_ACCOUNT_ALREADY_CONNECTED"
                ) from None
            if existing["status"] == "active":
                account = get_connector_account(create_session, auth.workspace_id, existing["id"])
                assert account is not None
                return _to_response(account)

            create_session.execute(
                text(
                    """
                    UPDATE connector_accounts SET
                        display_name = :display_name,
                        granted_scopes = :granted_scopes,
                        encrypted_credentials = :encrypted_credentials,
                        status = 'active', status_detail = NULL, last_error = NULL,
                        disconnected_at = NULL, updated_by = :actor_id, updated_at = :now,
                        version = version + 1
                    WHERE id = :id
                    """
                ),
                {
                    "id": existing["id"],
                    "display_name": authorization.display_name,
                    "granted_scopes": list(authorization.granted_scopes),
                    "encrypted_credentials": encrypt_credential(authorization.credential),
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
            reactivated = get_connector_account(create_session, auth.workspace_id, existing["id"])
            assert reactivated is not None
            response = _to_response(reactivated)
            _write_side_effects(
                create_session,
                auth,
                request,
                event_type="connector_account.reconnected",
                aggregate_id=reactivated.id,
                version=reactivated.version,
                now=now,
            )
            return response

        created = get_connector_account(create_session, auth.workspace_id, account_id)
        assert created is not None
        response = _to_response(created)
        _write_side_effects(
            create_session,
            auth,
            request,
            event_type="connector_account.created",
            aggregate_id=created.id,
            version=created.version,
            now=now,
        )
        return response
