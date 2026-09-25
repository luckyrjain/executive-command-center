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
import logging
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from hashlib import sha256
from secrets import token_urlsafe
from typing import Annotated, NoReturn
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
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
    get_connector_account,
)
from ecc.domains.engineering.connectors import AdapterAuthorizationError, ConnectorAccountContext
from ecc.domains.engineering.crypto import decrypt_credential, encrypt_credential
from ecc.domains.personal.gmail_adapter import GmailAdapter
from ecc.observability import (
    RevokeSite,
    queue_lifecycle_event,
    record_connector_enrollment_refused,
)
from ecc.platform import audit_outbox, authz
from ecc.platform.connector_security import (
    RevokeTokenKind,
    integrity_error_log_fields,
    is_unique_violation,
    revoke_if_safe,
    write_refusal_audit,
)

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
_logger = logging.getLogger(__name__)

_UNIQUE_EXTERNAL_ACCOUNT_CONSTRAINT = "uq_connector_accounts_workspace_provider_external_id"

# (credential context, token kind for `revoke_is_safe`, metric site label)
_PendingRevoke = tuple[ConnectorAccountContext, RevokeTokenKind, RevokeSite]


class _OwnerRefusal(Exception):
    """The callback's Google account is already connected in this
    workspace by a DIFFERENT member (Spec A S1.2, threat T2). Raised inside
    the business transaction and re-raised past the broad `except
    Exception` guard (which would queue the minted grant for the
    unconditional revoke drain), so the transaction rolls back -- releasing
    the `FOR UPDATE` row lock -- before the outer handler writes the
    refusal audit and returns `409`. Carries only the conflicting row's id,
    for the admin-only audit aggregate; nothing about that row reaches the
    caller or a log line.
    """

    def __init__(self, connector_account_id: UUID) -> None:
        super().__init__("connector owned by another member")
        self.connector_account_id = connector_account_id


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
    #
    # Spec A S1.6: each queued entry also carries its `revoke_is_safe`
    # token kind and metric site, and the drain in `finally` goes through
    # `revoke_if_safe` (never raises; under `ECC_GMAIL_REVOKE_SCOPE=global`
    # a Google grant is revoked only when no live `connector_accounts` row
    # anywhere still uses that Google account -- so the active-duplicate
    # and reconnect-replaced grants are skipped while the row they sit
    # beside is live).
    pending_revokes: list[_PendingRevoke] = []
    pending_revokes_on_commit: list[_PendingRevoke] = []
    response: ConnectorAccountResponse | None = None
    committed = False
    owner_refusal: _OwnerRefusal | None = None
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
            except IntegrityError as integrity_error:
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
                    if not is_unique_violation(
                        integrity_error, _UNIQUE_EXTERNAL_ACCOUNT_CONSTRAINT
                    ):
                        # Spec A S1.5: only the external-account unique
                        # key is a duplicate. Anything else (an FK or
                        # check violation) is a real failure -> 500. The
                        # except-guard just below still queues the
                        # minted grant for revoke.
                        _raise_persist_failed(integrity_error)
                    existing = (
                        create_session.execute(
                            text(
                                "SELECT id, status, owner_id, encrypted_credentials FROM "
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
                    if existing["owner_id"] != auth.user_id:
                        # Spec A S1.2: another member's connector for this
                        # Google account -- never return, reactivate, or
                        # overwrite it, whatever its status. A NULL owner
                        # refuses too (fails closed).
                        raise _OwnerRefusal(existing["id"])
                    if existing["status"] == "active":
                        pending_revokes.append(
                            (
                                ConnectorAccountContext(
                                    workspace_id=auth.workspace_id,
                                    connector_account_id=existing["id"],
                                    external_account_id=authorization.external_account_id,
                                    credential=authorization.credential,
                                ),
                                "duplicate",
                                "callback_duplicate",
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
                                (
                                    ConnectorAccountContext(
                                        workspace_id=auth.workspace_id,
                                        connector_account_id=existing["id"],
                                        external_account_id=authorization.external_account_id,
                                        credential=old_credential,
                                    ),
                                    "replaced",
                                    "reconnect_replaced",
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
                        audit_outbox.write_audit_and_outbox(
                            create_session,
                            auth,
                            request,
                            event_type="connector_account.reconnected",
                            aggregate_type="connector_account",
                            aggregate_id=reactivated.id,
                            aggregate_version=reactivated.version,
                            changed_fields=["*"],
                            payload={
                                "aggregate_id": str(reactivated.id),
                                "version": reactivated.version,
                            },
                            now=now,
                            domain="engineering_connector_account",
                        )
                        queue_lifecycle_event(
                            create_session,
                            "engineering_connector_account",
                            "connector_account.reconnected",
                            "allowed",
                        )
                except _OwnerRefusal:
                    # A refusal, not a failure to persist: the minted
                    # grant is NOT queued for the unconditional drain --
                    # the refusal handler after the `with` decides it.
                    raise
                except Exception:
                    pending_revokes.append(
                        (
                            ConnectorAccountContext(
                                workspace_id=auth.workspace_id,
                                connector_account_id=account_id,
                                external_account_id=authorization.external_account_id,
                                credential=authorization.credential,
                            ),
                            "minted_unpersisted",
                            "callback_failure",
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
                    (
                        ConnectorAccountContext(
                            workspace_id=auth.workspace_id,
                            connector_account_id=account_id,
                            external_account_id=authorization.external_account_id,
                            credential=authorization.credential,
                        ),
                        "minted_unpersisted",
                        "callback_failure",
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
                    audit_outbox.write_audit_and_outbox(
                        create_session,
                        auth,
                        request,
                        event_type="connector_account.created",
                        aggregate_type="connector_account",
                        aggregate_id=created.id,
                        aggregate_version=created.version,
                        changed_fields=["*"],
                        payload={"aggregate_id": str(created.id), "version": created.version},
                        now=now,
                        domain="engineering_connector_account",
                    )
                    queue_lifecycle_event(
                        create_session,
                        "engineering_connector_account",
                        "connector_account.created",
                        "allowed",
                    )
                except Exception:
                    pending_revokes.append(
                        (
                            ConnectorAccountContext(
                                workspace_id=auth.workspace_id,
                                connector_account_id=account_id,
                                external_account_id=authorization.external_account_id,
                                credential=authorization.credential,
                            ),
                            "minted_unpersisted",
                            "callback_failure",
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
    except _OwnerRefusal as refusal:
        # The `with` above has already rolled back and closed
        # `create_session` (row lock released); nothing was queued in
        # `pending_revokes` for this path. Handled below, after `finally`.
        owner_refusal = refusal
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
        _drain_pending_revokes(pending_revokes)
        if committed:
            _drain_pending_revokes(pending_revokes_on_commit)

    if owner_refusal is not None:
        _refuse_owned_by_another_member(
            request,
            auth,
            owner_refusal,
            ConnectorAccountContext(
                workspace_id=auth.workspace_id,
                connector_account_id=account_id,
                external_account_id=authorization.external_account_id,
                credential=authorization.credential,
            ),
        )

    assert response is not None
    return response


def _refuse_owned_by_another_member(
    request: Request,
    auth: AuthContext,
    refusal: _OwnerRefusal,
    minted: ConnectorAccountContext,
) -> NoReturn:
    """Spec A S1.2 refusal tail, run only after the business transaction
    rolled back: refusal audit (own txn, `denied`, no email) + metric ->
    minted grant revoked iff `revoke_is_safe(minted_unpersisted)` (under
    the default `global` scope never, since the other member's row is
    live; under `none` -- D2 proved revoke per-token -- the minted token
    alone is revoked, leaving the other member's grant intact) -> `409`
    with the bare code and no data about the other member's row.
    """
    write_refusal_audit(
        auth,
        request,
        event_type="connector_account.enrollment_refused",
        aggregate_type="connector_account",
        aggregate_id=refusal.connector_account_id,
        reason="owned_by_another_member",
        provider_or_type="gmail",
    )
    record_connector_enrollment_refused("gmail", "owned_by_another_member")
    _logger.warning("gmail_oauth_callback_refused: reason=owned_by_another_member")
    revoke_if_safe(
        _adapter,
        minted,
        provider="gmail",
        external_account_id=minted.external_account_id,
        token_kind="minted_unpersisted",
        exclude_row_id=None,
        site="callback_failure",
    )
    raise HTTPException(status_code=409, detail="CONNECTOR_OWNED_BY_ANOTHER_MEMBER")


def _raise_persist_failed(exc: IntegrityError) -> NoReturn:
    """Fail the request (500) for an `IntegrityError` that is not the
    duplicate-account unique violation (Spec A S1.5/S1.6, threat T7).

    Logs only `(sqlstate, constraint)` -- never `str(exc)`, whose psycopg
    `DETAIL: Key (...)=(...)` / `Failing row contains (...)` text carries
    row values (the Google email, encrypted credential bytes).

    Raised as an `HTTPException` *from None* rather than letting the
    `IntegrityError` propagate: FastAPI's exception middleware turns an
    `HTTPException` into a plain 500 response *inside* the app, so no
    exception ever reaches `request_observability_middleware`'s
    `exc_info=True` log line (which would otherwise serialize the full
    traceback -- including a chained `IntegrityError`'s message -- via
    `JsonFormatter`). `from None` additionally suppresses the implicit
    `__context__` chain for anything else that formats this exception
    (`/oauth/complete` catches it and redirects with its code).
    """
    sqlstate, constraint = integrity_error_log_fields(exc)
    _logger.error(
        "gmail_oauth_callback_persist_failed: sqlstate=%s constraint=%s", sqlstate, constraint
    )
    raise HTTPException(status_code=500, detail="CONNECTOR_ACCOUNT_PERSIST_FAILED") from None


def _drain_pending_revokes(pending: list[_PendingRevoke]) -> None:
    """Revoke each queued grant iff `revoke_is_safe` allows it (Spec A
    S1.6). Never raises. The same credential can be queued twice (a
    branch's own entry, then the wide `except Exception` guard's) -- it is
    attempted, and counted, once.
    """
    seen: set[str] = set()
    for context, token_kind, site in pending:
        if context.credential in seen:
            continue
        seen.add(context.credential)
        revoke_if_safe(
            _adapter,
            context,
            provider="gmail",
            external_account_id=context.external_account_id,
            token_kind=token_kind,
            exclude_row_id=None,
            site=site,
        )


@router.get("/oauth/complete", include_in_schema=False)
def gmail_oauth_complete_endpoint(
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    code: str | None = Query(default=None, min_length=1),
    state: str = Query(min_length=1),
) -> RedirectResponse:
    """The actual Google-facing OAuth redirect target -- `ECC_GMAIL_OAUTH_
    REDIRECT_URI` (and the matching entry in the Google Cloud Console
    OAuth client) must point here, not at `/oauth/callback` above. Google
    lands the browser's own top-level navigation on whatever URI that
    setting names, so the response has to be something a browser can
    usefully land on -- `/oauth/callback` is deliberately the opposite: a
    plain API endpoint returning `ConnectorAccountResponse` JSON, kept
    byte-for-byte unchanged here so its own ~30 integration tests
    (covering this feature's extensive revoke/rollback/race-condition
    review history) keep exercising the exact contract they always have.
    This endpoint is a thin wrapper: call that same function in-process
    (a plain Python call sharing this request's own `auth`/`session`, not
    a second HTTP round-trip or a duplicated copy of its logic), then
    convert whatever it returns or raises into a redirect back to
    `settings.frontend_url` -- the one thing a browser landing here can
    actually use.

    Before this endpoint existed, Google's registered redirect target
    *was* `/oauth/callback` itself, stranding the user's browser on raw
    backend JSON at the API origin with no way back into the app --
    `IMPLEMENTATION-STATUS.md`'s own "Task 8 evidence" section disclosed
    this as an accepted limitation; this closes it.

    **`code` is optional, deliberately** -- when the user clicks "Cancel"
    on Google's own consent screen (the mainline rejection path, not an
    edge case), Google's redirect carries `error=access_denied` and
    `state`, but no `code` at all. A required `code` would make FastAPI
    422 during dependency resolution, before this function's own
    try/except ever runs -- reintroducing the exact "stranded on raw
    backend JSON" bug this endpoint exists to close, for the single most
    common non-success path. A missing `code` redirects with
    `GMAIL_OAUTH_DENIED` without ever calling into `/oauth/callback`
    (which itself still requires `code`, correctly, for its own direct
    API-caller contract).

    **Only `HTTPException`/generic `Exception` raised from *inside* this
    function's own body are caught** -- an `auth`/`session` dependency
    failure (e.g. an expired ECC session cookie while the user was on
    Google's consent screen) is resolved by FastAPI before this body
    runs, so it still surfaces as a raw JSON 401 rather than a redirect.
    Accepted, not fixed: catching it would mean bypassing this codebase's
    `AuthDep`/`SessionDep` dependency-injection convention entirely for
    this one route, a larger and more fragile change than the narrow,
    low-likelihood case (`_STATE_TTL_SECONDS` gives a 10-minute consent
    window; a 7-day session would need to already be within that same
    window of expiring) warrants.

    **This function's own translation of `/oauth/callback`'s failure
    modes into a redirect is not contract-enforced** -- it only works
    because both are read together here. A future change to
    `/oauth/callback`'s own raised-exception shapes (a new `HTTPException.
    detail` shape, or a new non-`HTTPException` raised from a helper it
    calls) needs a matching update below; nothing currently forces that.
    """
    if code is None:
        query = urlencode({"gmail": "error", "code": "GMAIL_OAUTH_DENIED"})
        return RedirectResponse(
            f"{get_settings().frontend_url.rstrip('/')}/?{query}", status_code=302
        )

    try:
        gmail_oauth_callback_endpoint(request, auth, session, code=code, state=state)
    except HTTPException as exc:
        if isinstance(exc.detail, str):
            error_code = exc.detail
        else:
            error_code = exc.detail.get("code", "GMAIL_OAUTH_FAILED")
        query = urlencode({"gmail": "error", "code": error_code})
    except Exception as exc:
        # A non-`HTTPException` escaping `/oauth/callback` (a transient DB
        # error, a dropped connection, an `AssertionError`) would
        # otherwise propagate past this function too -- this app
        # registers no generic exception handler (`main.py`), so every
        # *other* endpoint's raw 500 is at least invisible to the end
        # user (the frontend's own `fetch` catches it and shows an
        # alert). This is the one endpoint where an unhandled exception
        # reaches the browser directly via a top-level navigation --
        # redirecting instead of re-raising is what keeps the user out of
        # a raw error page, so the failure is recorded here (it would
        # otherwise leave no trace, since nothing propagates to the
        # request middleware) and reported to the frontend as the same
        # generic `GMAIL_OAUTH_FAILED` code `/oauth/callback`'s own
        # `AdapterAuthorizationError` branch already uses.
        #
        # Logs only the code-defined error code and the exception *class*
        # (Spec A S1.6 / threat T7) -- never `str(exc)` or a traceback
        # (`exc_info`), since a DB driver's message (`DETAIL: Key (...)=
        # (...)`) or an adapter's text can carry Google emails, OAuth
        # codes/state, or credential bytes.
        _logger.error(
            "Unhandled error completing Gmail OAuth: code=%s error_class=%s",
            "GMAIL_OAUTH_FAILED",
            f"{type(exc).__module__}.{type(exc).__qualname__}",
        )
        query = urlencode({"gmail": "error", "code": "GMAIL_OAUTH_FAILED"})
    else:
        query = urlencode({"gmail": "connected"})
    return RedirectResponse(f"{get_settings().frontend_url.rstrip('/')}/?{query}", status_code=302)
