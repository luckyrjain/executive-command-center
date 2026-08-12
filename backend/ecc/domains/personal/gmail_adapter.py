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
expired (a 404 -- see `_sync_history`'s own `history_response.status_code
== 404` branch comment for why this fallback is a disclosed design
decision, not something the plan document named explicitly; neither
`_sync_history` nor `_sync_messages` carries its own docstring -- found by
round 13 review, this cross-reference had pointed at a docstring that
never existed since the original Task 2 commit). Every resource type
other than `"message"` still
zero-item-succeeds, matching `github_adapter.py`'s own "an unimplemented
resource type no-op-succeeds rather than raises" contract interpretation.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.errors import InvalidHeaderDefect
from email.headerregistry import HeaderRegistry
from email.utils import getaddresses, parseaddr
from hashlib import sha256
from json import dumps, loads
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import execute_run
from ecc.domains.engineering.connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorAuthorization,
    PermissionState,
    SyncOutcome,
)
from ecc.domains.governance.recommendation_models import RecommendationCreate
from ecc.domains.governance.recommendation_mutations import create_recommendation, synthetic_request

from .crypto import encrypt_field

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

# Task 5's own, much smaller bound -- deliberately not `_MAX_MESSAGES_PER_
# CALL`. `connector_accounts.py`'s own module docstring justifies its
# synchronous, non-worker-dispatched `/sync` handler on the premise that
# "a sync call is a single adapter method call" -- true for `backfill`/
# `incremental_sync` themselves (a `messages.get` round trip per message,
# cheap), but `detect_actions_since` adds a *second*, much more expensive
# round trip per candidate on top of that: a live Gmail body fetch *and* a
# live `execute_run` Ollama inference call (`router.py`'s own
# `timeout_seconds=40.0` per model call), run sequentially, still inside
# that same synchronous request handler. Reusing `_MAX_MESSAGES_PER_CALL`
# (200) here would let one `/sync` call block for up to 200 sequential
# Gmail-plus-Ollama round trips before the HTTP response returns --
# realistically minutes, worst-case well past most reverse-proxy/gateway
# timeouts -- found by round 2 review. A smaller bound reduces, but does
# not eliminate, that risk; it is not a substitute for moving this hook to
# a durable background dispatch (`ecc.domains.automation.worker`) before
# `email_action_detection_enabled` is ever turned on outside a controlled
# test, which remains the real fix and is out of scope for this task.
_MAX_ACTION_DETECTIONS_PER_CALL = 5

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

# `email_messages.recipients` is an unbounded `postgresql.ARRAY(VARCHAR(320))`
# (migration `0069`) -- storing an enormous array is not itself a crash
# risk the way an individual oversized address is (see `_MAX_EMAIL_
# ADDRESS_LENGTH`'s own comment, a per-element bound this one doesn't
# duplicate). The actual cost is `_resolve_or_create_person`: it opens its
# own independent `SessionFactory` transaction -- a real, synchronous,
# per-participant round trip to Postgres, not merely an in-memory parse --
# for every distinct sender/recipient `_process_message` hands it. Gmail
# forwards a sender's raw `To` header verbatim and RFC 5322 places no
# upper bound on how many addresses it may list; nothing before this guard
# bounded that count. `_MAX_MESSAGES_PER_CALL` already bounds total
# messages fetched in one `backfill`/`incremental_sync` call, but nothing
# bounded work *within* a single message -- a single crafted message with
# an extreme recipient count could still make one call do an effectively
# unbounded amount of synchronous DB work (and open that many short-lived
# connections) before ever finishing that one message, regardless of how
# small `_MAX_MESSAGES_PER_CALL` itself is -- found by review, a resource-
# exhaustion angle distinct from every prior malformed-input crash/
# corruption finding in this file. 500 matches Gmail's own documented
# per-message recipient limit for a consumer account (a generous cap
# genuine mail essentially never approaches); a header exceeding it keeps
# only the first this many valid recipients, in header order, rather than
# rejecting the whole message -- the same "degrade gracefully instead of
# failing outright" spirit as every other bound in this file.
_MAX_RECIPIENTS_PER_MESSAGE = 500

# `_MAX_RECIPIENTS_PER_MESSAGE` bounds *downstream* cost -- the DB work
# `_resolve_or_create_person` does per parsed recipient -- but nothing
# before this guard bounded the cost of *parsing* the header in the first
# place, and that parse is not free: `_to_header_has_grammar_defect`'s own
# `HeaderRegistry` cross-check (see that function's own docstring) is a
# from-scratch recursive-descent parser (`email._header_value_parser`)
# that reslices its remaining input on every token consumed rather than
# tracking an index into it -- independently profiled (`cProfile` against
# `HeaderRegistry()("To", ...)` directly): parse time grows roughly
# quadratically with the number of comma-separated fields in the header,
# not merely the `RecursionError` stack-depth hazard round 6 already
# closed for deeply *nested* comment syntax. A 2,000-address `To` header
# costs ~0.1s of pure CPU in a single call; 8,000 addresses ~2.4s; 16,000
# ~10s -- no crash, no exception to catch, nothing downstream of this call
# ever runs to bound it. The same quadratic-ish cost shows up for other,
# comma-free shapes too (a single mailbox with an extremely long
# many-word display name, or an extremely long dot-atom domain) --
# independently confirmed -- so a comma-count heuristic alone would not
# close every avenue; a length cap on the whole raw header does, since
# every shape that costs this much CPU necessarily costs that much
# *string*. Gmail forwards a sender's raw `To` header verbatim and RFC
# 5322 places no upper bound on a header's length; a sender fully
# controls their own outgoing headers -- the identical "single crafted
# message reaches unbounded synchronous cost, stalling the whole sync
# call" resource-exhaustion class `_MAX_RECIPIENTS_PER_MESSAGE`'s own
# comment already documents for entity-resolution DB work, found here one
# call earlier, in the parse itself -- found by round 7 review. 50,000
# characters is deliberately generous relative to `_MAX_RECIPIENTS_PER_
# MESSAGE`'s own 500-recipient cap (~100 characters per recipient entry,
# well above a typical `"Name" <address>, ` entry's real length, and still
# well under `_MAX_EMAIL_ADDRESS_LENGTH`'s own 320-character *address*
# bound times 500) -- no genuine message this adapter needs to parse
# correctly is expected to approach it -- while bounding the worst
# measured shape's cost at that length to well under a second (~0.64s,
# independently measured) rather than leaving it unbounded. A header
# exceeding it is treated the same as any other header this adapter
# "can't make sense of" (see `_getaddresses_resilient`'s/`_to_header_has_
# grammar_defect`'s own docstrings for that established contract): zero
# recipients, dropping the message via `_process_message`'s own `if not
# recipients: return None`, rather than spending unbounded CPU trying.
_MAX_TO_HEADER_LENGTH = 50_000


def _to_header_exceeds_length_limit(to_header: str) -> bool:
    return len(to_header) > _MAX_TO_HEADER_LENGTH


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


# Not underscore-prefixed: `ecc.domains.attention.attention` (a different
# domain package) imports this directly to reproduce the exact same
# `entity_aliases.normalized_value` normalization when matching a thread's
# last-inbound sender against a resolved contact (see that module's own
# `_score_awaiting_reply`/`regenerate_attention` comments for why the match
# has to happen in Python rather than SQL). Every other private-helper
# import elsewhere in this codebase (`identity/invitations.py` <-
# `identity/accounts.py`, `engineering/write_actions.py` <-
# `engineering/{gitlab,jira}_adapter.py`) is between sibling modules in the
# *same* domain package and keeps the leading underscore; this is the one
# cross-domain case, so it gets a real public name instead of reaching past
# another domain's underscore.
def normalize_email(value: str) -> str:
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


def _contains_unpaired_surrogate(value: str) -> bool:
    """A second, orthogonal way a `str` that survived `json.loads` cleanly
    can still be unwritable to Postgres -- found by round 2 review, a
    distinct failure class from both `_contains_nul` and `_MAX_EMAIL_
    ADDRESS_LENGTH` above (neither of which catches this). Strict JSON
    grammar (RFC 8259) specifies `\\uXXXX` as a raw UTF-16 code unit, not
    a validated Unicode scalar value, so a lone (unpaired) surrogate
    escape -- e.g. `\\ud800` with no following low surrogate -- is valid
    JSON text; Python's `json.loads` decodes it without complaint into a
    `str` that LOOKS like any other string but cannot be encoded to UTF-8
    (`str.encode("utf-8")` itself raises `UnicodeEncodeError: 'utf-8'
    codec can't encode character '\\ud800' ... surrogates not allowed`).
    psycopg needs exactly that encoding to put a text parameter on the
    wire, so this reaches the database driver as an uncaught, un-typed
    Python `UnicodeEncodeError` -- not even a DB-side `DataError` the way
    an oversized address or an embedded NUL byte would be -- aborting the
    entire sync call for a single malformed message, permanently wedging
    `incremental_sync` the same way `_MAX_EMAIL_ADDRESS_LENGTH`'s own
    comment describes for its own failure class. A real Gmail sender can
    plausibly trigger this via a malformed RFC 2047 encoded-word header
    (`=?UTF-16?B?...?=`) that Gmail's own header decoder resolves
    leniently into surrogate-carrying text rather than rejecting outright
    -- reachable input, not merely a hypothetical one. Every guard below
    that already checks `_contains_nul` on Gmail-sourced text checks this
    too, for the same reason.
    """
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


# `email_threads.external_thread_id`/`email_messages.external_message_id`
# are `VARCHAR(255)` (migration `0069`) -- Gmail's own `id`/`threadId`
# fields are, in every documented and observed case, short opaque hex
# strings the Gmail backend itself assigns (not attacker-supplied header
# content the way `From`/`To` are), but nothing in this adapter -- or in
# Gmail's own JSON response contract -- actually guarantees that shape.
# `_process_message` already treats every other field pulled from this
# same response body as untrusted-until-validated (type-checked, `None`-
# checked); an oversized or NUL-/surrogate-carrying `id`/`threadId` would
# reach these two bounded columns the same unguarded way `sender`/
# `recipients` did before round 1's fix, for the identical uncaught-
# DataError/UnicodeEncodeError, whole-call-aborting, cursor-never-
# advances failure class -- found by round 2 review. Matches
# `_MAX_EMAIL_ADDRESS_LENGTH`'s own column-width bound in spirit, sized to
# this pair of columns instead.
_MAX_EXTERNAL_ID_LENGTH = 255


def _is_valid_external_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_EXTERNAL_ID_LENGTH
        and not _contains_nul(value)
        and not _contains_unpaired_surrogate(value)
    )


