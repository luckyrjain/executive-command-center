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

**`backfill`/`incremental_sync`/`handle_webhook` are stubbed this task**
(Task 2's own scope, design doc Decision 1) -- each returns a zero-item
`succeeded` outcome rather than raising, matching `github_adapter.py`'s own
"an unimplemented resource type no-op-succeeds rather than raises" contract
interpretation for a not-yet-implemented feature.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from json import dumps, loads
from typing import Any
from urllib.parse import urlencode

import httpx

from ecc.config import get_settings
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
        """Stubbed -- Task 2's own scope (design doc Decision 1, plan Task
        2). Returns a zero-item success rather than raising, matching
        `github_adapter.py`'s identical "not-yet-implemented resource type"
        contract interpretation.
        """
        return SyncOutcome(
            resource_type=resource_type, items_processed=0, status="succeeded", next_cursor=None
        )

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        """Stubbed -- Task 2's own scope. See `backfill`'s identical note."""
        return SyncOutcome(
            resource_type=resource_type, items_processed=0, status="succeeded", next_cursor=None
        )

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
