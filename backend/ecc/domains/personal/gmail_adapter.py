"""Gmail `ConnectorAdapter`/`OAuth2ConnectorAdapter` (Phase 10 Gmail
Connector Task 1, design doc Decision 1: `docs/superpowers/specs/
2026-08-04-phase-10-gmail-connector-design.md`).

**The first true 3-legged OAuth2 adapter in this registry.** Every existing
adapter (`github`/`gitlab`/`jira`/`datadog`/`sandbox`) is PAT-based --
`authorize(credential: str)` is never called for `gmail`; this adapter
implements `ConnectorAdapter` (for `backfill`/`incremental_sync`/
`handle_webhook`/`refresh_permissions`/`disconnect`, `authorize` itself
correctly never invoked) *and* `OAuth2ConnectorAdapter` (for
`get_authorization_url`/`handle_oauth_callback`) on the same class --
structural typing means no explicit multiple inheritance is required, only
both method sets actually existing on the object (`ecc.domains.engineering.
connectors`' own module docstring explains why this is two Protocols, not
one widened).

**HTTP client shape mirrors `github_adapter.py`'s established precedent**
(an injectable `transport: httpx.BaseTransport | None` constructor
parameter, used only by tests; narrow exception handling re-raised as this
module's own typed exceptions). Two `httpx.Client`s, not one -- Google's
OAuth2 token endpoint (`oauth2.googleapis.com`) and the Gmail REST API
(`gmail.googleapis.com`) are different hosts, unlike every prior adapter's
single-host API.

**Credential shape is a JSON string, not a bare token.** Every existing
adapter's `credential` is a single opaque token string; Gmail's OAuth grant
is an access/refresh token *pair* plus an expiry. `_pack_credential`/
`_unpack_credential` below serialize/deserialize
`{"access_token", "refresh_token", "expires_at"}` as a JSON string --
`ConnectorAccountContext.credential` stays `str` (the Protocol's own type),
`ecc.domains.engineering.crypto.encrypt_credential`/`decrypt_credential`
(Phase 6 Decision 2's key, reused verbatim per design doc Decision 2 --
*not* the personal-data key, which is reserved for synced email content,
not the OAuth grant itself) still store/retrieve it as an opaque blob with
no change to that module or to `connector_accounts.encrypted_credentials`'s
`bytea` column.

**Account identity comes from Gmail's own `users.getProfile`, not a
separate `email`/`profile`/`openid` OAuth scope.** `GET /gmail/v1/users/
me/profile` returns `emailAddress` and is already covered by the
`gmail.metadata` scope this adapter requests -- avoiding a fourth OAuth
scope this task has no other use for.

**The internal allowlist (design doc Decision 3) is enforced by the
caller, not this class's own `get_authorization_url`/`handle_oauth_
callback` bodies.** `OAuth2ConnectorAdapter`'s own docstring says an
adapter "raises `AdapterAuthorizationError` for a caller this adapter's
own internal allowlist rejects... before any redirect URL is even
generated" -- but `get_authorization_url(state: str) -> str`'s fixed
Protocol signature carries no account/email argument to check an allowlist
against (the Gmail account itself is not known pre-redirect at all).
`is_account_allowed` below is this adapter's own additional method (not
part of either Protocol -- `gmail_oauth.py` imports `GmailAdapter`
concretely, not through the generic registry, specifically so it can call
Gmail-specific methods beyond the two Protocols' shared shape) that
`gmail_oauth.py`'s router calls twice: once against the ECC-authenticated
caller's own `users.email` before ever calling `get_authorization_url` at
all (the fast, pre-redirect reject the design doc describes), and once
again against the actual Google account `handle_oauth_callback` resolves
(the authoritative check -- a caller could authorize a *different* Google
account at Google's own consent screen than the one implied by their ECC
login email, and only this second check catches that).

**`state` CSRF verification is the caller's responsibility, not this
class's.** A `ConnectorRegistry`-held adapter instance is a stateless
singleton shared across every request; it has no per-flow store to compare
`handle_oauth_callback`'s `state` argument against what a *prior, separate*
call to `get_authorization_url` (a different HTTP request, and in a
multi-worker deployment quite possibly a different process) issued.
`gmail_oauth.py`'s router owns state generation and verification (an
HMAC-signed, expiring, session-bound token -- see that module's own
docstring), and only invokes `handle_oauth_callback` once its own
verification has already passed; this method does not re-derive or
re-check `state` itself, since it has no way to.

**`backfill`/`incremental_sync` are real as of Task 2** (`handle_webhook`
remains stubbed -- push-notification-based sync is still explicitly
deferred, see that method's own docstring). Both sync `gmail.metadata`
(headers only -- `From`/`To`/`Subject`; `email_messages.body`/`snippet`
stay `NULL` until a future task's `gmail.readonly` fetch) into `email_
threads`/`email_messages`, and resolve each message's sender/recipients
into `pkos_nodes` via entity-resolution match hierarchy level 3 (`docs/
phases/phase-002/ENTITY-RESOLUTION-CONTRACT.md`: "exact normalized
workspace-scoped identifier such as verified email") -- see `_resolve_or_
create_person`'s own docstring. `backfill` covers a `since`-bounded window
(default the last 30 days) via Gmail's `messages.list`/`q=after:...`;
`incremental_sync` resumes from a Gmail `historyId` cursor via `history.
list`, falling back to a fresh `backfill` both when `cursor` is `None`
(the Protocol's own contract) and when Gmail reports the cursor has
expired (a 404 -- see `_sync_history`'s own docstring for why this
fallback is a disclosed design decision, not something the plan document
named explicitly). Every resource type other than `"message"` still
zero-item-succeeds, matching `github_adapter.py`'s own "an unimplemented
resource type no-op-succeeds rather than raises" contract interpretation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from hashlib import sha256
from json import dumps, loads
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ecc.config import get_settings
from ecc.database import SessionFactory
from ecc.domains.engineering.connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorAuthorization,
    PermissionState,
    SyncOutcome,
)

GOOGLE_OAUTH_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_BASE_URL = "https://oauth2.googleapis.com"
GMAIL_API_BASE_URL = "https://gmail.googleapis.com"

# Design doc Decision 3's revised framing: `gmail.readonly` is requested
# broadly at connect time (not merely per-thread-on-demand), since the
# proactive action-detection tool (Task 5) needs to read a body before a
# human has opened the thread. Both scopes are Google restricted-tier --
# see that decision's own "verified directly against Google's current
# documentation" note for why there is no lighter-tier scope to fall back
# to here.
REQUIRED_SCOPES: frozenset[str] = frozenset(
    {
        "https://www.googleapis.com/auth/gmail.metadata",
        "https://www.googleapis.com/auth/gmail.readonly",
    }
)


# Task 2 sync tuning -- mirrors `github_adapter.py`'s own `_PAGE_SIZE`/
# `_MAX_PAGES_PER_CALL`/`_RATE_LIMIT_MAX_WAIT_SECONDS` bounds, adapted to
# Gmail's one-list-call-plus-N-per-message-get-calls shape rather than
# GitHub's single paginated list. `_MAX_MESSAGES_PER_CALL` bounds total
# `messages.get` calls (the dominant cost) across every page fetched in one
# `backfill`/`incremental_sync` invocation, not `_MESSAGE_PAGE_SIZE` alone.
_MESSAGE_PAGE_SIZE = 50
_MAX_MESSAGES_PER_CALL = 200
_RATE_LIMIT_MAX_WAIT_SECONDS = 5.0
_DEFAULT_BACKFILL_WINDOW = timedelta(days=30)

# Gmail's own quota-error shape (`{"error": {"errors": [{"reason": ...}]}}`)
# distinguishes rate limiting from every other 403 (insufficient scope,
# account suspended, ...) only by `reason` -- treating every 403 as
# rate-limited would misreport a real, non-transient failure as `partial`
# (retry later, this will resolve itself) instead of raising, silently
# masking a problem that will never clear on its own without ever
# surfacing to the caller as anything worse than a stalled sync.
_RATE_LIMIT_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"})
_RATE_LIMIT_ERROR_SUMMARY = "Gmail rate limit exceeded; sync paused, will resume next call"

# `entity_aliases.alias_type` is a free-form `VARCHAR(50)` with no CHECK
# constraint (unlike `connector_accounts.provider`/`personal_domains.
# domain_key`'s closed sets) -- no migration is needed to introduce a new
# value here, matching every other alias_type this codebase already
# writes without a schema change.
_EMAIL_ALIAS_TYPE = "email"

# `email_messages.sender`/`.recipients` are `VARCHAR(320)` (migration
# `0069`) -- `email.utils.parseaddr` itself has no length limit, and a
# `From`/`To` header with no `<...>` delimiter (or one whose "address"
# portion is itself absurdly long) parses successfully into an `address`
# of whatever length the header happened to be. Gmail forwards a sender's
# raw header verbatim; an attacker-controlled sender crafting an
# oversized `From` address previously reached Postgres uncaught as a
# `StringDataRightTruncation` `DataError` -- not caught anywhere between
# `_process_message` and its two call sites in `_sync_messages`/`_sync_
# history`, so it escaped that message's own "malformed input is skipped,
# not raised" contract (see `_process_message`'s own docstring) and
# aborted the *entire* sync call instead of just that one message. Worse
# for `incremental_sync`: since the call never reaches a `return` that
# advances `next_cursor` past this message, the identical oversized
# message is fetched and crashes again on every subsequent call --
# permanently wedging that connector account's sync until manual
# intervention, from a single crafted email. `_parse_address` below
# rejects an over-length address the same way it already rejects an
# address-less header -- found by review, not part of the original
# implementation.
_MAX_EMAIL_ADDRESS_LENGTH = 320


def _pack_credential(access_token: str, refresh_token: str, expires_at: datetime) -> str:
    return dumps(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at.isoformat(),
        }
    )


def _unpack_credential(credential: str) -> dict[str, str]:
    """Every caller (`refresh_permissions`, `disconnect`) catches only
    `(ValueError, TypeError)` around this call -- `loads` itself only ever
    raises `ValueError` (malformed JSON), but valid JSON that isn't an
    object (a list, `null`, a bare number) would decode successfully and
    silently violate this function's own `dict[str, str]` return
    annotation, surfacing later as an uncaught `AttributeError` on the
    caller's first `.get(...)` instead -- found by round 5 review. Raising
    `TypeError` here instead keeps both callers' existing narrow `except`
    sufficient, rather than requiring every call site to separately guard
    against a shape violation this function itself is responsible for.
    """
    data = loads(credential)
    if not isinstance(data, dict):
        raise TypeError(
            f"Gmail credential JSON must decode to an object, got {type(data).__name__}"
        )
    return data


# -- Task 2: DB helpers shared by `backfill`/`incremental_sync` --------------
#
# `ConnectorAccountContext` (the argument every `ConnectorAdapter` method
# receives) carries no `owner_id` -- Phase 6's connector model is
# workspace-shared, not per-owner. Gmail's own `connector_accounts` row does
# have one, though (`gmail_oauth.py`'s `gmail_oauth_callback_endpoint` sets
# it to the connecting user, `auth.user_id`, at INSERT time -- see that
# module's own INSERT), so `_owner_id_for_account` below reads it back
# directly rather than requiring `owner_id` to be threaded through a wider
# Protocol change every other adapter would need to accept and ignore.


def _owner_id_for_account(
    session: Session, workspace_id: UUID, connector_account_id: UUID
) -> UUID | None:
    row = session.execute(
        text(
            "SELECT owner_id FROM connector_accounts "
            "WHERE workspace_id = :workspace_id AND id = :connector_account_id"
        ),
        {"workspace_id": workspace_id, "connector_account_id": connector_account_id},
    ).one_or_none()
    return row[0] if row is not None else None


def _email_consent_active(session: Session, workspace_id: UUID, owner_id: UUID) -> bool:
    """Plan Task 2: "each re-invocation re-verifies the `email` domain's
    `domain_consents` row is still active at call time, not merely at
    original connect time" -- called both before a sync call starts
    fetching anything, and again before writing each message it fetches
    (see `_sync_messages`), so a consent revoked mid-call halts further
    writes rather than only being checked once at the top.
    """
    row = session.execute(
        text(
            "SELECT 1 FROM domain_consents WHERE workspace_id = :workspace_id "
            "AND owner_id = :owner_id AND domain_key = 'email' AND revoked_at IS NULL"
        ),
        {"workspace_id": workspace_id, "owner_id": owner_id},
    ).one_or_none()
    return row is not None


def _normalize_email(value: str) -> str:
    return value.strip().casefold()


def _contains_nul(value: str) -> bool:
    """Postgres `text`/`varchar` columns can never store a `0x00` byte, at
    all, regardless of column width -- a distinct constraint from
    `_MAX_EMAIL_ADDRESS_LENGTH`'s column-width check, and one no valid RFC
    5322 header value should ever trigger, but Gmail forwards a sender's
    raw header verbatim and `json.loads` happily decodes a `\\u0000`
    escape in Gmail's own response body into a real NUL character in the
    resulting Python string -- reaching this adapter no differently than
    any other header byte. Every one of `sender`/`recipients`/`canonical_
    name`/`subject` is a plain `text`/`varchar` column (migrations `0069`/
    `0001`); an unguarded NUL previously reached Postgres uncaught
    (`psycopg.DataError: PostgreSQL text fields cannot contain NUL (0x00)
    bytes`), aborting the entire sync call for a single malformed message
    -- the identical failure class `_MAX_EMAIL_ADDRESS_LENGTH` already
    closed for over-length addresses, found by the same review pass.
    """
    return "\x00" in value


def _parse_address(header_value: str) -> tuple[str, str] | None:
    """`From`/`To` header values are `"Display Name <addr@example.com>"` or
    a bare `addr@example.com` -- `email.utils.parseaddr` (stdlib, not a
    hand-rolled regex) handles both, plus the quoted-display-name and
    comment-syntax edge cases a naive `<...>` split would mis-parse.
    Returns `(display_name, address)` (`display_name` `""` when the header
    carried no name portion), or `None` for a header this adapter cannot
    make sense of (empty, an address-less comment-only value, an address
    longer than `email_messages.sender`/`.recipients` can ever store --
    see `_MAX_EMAIL_ADDRESS_LENGTH`'s own comment -- or either half
    containing a NUL byte no `text`/`varchar` column can ever store, see
    `_contains_nul`'s own comment) rather than a placeholder a caller
    might mistake for a real, resolvable identity.
    """
    display_name, address = parseaddr(header_value)
    if (
        not address
        or len(address) > _MAX_EMAIL_ADDRESS_LENGTH
        or _contains_nul(address)
        or _contains_nul(display_name)
    ):
        return None
    return display_name, address


def _resolve_or_create_person(
    *,
    workspace_id: UUID,
    owner_id: UUID,
    email: str,
    display_name: str,
    source_ref: str,
    now: datetime,
) -> UUID:
    """Entity-resolution match hierarchy level 3 (`docs/phases/phase-002/
    ENTITY-RESOLUTION-CONTRACT.md`: "Exact normalized workspace-scoped
    identifier such as verified email") -- deterministic, so this always
    either attaches to an existing entity or creates a new one outright;
    it never creates a `resolution_candidates` row (that machinery is for
    *fuzzy* level-5 matches this exact-identifier case never needs).

    Opens and commits its own short transaction rather than reusing a
    caller-supplied `Session` -- keeping entity resolution independent of
    whatever transaction (if any) writes the `email_messages` row that
    triggered it means a resolution race (two messages in the same sync
    call, or two concurrent sync calls, naming the same participant for
    the first time) only ever risks re-running *this* function, never
    rolling back an unrelated message write alongside it.
    """
    normalized = _normalize_email(email)
    with SessionFactory() as session, session.begin():
        existing = session.execute(
            text(
                "SELECT entity_id FROM entity_aliases WHERE workspace_id = :workspace_id "
                "AND alias_type = :alias_type AND normalized_value = :normalized_value"
            ),
            {
                "workspace_id": workspace_id,
                "alias_type": _EMAIL_ALIAS_TYPE,
                "normalized_value": normalized,
            },
        ).one_or_none()
        if existing is not None:
            return cast(UUID, existing[0])

        node_id = uuid4()
        evidence_id = uuid4()
        canonical_name = display_name.strip() or email
        try:
            session.execute(
                text(
                    """
                    INSERT INTO pkos_nodes (
                        id, workspace_id, node_type, canonical_name, attributes,
                        status, confidence, version, created_at, updated_at,
                        owner_id, visibility
                    ) VALUES (
                        :id, :workspace_id, 'person', :canonical_name, '{}'::jsonb,
                        'active', 1.00, 1, :now, :now, :owner_id, 'workspace'
                    )
                    """
                ),
                {
                    "id": node_id,
                    "workspace_id": workspace_id,
                    "canonical_name": canonical_name,
                    "now": now,
                    "owner_id": owner_id,
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO pkos_evidence (
                        id, workspace_id, node_id, source_type, source_ref, sha256,
                        captured_at, evidence_state
                    ) VALUES (
                        :id, :workspace_id, :node_id, 'gmail_sync', :source_ref, :sha256,
                        :now, 'available'
                    )
                    """
                ),
                {
                    "id": evidence_id,
                    "workspace_id": workspace_id,
                    "node_id": node_id,
                    "source_ref": source_ref,
                    "sha256": sha256(source_ref.encode()).hexdigest(),
                    "now": now,
                },
            )
            session.execute(
                text(
                    """
                    INSERT INTO entity_aliases (
                        id, workspace_id, entity_id, alias_type, normalized_value,
                        source_id, confidence, created_at
                    ) VALUES (
                        :id, :workspace_id, :entity_id, :alias_type, :normalized_value,
                        :source_id, 1.00, :now
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "entity_id": node_id,
                    "alias_type": _EMAIL_ALIAS_TYPE,
                    "normalized_value": normalized,
                    "source_id": evidence_id,
                    "now": now,
                },
            )
        except IntegrityError:
            # Lost a race against a concurrent resolution of the same
            # email (`uq_entity_aliases_workspace_type_value`) -- the
            # winner's row is authoritative; use it rather than raising.
            session.rollback()
            with SessionFactory() as retry_session, retry_session.begin():
                winner = retry_session.execute(
                    text(
                        "SELECT entity_id FROM entity_aliases WHERE workspace_id = :workspace_id "
                        "AND alias_type = :alias_type AND normalized_value = :normalized_value"
                    ),
                    {
                        "workspace_id": workspace_id,
                        "alias_type": _EMAIL_ALIAS_TYPE,
                        "normalized_value": normalized,
                    },
                ).one()
                return cast(UUID, winner[0])
        return node_id


def _upsert_thread(
    session: Session,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    connector_account_id: UUID,
    external_thread_id: str,
    subject: str | None,
    last_message_at: datetime,
    now: datetime,
) -> UUID:
    row = session.execute(
        text(
            """
            INSERT INTO email_threads (
                id, workspace_id, owner_id, domain_key, connector_account_id,
                external_thread_id, subject, last_message_at, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :owner_id, 'email', :connector_account_id,
                :external_thread_id, :subject, :last_message_at, :now, :now
            )
            ON CONFLICT (workspace_id, connector_account_id, external_thread_id) DO UPDATE SET
                last_message_at = GREATEST(email_threads.last_message_at, EXCLUDED.last_message_at),
                subject = COALESCE(email_threads.subject, EXCLUDED.subject),
                updated_at = :now
            RETURNING id
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "connector_account_id": connector_account_id,
            "external_thread_id": external_thread_id,
            "subject": subject,
            "last_message_at": last_message_at,
            "now": now,
        },
    ).one()
    return cast(UUID, row[0])