def _validate_address(display_name: str, address: str) -> tuple[str, str] | None:
    """Shared validation for an already-split `(display_name, address)`
    pair -- factored out of `_parse_address` (round 2 review) so
    `_process_message`'s recipient loop can apply the identical guard to
    each result of `email.utils.getaddresses` (which is comma-list-aware,
    see that call site's own comment) without re-parsing an already-parsed
    pair back through `parseaddr`.

    `None` for a pair this adapter cannot make sense of (no address, an
    address longer than `email_messages.sender`/`.recipients` can ever
    store -- see `_MAX_EMAIL_ADDRESS_LENGTH`'s own comment -- or either
    half containing a NUL byte or an unpaired UTF-16 surrogate no
    `text`/`varchar` column can ever store, see `_contains_nul`'s/
    `_contains_unpaired_surrogate`'s own comments) rather than a
    placeholder a caller might mistake for a real, resolvable identity.
    """
    if (
        not address
        or len(address) > _MAX_EMAIL_ADDRESS_LENGTH
        or _contains_nul(address)
        or _contains_nul(display_name)
        or _contains_unpaired_surrogate(address)
        or _contains_unpaired_surrogate(display_name)
    ):
        return None
    return display_name, address


def _parse_address(header_value: str) -> tuple[str, str] | None:
    """`From`/`To` header values are `"Display Name <addr@example.com>"` or
    a bare `addr@example.com` -- `email.utils.parseaddr` (stdlib, not a
    hand-rolled regex) handles both, plus the quoted-display-name and
    comment-syntax edge cases a naive `<...>` split would mis-parse. Used
    for the (always single-address) `From` header; see `_validate_
    address`'s own docstring for the multi-address `To` case and what
    `None` here means.

    Wrapped in `try`/`except RecursionError` -- round 6 review: RFC 5322
    permits arbitrarily nested `(...)` comments inside an address, and
    `parseaddr`'s underlying `_AddressList`/`_parseaddr` grammar recurses
    one stack frame per nesting level with no depth cap of its own.
    Independently confirmed in a standalone interpreter: a `From` header of
    only ~500 nested, *matched* parens around an otherwise ordinary address
    (`"(" * 497 + "a@example.test" + ")" * 497`, 1008 bytes total -- just
    over a single RFC 5322 unfolded header line's 998-byte *recommended*
    limit, which is advisory, not a hard cutoff any parser enforces, and
    moot here regardless since this module receives Gmail's response as an
    already-decoded JSON string field, never raw SMTP line-folded bytes)
    raises an uncaught `RecursionError`, not any exception type this
    module already anticipated. Gmail forwards a sender's raw `From`
    header verbatim and a
    sender fully controls their own outgoing headers -- the identical
    "single crafted message reaches an uncaught crash, aborting the entire
    sync call, and for `incremental_sync` wedging that account permanently
    since the cursor never advances past it" failure class every guard
    above this one exists to close (see `_MAX_EMAIL_ADDRESS_LENGTH`'s own
    comment), reached here via a stdlib recursion depth rather than a
    length/NUL/surrogate check. Treated as unparseable, the same as any
    other header `parseaddr` can't make sense of.
    """
    try:
        display_name, address = parseaddr(header_value)
    except RecursionError:
        return None
    return _validate_address(display_name, address)


def _getaddresses_resilient(to_header: str) -> list[tuple[str, str]]:
    """`_process_message`'s `To`-header call site for `getaddresses`
    (round 2 review's fix for the naive-`split(",")` comma-in-display-name
    corruption bug, see `_validate_address`'s own docstring) -- but
    `getaddresses` itself defaults to `strict=True` (CPython's own fix for
    a *different* header-spoofing class, e.g. `getaddresses(['alice@x.com
    <bob@y.com>'])` -- no comma at all, one field that parses ambiguously
    into two addresses): it counts each fieldvalue's own top-level,
    outside-quotes commas, and if the number of addresses it actually
    parsed doesn't match `1 + comma_count`, it discards the *entire*
    parse and returns a single `[("", "")]` sentinel -- not just the
    offending part.

    That counting formula silently miscounts one common, entirely benign
    case: a header ending in a bare trailing comma (`"alice@x.test,
    bob@x.test,"` -- real mail systems produce this; Gmail forwards a
    sender's raw header verbatim, and a sender fully controls their own
    outgoing headers). A trailing comma is consumed without leaving a
    padding empty tuple behind (unlike a *leading* or *internal* stray
    comma, both of which parse to an explicit `("", "")` entry that keeps
    the count formula satisfied and are already handled correctly, without
    needing this function at all, since `_validate_address` drops that
    empty entry same as it drops any other invalid one) -- so the parsed
    count comes in one short of expected, and `getaddresses` treats the
    whole header as unparseable. Every real recipient in an otherwise
    perfectly well-formed `To` header is silently discarded, `recipients`
    ends up empty, and `_process_message`'s own `if not recipients: return
    None` drops the *entire* message -- sender, subject, thread, all of
    it -- with no error, no `error_summary`, nothing in `items_processed`
    to distinguish it from a message that was never fetched at all. An
    attacker who wants a message to their target's connected inbox to
    never appear in the synced entity graph needs only append one comma
    to their own `To` header -- found by round 3 review, a new failure
    mode introduced by round 2's own fix (round 2's test suite exercises
    `getaddresses` only via well-formed and quoted-comma headers, never a
    trailing-comma one).

    Retrying, with every bare trailing comma (plus any trailing whitespace
    interspersed among them) stripped, recovers this case without touching
    `getaddresses`' actual protection: a header with no comma at all to
    strip is returned unchanged (still correctly rejected for the specific
    no-comma string, e.g. the `alice@x.com<bob@y.com>` spoofing shape
    above -- that case has no trailing comma, so the retry is a no-op and
    the sentinel stands; see `_to_header_has_grammar_defect`'s own
    docstring for why a comma-*appended* variant of that same shape is a
    materially different, separately-guarded case, not covered by this
    reasoning), and a quoted display name's own internal comma is never
    touched (only a comma that is the header's own trailing character,
    outside any quote/angle-bracket construct by construction -- a valid
    one must already be closed before the string ends -- is ever
    stripped).

    Both `getaddresses` calls are wrapped in `try`/`except RecursionError`
    -- round 6 review, the identical unbounded-comment-nesting hazard
    `_parse_address`'s own docstring documents for `parseaddr` (the two
    share the same underlying `_AddressList`/`_parseaddr` grammar, and
    independently confirmed to crash the same way here: `getaddresses(["("
    * 497 + "a@example.test" + ")" * 497])` raises an uncaught
    `RecursionError` (see `_parse_address`'s own docstring for why the
    exact byte count here doesn't matter to this module either way). A
    `To` header is exactly as attacker-controlled as `From`, so
    treating a `RecursionError` here as "unparseable" -- returning the same
    `[("", "")]` sentinel `_validate_address` already discards downstream,
    same as the existing no-comma-to-strip return path -- keeps this
    function's contract intact (an un-recoverable header yields no
    recipients, dropping the message via `_process_message`'s own `if not
    recipients: return None`, rather than crashing the whole sync call and
    permanently wedging `incremental_sync` behind it).
    """
    try:
        parsed = getaddresses([to_header])
    except RecursionError:
        return [("", "")]
    if parsed != [("", "")]:
        return parsed
    stripped = to_header.rstrip()
    trimmed_any = False
    while stripped.endswith(","):
        stripped = stripped[:-1].rstrip()
        trimmed_any = True
    if not trimmed_any or not stripped:
        return parsed
    try:
        return getaddresses([stripped])
    except RecursionError:
        return [("", "")]


# `getaddresses(strict=True)`'s own address-count check (see `_getaddresses_
# resilient`'s own docstring for the full mechanism) compares only the
# *aggregate* parsed-address count against `1 + top-level-comma-count` --
# never that each individual comma-delimited field parses to exactly one
# address. `HeaderRegistry` -- a from-scratch RFC 5322 grammar parser, not
# a count heuristic -- is used only as a defect cross-check on top of the
# existing `getaddresses`-based parse (see `_to_header_has_grammar_defect`'s
# own docstring for why this collision can't be closed by post-processing
# `getaddresses`' own return value any further).
_TO_HEADER_REGISTRY = HeaderRegistry()


