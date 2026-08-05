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
    _sanitize_adapter_error,
    _to_response,
    _write_side_effects,
    get_connector_account,
)
from ecc.domains.engineering.connectors import AdapterAuthorizationError, ConnectorAccountContext
from ecc.domains.engineering.crypto import decrypt_credential, encrypt_credential
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
    # Release `session`'s pooled connection before the slow, sequential
    # outbound HTTPS calls inside `handle_oauth_callback` (Google's token
    # endpoint, then its profile endpoint -- up to ~20s combined) --
    # mirrors `create_connector_endpoint`'s own identical, documented fix
    # (`connector_accounts.py`): holding a pooled connection idle across a
    # slow adapter call reintroduces the same app-wide pool-exhaustion risk
    # `/sync` was restructured to avoid (round 10 review found this
    # endpoint had never adopted that established pattern). `session` is
    # not referenced again below -- everything after this call uses its
    # own fresh `create_session`.
    session.close()

    try:
        authorization = _adapter.handle_oauth_callback(code, state)
    except AdapterAuthorizationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "GMAIL_OAUTH_FAILED", "error": _sanitize_adapter_error(str(exc))},
        ) from exc
    assert authorization.credential is not None  # handle_oauth_callback always sets it

    now = datetime.now(UTC)
    account_id = uuid4()
    # Round 23 review: every `_adapter.disconnect(...)` call below used to
    # run *inside* `create_session`'s open transaction, several of them
    # (the `active`-row and reactivation branches) while still holding the
    # re-`SELECT ... FOR UPDATE` row lock taken below -- `GmailAdapter.
    # disconnect()` makes a real, blocking outbound HTTPS call (Google's
    # `/revoke`, up to `timeout_seconds=10.0`; round 27 review: not
    # actually the first in this registry to do so -- `gitlab_adapter.py`'s
    # `disconnect()` already does via `/personal_access_tokens/self`, and
    # benefits from this same fix via `disable_connector_endpoint`'s own
    # generic, provider-agnostic phase split -- but this was still the
    # first *reconnect*-path call site, the only one holding a row lock
    # rather than just a pooled connection), so this held a pooled
    # connection *and* a
    # row lock across that call on the two most mainline paths through
    # this branch (a racing reconnect, and any ordinary reconnect of a
    # previously-disconnected account) -- dynamically proven via a real
    # Postgres `LockNotAvailable`/`pg_stat_activity: idle in transaction`
    # repro. Every revoke call below is now deferred: it only *records*
    # which credential needs revoking (`pending_revokes`), and the actual
    # network calls happen in the `finally` block after `create_session`
    # has fully closed (releasing both the connection and the lock) --
    # `_adapter.disconnect()` is best-effort and never raises, and never
    # was consulted for what to persist, so deferring it changes nothing
    # about correctness, only when the network call happens relative to
    # the transaction.
    #
    # Round 26 review: **not every queued credential is safe to revoke
    # unconditionally on rollback.** `pending_revokes` below is for a
    # credential that is *definitely* being discarded no matter how this
    # request ends -- the `active`-row branch's just-exchanged new grant
    # (the existing row is only ever read, never written, so nothing
    # about a later failure changes the fact this grant was never going
    # to be persisted), and every catch-all `except Exception:` queuing
    # `authorization.credential` when the `INSERT`/success-path tail
    # itself fails outright (nothing was ever persisted either way, so
    # revoking the grant that would have been is correct regardless of
    # commit/rollback -- rounds 4/7/10/11/12's own reasoning). The
    # reactivation branch's *old* credential is different in kind: it is
    # only supposed to be discarded *because* the `UPDATE` a few lines
    # below is about to replace it with the new grant -- a revoke that is
    # correct if and only if that `UPDATE` (and the rest of this
    # transaction) actually commits. Queuing it into the same
    # unconditional-drain list as everything else meant a transient,
    # wholly unrelated failure later in the same branch (a dropped
    # connection, a deadlock, `statement_timeout`) rolled the `UPDATE`
    # back -- leaving the row's stored credential exactly as it was
    # before this request -- while still revoking that same credential at
    # Google, since the round-23 `finally` block below drains
    # unconditionally "whether the transaction committed or rolled back."
    # Dynamically confirmed: reactivating a non-`disconnected` row (e.g.
    # `error`/`rate_limited`/`permission_lost` -- states where, unlike
    # `disconnected`, nothing has necessarily already revoked the stored
    # credential at Google) whose credential is still genuinely live, then
    # failing a later statement in the same branch so the `UPDATE` rolls
    # back, left the row still reporting its old, pre-request status with
    # its old credential still stored -- but that stored credential had
    # already been revoked at Google by this same failed request, with no
    # local signal anything was wrong. `pending_revokes_on_commit` holds
    # exactly this one class of entry, drained only if the whole
    # transaction actually commits (`committed` below) -- every other
    # queue entry in this function remains in the always-drain
    # `pending_revokes` list, matching rounds 4-12's original reasoning,
    # which was correct for all of them except this one.
    pending_revokes: list[ConnectorAccountContext] = []
    pending_revokes_on_commit: list[ConnectorAccountContext] = []
    response: ConnectorAccountResponse | None = None
    committed = False
    try:
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
                # `uq_connector_accounts_workspace_provider_external_id`
                # already has a row for this exact Google account -- two
                # real cases, not one. (1) Two distinct, successfully-
                # exchanged consent completions for the same account
                # racing each other (see module docstring for why a
                # literal same-`code` replay cannot reach here at all)
                # while the row is still `active`: return the existing
                # row's current state as-is -- there is nothing new to
                # record.
                # (2) The row is `disconnected`/`permission_lost`/`error`/
                # `rate_limited` -- the user just completed a real Google
                # consent flow and `authorization` above holds a freshly
                # exchanged, valid, allowlist-checked credential. Silently
                # returning the old (broken) row here would discard that
                # credential -- for `disconnected` specifically, one
                # already revoked at Google by `GmailAdapter.disconnect()`,
                # permanently stranding the account with no reactivate/
                # PATCH endpoint anywhere in `connector_accounts.py` to
                # recover it. Reactivate the row with the new credential
                # instead -- the one write path that both (1) and (2)
                # share is "the account exists, make its stored state
                # match what was just proven valid," which is exactly
                # what an UPDATE does for a non-`active` row and a
                # harmless no-op-equivalent read for an already-`active`
                # one.
                #
                # Either way, `authorization.credential` (case 1) or the
                # row's own pre-update `encrypted_credentials` (case 2) is
                # a real, live Google grant this handler is about to
                # discard/overwrite without ever persisting it as the
                # account's current credential -- round 4 review found
                # this is exactly the same "obtained but never revoked"
                # bug class rounds 2-3 closed inside `handle_oauth_
                # callback` itself, just relocated to this router branch.
                # Whichever credential is being dropped in each case is
                # queued in `pending_revokes` (round 23: no longer
                # revoked inline) before returning/overwriting.
                #
                # Everything below -- the re-`SELECT`, and every branch's
                # own further work -- is wrapped in one more `try`,
                # closing with a single `except Exception:` that queues
                # `authorization.credential` before re-raising (round 12
                # review). This is deliberately one wide guard, not per-
                # statement ones: rounds 7/10/11 each closed a *specific*
                # unprotected statement here (the re-`SELECT` failing
                # entirely still had no guard even after round 11's fix,
                # since that fix only wrapped the reactivation branch's
                # own follow-on writes) -- the same "found one more
                # unprotected statement" pattern recurring three times
                # over is itself evidence that patching statement-by-
                # statement doesn't converge. A single guard around the
                # whole branch closes the entire class at once: any
                # exception raised anywhere below, before or after any
                # branch's own more specific queued revoke, still ends
                # here. A redundant queued revoke (e.g. the `active`/
                # reactivate branches' own queue entries, followed by this
                # same credential queued again if something later in the
                # same branch then fails) is a harmless no-op per
                # `_revoke_best_effort`'s own contract, not a correctness
                # concern.
                try:
                    existing = (
                        create_session.execute(
                            text(
                                "SELECT id, status, encrypted_credentials FROM "
                                "connector_accounts WHERE workspace_id = :workspace_id "
                                "AND provider = 'gmail' "
                                "AND external_account_id = :external_account_id "
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
                        # The row that caused the `IntegrityError`
                        # disappeared between the failed INSERT and this
                        # re-`SELECT`, inside the same transaction -- not
                        # currently reachable (nothing in this codebase
                        # hard-deletes a `connector_accounts` row, and
                        # Postgres guarantees the conflicting row's own
                        # transaction already committed before the
                        # `IntegrityError` fires, so the row should always
                        # still be here). Still, `authorization.
                        # credential` is a real, freshly obtained Google
                        # grant that this 409 is about to drop without
                        # ever persisting it -- round 7 review found this
                        # was the one branch of this handler not revoking
                        # the credential it discards, unlike its two
                        # siblings below.
                        raise HTTPException(
                            status_code=409, detail="GMAIL_ACCOUNT_ALREADY_CONNECTED"
                        ) from None
                    if existing["status"] == "active":
                        pending_revokes.append(
                            ConnectorAccountContext(
                                workspace_id=auth.workspace_id,
                                connector_account_id=existing["id"],
                                external_account_id=authorization.external_account_id,
                                credential=authorization.credential,
                            )
                        )
                        account = get_connector_account(
                            create_session, auth.workspace_id, existing["id"]
                        )
                        assert account is not None
                        response = _to_response(account)
                    else:
                        try:
                            old_credential = decrypt_credential(existing["encrypted_credentials"])
                        except Exception:  # noqa: BLE001 -- best-effort, never blocks reactivation
                            old_credential = None
                        if old_credential is not None:
                            # Commit-contingent (see the block comment
                            # above `pending_revokes`'s own declaration):
                            # this credential is only actually being
                            # discarded if the `UPDATE` immediately below
                            # -- and the rest of this transaction -- goes
                            # on to commit.
                            pending_revokes_on_commit.append(
                                ConnectorAccountContext(
                                    workspace_id=auth.workspace_id,
                                    connector_account_id=existing["id"],
                                    external_account_id=authorization.external_account_id,
                                    credential=old_credential,
                                )
                            )

                        create_session.execute(
                            text(
                                """
                                UPDATE connector_accounts SET
                                    display_name = :display_name,
                                    granted_scopes = :granted_scopes,
                                    encrypted_credentials = :encrypted_credentials,
                                    status = 'active', status_detail = NULL, last_error = NULL,
                                    disconnected_at = NULL, updated_by = :actor_id,
                                    updated_at = :now, version = version + 1
                                WHERE id = :id
                                """
                            ),
                            {
                                "id": existing["id"],
                                "display_name": authorization.display_name,
                                "granted_scopes": list(authorization.granted_scopes),
                                "encrypted_credentials": encrypt_credential(
                                    authorization.credential
                                ),
                                "actor_id": auth.user_id,
                                "now": now,
                            },
                        )
                        reactivated = get_connector_account(
                            create_session, auth.workspace_id, existing["id"]
                        )
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
                except Exception:
                    pending_revokes.append(
                        ConnectorAccountContext(
                            workspace_id=auth.workspace_id,
                            connector_account_id=account_id,
                            external_account_id=authorization.external_account_id,
                            credential=authorization.credential,
                        )
                    )
                    raise
            except Exception:
                # Anything other than `IntegrityError` here (a dropped
                # connection, a deadlock, the app's own `statement_timeout`
                # firing under transient load -- round 10 review) means the
                # `INSERT` above never committed, so `authorization.credential`
                # -- a real, successfully-exchanged Google grant -- is about to
                # be silently orphaned: never persisted to `connector_accounts`,
                # never revoked, the same "obtained but never revoked" bug
                # class closed everywhere else in this flow. Queuing here is
                # idempotent-if-redundant with whatever this exception's own
                # `IntegrityError` branch may have already queued.
                pending_revokes.append(
                    ConnectorAccountContext(
                        workspace_id=auth.workspace_id,
                        connector_account_id=account_id,
                        external_account_id=authorization.external_account_id,
                        credential=authorization.credential,
                    )
                )
                raise

            if response is None:
                # Round 12 review: the `INSERT` above committed, but the row is
                # only actually usable once the response is built and the audit
                # event is written -- both still inside this same outer
                # transaction. A failure in any of the statements below
                # (the identical dropped-connection/deadlock/`statement_timeout`
                # classes named throughout this function) rolls the whole
                # transaction back, undoing the `INSERT` -- the same "obtained
                # but never revoked" bug class this whole function has closed
                # everywhere else, reopened here because this is the one path
                # that *persists* `authorization.credential` rather than
                # discarding it, so nothing upstream already queued it.
                try:
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
                except Exception:
                    pending_revokes.append(
                        ConnectorAccountContext(
                            workspace_id=auth.workspace_id,
                            connector_account_id=account_id,
                            external_account_id=authorization.external_account_id,
                            credential=authorization.credential,
                        )
                    )
                    raise
        # Reached only if the `with` block above exited normally -- no
        # exception propagated past it, so `create_session`'s transaction
        # actually committed. Guards `pending_revokes_on_commit`'s drain
        # below (round 26 review; see the block comment above that list's
        # declaration for why its entries specifically must not be
        # revoked on a rollback the way every other queued entry safely
        # can be).
        committed = True
    finally:
        # `create_session` is fully closed by this point (the `with` block
        # above has already exited) -- no pooled connection or row lock is
        # held during any of these calls. `pending_revokes` runs whether
        # the transaction committed or rolled back, and whether or not an
        # exception is about to propagate past this `finally` (Python
        # re-raises it automatically afterward) -- every entry in it is a
        # credential that was never going to be persisted either way.
        # `pending_revokes_on_commit` only runs if `committed` -- its
        # entries are only actually being discarded when this request's
        # own replacement write for them landed for real.
        for pending_context in pending_revokes:
            _adapter.disconnect(pending_context)
        if committed:
            for pending_context in pending_revokes_on_commit:
                _adapter.disconnect(pending_context)

    assert response is not None
    return response