def _insert_message_if_new(
    session: Session,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    thread_id: UUID,
    external_message_id: str,
    sender: str,
    recipients: list[str],
    sent_at: datetime,
    direction: str,
    now: datetime,
) -> UUID | None:
    """`ON CONFLICT DO NOTHING` -- a re-synced message (backfill re-run,
    incremental/backfill overlap) is a silent no-op, matching `CONNECTOR-
    CONTRACT.md`'s "Backfill resumes without duplicate projections."
    Returns `None` when the row already existed, so the caller can skip
    entity resolution for an already-processed message's participants.
    """
    row = session.execute(
        text(
            """
            INSERT INTO email_messages (
                id, workspace_id, owner_id, thread_id, external_message_id,
                sender, recipients, sent_at, direction, snippet, body,
                body_fetched_at, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :owner_id, :thread_id, :external_message_id,
                :sender, :recipients, :sent_at, :direction, NULL, NULL,
                NULL, :now, :now
            )
            ON CONFLICT (workspace_id, thread_id, external_message_id) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "thread_id": thread_id,
            "external_message_id": external_message_id,
            "sender": sender,
            "recipients": recipients,
            "sent_at": sent_at,
            "direction": direction,
            "now": now,
        },
    ).one_or_none()
    return row[0] if row is not None else None


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    error = body.get("error")
    if not isinstance(error, dict):
        return False
    errors = error.get("errors")
    if not isinstance(errors, list):
        return False
    return any(
        isinstance(item, dict) and item.get("reason") in _RATE_LIMIT_REASONS for item in errors
    )


def _bearer_headers(credential: str) -> dict[str, str]:
    access_token = _unpack_credential(credential).get("access_token", "")
    return {"Authorization": f"Bearer {access_token}"}


def _coerce_int(value: Any) -> int | None:
    """`dict.get(...)`'s return type (`Any | None`) does not narrow to a
    plain `Any` for a bare `int(...)` call the way it does once assigned
    to a locally-typed variable first -- every Gmail response field this
    adapter converts to an `int` (a `historyId`, `internalDate`) funnels
    through here rather than repeating the same `is None` guard at each
    call site.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class GmailAdapter:
    provider = "gmail"
    required_scopes: frozenset[str] = REQUIRED_SCOPES

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        oauth_kwargs: dict[str, Any] = {
            "base_url": GOOGLE_OAUTH_BASE_URL,
            "timeout": timeout_seconds,
        }
        gmail_kwargs: dict[str, Any] = {"base_url": GMAIL_API_BASE_URL, "timeout": timeout_seconds}
        if transport is not None:
            oauth_kwargs["transport"] = transport
            gmail_kwargs["transport"] = transport
        self._oauth_client = httpx.Client(**oauth_kwargs)
        self._gmail_client = httpx.Client(**gmail_kwargs)
        self._sleep = sleep

    # -- Gmail-specific, not part of either Protocol (see module docstring) --

    def is_account_allowed(self, email: str) -> bool:
        """`email` empty/blank never matches -- an unset allowlist entry
        (`""` between two commas, or the setting itself entirely empty)
        must never be treated as "any account allowed."
        """
        settings = get_settings()
        allowlist = {
            entry.strip().casefold()
            for entry in settings.gmail_oauth_allowlist.split(",")
            if entry.strip()
        }
        normalized = email.strip().casefold()
        return bool(normalized) and normalized in allowlist

    # -- OAuth2ConnectorAdapter -----------------------------------------------

    def get_authorization_url(self, state: str) -> str:
        settings = get_settings()
        if not settings.gmail_oauth_client_id or not settings.gmail_oauth_redirect_uri:
            raise AdapterAuthorizationError("Gmail OAuth client is not configured")
        params = {
            "client_id": settings.gmail_oauth_client_id,
            "redirect_uri": settings.gmail_oauth_redirect_uri,
            "response_type": "code",
            "scope": " ".join(sorted(REQUIRED_SCOPES)),
            # `access_type=offline` + `prompt=consent` -- the standard pair
            # that guarantees Google returns a `refresh_token` even for an
            # account that has previously authorized this app (Google
            # otherwise omits it on a repeat consent), since a Gmail
            # connection with no refresh token would silently stop working
            # the moment the initial access token expires.
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_OAUTH_AUTHORIZE_URL}?{urlencode(params)}"

    def handle_oauth_callback(self, code: str, state: str) -> ConnectorAuthorization:
        settings = get_settings()
        if not settings.gmail_oauth_client_id or not settings.gmail_oauth_client_secret:
            raise AdapterAuthorizationError("Gmail OAuth client is not configured")
        try:
            response = self._oauth_client.post(
                "/token",
                data={
                    "code": code,
                    "client_id": settings.gmail_oauth_client_id,
                    "client_secret": settings.gmail_oauth_client_secret,
                    "redirect_uri": settings.gmail_oauth_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
        except httpx.HTTPError as exc:
            raise AdapterAuthorizationError(f"Gmail token exchange failed: {exc}") from exc
        if response.status_code != 200:
            raise AdapterAuthorizationError(
                f"Gmail token exchange failed with status {response.status_code}"
            )
        # Every rejection from here on happens *after* a real, successful
        # token exchange -- Google has already granted something, whether
        # this method goes on to accept or reject it (even the very next
        # check, "response missing a field," can't rule out a real
        # `refresh_token` having come back alongside a missing/empty
        # `access_token`). A bare per-branch `self._revoke_best_effort(...)`
        # before each individual `raise` (this method's own first-round
        # fix) only covers whichever branches someone remembered to add it
        # to -- review found a real gap that shape left behind (a Gmail
        # profile-lookup 5xx, or any other branch added here later, would
        # have leaked a live, ECC-unrecorded grant just like the branches
        # that *were* covered). Wrapping the whole remainder in one
        # try/except instead makes revoke-on-reject the rule the code
        # itself enforces, not a per-branch reminder -- no future rejection
        # branch added inside this block can silently skip it. `refresh_
        # token or ""` below: `_revoke_best_effort` only ever needs a
        # non-empty token to do anything real: Google's own `/revoke` call
        # with an empty token is a harmless, swallowed-by-design no-op
        # (see that method's own docstring), matching the "always safe to
        # call, only useful when there was something to revoke" contract
        # this whole guard already relies on.
        #
        # `response.json()`/`body.get(...)` are inside this same guard, not
        # ahead of it -- round 6 review found a 200 response whose body
        # isn't valid JSON (`response.json()` raises `json.JSONDecodeError`,
        # a `ValueError`) or decodes to something other than an object (a
        # list/`null`/number, `body.get(...)` then raising `AttributeError`)
        # previously escaped uncaught *before* this guard began, both
        # defeating the router's `AdapterAuthorizationError`-only catch
        # (surfacing as an unhandled 500 outside the app's structured error
        # envelope) and, for the same reason every branch inside this guard
        # exists, skipping revoke-on-reject entirely -- the same "shape
        # trusted without validation" bug class as `_unpack_credential`
        # (see that function's own docstring), just one call earlier.
        # `refresh_token` is pre-initialized so the `except` clause below
        # has a value to revoke (empty, if parsing failed before assignment
        # -- a harmless no-op per the same contract noted above).
        refresh_token: str | None = None
        try:
            try:
                body = response.json()
            except ValueError as exc:
                raise AdapterAuthorizationError(
                    f"Gmail token exchange returned a non-JSON response body: {exc}"
                ) from exc
            if not isinstance(body, dict):
                raise AdapterAuthorizationError(
                    "Gmail token exchange returned a non-object response body: "
                    f"{type(body).__name__}"
                )
            access_token = body.get("access_token")
            refresh_token = body.get("refresh_token")
            expires_in = body.get("expires_in")
            # Round 8 review: unlike `scope`/`emailAddress` (round 7), a
            # non-string `access_token`/`refresh_token` doesn't crash
            # anywhere in this method -- it would just get silently
            # embedded in the `Bearer` header and JSON-serialized into the
            # persisted credential via `_pack_credential`. Guarded anyway,
            # for the same reason every other field in this response body
            # already is: a non-string truthy value (a JSON number) passes
            # the bare `if not access_token:` check the same way a non-
            # string `emailAddress` did.
            # `isinstance(expires_in, bool)`: round 9 review -- `bool` is a
            # subtype of `int` in Python, so `"expires_in": true/false`
            # would otherwise pass both `is None` and the later
            # `float(expires_in)` coercion silently (`float(True) == 1.0`),
            # storing a credential that claims to expire in ~1 second/
            # immediately instead of being rejected the way every other
            # truthy-but-wrong-type value in this response body now is.
            if (
                not isinstance(access_token, str)
                or not access_token
                or not isinstance(refresh_token, str)
                or not refresh_token
                or expires_in is None
                or isinstance(expires_in, bool)
            ):
                raise AdapterAuthorizationError(
                    "Gmail token exchange response missing access_token/refresh_token/"
                    "expires_in -- a repeat consent without a fresh refresh_token, or a "
                    "malformed response"
                )
            # `body.get("scope", "")`'s default only applies when the key is
            # *absent* -- a response with `"scope": null` (key present,
            # value `None`) would still reach `.split()` and raise an
            # uncaught `AttributeError` (round 7 review: the same "shape
            # trusted without validation" gap rounds 5-6 closed for
            # `_unpack_credential` and the response bodies themselves, one
            # field deeper).
            raw_scope = body.get("scope")
            if raw_scope is not None and not isinstance(raw_scope, str):
                raise AdapterAuthorizationError(
                    f"Gmail token exchange returned a non-string scope: {raw_scope!r}"
                )
            granted = frozenset(s for s in (raw_scope or "").split() if s)
            if not REQUIRED_SCOPES.issubset(granted):
                missing = ", ".join(sorted(REQUIRED_SCOPES - granted))
                raise AdapterAuthorizationError(
                    f"Gmail grant is missing required scope(s): {missing}"
                )

            try:
                expires_in_seconds = float(expires_in)
            except (TypeError, ValueError) as exc:
                raise AdapterAuthorizationError(
                    f"Gmail token exchange returned a non-numeric expires_in: {expires_in!r}"
                ) from exc
            expires_at = datetime.now(UTC).timestamp() + expires_in_seconds
            try:
                # Round 8 review: `float(expires_in)` accepts `inf`/`nan`
                # (real risk, not exotic -- Python's `json.loads` itself
                # accepts the non-strict `Infinity`/`NaN` literals by
                # default) and any huge-but-ordinary integer; either one
                # makes `datetime.fromtimestamp(expires_at, ...)` raise
                # `OverflowError`/`OSError`/`ValueError` -- a type this
                # guard doesn't otherwise anticipate. That call itself only
                # happens later, on the success path *after* this guard
                # already exits (`credential = _pack_credential(...)`
                # below) -- so without this check here, that failure would
                # both skip revoke-on-reject for the just-obtained grant
                # and escape uncaught past the router's `AdapterAuthorization
                # Error`-only catch, the same envelope-bypass bug class
                # rounds 5-7 closed elsewhere. Performing the exact same
                # conversion here, inside the guard, means a bad value is
                # caught here and the real one below is guaranteed to
                # succeed -- not a different, hand-picked bound that could
                # itself drift out of sync with what `fromtimestamp` accepts.
                datetime.fromtimestamp(expires_at, tz=UTC)
            except (OverflowError, OSError, ValueError) as exc:
                raise AdapterAuthorizationError(
                    f"Gmail token exchange returned an out-of-range expires_in: {expires_in!r}"
                ) from exc

            try:
                profile_response = self._gmail_client.get(
                    "/gmail/v1/users/me/profile",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except httpx.HTTPError as exc:
                raise AdapterAuthorizationError(f"Gmail profile lookup failed: {exc}") from exc
            if profile_response.status_code != 200:
                raise AdapterAuthorizationError(
                    f"Gmail profile lookup failed with status {profile_response.status_code}"
                )
            try:
                profile_body = profile_response.json()
            except ValueError as exc:
                raise AdapterAuthorizationError(
                    f"Gmail profile lookup returned a non-JSON response body: {exc}"
                ) from exc
            if not isinstance(profile_body, dict):
                raise AdapterAuthorizationError(
                    "Gmail profile lookup returned a non-object response body: "
                    f"{type(profile_body).__name__}"
                )
            email_address = profile_body.get("emailAddress")
            # A non-string truthy value (e.g. a JSON number) would pass a
            # bare `if not email_address:` check and then raise an uncaught
            # `AttributeError` inside `is_account_allowed`'s own `.strip()`
            # call below -- round 7 review, same gap class as `scope` above.
            if not isinstance(email_address, str) or not email_address:
                raise AdapterAuthorizationError("Gmail profile response missing emailAddress")

            if not self.is_account_allowed(email_address):
                raise AdapterAuthorizationError(
                    f"Gmail account {email_address!r} is not on the internal allowlist"
                )
        except Exception:
            self._revoke_best_effort(refresh_token or "")
            raise

        credential = _pack_credential(
            access_token, refresh_token, datetime.fromtimestamp(expires_at, tz=UTC)
        )
        return ConnectorAuthorization(
            external_account_id=email_address,
            display_name=email_address,
            granted_scopes=granted,
            credential=credential,
        )

    # -- ConnectorAdapter ------------------------------------------------------

    def authorize(self, credential: str) -> ConnectorAuthorization:
        """Never called -- `gmail` accounts are created exclusively through
        `handle_oauth_callback` above (`gmail_oauth.py` never calls
        `authorize`). Present only because `ConnectorAdapter` still declares
        it (see that Protocol's own docstring on `OAuth2ConnectorAdapter`
        adapters using this instead of relying on `authorize`); raising
        unconditionally documents that this path is genuinely unreachable in
        production rather than silently no-op-succeeding.
        """
        raise AdapterAuthorizationError(
            "GmailAdapter.authorize is not used -- Gmail accounts connect via "
            "get_authorization_url/handle_oauth_callback (OAuth2ConnectorAdapter)"
        )

    def backfill(
        self,
        account: ConnectorAccountContext,
        resource_type: str,
        since: datetime | None = None,
    ) -> SyncOutcome:
        """`message` is the only resource type `gmail` accounts ever sync
        (see `connector_accounts.py`'s `ResourceType` widening); any other
        value zero-item-succeeds, matching every other adapter's
        "not-yet-implemented resource type" contract interpretation.

        `since` defaults to `_DEFAULT_BACKFILL_WINDOW` (30 days) per plan
        Task 2 -- explicit callers (the "expand history" UI, not built this
        task) pass a real value for a wider or narrower window.
        """
        if resource_type != "message":
            return SyncOutcome(
                resource_type=resource_type, items_processed=0, status="succeeded", next_cursor=None
            )
        window_start = since or (datetime.now(UTC) - _DEFAULT_BACKFILL_WINDOW)
        query = f"after:{int(window_start.timestamp())}"
        return self._sync_messages(account, query=query)

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        """`cursor` is a Gmail `historyId` (a string-encoded integer, per
        `ConnectorAdapter.incremental_sync`'s own "resumes from cursor, or
        behaves like a fresh backfill if cursor is None" contract).
        """
        if resource_type != "message":
            return SyncOutcome(
                resource_type=resource_type,
                items_processed=0,
                status="succeeded",
                next_cursor=cursor,
            )
        if cursor is None:
            return self.backfill(account, resource_type, since=None)
        return self._sync_history(account, start_history_id=cursor)

    def _request_with_rate_limit_retry(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        """Mirrors `github_adapter.py`'s identically-named method -- one
        bounded wait, one retry; a still-rate-limited retry gives up rather
        than being trusted as a normal response (that adapter's own round-4
        review finding, applied here from the start rather than rediscovered
        a second time). Returns `None` only when rate-limited beyond the
        bounded wait -- callers treat that as "give up for now, resume next
        sync call."
        """
        try:
            response = self._gmail_client.request(method, path, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Gmail request failed: {exc}") from exc
        if not _is_rate_limited(response):
            return response

        wait_seconds = self._rate_limit_wait_seconds(response)
        if wait_seconds > _RATE_LIMIT_MAX_WAIT_SECONDS:
            return None
        self._sleep(wait_seconds)
        try:
            retry_response = self._gmail_client.request(
                method, path, headers=headers, params=params
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Gmail request failed: {exc}") from exc
        if _is_rate_limited(retry_response):
            return None
        return retry_response

    def _rate_limit_wait_seconds(self, response: httpx.Response) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        # Gmail's own quota errors rarely send `Retry-After` (unlike
        # GitHub's `X-RateLimit-Reset`, Gmail has no equivalent header) --
        # a short fixed backoff still gives a per-second quota window a
        # real chance to reset before this call's own bounded-wait budget
        # is spent, matching `_RATE_LIMIT_MAX_WAIT_SECONDS`'s own "long
        # enough to ride out a short reset window inline" framing.
        return 1.0

    def _sync_messages(self, account: ConnectorAccountContext, *, query: str) -> SyncOutcome:
        with SessionFactory() as session, session.begin():
            owner_id = _owner_id_for_account(
                session, account.workspace_id, account.connector_account_id
            )
        if owner_id is None:
            raise RuntimeError("Gmail connector account has no owner_id on record")
        with SessionFactory() as session, session.begin():
            if not _email_consent_active(session, account.workspace_id, owner_id):
                raise RuntimeError("email domain consent is not active")

        headers = _bearer_headers(account.credential)
        now = datetime.now(UTC)
        items_processed = 0
        highest_history_id: int | None = None
        page_token: str | None = None
        calls_made = 0

        while calls_made < _MAX_MESSAGES_PER_CALL:
            list_response = self._request_with_rate_limit_retry(
                "GET",
                "/gmail/v1/users/me/messages",
                headers=headers,
                params={
                    k: v
                    for k, v in {
                        "q": query,
                        "maxResults": _MESSAGE_PAGE_SIZE,
                        "pageToken": page_token,
                    }.items()
                    if v is not None
                },
            )
            if list_response is None:
                return SyncOutcome(
                    resource_type="message",
                    items_processed=items_processed,
                    status="partial",
                    next_cursor=str(highest_history_id) if highest_history_id else None,
                    error_summary=_RATE_LIMIT_ERROR_SUMMARY,
                )
            if list_response.status_code != 200:
                raise RuntimeError(
                    f"Gmail message list failed with status {list_response.status_code}"
                )
            try:
                list_body = list_response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Gmail message list returned a non-JSON response body: {exc}"
                ) from exc
            if not isinstance(list_body, dict):
                raise RuntimeError("Gmail message list returned a non-object response body")

            message_refs = list_body.get("messages")
            if not isinstance(message_refs, list):
                message_refs = []

            budget_exhausted = False
            for ref in message_refs:
                if calls_made >= _MAX_MESSAGES_PER_CALL:
                    # Round 1 review: this page's own `message_refs` still
                    # had unprocessed entries when the shared budget ran
                    # out. Falling through to the `nextPageToken` check
                    # below unconditionally is only correct when there
                    # *is* a next page (the outer `while` then exits on
                    # its own condition and the bottom-of-function
                    # `partial` return fires); when this same page happens
                    # to be Gmail's own *last* page (no `nextPageToken`),
                    # that check would otherwise take the "fully caught
                    # up" branch and report `status="succeeded"` despite
                    # these remaining refs never having been fetched --
                    # silent data loss reported as success, and a
                    # `next_cursor` an `incremental_sync` caller would
                    # trust as "nothing older remains." `budget_exhausted`
                    # forces the same bounded partial outcome the
                    # multiple-page case already gets correctly.
                    budget_exhausted = True
                    break
                message_id = ref.get("id") if isinstance(ref, dict) else None
                if not isinstance(message_id, str) or not message_id:
                    continue
                calls_made += 1

                # Re-checked per message, not merely once at the top of this
                # call -- plan Task 2: "a revoked-mid-window consent halts
                # the call." A long backfill can span the time it takes a
                # user to revoke consent mid-call; this bounds how much
                # further syncing happens after that, to one message's
                # worth of lag rather than the whole remaining call.
                with SessionFactory() as session, session.begin():
                    if not _email_consent_active(session, account.workspace_id, owner_id):
                        return SyncOutcome(
                            resource_type="message",
                            items_processed=items_processed,
                            status="partial",
                            next_cursor=str(highest_history_id) if highest_history_id else None,
                            error_summary="email domain consent was revoked mid-sync",
                        )

                get_response = self._request_with_rate_limit_retry(
                    "GET",
                    f"/gmail/v1/users/me/messages/{message_id}",
                    headers=headers,
                    params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject"]},
                )
                if get_response is None:
                    return SyncOutcome(
                        resource_type="message",
                        items_processed=items_processed,
                        status="partial",
                        next_cursor=str(highest_history_id) if highest_history_id else None,
                        error_summary=_RATE_LIMIT_ERROR_SUMMARY,
                    )
                if get_response.status_code != 200:
                    raise RuntimeError(
                        f"Gmail message fetch failed with status {get_response.status_code}"
                    )
                try:
                    message_body = get_response.json()
                except ValueError as exc:
                    raise RuntimeError(
                        f"Gmail message fetch returned a non-JSON response body: {exc}"
                    ) from exc
                if not isinstance(message_body, dict):
                    raise RuntimeError("Gmail message fetch returned a non-object response body")

                observed_history_id = self._process_message(
                    message_body,
                    workspace_id=account.workspace_id,
                    owner_id=owner_id,
                    connector_account_id=account.connector_account_id,
                    now=now,
                )
                items_processed += 1
                if observed_history_id is not None:
                    highest_history_id = max(highest_history_id or 0, observed_history_id)

            if budget_exhausted:
                break

            page_token = list_body.get("nextPageToken")
            if not isinstance(page_token, str) or not page_token:
                # No further pages -- fully caught up to `query`. Only now
                # is it safe to hand a cursor to `incremental_sync`: a
                # `partial` result (rate-limited, or the page-token loop
                # exiting below with more pages still remaining) means
                # historical mail this call hasn't reached yet could still
                # be older than whatever historyId a partial result would
                # report, so `next_cursor` stays `None` for anything short
                # of a full, uninterrupted pass -- see this method's own
                # `partial`-branch returns above, none of which set it.
                if highest_history_id is None:
                    # Zero messages found in `query`'s window -- no
                    # historyId was ever observed to seed a cursor from.
                    # Falls back to `users.getProfile`, the only other Gmail
                    # endpoint that reports the mailbox's current historyId,
                    # so a subsequent `incremental_sync` call still has a
                    # real cursor to resume from instead of permanently
                    # deferring back to `backfill` every time.
                    profile_response = self._request_with_rate_limit_retry(
                        "GET", "/gmail/v1/users/me/profile", headers=headers
                    )
                    if profile_response is not None and profile_response.status_code == 200:
                        try:
                            profile_body = profile_response.json()
                        except ValueError:
                            profile_body = None
                        if isinstance(profile_body, dict):
                            highest_history_id = _coerce_int(profile_body.get("historyId"))
                return SyncOutcome(
                    resource_type="message",
                    items_processed=items_processed,
                    status="succeeded",
                    next_cursor=str(highest_history_id) if highest_history_id else None,
                )

        return SyncOutcome(
            resource_type="message",
            items_processed=items_processed,
            status="partial",
            next_cursor=None,
            error_summary=(
                f"Gmail message sync hit the {_MAX_MESSAGES_PER_CALL}-message per-call bound "
                "with more messages remaining; sync paused, will resume next call"
            ),
        )

    def _sync_history(
        self, account: ConnectorAccountContext, *, start_history_id: str
    ) -> SyncOutcome:
        with SessionFactory() as session, session.begin():
            owner_id = _owner_id_for_account(
                session, account.workspace_id, account.connector_account_id
            )
        if owner_id is None:
            raise RuntimeError("Gmail connector account has no owner_id on record")
        with SessionFactory() as session, session.begin():
            if not _email_consent_active(session, account.workspace_id, owner_id):
                raise RuntimeError("email domain consent is not active")

        headers = _bearer_headers(account.credential)
        now = datetime.now(UTC)
        items_processed = 0
        latest_history_id: int | None = None
        page_token: str | None = None
        calls_made = 0
        seen_message_ids: set[str] = set()

        while calls_made < _MAX_MESSAGES_PER_CALL:
            history_response = self._request_with_rate_limit_retry(
                "GET",
                "/gmail/v1/users/me/history",
                headers=headers,
                params={
                    k: v
                    for k, v in {
                        "startHistoryId": start_history_id,
                        "historyTypes": "messageAdded",
                        "maxResults": _MESSAGE_PAGE_SIZE,
                        "pageToken": page_token,
                    }.items()
                    if v is not None
                },
            )
            if history_response is None:
                return SyncOutcome(
                    resource_type="message",
                    items_processed=items_processed,
                    status="partial",
                    next_cursor=start_history_id,
                    error_summary=_RATE_LIMIT_ERROR_SUMMARY,
                )
            if history_response.status_code == 404:
                # `startHistoryId` has expired (Gmail retains history for a
                # rolling window, not indefinitely) -- the only correct
                # recovery is a fresh backfill; there is no partial-history
                # replay possible once the server has dropped the record.
                # Disclosed design decision (plan Task 2 names only
                # "polling-based ... via historyId cursor," not this
                # fallback): a real Gmail deployment will eventually hit
                # this on any long-idle connector account, and silently
                # raising instead would strand that account requiring
                # manual disconnect/reconnect to recover.
                return self.backfill(account, "message", since=None)
            if history_response.status_code != 200:
                raise RuntimeError(
                    f"Gmail history fetch failed with status {history_response.status_code}"
                )
            try:
                history_body = history_response.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"Gmail history fetch returned a non-JSON response body: {exc}"
                ) from exc
            if not isinstance(history_body, dict):
                raise RuntimeError("Gmail history fetch returned a non-object response body")

            observed_history_id = _coerce_int(history_body.get("historyId"))
            if observed_history_id is not None:
                latest_history_id = observed_history_id

            history_entries = history_body.get("history")
            if not isinstance(history_entries, list):
                history_entries = []
            message_ids: list[str] = []
            for entry in history_entries:
                added = entry.get("messagesAdded") if isinstance(entry, dict) else None
                if not isinstance(added, list):
                    continue
                for item in added:
                    message = item.get("message") if isinstance(item, dict) else None
                    message_id = message.get("id") if isinstance(message, dict) else None
                    if (
                        isinstance(message_id, str)
                        and message_id
                        and message_id not in seen_message_ids
                    ):
                        seen_message_ids.add(message_id)
                        message_ids.append(message_id)

            budget_exhausted = False
            for message_id in message_ids:
                if calls_made >= _MAX_MESSAGES_PER_CALL:
                    # Round 1 review: same fix as `_sync_messages`'
                    # identical loop shape -- this page's own `message_
                    # ids` still had unprocessed entries when the shared
                    # budget ran out. Without this flag, a bound hit on
                    # Gmail's own last history page (no `nextPageToken`)
                    # would fall through to the "fully caught up" branch
                    # below and report `status="succeeded"` despite these
                    # remaining messages never having been fetched.
                    budget_exhausted = True
                    break
                calls_made += 1

                with SessionFactory() as session, session.begin():
                    if not _email_consent_active(session, account.workspace_id, owner_id):
                        return SyncOutcome(
                            resource_type="message",
                            items_processed=items_processed,
                            status="partial",
                            next_cursor=start_history_id,
                            error_summary="email domain consent was revoked mid-sync",
                        )

                get_response = self._request_with_rate_limit_retry(
                    "GET",
                    f"/gmail/v1/users/me/messages/{message_id}",
                    headers=headers,
                    params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject"]},
                )
                if get_response is None:
                    return SyncOutcome(
                        resource_type="message",
                        items_processed=items_processed,
                        status="partial",
                        next_cursor=start_history_id,
                        error_summary=_RATE_LIMIT_ERROR_SUMMARY,
                    )
                if get_response.status_code != 200:
                    raise RuntimeError(
                        f"Gmail message fetch failed with status {get_response.status_code}"
                    )
                try:
                    message_body = get_response.json()
                except ValueError as exc:
                    raise RuntimeError(
                        f"Gmail message fetch returned a non-JSON response body: {exc}"
                    ) from exc
                if not isinstance(message_body, dict):
                    raise RuntimeError("Gmail message fetch returned a non-object response body")

                self._process_message(
                    message_body,
                    workspace_id=account.workspace_id,
                    owner_id=owner_id,
                    connector_account_id=account.connector_account_id,
                    now=now,
                )
                items_processed += 1

            if budget_exhausted:
                break

            page_token = history_body.get("nextPageToken")
            if not isinstance(page_token, str) or not page_token:
                return SyncOutcome(
                    resource_type="message",
                    items_processed=items_processed,
                    status="succeeded",
                    next_cursor=str(latest_history_id) if latest_history_id else start_history_id,
                )

        return SyncOutcome(
            resource_type="message",
            items_processed=items_processed,
            status="partial",
            next_cursor=start_history_id,
            error_summary=(
                f"Gmail history sync hit the {_MAX_MESSAGES_PER_CALL}-message per-call bound "
                "with more messages remaining; sync paused, will resume next call"
            ),
        )

    def _process_message(
        self,
        body: dict[str, Any],
        *,
        workspace_id: UUID,
        owner_id: UUID,
        connector_account_id: UUID,
        now: datetime,
    ) -> int | None:
        """Writes the thread/message rows and resolves participant
        entities for one already-fetched `messages.get(format=metadata)`
        response body. Returns the message's own `historyId` (Gmail
        includes it on every message resource) when present and
        well-formed, so callers can track the highest historyId seen
        across a sync call.

        A malformed/incomplete response shape is skipped (this message
        contributes to `items_processed` in the caller but writes nothing)
        rather than raising -- unlike `handle_oauth_callback`'s revoke-on-
        reject guard, there is no live grant or rejection consequence a
        skip needs to protect here, and every other message in the same
        sync call is unaffected by one bad one.
        """
        message_id = body.get("id")
        external_thread_id = body.get("threadId")
        if not isinstance(message_id, str) or not message_id:
            return None
        if not isinstance(external_thread_id, str) or not external_thread_id:
            return None

        payload = body.get("payload")
        raw_headers = payload.get("headers") if isinstance(payload, dict) else None
        header_map: dict[str, str] = {}
        if isinstance(raw_headers, list):
            for entry in raw_headers:
                if (
                    isinstance(entry, dict)
                    and isinstance(entry.get("name"), str)
                    and isinstance(entry.get("value"), str)
                ):
                    header_map.setdefault(entry["name"].casefold(), entry["value"])

        from_header = header_map.get("from")
        to_header = header_map.get("to")
        sender_parsed = _parse_address(from_header) if from_header else None
        if sender_parsed is None:
            return None
        sender_name, sender_email = sender_parsed

        recipients: list[str] = []
        recipient_names: dict[str, str] = {}
        if to_header:
            for raw_address in to_header.split(","):
                parsed = _parse_address(raw_address)
                if parsed is not None:
                    name, address = parsed
                    recipients.append(address)
                    recipient_names.setdefault(address, name)
        if not recipients:
            return None

        internal_date_ms = _coerce_int(body.get("internalDate"))
        if internal_date_ms is None:
            return None
        try:
            sent_at = datetime.fromtimestamp(internal_date_ms / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None

        label_ids = body.get("labelIds")
        direction = "outbound" if isinstance(label_ids, list) and "SENT" in label_ids else "inbound"

        history_id = _coerce_int(body.get("historyId"))

        # `email_threads.subject` is nullable (a "no Subject header" and a
        # "Subject header this adapter can't store" both already mean "no
        # subject on record" to any reader of that column) -- unlike a
        # NUL-containing sender/recipient address (which makes the whole
        # message unresolvable and is treated as fully malformed, see
        # `_parse_address`), a NUL-containing `Subject` doesn't prevent
        # resolving who the message is from/to, so only this one field is
        # dropped rather than skipping the entire message over a header
        # nothing else here depends on. See `_contains_nul`'s own comment
        # for why this guard exists at all.
        raw_subject = header_map.get("subject")
        subject = (
            raw_subject if raw_subject is not None and not _contains_nul(raw_subject) else None
        )

        with SessionFactory() as session, session.begin():
            thread_id = _upsert_thread(
                session,
                workspace_id=workspace_id,
                owner_id=owner_id,
                connector_account_id=connector_account_id,
                external_thread_id=external_thread_id,
                subject=subject,
                last_message_at=sent_at,
                now=now,
            )
            inserted_id = _insert_message_if_new(
                session,
                workspace_id=workspace_id,
                owner_id=owner_id,
                thread_id=thread_id,
                external_message_id=message_id,
                sender=sender_email,
                recipients=recipients,
                sent_at=sent_at,
                direction=direction,
                now=now,
            )

        if inserted_id is not None:
            source_ref = f"gmail:{message_id}"
            participants: dict[str, str] = {sender_email: sender_name, **recipient_names}
            for participant_email, participant_name in participants.items():
                _resolve_or_create_person(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    email=participant_email,
                    display_name=participant_name,
                    source_ref=source_ref,
                    now=now,
                )

        return history_id

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: Mapping[str, str]
    ) -> SyncOutcome:
        """Stubbed -- push-notification-based sync is explicitly deferred,
        not yet reconfirmed (design doc's own "Explicitly deferred" list;
        `PHASE-010-gmail-connector.md`'s "polling assumed, Pub/Sub not
        committed" note). No receiving route calls this in this task's
        scope.
        """
        return SyncOutcome(
            resource_type="thread", items_processed=0, status="succeeded", next_cursor=None
        )

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        """A still-valid access token, or one this call successfully
        refreshes via the stored `refresh_token`, is `active`; a refresh
        that Google itself rejects (`invalid_grant` -- the refresh token
        was revoked, e.g. the user removed this app's access from their
        Google Account settings) is `permission_lost`. Does not persist a
        refreshed access token anywhere -- `ecc.domains.engineering.
        connector_accounts` has no call site wired to this method's return
        value yet beyond the `PermissionState` itself (matching every other
        adapter's identical "no HTTP caller yet" disclosed gap from Phase 6
        Task 1); a future task threading a refreshed credential back into
        storage is separate work from detecting the loss itself.
        """
        try:
            credential = _unpack_credential(account.credential)
        except (ValueError, TypeError):
            return "permission_lost"
        settings = get_settings()
        try:
            response = self._oauth_client.post(
                "/token",
                data={
                    "refresh_token": credential.get("refresh_token", ""),
                    "client_id": settings.gmail_oauth_client_id,
                    "client_secret": settings.gmail_oauth_client_secret,
                    "grant_type": "refresh_token",
                },
            )
        except httpx.HTTPError:
            return "active"
        if response.status_code == 400:
            return "permission_lost"
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        """Real provider-side revocation -- unlike every existing PAT-based
        adapter (none of which have a revocation API this connector can
        call on the user's behalf), Google's OAuth2 revoke endpoint really
        does end the grant. Best-effort: a revoke call Google itself
        rejects (already-revoked, malformed token) does not raise --
        `CONNECTOR-CONTRACT.md`'s "must not raise for provider does not
        support revocation" extends here to "does not raise for revocation
        that turns out to be a no-op," since the caller's own intent
        (disconnect) is satisfied either way.
        """
        try:
            credential = _unpack_credential(account.credential)
        except (ValueError, TypeError):
            return None
        self._revoke_best_effort(credential.get("refresh_token", ""))

    def _revoke_best_effort(self, refresh_token: str) -> None:
        """Shared by `disconnect` and the single `try/except` guarding
        every post-token-exchange rejection branch inside `handle_oauth_
        callback` (see that method's own comment) -- each obtains a real,
        live Google grant before discovering the rejection, and none of
        them ever persists a `connector_accounts` row for it, so
        `disconnect` (which needs one) can never be reached to clean it up
        otherwise. Without this, a rejected callback would leave a
        standing, ECC-unrecorded OAuth grant for `gmail.metadata`/`gmail.
        readonly` at Google that only the account owner manually visiting
        Google's own third-party-app permissions page could end -- found
        by review (an initial fix covering only two of the six actual
        rejection branches individually was itself a second review-found
        gap, closed by switching to the single-guard shape instead), not
        the original implementation.
        """
        try:
            self._oauth_client.post("/revoke", data={"token": refresh_token})
        except httpx.HTTPError:
            pass
        return None