def _to_header_has_grammar_defect(to_header: str) -> bool:
    """`_getaddresses_resilient`'s own trailing-comma fix (round 3 review)
    closed one failure mode of `getaddresses(strict=True)`'s aggregate
    `1 + comma_count` address-count check -- round 4 review found the
    identical check can be fooled in the *opposite*, more consequential
    direction. `getaddresses(['bob@example.test<carol@example.test>'])` --
    the exact ambiguous, no-comma "one field parses into two addresses"
    shape `strict=True` exists to reject (see `_getaddresses_resilient`'s
    own docstring, and this module's `test_to_header_ambiguous_no_comma_
    address_is_still_safely_dropped`) -- correctly returns the `[("",
    "")]` sentinel. Append exactly one trailing comma --
    `'bob@example.test<carol@example.test>,'`, the identical benign
    artifact `_getaddresses_resilient`'s own fix exists to recover real
    recipients from -- and the aggregate arithmetic flips: the one
    top-level comma raises the expected count to 2; the malformed field
    alone still parses (via the legacy non-strict `_AddressList` grammar
    both strict and non-strict modes share internally) into exactly two
    addresses, `bob@example.test` and `carol@example.test`; the empty
    trailing field contributes zero; `2 == 2` satisfies the check.
    `getaddresses` returns `[('', 'bob@example.test'), ('',
    'carol@example.test')]` as if they were two genuine, distinct
    recipients -- no sentinel, no error, not even routed through
    `_getaddresses_resilient`'s own retry path (the very first, un-retried
    `getaddresses` call already returns this non-sentinel result). Both
    fabricated addresses pass every existing guard (`_validate_address`'s
    length/NUL/surrogate checks, neither of which this collision leaves
    any trace for) and would get written to `email_messages.recipients`
    and resolved into two real `pkos_nodes` person entities -- the
    identical "attacker-controlled `To` header silently corrupts the
    entity graph" impact round 2 already fixed once for the naive-split
    comma-in-display-name case, here via a different mechanism (a
    comma-count collision, not a naive split), and reachable at the same
    one-character attacker cost (append a comma) `_getaddresses_
    resilient`'s own round-3 bug report used for the *opposite* direction
    -- a real Gmail sender fully controls their own `To` header.

    `getaddresses`' own count-based heuristic cannot distinguish this
    collision from a genuine two-recipient list -- by construction, both
    produce the same total count -- so no amount of further post-
    processing of `getaddresses`' own return value can recover the
    distinction; this needs an independently-derived signal.
    `email.headerregistry.HeaderRegistry` parses the same RFC 5322 grammar
    per-token rather than by aggregate count, and surfaces the malformed
    field as an explicit, non-empty `.defects` tuple
    (`InvalidHeaderDefect('invalid address in address-list')`) for the
    comma-appended collision case, while reporting an empty `.defects` for
    every legitimate multi-address shape this module already accepts (a
    plain comma-separated list, the quoted-comma display name, and
    `_getaddresses_resilient`'s own trailing-comma recovery case --
    independently confirmed against all three, plus an oversized address,
    before relying on this check). Used here only as a yes/no cross-check
    layered on top of the existing `getaddresses`-based parse, not a
    replacement for it -- swapping the primary parser this late in an
    already-reviewed, already-tested mechanism is a materially larger,
    riskier change for the same fix.

    Wrapped in a narrow `except` -- `HeaderRegistry` itself raises an
    uncaught `UnicodeEncodeError` for a header carrying an unpaired UTF-16
    surrogate (the same underlying encode failure `_contains_unpaired_
    surrogate` exists to catch elsewhere in this file, reachable here one
    call earlier), which would otherwise reintroduce the exact
    uncaught-crash failure class round 2 already closed for this module.
    Returning `False` (no defect found) on any such exception is safe, not
    merely convenient: it defers entirely to `_getaddresses_resilient`'s
    own existing parse and `_validate_address`'s own NUL/surrogate checks
    on whatever addresses that produces, rather than this cross-check
    taking responsibility for a failure class it isn't needed for and was
    never meant to guard.

    Checks specifically for an `InvalidHeaderDefect` among `.defects`, not
    `bool(.defects)` against the tuple as a whole -- found by round 5
    review, a false-positive gap in this same function's own first cut.
    `HeaderRegistry` reports several unrelated, genuinely benign defect
    classes on headers this module already accepts and correctly recovers
    real recipients from without this cross-check's help: an interior
    double comma or a comma-plus-whitespace empty entry (`"alice@x.test,
    , bob@x.test"`, `"alice@x.test,,bob@x.test"` -- both produce only
    `ObsoleteHeaderDefect('address-list entry with no content')`/
    `ObsoleteHeaderDefect('empty element in address-list')`, never
    `InvalidHeaderDefect`, independently confirmed in a standalone
    interpreter) parse via `getaddresses` into the two real addresses plus
    a spurious `("", "")` entry `_validate_address` already drops on its
    own -- exactly the same benign shape `_getaddresses_resilient`'s own
    trailing-comma case is. A real Gmail sender's mail-merge tool or a
    stray extra separator in a forwarded header can produce this. `bool(
    .defects)` treated that non-empty-but-harmless tuple identically to
    the genuine `InvalidHeaderDefect('invalid address in address-list')`
    the round 4 collision case raises, gating the entire recipient loop
    off and silently dropping the whole message (sender, subject, thread,
    all of it, via `_process_message`'s own `if not recipients: return
    None`) over a header that was never actually ambiguous -- the
    identical "one comma away from vanishing an otherwise well-formed
    message" impact `_getaddresses_resilient`'s own round 3 fix closed for
    a different comma placement, reintroduced here by round 4's own fix
    for yet another comma placement. Every collision variant this
    function exists to catch (`_to_header_has_grammar_defect`'s own
    docstring above, plus a wider fuzz pass across `<...>`-adjacent
    phrase/comment/local-part variations, all re-verified in a standalone
    interpreter before relying on this) raises `InvalidHeaderDefect`
    specifically, alongside whatever incidental `ObsoleteHeaderDefect`s the
    same malformed field also triggers -- narrowing the check to that one
    defect class closes the false-positive gap without reopening the
    collision this function was written for.

    `RecursionError` added to the caught tuple by round 6 review, alongside
    the pre-existing `UnicodeEncodeError`/etc. -- `HeaderRegistry` parses
    the identical RFC 5322 grammar `getaddresses`/`parseaddr` do and
    recurses the same way on unbounded `(...)` comment nesting (see
    `_parse_address`'s own docstring for the full mechanism and the
    concrete reproduction; independently confirmed to crash `HeaderRegistry`
    at the same nesting depth). Reachable here even when this function runs
    *before* `_getaddresses_resilient` ever does (see this function's own
    call site in `_process_message`), so this module's very first parse of
    a comment-nesting-bomb `To` header would otherwise still be this one.
    Returning `False` here is safe for the same reason the existing
    catches already are (see this function's own docstring above): it
    defers to `_getaddresses_resilient`'s own parse, which independently
    catches the identical `RecursionError` on the same input and yields no
    recipients -- not a bypass of this cross-check's protection, since the
    collision this function guards against has no comment-nesting shape of
    its own to hide behind a `RecursionError` return value.

    `AttributeError` added to the caught tuple by round 7 review -- a
    distinct, previously-uncaught crash in the same `HeaderRegistry`
    call, unrelated to any of the above. RFC 5322's `group` syntax
    (`"group-name: member@example.test, other@example.test;"`) is valid
    grammar `HeaderRegistry` otherwise parses cleanly (returning a
    `Group` object with no member `local_part`, unlike an ordinary
    `Mailbox` address), but a `To` header consisting of two such groups
    -- or a group immediately followed by a bare address -- with no
    comma between them (invalid grammar `HeaderRegistry`'s own legacy
    `_AddressList` fallback still attempts to recover from rather than
    reject outright) makes that recovery path treat the second group/
    address as an errant trailing component of the first and try to read
    `.local_part` off it, raising an uncaught `AttributeError: 'Group'
    object has no attribute 'local_part'` from inside `HeaderRegistry.
    __call__` itself -- independently confirmed in a standalone
    interpreter, reproducible with as little as `"g:a@x.test;b@x.test"`
    (no nesting, no nonstandard characters, nothing exotic). None of the
    six exception types already caught above are raised for this input;
    Gmail forwards a sender's raw `To` header verbatim and a sender fully
    controls their own outgoing headers, including this syntax -- the
    identical "single crafted message reaches an uncaught crash, aborting
    the entire sync call, and for `incremental_sync` wedging that account
    permanently since the cursor never advances past it" failure class
    every other guard in this file exists to close, reached here via a
    stdlib construction-time `AttributeError` rather than a length/NUL/
    surrogate check or a `RecursionError`. Returning `False` here is safe
    for the identical reason the pre-existing catches already are: it
    defers to `_getaddresses_resilient`'s own independent parse of the
    same header (which handles this exact group-syntax input without
    raising -- confirmed separately -- rather than sharing this crash),
    not a bypass of this cross-check's own protection.
    """
    try:
        defects = _TO_HEADER_REGISTRY("To", to_header).defects
    except (
        UnicodeEncodeError,
        ValueError,
        TypeError,
        LookupError,
        IndexError,
        RecursionError,
        AttributeError,
    ):
        return False
    return any(isinstance(defect, InvalidHeaderDefect) for defect in defects)


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
    normalized = normalize_email(email)
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
    # `updated_at = CASE ... END`, not an unconditional `:now` (found this
    # round of review): `_process_message` calls this function once per
    # message *returned by Gmail*, including a message this thread has
    # already stored -- `_insert_message_if_new`'s own docstring names
    # exactly this as an expected, not exceptional, case ("a re-synced
    # message (backfill re-run, incremental/backfill overlap) is a silent
    # no-op"). An unconditional `updated_at = :now` on every ON CONFLICT
    # branch bumped this row's `updated_at` on that no-op resync too, with
    # no change to `last_message_at`/`subject`/any other observable
    # column. `attention.py`'s own `email_thread_rows` comment stakes the
    # dismissed-state-preservation invariant on `updated_at` changing "on
    # every write that touches this thread, including a new message
    # arriving" -- true, but it also changed on a write that touched the
    # thread *without* a new message arriving, which silently broke that
    # exact invariant: dismissing an `email_thread` attention item,
    # followed by an ordinary backfill re-run or incremental/backfill
    # overlap touching the same thread with zero new content, un-dismissed
    # it on the next `regenerate_attention` call. Reproduced directly
    # against real Postgres before this fix (dismiss, then an `email_
    # threads` write identical to the one `_upsert_thread`'s own ON
    # CONFLICT branch performs for a duplicate message, then regenerate --
    # the dismissed item reappeared); confirmed absent after. Only bump
    # `updated_at` when this call's own inputs actually move the row's
    # observable state forward -- `last_message_at` advances (a genuinely
    # newer message, matching the `GREATEST` above) or `subject` is being
    # filled in for the first time (matching the `COALESCE` above) -- so a
    # true no-op resync leaves both `updated_at` and the derived
    # `attention_items` version untouched, exactly like every other scored
    # entity_type's real `version` column already behaves.
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
                updated_at = CASE
                    WHEN EXCLUDED.last_message_at > email_threads.last_message_at
                      OR (email_threads.subject IS NULL AND EXCLUDED.subject IS NOT NULL)
                    THEN :now
                    ELSE email_threads.updated_at
                END
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


# -- Task 5: proactive action detection --------------------------------------


def _extract_plain_text_body(payload: dict[str, Any]) -> str | None:
    """Walks a `messages.get(format=full)` response's own MIME `payload`
    tree for the first `text/plain` part's decoded body -- Gmail's own
    `body.data` is base64url-encoded (RFC 4648 section 5, not standard
    base64), matching the encoding every other Google Workspace API uses
    for binary-ish payload fields. A multipart message (`multipart/
    alternative`, `multipart/mixed`, ...) nests its real content under
    `payload.parts`, each itself shaped like a top-level `payload`, hence
    the recursion; a simple, non-multipart message has its body directly
    on the top-level `payload.body.data` with no `parts` at all. Returns
    `None` for a message with no `text/plain` part reachable this way
    (an HTML-only message, or a malformed/truncated response) -- callers
    treat that as "nothing to store," the same "malformed input is
    skipped, not raised" contract `_process_message` already documents
    for this file's other Gmail-response parsing.

    Recurses one stack frame per `parts` nesting level with no depth cap
    of its own -- round 9 review: a maliciously deep `multipart/mixed`
    nesting (each part wrapping exactly one child part, hundreds of levels
    deep) drives this past the interpreter's recursion limit and raises an
    uncaught `RecursionError`, the same failure class `_parse_address`'s
    own docstring already covers for stdlib header parsing, reached here
    via this function's own recursion instead. A sender fully controls
    their outgoing MIME structure the same way they control their
    headers -- the identical "single crafted message reaches an uncaught
    crash" failure class every guard in this file exists to close. Guarded
    at the caller (`_detect_action_for_message` wraps this call in
    `try`/`except RecursionError`), the same "guard at the point of use,
    not inside the recursive walk itself" shape as every `RecursionError`
    guard elsewhere in this module.
    """
    mime_type = payload.get("mimeType")
    body = payload.get("body")
    if mime_type == "text/plain" and isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, str) and data:
            try:
                return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
                    "utf-8", errors="replace"
                )
            except (ValueError, TypeError):
                return None
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                extracted = _extract_plain_text_body(part)
                if extracted is not None:
                    return extracted
    return None


@dataclass(frozen=True, slots=True)
class _BodyParseOutcome:
    """What parsing one `messages.get(format=full)` response concluded.
    `retriable=True` (`text` unused, left `""`) means this call couldn't
    reach a durable conclusion -- a non-200/no response, malformed JSON, a
    response shaped unlike Gmail's own contract -- so the caller must
    leave `body IS NULL` and let a future call retry. `retriable=False`
    means this message's content is a permanent, immutable fact (Gmail
    message content never changes) that must be committed once and never
    re-attempted -- `text` is `""` when the response parsed fine but had
    nothing extractable (no `text/plain` part, or a nesting deep enough to
    exceed the recursion limit), matching `email_action_tools.py`'s own
    "genuinely empty, not unfetched" convention.

    Exists so "should this call retry, or write and stop" is a single
    explicit field read at one call site (`fetch_and_store_body`), not two
    different meanings both spelled `None` -- a bare function-level
    `return None` (retriable) versus a local `plain_text = None` that
    still falls through to a write (resolved-empty) -- which is exactly
    the shape of bug rounds 4 and 9 review each found and fixed once (a
    new `except` branch landing on the wrong side of that distinction).
    """

    retriable: bool
    text: str = ""


def _parse_message_body_response(get_response: httpx.Response | None) -> _BodyParseOutcome:
    """Pure parsing, no I/O -- `fetch_and_store_body` owns the actual
    Gmail request and the `email_messages` write. Every branch here
    mirrors that method's own pre-extraction behavior exactly; see
    `_BodyParseOutcome`'s own docstring for why the return type exists.
    """
    if get_response is None or get_response.status_code != 200:
        return _BodyParseOutcome(retriable=True)
    try:
        body_json = get_response.json()
    except ValueError:
        # Malformed/truncated JSON -- presumed a transient response
        # glitch (a proxy cutting the body short, say), not a property of
        # the message itself.
        return _BodyParseOutcome(retriable=True)
    except RecursionError:
        # Round 9 review: `httpx.Response.json()` calls `json.loads`
        # directly, whose own recursive-descent parser has no depth cap
        # either -- a sufficiently deep JSON array/object nesting in
        # Gmail's response raises `RecursionError` here, not `ValueError`.
        # Unlike a `ValueError`, this is *not* presumed transient: Gmail
        # generates `messages.get`'s response deterministically from this
        # specific message's own stored MIME structure, so a response
        # nested this deeply is a permanent property of the message, not
        # a passing glitch -- resolved-empty, not retriable, precisely so
        # this message is marked handled once and for all (returning
        # retriable here instead would leave `body IS NULL` forever,
        # reopening the round-4-fixed "poison message" failure class
        # through this new trigger).
        return _BodyParseOutcome(retriable=False)
    if not isinstance(body_json, dict):
        return _BodyParseOutcome(retriable=True)
    payload = body_json.get("payload")
    if not isinstance(payload, dict):
        return _BodyParseOutcome(retriable=True)
    try:
        plain_text = _extract_plain_text_body(payload)
    except RecursionError:
        # See `_extract_plain_text_body`'s own docstring: a maliciously
        # deep `parts` nesting drives its recursive walk past the
        # interpreter's recursion limit -- the identical "deterministic
        # property of this message, not a transient glitch" reasoning as
        # the `except RecursionError` above, just reached one step
        # further down, after the response parsed as JSON but its MIME
        # tree couldn't be walked.
        return _BodyParseOutcome(retriable=False)
    # A `payload` this function got far enough to structurally parse (a
    # 200 response, valid JSON, a dict body, a dict `payload`) that
    # genuinely has no `text/plain` part is a fact about the message
    # itself that will not change on retry -- Gmail message content is
    # immutable -- unlike the non-200/malformed-JSON/missing-`payload`
    # cases above, which stay retriable because those genuinely are
    # transient or indicate a response this function could not make
    # sense of, not a property of the message. Round 4 review's own
    # finding: this was the one path through the original, unextracted
    # version of this logic that could `return None`-meaning-retriable
    # despite having nothing left to retry -- silently leaving `body IS
    # NULL` forever, re-selected, re-fetched, and re-rejected by every
    # future call indefinitely.
    return _BodyParseOutcome(retriable=False, text=plain_text or "")


def _register_message_evidence(
    session: Session, *, workspace_id: UUID, node_id: UUID, external_message_id: str, now: datetime
) -> UUID:
    """One fresh `pkos_evidence` row citing the specific message that
    triggered a detection run -- distinct from whatever (possibly much
    older) `pkos_evidence` row `_resolve_or_create_person` created the
    first time this sender was ever seen (see that function's own
    docstring): a recommendation's `evidence_ids` must point at the email
    actually reasoned about, not merely at "this sender is a known
    person." `source_ref` is deliberately namespaced (`gmail:detect_
    action:...`, not `_resolve_or_create_person`'s own bare `gmail:...`)
    so the two purposes never collide on the same `sha256`.
    """
    evidence_id = uuid4()
    source_ref = f"gmail:detect_action:{external_message_id}"
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
    return evidence_id


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


# `_sync_history`'s own `resumable_history_id` (round 12) resumes at
# `history[]` *record* granularity -- correct as far as it goes, but round
# 13 review found it doesn't go far enough: nothing bounds a single
# record's own `messagesAdded` list to fit within `_MAX_MESSAGES_PER_CALL`,
# and `history.list(startHistoryId=X)` has no way to resume a *record's
# own* message list mid-way, only ever returning it whole again for the
# identical `X`. A record whose own message count exceeds the budget can
# therefore never fully complete no matter how many times `incremental_
# sync` is retried: every retry re-lists the same oversized record from
# its own first message, re-spends the entire budget re-fetching (mostly
# harmlessly idempotent, but never-progressing) messages already written
# by an earlier retry, and hits the identical exhaustion point every
# time -- the same livelock class round 12 closed for the *inter*-record
# case, reopened here for the *intra*-record one. Reproduced directly
# against real Postgres (see `test_incremental_sync_makes_forward_
# progress_through_a_single_oversized_history_record` in the sync test
# file): one history record with five `messagesAdded` entries, budget
# monkeypatched to 2 -- six consecutive `incremental_sync` calls each
# processed only the same first two messages and reported the same
# `next_cursor` every time; the remaining three were never written by any
# of them.
#
# `incremental_sync`'s own `cursor` contract (`ConnectorAdapter`'s
# Protocol) is a bare `str`, not a structured resume-state object, but
# nothing requires that `str` to hold only a bare historyId -- these two
# functions extend it to optionally also carry "and skip the first N
# messages of record M" as `"{list_start_history_id}:{record_id}:{skip_
# count}"`, still a single opaque string from the Protocol's own point of
# view. Safe because `history.list` walks strictly ascending by record
# `id` for a fixed `startHistoryId` (the same guarantee `resumable_
# history_id` itself already depends on) and a `messagesAdded` list is
# itself a fixed, already-happened fact -- both the record's own position
# in the walk and its own message list are stable across retries with no
# intervening change, so "skip the first N messages of record M" means
# the same thing on every retry it's replayed against. A bare cursor from
# before this fix (or one `_sync_messages`' own differently-shaped next_
# cursor happens to hand `incremental_sync` after a `cursor is None`
# fallback) has no `:` in it and parses as the plain, no-skip case;
# Gmail's own `historyId` values are documented as plain digit strings and
# never contain one.
def _parse_history_cursor(cursor: str) -> tuple[str, int | None, int]:
    """Returns `(list_start_history_id, stuck_record_id, skip_count)` --
    `list_start_history_id` is always what `_sync_history` should pass as
    `history.list`'s own `startHistoryId`; `stuck_record_id`/`skip_count`
    are `None`/`0` for a plain cursor. Malformed compound state (the wrong
    number of `:`-separated fields, or a non-integer/negative record id or
    skip count -- never produced by `_build_history_cursor` itself, but
    this module doesn't control what a caller ultimately persists and
    replays back) degrades to treating the *whole* string as a plain
    `list_start_history_id` rather than guessing at a partial parse:
    Gmail's own `history.list` call then either accepts it (if it's still
    numeric) or rejects it with the non-200 this method already raises on
    -- never a silent misinterpretation of which messages were already
    skipped.
    """
    parts = cursor.split(":")
    if len(parts) != 3:
        return cursor, None, 0
    list_start_history_id, record_id_str, skip_count_str = parts
    record_id = _coerce_int(record_id_str)
    skip_count = _coerce_int(skip_count_str)
    if record_id is None or record_id < 0 or skip_count is None or skip_count < 0:
        return cursor, None, 0
    return list_start_history_id, record_id, skip_count


def _build_history_cursor(
    list_start_history_id: str,
    resumable_history_id: int | None,
    stuck_record_id: int | None,
    stuck_offset: int,
) -> str:
    """Inverse of `_parse_history_cursor` -- `stuck_offset <= 0` (nothing
    of `stuck_record_id` has actually been processed yet this call, or
    there is no stuck record at all) omits the compound suffix entirely
    rather than encoding a no-op skip: a bare `resumable_history_id`-or-
    `list_start_history_id` cursor already means "re-fetch this record
    from its own start" with no help from a `:0` suffix, and every plain
    cursor `_sync_history` produced before this fix stays exactly as
    plain as it always was.
    """
    base = str(resumable_history_id) if resumable_history_id is not None else list_start_history_id
    if stuck_record_id is None or stuck_offset <= 0:
        return base
    return f"{base}:{stuck_record_id}:{stuck_offset}"


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
        Task 2 -- explicit callers (`SyncRequest.since`, the "expand
        history" UI built in Task 8) pass a real value for a wider or
        narrower window.
        """
        if resource_type != "message":
            return SyncOutcome(
                resource_type=resource_type, items_processed=0, status="succeeded", next_cursor=None
            )
        window_start = since or (datetime.now(UTC) - _DEFAULT_BACKFILL_WINDOW)
        if window_start.tzinfo is None:
            # Round 8 review (PR #130): `datetime.timestamp()` on a naive
            # value is interpreted as local system time, not UTC. Round 17
            # closed this as unreachable since nothing passed a real `since`
            # then; Task 8's `SyncRequest.since` is now a real HTTP-reachable
            # caller, so a naive value must be treated as UTC explicitly.
            window_start = window_start.replace(tzinfo=UTC)
        query = f"after:{int(window_start.timestamp())}"
        return self._sync_messages(account, query=query)

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        """`cursor` is a Gmail `historyId` (a string-encoded integer), or --
        round 13 review -- `_sync_history`'s own compound `"{historyId}:
        {record_id}:{skip_count}"` form (see `_parse_history_cursor`'s own
        module-level comment); either way it's still a single opaque `str`
        from this method's own point of view, per `ConnectorAdapter.
        incremental_sync`'s own "resumes from cursor, or behaves like a
        fresh backfill if cursor is None" contract.
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
                # `next_cursor=None`, not `str(highest_history_id)` --
                # found by round 11 review, see this method's own
                # `get_response is None` branch below for the full
                # mechanism (identical bug, identical fix, applied at
                # three sites total in this method -- this is the first
                # of them; the second is the mid-loop consent-revocation
                # check just below, in the same `for` loop).
                return SyncOutcome(
                    resource_type="message",
                    items_processed=items_processed,
                    status="partial",
                    next_cursor=None,
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
                            next_cursor=None,
                            error_summary="email domain consent was revoked mid-sync",
                        )

                get_response = self._request_with_rate_limit_retry(
                    "GET",
                    f"/gmail/v1/users/me/messages/{message_id}",
                    headers=headers,
                    params={"format": "metadata", "metadataHeaders": ["From", "To", "Subject"]},
                )
                if get_response is None:
                    # `next_cursor=None`, not `str(highest_history_id) if
                    # highest_history_id else None` -- found by round 11
                    # review, a cursor-regression data-loss bug distinct
                    # from every prior finding in this file (none of which
                    # touched cursor *value* correctness, only whether one
                    # was handed out at all). `messages.list` -- unlike
                    # `history.list`'s own strictly historyId-ordered
                    # walk -- has no documented ordering guarantee tying
                    # list position to `historyId`, and Gmail's default
                    # sort is newest-received-first, which does not imply
                    # ascending `historyId` either (an older message can
                    # still have a *lower* `historyId` than one listed
                    # ahead of it in this same page). `highest_history_id`
                    # is a `max()` over every message *already* processed
                    # this call, seeded here by whichever earlier refs in
                    # this same page-or-run happened to have the largest
                    # `historyId` -- not by whichever ref is closest to
                    # `message_id`, the one this branch is about to skip.
                    # Handing that `max()` out as `next_cursor` when this
                    # message (and everything still queued behind it) was
                    # never reached is unsafe: `_sync_history`'s own
                    # `history.list(startHistoryId=...)` call only ever
                    # returns entries with a strictly *greater* `historyId`
                    # than the cursor it's given, so a subsequent
                    # `incremental_sync` using this `next_cursor` would
                    # never see `message_id` (or any other still-unfetched
                    # message whose own `historyId` happens to be at or
                    # below it) again -- silently, permanently, with the
                    # sync call itself still reporting real, if partial,
                    # progress (`items_processed` > 0, no error masking
                    # anything). Reproduced directly against real Postgres
                    # (not merely reasoned about): two messages in one
                    # `messages.list` page, the first with `historyId=2000`
                    # processed successfully, the second (`historyId=500`,
                    # deliberately lower -- an older message Gmail's own
                    # default newest-first list ordering can and does
                    # place behind a newer, higher-`historyId` one)
                    # rate-limited on its own `messages.get` call; before
                    # this fix, `next_cursor` came back `"2000"` with the
                    # `historyId=500` message never written, a cursor a
                    # real `history.list(startHistoryId=2000)` call would
                    # never surface it through again. This method's sibling
                    # `_sync_history` never had this bug -- unlike this
                    # method's `highest_history_id` (an unsafe `max()` over
                    # an ordering `messages.list` never guarantees),
                    # `_sync_history`'s own resume value is always derived
                    # from `history.list`'s strictly-*ascending* record
                    # order (`start_history_id` while this call has
                    # completed no record yet, `resumable_history_id` once
                    # one has -- see that variable's own comment; round 12
                    # review revised it to advance mid-walk *because* that
                    # ordering guarantee makes doing so safe there, the
                    # opposite conclusion this method's own unordered
                    # `max()` reaches -- found stale by round 13 review,
                    # this comment previously still claimed `_sync_history`
                    # pins `next_cursor` at `start_history_id`
                    # unconditionally, true when round 11 wrote it but not
                    # after round 12's own fix), so it is always safe to
                    # hand back regardless of how much progress this call
                    # made. The "no further pages" branch below this loop
                    # already carried a comment claiming every partial
                    # branch above it left `next_cursor` unset -- true of
                    # `_sync_history`'s own then-current behavior, not of
                    # this method until this fix, the discrepancy itself a
                    # signal something here didn't match its own documented
                    # invariant. Matches the
                    # already-correct `budget_exhausted` partial return at
                    # the bottom of this method (which has always used
                    # `next_cursor=None` unconditionally) rather than
                    # inventing a third convention; a `None` cursor simply
                    # means the next `incremental_sync` call falls back to
                    # a fresh `backfill`, which safely re-covers this
                    # message (and everything behind it) rather than
                    # silently orphaning it.
                    return SyncOutcome(
                        resource_type="message",
                        items_processed=items_processed,
                        status="partial",
                        next_cursor=None,
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
                # `partial`-branch returns above, none of which set it
                # (round 11 review: all three of them did, until fixed --
                # see the `get_response is None` branch's own long comment
                # above for the full mechanism; this docstring's claim was
                # aspirational, not yet true, before that fix).
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
                    # `is not None`, not a bare truthiness check -- round 16
                    # review: `highest_history_id` is an `int | None`, and
                    # `0` is a value `_coerce_int` can legitimately return
                    # (from either a message's own `historyId` field, line
                    # ~1710 above, or the `users.getProfile` fallback just
                    # above) but is falsy in Python. `if highest_history_id
                    # else None` previously treated an observed `historyId`
                    # of exactly `0` identically to "nothing was ever
                    # observed at all," silently discarding a real cursor
                    # value on an otherwise fully-caught-up `"succeeded"`
                    # sync -- the same `bool`-vs-`int` confusion round 9
                    # review closed for `expires_in` in `handle_oauth_
                    # callback` (`isinstance(expires_in, bool)`), here via
                    # truthiness rather than `bool`'s own `int` subtyping.
                    # Nothing before this fix validated Gmail's own
                    # `historyId` fields can never be exactly `0` (unlike,
                    # say, `_parse_history_cursor`'s own `record_id < 0`
                    # check, added by round 14 review for a value this
                    # module itself never produces but also never rules
                    # out on the read side) -- every other `is None`/`is
                    # not None` check on this same variable earlier in this
                    # method (e.g. `if highest_history_id is None:` just
                    # above) already gets this right; this was the one
                    # remaining truthiness-shortcut site.
                    next_cursor=str(highest_history_id) if highest_history_id is not None else None,
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

        # Round 13 review: `start_history_id` (this method's own `cursor`
        # argument) may be a plain historyId or a `_build_history_cursor`-
        # produced compound string carrying "and skip the first N messages
        # of record M" -- see that function's own module-level comment for
        # why. `list_start_history_id` is what every `history.list` call
        # below actually uses for `startHistoryId`; `pending_stuck_record_
        # id`/`pending_skip_count` are consulted exactly once, at the one
        # record (if any) whose own `id` matches, to trim `record_message_
        # ids` before that record's own message loop runs.
        list_start_history_id, pending_stuck_record_id, pending_skip_count = _parse_history_cursor(
            start_history_id
        )

        headers = _bearer_headers(account.credential)
        now = datetime.now(UTC)
        items_processed = 0
        latest_history_id: int | None = None
        page_token: str | None = None
        calls_made = 0
        seen_message_ids: set[str] = set()
        # Round 12 review: the historyId of the last *fully processed*
        # `history[]` record (every one of its own `messagesAdded` entries
        # actually fetched and written) seen so far in this call, across
        # every page walked -- `None` until the first record completes.
        # Every partial-return site below now resumes from this instead of
        # unconditionally pinning `next_cursor` at `start_history_id` --
        # see this variable's own first read site (the `history_response is
        # None` branch just below) for the full mechanism and why the
        # unconditional-pin behavior was itself a bug, not merely
        # suboptimal. Round 13: still record-granularity only -- see
        # `_build_history_cursor`'s own module-level comment for the
        # narrower, message-granularity state layered on top of it.
        resumable_history_id: int | None = None

        while calls_made < _MAX_MESSAGES_PER_CALL:
            history_response = self._request_with_rate_limit_retry(
                "GET",
                "/gmail/v1/users/me/history",
                headers=headers,
                params={
                    k: v
                    for k, v in {
                        "startHistoryId": list_start_history_id,
                        "historyTypes": "messageAdded",
                        "maxResults": _MESSAGE_PAGE_SIZE,
                        "pageToken": page_token,
                    }.items()
                    if v is not None
                },
            )
            if history_response is None:
                # Round 12: resumes from `resumable_history_id` instead of
                # unconditionally pinning `start_history_id` -- see this
                # method's own module-level comment for the full mechanism.
                # Round 13: `page_token is None` here means this is the
                # very *first* `history.list` attempt of this call --
                # nothing has been examined yet, so whatever mid-record
                # position this call *started* from (`pending_stuck_
                # record_id`/`pending_skip_count`, parsed from `start_
                # history_id` itself above) is still exactly correct and
                # is echoed back unchanged via `start_history_id` verbatim,
                # rather than rebuilding it (and risking silently dropping
                # that state) through `_build_history_cursor`. On any
                # *later* page of this same call, reaching here always
                # means every earlier page's records already fully
                # completed without interruption (a mid-record interruption
                # always `break`s the outer `while` loop itself, never
                # falls through to fetch another page) -- there is no
                # stuck record to preserve, so `_build_history_cursor`'s
                # own `None`/`0` correctly produces a plain cursor.
                return SyncOutcome(
                    resource_type="message",
                    items_processed=items_processed,
                    status="partial",
                    next_cursor=(
                        start_history_id
                        if page_token is None
                        else _build_history_cursor(
                            list_start_history_id, resumable_history_id, None, 0
                        )
                    ),
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

            # Round 12 review: processed one `history[]` record at a time
            # (not flattened into one page-wide `message_ids` list first,
            # the original shape) so that hitting `_MAX_MESSAGES_PER_CALL`
            # mid-page can still tell *which* records were fully finished
            # before the budget ran out. `history.list`'s own records are
            # walked in strictly ascending `id` order (see `resumable_
            # history_id`'s own comment above) -- a message-count bound
            # that stops mid-record still leaves every *earlier* record in
            # this same page (and every prior page in this same call)
            # genuinely, safely resumable from.
            budget_exhausted = False
            record_history_id: int | None = None
            already_skipped_this_record = 0
            processed_in_record_this_call = 0
            for entry in history_entries:
                if not isinstance(entry, dict):
                    continue
                record_history_id = _coerce_int(entry.get("id"))
                added = entry.get("messagesAdded")
                record_message_ids: list[str] = []
                if isinstance(added, list):
                    for item in added:
                        message = item.get("message") if isinstance(item, dict) else None
                        message_id = message.get("id") if isinstance(message, dict) else None
                        if (
                            isinstance(message_id, str)
                            and message_id
                            and message_id not in seen_message_ids
                        ):
                            seen_message_ids.add(message_id)
                            record_message_ids.append(message_id)

                # Round 13 review: this is the one record (if any) a PRIOR
                # call got stuck partway through -- see `_parse_history_
                # cursor`'s own module-level comment for why a record's own
                # `id` is unique/strictly-increasing enough per call to
                # match at most once. Every *other* record's `record_
                # message_ids` is untouched (`already_skipped_this_record`
                # stays `0`), identical to round 12's own behavior.
                already_skipped_this_record = 0
                if record_history_id is not None and record_history_id == pending_stuck_record_id:
                    already_skipped_this_record = pending_skip_count
                    record_message_ids = record_message_ids[pending_skip_count:]

                record_fully_processed = True
                processed_in_record_this_call = 0
                for message_id in record_message_ids:
                    if calls_made >= _MAX_MESSAGES_PER_CALL:
                        # Round 1 review: this page's own record still had
                        # unprocessed entries when the shared budget ran
                        # out. Without this flag, a bound hit on Gmail's
                        # own last history page (no `nextPageToken`) would
                        # fall through to the "fully caught up" branch
                        # below and report `status="succeeded"` despite
                        # these remaining messages never having been
                        # fetched.
                        #
                        # `record_fully_processed = False` -- round 12
                        # review, added alongside the pre-existing
                        # `budget_exhausted` flag: this specific record
                        # must NOT advance `resumable_history_id` below,
                        # since it was interrupted partway through, not
                        # completed. `resumable_history_id` closes the
                        # *inter*-record instance of this gap: a subsequent
                        # call resumes past every record this call *did*
                        # finish. Round 13: `already_skipped_this_record`/
                        # `processed_in_record_this_call`, combined via
                        # `_build_history_cursor` at every return site
                        # below, close the *intra*-record instance --
                        # without them, a single record whose own message
                        # count exceeds this budget could never finish no
                        # matter how many retries, since every one of them
                        # would re-walk this same record from its own first
                        # message and re-hit this identical break every
                        # time. Reproduced directly against real Postgres:
                        # one record with five `messagesAdded` entries,
                        # budget monkeypatched to 2 -- six consecutive
                        # `incremental_sync` calls each processed only the
                        # same first two messages and reported the same
                        # `next_cursor` every time; the remaining three
                        # were never written by any of them.
                        budget_exhausted = True
                        record_fully_processed = False
                        break
                    calls_made += 1

                    with SessionFactory() as session, session.begin():
                        if not _email_consent_active(session, account.workspace_id, owner_id):
                            return SyncOutcome(
                                resource_type="message",
                                items_processed=items_processed,
                                status="partial",
                                next_cursor=_build_history_cursor(
                                    list_start_history_id,
                                    resumable_history_id,
                                    record_history_id,
                                    already_skipped_this_record + processed_in_record_this_call,
                                ),
                                error_summary="email domain consent was revoked mid-sync",
                            )

                    get_response = self._request_with_rate_limit_retry(
                        "GET",
                        f"/gmail/v1/users/me/messages/{message_id}",
                        headers=headers,
                        params={
                            "format": "metadata",
                            "metadataHeaders": ["From", "To", "Subject"],
                        },
                    )
                    if get_response is None:
                        return SyncOutcome(
                            resource_type="message",
                            items_processed=items_processed,
                            status="partial",
                            next_cursor=_build_history_cursor(
                                list_start_history_id,
                                resumable_history_id,
                                record_history_id,
                                already_skipped_this_record + processed_in_record_this_call,
                            ),
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
                        raise RuntimeError(
                            "Gmail message fetch returned a non-object response body"
                        )

                    self._process_message(
                        message_body,
                        workspace_id=account.workspace_id,
                        owner_id=owner_id,
                        connector_account_id=account.connector_account_id,
                        now=now,
                    )
                    items_processed += 1
                    processed_in_record_this_call += 1

                if budget_exhausted:
                    break
                if record_fully_processed and record_history_id is not None:
                    resumable_history_id = record_history_id

            if budget_exhausted:
                break

            page_token = history_body.get("nextPageToken")
            if not isinstance(page_token, str) or not page_token:
                return SyncOutcome(
                    resource_type="message",
                    items_processed=items_processed,
                    status="succeeded",
                    # `is not None`, not a bare truthiness check -- round 16
                    # review: the identical `0`-is-falsy-but-legitimate gap
                    # this method's sibling `_sync_messages` had for its own
                    # `highest_history_id` (see that method's own comment on
                    # its analogous return, just above this one in file
                    # order) -- `latest_history_id` is set directly from
                    # `history.list`'s own page-level `historyId` field
                    # (`_coerce_int`, which can return `0`) a few lines
                    # above. `if latest_history_id else list_start_
                    # history_id` previously fell back to the *stale*
                    # starting cursor on a genuinely fully-caught-up
                    # `"succeeded"` sync whenever Gmail's own reported
                    # `historyId` happened to be exactly `0`, discarding
                    # the real (if implausible) value Gmail actually
                    # returned.
                    next_cursor=(
                        str(latest_history_id)
                        if latest_history_id is not None
                        else list_start_history_id
                    ),
                )

        # Round 12: resumes from `resumable_history_id` instead of
        # unconditionally pinning `start_history_id` -- see this method's
        # own module-level comment for the full mechanism. Round 13: this
        # is the `while calls_made < _MAX_MESSAGES_PER_CALL` loop's own two
        # possible exits -- either its condition failing naturally (every
        # record on the just-finished page completed, `budget_exhausted`
        # never set this pass, so `record_history_id`/`processed_in_
        # record_this_call` reflect that *completed* last record rather
        # than an interrupted one) or the explicit `if budget_exhausted:
        # break` above falling through to this same statement (budget ran
        # out mid-record). `_build_history_cursor` only needs to know
        # *which* case this is: passing `record_history_id`/`already_
        # skipped_this_record + processed_in_record_this_call` when `budget_
        # exhausted` and `None`/`0` otherwise reuses the exact same helper
        # (and the exact same in-scope local variables Python's own
        # function-level -- not block-level -- scoping keeps live from the
        # `for` loop above) rather than needing a third, separately-tracked
        # piece of state for this one site.
        return SyncOutcome(
            resource_type="message",
            items_processed=items_processed,
            status="partial",
            next_cursor=_build_history_cursor(
                list_start_history_id,
                resumable_history_id,
                record_history_id if budget_exhausted else None,
                (already_skipped_this_record + processed_in_record_this_call)
                if budget_exhausted
                else 0,
            ),
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
        # `_is_valid_external_id` (length/NUL/unpaired-surrogate, see its
        # own comment) -- round 2 review: previously only type/emptiness
        # were checked here, unlike every other Gmail-sourced string this
        # method handles. Gmail's own `id`/`threadId` values are not
        # attacker-influenced the way `From`/`To` header content is, but
        # nothing about this response body's *contract* actually
        # guarantees that shape, and both flow straight into `VARCHAR(255)`
        # columns (`email_threads.external_thread_id`/`email_messages.
        # external_message_id`, migration `0069`) -- an oversized or
        # unwritable value here would hit the identical uncaught-crash,
        # cursor-never-advances failure class `_MAX_EMAIL_ADDRESS_LENGTH`
        # closed for `sender`/`recipients`. `message_id` doubly matters
        # here: it also seeds `source_ref = f"gmail:{message_id}"` below,
        # which is `.encode()`-d for its `sha256` in `_resolve_or_create_
        # person` -- an unpaired surrogate there raises the exact
        # `UnicodeEncodeError` `_contains_unpaired_surrogate`'s own
        # comment describes, uncaught, from a call this function's own
        # docstring promises never raises for a malformed message.
        message_id = body.get("id")
        external_thread_id = body.get("threadId")
        if not _is_valid_external_id(message_id) or not _is_valid_external_id(external_thread_id):
            return None
        assert isinstance(message_id, str)
        assert isinstance(external_thread_id, str)

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
        # `not _to_header_exceeds_length_limit(to_header)` -- round 7
        # review, checked first (see that function's own comment): a
        # header long enough to trip this never reaches either parser
        # below, closing the unbounded-CPU angle before it starts rather
        # than after paying for it.
        #
        # `to_header and not _to_header_has_grammar_defect(to_header)` --
        # round 4 review: `_to_header_has_grammar_defect` (see its own
        # docstring) catches a header shape `getaddresses`' own aggregate
        # address-count check cannot -- an ambiguous, single malformed
        # field whose comma-count coincidentally matches its own bogus
        # multi-address parse, which `getaddresses` (and by extension
        # `_getaddresses_resilient`, whose retry path this never even
        # reaches) accepts as if it were genuine. Checked once, ahead of
        # the whole loop, rather than per-parsed-address -- a defect
        # anywhere in the header taints every address `getaddresses`
        # claims to have found in it, not just the malformed field's own
        # share, the same "can't make sense of it -> treat as zero
        # recipients" contract `_validate_address`'s own docstring already
        # establishes for a single bad field.
        if (
            to_header
            and not _to_header_exceeds_length_limit(to_header)
            and not _to_header_has_grammar_defect(to_header)
        ):
            # `email.utils.getaddresses` (not a naive `to_header.split(",")`)
            # -- round 2 review found the naive split mis-parses a `To`
            # header whose display name itself legitimately contains a
            # comma (`'"Doe, John" <john@example.com>, jane@example.com'`):
            # it breaks that single, valid address into two fragments
            # (`'"Doe'`, `' John" <john@example.com>'`), and `parseaddr`
            # on each fragment separately silently *loses* the real
            # address entirely -- `' John" <john@example.com>'` parses to
            # `('', 'John')`, an address of `"John"`, not
            # `john@example.com` -- while the other fragment (`'"Doe'`)
            # parses to a bogus address of `"Doe"`. Both bogus fragments
            # pass `_validate_address` (non-empty, no NUL, under the
            # length bound) and would each get written to `email_messages.
            # recipients` and resolved into a real `pkos_nodes` person
            # with a fake "email" -- silent entity-graph corruption from a
            # single crafted header, not a crash, but the identical
            # "attacker-controlled `To` header reaches storage unvalidated"
            # class the NUL/length guards elsewhere in this file already
            # close. A real Gmail sender fully controls their own `To`
            # header, including any legitimate-looking quoted display
            # name. `getaddresses` (stdlib, comma-list-aware, the same
            # RFC 5322 parser `parseaddr` itself is built on) is the
            # correct API for a comma-separated address list and handles
            # the quoted-comma case, an empty header, and a header that is
            # only commas identically to before. `_getaddresses_resilient`
            # (round 3 review), not `getaddresses` directly -- see its own
            # docstring for a *different* silent-data-loss failure mode
            # `getaddresses`' own `strict=True` default introduces for a
            # bare trailing comma.
            #
            # `if len(recipients) >= _MAX_RECIPIENTS_PER_MESSAGE: break` --
            # see that constant's own comment. Checked before validating
            # each next address (not after appending it) so the loop
            # actually stops opening no more than `_MAX_RECIPIENTS_PER_
            # MESSAGE` entity-resolution transactions below, rather than
            # merely stopping storage one iteration too late.
            for name, address in _getaddresses_resilient(to_header):
                if len(recipients) >= _MAX_RECIPIENTS_PER_MESSAGE:
                    break
                parsed = _validate_address(name, address)
                if parsed is not None:
                    valid_name, valid_address = parsed
                    recipients.append(valid_address)
                    recipient_names.setdefault(valid_address, valid_name)
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
        # for why this guard exists at all; `_contains_unpaired_surrogate`
        # (round 2 review) closes the same class of gap for `Subject` that
        # it closes for `sender`/`recipients` elsewhere in this function.
        raw_subject = header_map.get("subject")
        subject = (
            raw_subject
            if raw_subject is not None
            and not _contains_nul(raw_subject)
            and not _contains_unpaired_surrogate(raw_subject)
            else None
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
                # `except Exception` -- round 9 review: `_insert_message_if_new`
                # above already committed the `email_messages`/`email_threads`
                # write in its own transaction, closed before this loop even
                # starts (see `_resolve_or_create_person`'s own docstring for
                # why entity resolution is deliberately a *separate*
                # transaction per participant, not reused from that write).
                # An uncaught failure here -- a transient one (a deadlock, a
                # dropped connection, the app's own `statement_timeout` under
                # load; none of this requires a malformed message, unlike
                # every other guard in this file) as much as an unexpected
                # one -- previously propagated straight out of
                # `_process_message`, uncaught by either call site in `_sync_
                # messages`/`_sync_history`, aborting the *entire* sync call:
                # every message still queued behind this one in the same
                # call, not merely this one message's own remaining
                # participants, went unprocessed -- and for `incremental_
                # sync`, since no `return` past this point was ever reached,
                # `next_cursor` never advanced, so the identical failure
                # would repeat on every subsequent call, wedging that
                # connector account exactly like every uncaught-crash finding
                # in this file's own review history. Worse than those: once
                # the transient condition clears and a retry succeeds in
                # reaching this message again, `_insert_message_if_new`'s own
                # `ON CONFLICT DO NOTHING` returns `None` (the row already
                # exists from the *first*, crashed attempt) -- so this whole
                # `if inserted_id is not None:` block, including every
                # participant that failed to resolve the first time, is
                # skipped on every future call, forever. The message row
                # exists, `sync` reports `"succeeded"`, and nothing anywhere
                # signals that one of its participants was never linked into
                # `pkos_nodes` -- silent, permanent data loss in the entity
                # graph, indistinguishable from a message with a genuinely
                # sender-only/no-op participant set, found by review (not
                # part of the original implementation). Caught the same way
                # every other single-item failure in this file is: skip only
                # this one participant's link and keep going, isolating the
                # blast radius to "this message-participant pair is missing
                # from the entity graph" (the person themselves still gets
                # resolved the next time *any* other message names them, so
                # this is not a permanent loss of the entity, only of this
                # one message's own evidence of them) rather than the crash
                # cascading into every other message and account still
                # waiting in this call.
                try:
                    _resolve_or_create_person(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        email=participant_email,
                        display_name=participant_name,
                        source_ref=source_ref,
                        now=now,
                    )
                except Exception:
                    continue

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

    # -- Task 5: proactive action detection -- not part of either Protocol,
    # same "Gmail-specific, called only from `sync_connector_endpoint`'s
    # own explicit `account.provider == "gmail"` branch" shape as `is_
    # account_allowed` above. --

    def detect_actions_since(
        self,
        context: ConnectorAccountContext,
        *,
        since: datetime,
        ollama_adapter: OllamaAdapter | None = None,
    ) -> None:
        """Called from `connector_accounts.sync_connector_endpoint`'s phase
        3, after a successful `backfill`/`incremental_sync` (see that
        module's own call-site comment). Feature-flagged off by default
        (`config.py:email_action_detection_enabled`) -- Task 1-4's own
        sync/OAuth/create-path machinery is unaffected either way.

        For up to `_MAX_ACTION_DETECTIONS_PER_CALL` inbound messages still
        `body IS NULL` (`gmail.metadata`-only sync never populates it) in
        this account's own threads: fetches the full body (`gmail.
        readonly`, `format=full`), stores it encrypted, registers it as
        `pkos_evidence`, and runs it through the `email.detect_action` AI
        task type. A `has_action: true` result becomes a `source="ai"`
        recommendation via Task 4's create-path (`create_recommendation`)
        -- this method never writes a `tasks`/`commitments`/`risks` row
        directly.

        `body IS NULL`, not `created_at >= since`, is the eligibility
        filter -- `since` only orders which eligible messages this call
        prioritizes (this call's own newly-synced messages first, via
        `ORDER BY (created_at >= since) DESC`), never which ones it
        excludes. Round 2 review's own smaller `_MAX_ACTION_DETECTIONS_
        PER_CALL` bound made a latent bug in the original `created_at >=
        since` *filter* design certain to hit in ordinary use, found by
        round 3 review: any candidate left over after one call (more
        eligible messages existed than the bound allowed) would have its
        `created_at` permanently fall behind every later call's own,
        strictly-later `since` -- silently and permanently excluding it
        from every future call, not merely delaying it, with no error or
        trace anywhere. `body IS NULL` never regresses once a message's
        body is actually fetched, so using it alone as the filter (with
        `since` demoted to an ordering hint) makes a message's eligibility
        durable across calls: a backlog beyond one call's own bound is
        worked down over subsequent calls instead of being lost.

        Best-effort per message: one message's failure (a transient Gmail
        API error, a malformed response, a grounding-check rejection)
        does not prevent the next message in the same batch from being
        tried -- the same "skip only this one, keep going" discipline
        `_process_message`'s own participant-resolution loop already
        establishes for a different per-item failure.

        That "skip and keep going" guarantee is per-*batch*, not per-
        *message* across calls: `_detect_action_for_message`'s own body-
        fetch UPDATE commits (`body IS NULL` no longer matches) before
        evidence registration/`execute_run`/`create_recommendation` run,
        so a failure in any of those *later* steps leaves the message
        permanently ineligible for a future `detect_actions_since` call's
        own `WHERE ... AND m.body IS NULL` selection -- not retried, just
        quietly never proactively scanned again. A deliberate trade-off
        (retrying would mean re-selecting on some signal other than "body
        not yet fetched," which nothing here currently tracks), not an
        oversight -- found and documented by round 1 review.

        `_detect_action_for_message` applies that same "commit the body
        UPDATE, accept no further retry" treatment to one case *before*
        evidence registration too, as of round 4 review's fix: a message
        whose Gmail response parsed successfully (200, valid JSON, a dict
        `payload`) but yielded no `text/plain` part is written with an
        empty-string body rather than left `body IS NULL` -- otherwise it
        would be re-selected, re-fetched, and re-rejected by every future
        call forever (round 4's finding: this was the one path through
        `_detect_action_for_message` that could never make a message
        ineligible, unlike every other early return, which stays `body IS
        NULL` because the failure genuinely may be transient -- a rate-
        limited or non-200 response, an unparseable response body, a
        missing `payload` key). An empty-string body is exactly `email_
        action_tools.py`'s own "genuinely empty" case, not "not yet
        fetched" -- that tool renders it as an empty message rather than
        omitting it.

        Builds its own `AuthContext` from the account's `owner_id`, not a
        caller-supplied one -- the created recommendation must belong to
        the connected account's own owner (`_owner_id_for_account`),
        regardless of which user's own request happened to trigger this
        particular sync call.

        `ollama_adapter`: `None` in production (`execute_run`'s own
        default, a real network-hitting `OllamaAdapter()`) -- threaded
        through only so tests can inject a mocked transport, the same
        `OllamaAdapterDep`-style dependency-injection shape `ai_insights.py`'s
        own HTTP endpoint already uses for the identical purpose.
        """
        if not get_settings().email_action_detection_enabled:
            return

        with SessionFactory() as session, session.begin():
            owner_id = _owner_id_for_account(
                session, context.workspace_id, context.connector_account_id
            )
        if owner_id is None:
            return
        with SessionFactory() as session, session.begin():
            if not _email_consent_active(session, context.workspace_id, owner_id):
                return

        with SessionFactory() as session, session.begin():
            rows = (
                session.execute(
                    text(
                        """
                        SELECT m.id, m.thread_id, m.external_message_id, m.sender
                        FROM email_messages m
                        JOIN email_threads t
                          ON t.id = m.thread_id AND t.workspace_id = m.workspace_id
                        WHERE m.workspace_id = :workspace_id AND m.owner_id = :owner_id
                          AND t.connector_account_id = :connector_account_id
                          AND m.direction = 'inbound' AND m.body IS NULL
                        ORDER BY (m.created_at >= :since) DESC, m.sent_at ASC
                        LIMIT :limit
                        """
                    ),
                    {
                        "workspace_id": context.workspace_id,
                        "owner_id": owner_id,
                        "connector_account_id": context.connector_account_id,
                        "since": since,
                        # `_MAX_ACTION_DETECTIONS_PER_CALL`, not `_MAX_
                        # MESSAGES_PER_CALL` -- see that constant's own
                        # comment. Round 1 review flagged this query's
                        # missing bound as defense-in-depth against
                        # `backfill`/`incremental_sync`'s own budget
                        # someday growing; round 2 review found the
                        # sharper reason this needs its own, much smaller
                        # bound regardless: every row here costs a live
                        # Gmail `messages.get` *and* a live Ollama
                        # inference call, sequentially, inside `/sync`'s
                        # own synchronous request handler.
                        "limit": _MAX_ACTION_DETECTIONS_PER_CALL,
                    },
                )
                .mappings()
                .all()
            )

        headers = _bearer_headers(context.credential)
        for row in rows:
            # Re-checked per message, not merely once above -- round 7
            # review found this loop never repeated the same recheck
            # `_sync_messages`/`_sync_history` already do before every one
            # of their own writes, despite `SYNC-CONTRACT.md`'s own general
            # contract ("An active `email` domain consent is checked
            # before external fetch and again before each message write.
            # Revocation during a call stops further writes.") covering
            # this write path too. Consent-revocation disconnect/purge is
            # not yet implemented (Task 7, per this phase's own status
            # doc) -- today, this per-message recheck is the *only*
            # enforcement standing between a user revoking `email` domain
            # consent and this loop continuing to fetch, decrypt-and-store,
            # and run their mail through an AI model regardless. `return`,
            # not `continue`, on revocation -- stops the whole remaining
            # batch, matching `_sync_messages`' identical "halts the call"
            # semantics, not merely this one row.
            with SessionFactory() as session, session.begin():
                if not _email_consent_active(session, context.workspace_id, owner_id):
                    return
            try:
                self._detect_action_for_message(
                    workspace_id=context.workspace_id,
                    owner_id=owner_id,
                    message_id=row["id"],
                    thread_id=row["thread_id"],
                    external_message_id=row["external_message_id"],
                    sender=row["sender"],
                    headers=headers,
                    ollama_adapter=ollama_adapter,
                )
            except Exception:  # noqa: BLE001 -- one message's failure never stops the batch
                continue

    def fetch_and_store_body(
        self,
        *,
        workspace_id: UUID,
        message_id: UUID,
        external_message_id: str,
        headers: dict[str, str],
    ) -> str | None:
        """Fetches one message's full body (`gmail.readonly`, `format=
        full`), encrypts and stores it, and returns the decrypted plain
        text on success (`""` for a genuinely empty/unextractable
        message, matching `email_action_tools.py`'s own "empty, not
        omitted" convention) -- or `None` if this call could not commit a
        body (a transient failure, still `body IS NULL` and eligible for
        a future call, or lost a race against another call that already
        fetched this same message).

        Extracted from `_detect_action_for_message` (Task 5) so Task 6's
        `gmail_threads.py:get_thread_endpoint` (a second, cross-module
        caller -- the reason this method is not underscore-prefixed like
        every other single-caller helper in this file) reuses this exact
        fetch-and-store mechanism for its own on-demand, explicit-thread-
        open fetch, rather than a second one -- the phase plan's own
        "matching Task 5's own fetch-and-store path" Task 6 instruction.
        Callers are themselves responsible for checking `body IS NULL`
        before calling this (this method always makes a live Gmail call
        regardless of current state) -- both current callers do so via
        their own eligibility query. Parsing the response into "retry
        later" vs. "permanent, write this" is `_parse_message_body_
        response`'s own job -- see that function's docstring and
        `_BodyParseOutcome`'s for why that decision is a single explicit
        field, not two different things both spelled `None`.
        """
        get_response = self._request_with_rate_limit_retry(
            "GET",
            f"/gmail/v1/users/me/messages/{external_message_id}",
            headers=headers,
            params={"format": "full"},
        )
        outcome = _parse_message_body_response(get_response)
        if outcome.retriable:
            return None
        now = datetime.now(UTC)
        # `email_action_tools.py`'s own "genuinely empty" case (that
        # module's docstring): its `get_thread_content_tool` renders `body
        # = ''` as an empty message rather than omitting it, which is
        # correct here -- there is no content to omit-as-not-yet-fetched.
        encrypted_body = encrypt_field(outcome.text)

        with SessionFactory() as session, session.begin():
            # `AND body IS NULL`: lost a race against another call already
            # fetching this same message's body (e.g. an overlapping
            # `incremental_sync`, or Task 6's own on-demand fetch racing
            # this proactive one) -- the winner's own write is
            # authoritative, so this one skips rather than overwriting
            # already-stored content a second time.
            updated = session.execute(
                text(
                    "UPDATE email_messages SET body = :body, body_fetched_at = :now, "
                    "updated_at = :now WHERE id = :id AND workspace_id = :workspace_id "
                    "AND body IS NULL"
                ),
                {
                    "body": encrypted_body,
                    "now": now,
                    "id": message_id,
                    "workspace_id": workspace_id,
                },
            )
            if cast("CursorResult[Any]", updated).rowcount == 0:
                return None
        return outcome.text

    def _detect_action_for_message(
        self,
        *,
        workspace_id: UUID,
        owner_id: UUID,
        message_id: UUID,
        thread_id: UUID,
        external_message_id: str,
        sender: str,
        headers: dict[str, str],
        ollama_adapter: OllamaAdapter | None,
    ) -> None:
        plain_text = self.fetch_and_store_body(
            workspace_id=workspace_id,
            message_id=message_id,
            external_message_id=external_message_id,
            headers=headers,
        )
        if not plain_text:
            return
        now = datetime.now(UTC)

        node_id = _resolve_or_create_person(
            workspace_id=workspace_id,
            owner_id=owner_id,
            email=sender,
            display_name=sender,
            source_ref=f"gmail:{external_message_id}",
            now=now,
        )
        with SessionFactory() as session, session.begin():
            evidence_id = _register_message_evidence(
                session,
                workspace_id=workspace_id,
                node_id=node_id,
                external_message_id=external_message_id,
                now=now,
            )

        auth = AuthContext(workspace_id=workspace_id, user_id=owner_id, timezone="UTC")
        # Bare session, not wrapped in its own `with session.begin():` --
        # `execute_run`/`create_recommendation` each manage their own
        # transaction boundaries and call `session.commit()` internally
        # (see `execute_run`'s own docstring on why it is "deliberately
        # not wrapped"; `ai_insights.py:generate_insight_endpoint`'s
        # identical three-phase shape is the precedent this mirrors).
        session = SessionFactory()
        try:
            run = execute_run(
                "email.detect_action",
                "restricted",
                # `message_id` (round 14 review): the specific message this
                # whole call exists to evaluate -- threaded through so
                # `email.get_thread_content`'s own cap can never silently
                # exclude it from an oversized thread (see that tool's own
                # docstring for the mechanism this closes).
                {"thread_id": str(thread_id), "message_id": str(message_id)},
                session=session,
                auth=auth,
                ollama_adapter=ollama_adapter,
            )
            if run.status != "completed" or run.output is None:
                return
            output = run.output
            if not output.get("has_action"):
                return

            # `evidence_ids=[evidence_id]` -- only the message that
            # triggered *this* detection run, not every id in the model's
            # own `output["cited_message_ids"]` (which may also name
            # earlier messages in the same thread, already grounded and
            # already fetched by a prior run of this same method). Those
            # earlier messages each got their own `pkos_evidence` row the
            # call that first fetched *their* body, but this method keeps
            # no message-id -> evidence-id index to look that back up by,
            # and re-registering a fresh evidence row per cited id here
            # would duplicate evidence already on record for the same
            # message. A human confirming this recommendation still sees
            # the full cited thread content in `rationale`/the prompt this
            # ran against; `evidence_ids` itself is a narrower pointer to
            # "the message that made this run happen," not an exhaustive
            # citation index -- a deliberate scope decision, not an
            # oversight, found and documented by round 1 review.
            recommendation_payload = RecommendationCreate(
                recommendation_type="email_action_detected",
                target_type=output["target_type"],
                proposed_action={"operation": "create", "value": None},
                proposed_fields=output["proposed_fields"],
                rationale=output["rationale"],
                confidence=output["confidence"],
                evidence_ids=[evidence_id],
                source="ai",
            )
            create_recommendation(
                session,
                auth,
                recommendation_payload,
                synthetic_request(uuid4(), uuid4()),
                f"email-detect-action:{external_message_id}",
            )
        finally:
            session.close()
