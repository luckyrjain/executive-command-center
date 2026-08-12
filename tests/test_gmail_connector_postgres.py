"""Phase 10 Gmail Connector Task 1 (`backend/ecc/domains/personal/
gmail_adapter.py`, `backend/ecc/domains/personal/gmail_oauth.py`).

Covers, per the implementation plan's own Task 1 test list
(`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`), plus
several branches the plan's own list didn't enumerate but that a
multi-round adversarial review found and required real coverage for:

1. Allowlist rejects a non-listed account before any Google call
   (`GmailAdapter.is_account_allowed`, and `POST /oauth/start`'s own
   pre-redirect check).
2. Authorization-URL generation (`GmailAdapter.get_authorization_url`).
3. Callback/code-exchange against a mocked token-endpoint response
   (`GmailAdapter.handle_oauth_callback`, at the adapter level for every
   distinct post-exchange rejection branch -- missing access/refresh
   token, non-numeric/infinite/out-of-range `expires_in`, missing required
   scope, profile-lookup network error or non-200 status, missing
   `emailAddress`, non-allowlisted account -- plus the real `GET /oauth/
   callback` route end to end for a representative sample of these
   (the token-exchange-HTTP-status-failure case, and one post-exchange
   case, missing required scope) proving the router's `except
   AdapterAuthorizationError` clause -- generic, not branch-specific --
   correctly surfaces the app's structured error envelope; round 8 review
   found the docstring here previously implied every one of these branches
   had its own dedicated route-level test, which was never true and isn't
   the intent -- the router-level mechanism only needs proving once per
   rejection *class* (pre-exchange vs. post-exchange), not once per
   branch) --
   each asserting both the raised error and that the callback's `GET`
   route surfaces it through the app's real error envelope
   (`json()["error"]["code"]`, not FastAPI's bare `detail` shape).
4. Refresh-token renewal (`GmailAdapter.refresh_permissions`), including
   the fail-open-on-network-error case.
5. Encrypted-field-never-returned-in-list-view (`ConnectorAccountResponse`
   never carries `encrypted_credentials`, for a `gmail` row exactly like
   any other provider).
6. Workspace isolation (a `gmail` connector account created in one
   workspace is invisible to a session in a different workspace).
7. CSRF `state` verification (`_verify_state`): rejects a wrong signature,
   an expired state, and a state minted for a different session.
8. The 422 not-configured path, both at `GmailAdapter` (missing OAuth
   client id/secret) and at `POST /oauth/start`/`GET /oauth/callback`
   router level.
9. `disconnect`'s malformed-credential case (returns `None` rather than
   raising), its real provider-side revocation call, and the network-error
   case (never raises).
10. Revoke-on-rejection: every post-token-exchange rejection branch in
    `handle_oauth_callback` (see item 3's list) revokes the just-obtained,
    never-to-be-persisted Google grant via `/revoke` before raising --
    closing a real gap review found where an incomplete, per-branch-only
    version of this fix left 4 of 6 rejection branches leaking a live,
    ECC-unrecorded OAuth grant. Asserted via the mocked transport's own
    `revoked_tokens` capture, not merely "no exception raised."
11. Reconnecting a previously disconnected/errored account reactivates the
    existing `connector_accounts` row with the new credential (rather
    than the naive "conflict means already connected" response silently
    discarding it), including the `connector_account.reconnected` audit
    event this writes, and revokes the credential being overwritten
    (round 4: the same revoke-on-discard gap as item 10, relocated to
    `gmail_oauth_callback_endpoint`'s own `IntegrityError` handler --
    covers both its `active`-row branch, discarding the just-obtained new
    grant, and its reactivate branch, discarding the row's prior stored
    grant) -- including that branch's best-effort fallback when the row's
    prior credential itself cannot be decrypted (round 5: reactivation
    must still succeed, matching `disable_connector_endpoint`'s own
    identical, already-covered case).
12. Audit-event dedup: a replayed callback (same code/state) returns the
    same row without writing a second `connector_account.created` event.
13. A 200 response body that isn't valid JSON, or is valid JSON that isn't
    an object, from either Google endpoint `handle_oauth_callback` calls
    (`/token`, `/gmail/v1/users/me/profile`) is rejected the same way any
    other malformed response is -- round 6: this shape was previously
    trusted without validation, ahead of the revoke-on-reject guard for
    the token response specifically, so an atypical 200 body would have
    both skipped revocation and escaped as an uncaught exception type the
    router's `AdapterAuthorizationError`-only catch does not handle. All
    four cells of the {non-JSON, non-object} x {token, profile} matrix are
    covered (round 7 review found the non-JSON-profile cell was actually
    still missing despite this item's own "either endpoint" claim at the
    time -- added, closing the gap between what this docstring said and
    what the file actually tested).
14. `authorize`'s unconditional raise (never called in production --
    `gmail` connects exclusively through `get_authorization_url`/
    `handle_oauth_callback`); `_unpack_credential`'s shape-validation
    `TypeError` branch (round 5) reached through `refresh_permissions`/
    `disconnect`, not just the raw function in isolation -- both round 6:
    self-evidently-correct code that had no test at all before.
15. A `"scope": null` token-exchange response, and a non-string
    `emailAddress` in the profile response -- round 7: two fields that,
    unlike every sibling field in the same two response bodies, had no
    type guard before use (`body.get("scope", "")`'s default only applies
    when the key is *absent*, not when present with a `null` value; a
    bare `if not email_address:` passes a non-empty non-string value like
    a JSON number). Both previously raised an uncaught `AttributeError`
    on the very next line (`.split()`, `is_account_allowed`'s `.strip()`)
    instead of the intended `AdapterAuthorizationError`. Round 18 review
    found the null-scope test alone left the guard's *other* branch
    untested: `raw_scope is None` skips the `isinstance` check by design
    (falling through to "missing required scope" instead), so a truthy,
    non-string `scope` (a JSON number/array) -- the actual case the
    `isinstance(raw_scope, str)` check exists to catch -- had no test at
    all, unlike `access_token`/`refresh_token`/`emailAddress`'s own
    dedicated `rejects_non_string_*` siblings. Closed with
    `test_handle_oauth_callback_rejects_non_string_scope`.
16. An infinite (`float("inf")`, which `json.loads` accepts by default) or
    merely huge (an ordinary large integer, no exotic JSON literal needed)
    `expires_in` -- round 8: `float(expires_in)` accepts both cleanly, but
    the resulting Unix timestamp made `datetime.fromtimestamp(...)` raise
    `OverflowError`/`ValueError` at `_pack_credential`'s call site, which
    (unlike every other field this method validates) sat on the success
    path *after* the revoke-on-reject guard already exited -- skipping
    revocation for the just-obtained grant entirely, not merely escaping
    the error envelope.
17. A non-string `access_token`/`refresh_token` -- round 8: the only two
    fields in the token-response body still checked only for truthiness
    rather than type, matching `scope`/`emailAddress`'s round-7 treatment
    for consistency (this pair never actually crashed anywhere in the
    method, unlike those two -- a non-string value would just be silently
    embedded in the `Bearer` header and persisted credential).
18. `"expires_in": true`/`false` -- round 9: `bool` is a subtype of `int`
    in Python, so a JSON boolean passed both the presence check and
    `float(expires_in)`'s coercion silently, the one field in this
    response body still not actually rejecting a wrong-but-truthy type
    after rounds 7-8 closed the others.
19. An empty-but-correctly-typed `refresh_token`/`emailAddress` -- round
    19: the `not isinstance(x, str) or not x` guard shape appears three
    times in `handle_oauth_callback` (`access_token`/`refresh_token`,
    `email_address`); `access_token` already had dedicated tests for both
    its sub-conditions (`..._rejects_missing_access_token` uses `""` for
    `not x`, `..._rejects_non_string_access_token` uses a JSON number for
    `not isinstance`), but `refresh_token` and `email_address` each only
    had tests tripping the `not isinstance` half (`None`/a JSON number) --
    the `not x` half, an empty string, was untested for both. Confirmed by
    mutation: removing `or not refresh_token`/the `or not email_address`
    clause left all 55 then-existing tests passing. For `refresh_token`
    this is a real behavioral gap, not just a coverage gap -- without the
    clause, `handle_oauth_callback` *succeeds* on an empty refresh token,
    persisting a credential that can never be refreshed once its access
    token expires (verified directly via a standalone script); for
    `email_address` the outcome is unchanged either way (`is_account_
    allowed("")` already rejects), so that test asserts the specific
    error message rather than just the exception type, since a bare
    `pytest.raises(AdapterAuthorizationError)` there would not have caught
    the same mutation. Closed with `test_handle_oauth_callback_rejects_
    empty_refresh_token` and `test_handle_oauth_callback_rejects_empty_
    email_address`.
20. `get_authorization_url`/`handle_oauth_callback`'s own "OAuth client not
    configured" guards (`not client_id or not redirect_uri`; `not client_id
    or not client_secret`) -- round 20: a different-shaped instance of the
    same asymmetric-branch-coverage bug class rounds 18-19 closed for the
    `isinstance(x, str) or not x` guards, found by broadening the search to
    every multi-sub-condition guard in the module rather than re-checking
    only that one shape a third time. Both existing "not configured" tests
    set *both* fields empty simultaneously, never proving each `or` operand
    independently gates the raise -- mutation-confirmed by flipping each
    guard's `or` to `and` (a plausible refactor-time typo) and observing
    all 57 then-existing tests still pass, meaning a partially-configured
    deployment (e.g. `client_secret` set, `client_id` blank) would silently
    proceed instead of being rejected. Closed with
    `test_get_authorization_url_raises_when_only_client_id_missing`/
    `..._only_redirect_uri_missing` and
    `test_handle_oauth_callback_rejects_when_only_client_id_missing`/
    `..._only_client_secret_missing`, each setting exactly one field empty
    and its sibling non-empty.
21. `expires_in` entirely absent from the token response (`body.get(
    "expires_in")` returning `None`) -- round 21: the big six-sub-condition
    guard's `expires_in is None` operand was the one sub-condition, across
    all six, no test exercised independently; every other `expires_in`-
    adjacent test (items 16/18 above) is present-but-wrong-type, never
    absent. Mutation-confirmed: removing this operand left all 61
    then-existing tests passing, not because the branch is unreachable but
    because `float(None)` a few lines later raises `TypeError`, caught by
    that call's own separate `except` and re-raised as the same exception
    type via a different message -- the identical "redundant-but-harmless,
    still worth a message-specific test" situation round 19 found for
    `email_address`. Closed with `test_handle_oauth_callback_rejects_
    missing_expires_in`, asserting the guard's own specific message.
22. `GmailAdapter.backfill`/`incremental_sync`/`handle_webhook` -- round 22:
    these three stub methods (Task 2's own scope) had no test at all,
    direct or indirect, unlike every sibling adapter's identical
    "documented no-op" stub methods (`test_sandbox_adapter_handle_webhook`,
    `test_handle_webhook_is_a_documented_no_op` (datadog),
    `test_handle_webhook_ignores_non_repository_events` (github),
    `test_handle_webhook_ignores_non_push_hook_events` (gitlab) -- each
    with its own dedicated test). Closed with `test_backfill_is_a_
    documented_stub`/`test_backfill_ignores_since`/`test_incremental_
    sync_is_a_documented_stub`/`test_handle_webhook_is_a_documented_stub`,
    plus `test_sync_endpoint_reused_as_is_for_a_gmail_account`, which for
    the first time proves this module's own docstring claim that `sync`
    (like `list`/`disable`) "reuses ... existing generic endpoints as-is"
    by calling the real `POST /connectors/{id}/sync` route end to end
    against a real `gmail`-provider account, not just the adapter method
    directly -- `backfill`/`incremental_sync` are reachable through that
    already-shipped, fully generic route for any registered provider
    today, `gmail` included. Phase 10 Task 2's own commit later made
    `backfill`/`incremental_sync` real (no longer stubs) and renamed three
    of this item's four tests to match -- `test_backfill_is_a_documented_
    stub` -> `test_backfill_no_ops_for_a_resource_type_other_than_message`,
    `test_backfill_ignores_since` -> `test_backfill_ignores_since_for_a_
    resource_type_other_than_message`, `test_incremental_sync_is_a_
    documented_stub` -> `test_incremental_sync_no_ops_for_a_resource_type_
    other_than_message` (`test_handle_webhook_is_a_documented_stub` is
    unchanged -- `handle_webhook` alone is still a stub) -- present since
    that same commit, missed by every round-1-through-5 doc-drift pass
    across this file (each checked `tests/test_gmail_connector_sync_
    postgres.py`'s own "Covers" list repeatedly but never re-swept this
    sibling file's), found and corrected by round 6 review.
23. The `IntegrityError` handler's every `_adapter.disconnect(...)` call
    used to run while `create_session`'s transaction -- and, for the
    `active`-row and reactivation branches, its `FOR UPDATE` row lock --
    was still open -- round 23: `GmailAdapter.disconnect()` is the first
    `disconnect()` in this registry to make a real, blocking outbound
    HTTPS call, so this held a pooled DB connection and, on the two
    mainline reconnect paths, a row lock for up to `timeout_seconds=10.0`.
    Closed by deferring every revoke to a `pending_revokes` list drained
    in a `finally` block after the transaction closes.
    `test_oauth_callback_releases_pool_connection_and_row_lock_before_
    revoking` dynamically proves the `active`-row branch's release: a
    concurrent raw `SELECT ... FOR UPDATE NOWAIT` on the same row succeeds
    while the losing consent's revoke is blocked on an `Event`, rather
    than raising `LockNotAvailable`.
24. Round 24: the reactivation branch queues a *different* credential
    (the row's own prior grant, not the newly exchanged one) into the
    same `pending_revokes` list as item 23's `active`-row branch -- a
    structurally distinct call site the round-23 test above never
    exercised. Mutation-confirmed the gap was real: reverting just this
    branch's `pending_revokes.append(...)` to an inline `disconnect()`
    call left the whole then-existing suite passing. Closed with
    `test_oauth_callback_reactivation_releases_pool_connection_and_row_
    lock_before_revoking`, the same repro shape, asserting the *old*
    credential -- not the new grant -- is what gets revoked.
25. Round 26: the reactivation branch's queued *old* credential (item 24)
    was drained unconditionally, the same as every other queued entry --
    correct for every other entry (each one is a credential that was
    never going to be persisted regardless of commit/rollback), but wrong
    for this one specifically: it is only actually being discarded if the
    `UPDATE` that replaces it goes on to commit. Dynamically confirmed:
    reactivating a non-`disconnected` row (`error`, where nothing has
    necessarily already revoked the stored credential, unlike
    `disconnected`) whose credential is still genuinely live, then failing
    a later statement so the whole transaction rolls back, revoked that
    still-good credential at Google while leaving the DB row completely
    unchanged. Fixed with `pending_revokes_on_commit`, drained only if the
    transaction actually commits. `test_oauth_callback_revokes_new_grant_
    when_reactivation_update_raises`'s own assertion updated to match
    (the old credential is no longer expected to be revoked when its own
    replacing `UPDATE` fails); `test_oauth_callback_reactivation_failure_
    preserves_still_live_credential_of_error_row` added for the `error`-
    status case specifically.

See `docs/phases/phase-010/IMPLEMENTATION-STATUS.md`'s own round-23/24/26
paragraphs for the full account of these fixes.

Rounds 10-12 each found and closed one more unguarded statement in
`gmail_oauth_callback_endpoint`'s revoke-on-failure coverage (a real
Google grant obtained but never revoked on an unexpected DB failure) --
see `docs/phases/phase-010/IMPLEMENTATION-STATUS.md`'s own round-10/11/12
paragraphs for the full account of each gap and fix, and each of the four
`test_oauth_callback_revokes_new_grant_when_*_raises` tests below for the
specific statement and scenario it covers. Round 10 also fixed a separate
connection-pool-release issue (`session.close()` before `handle_oauth_
callback`'s slow outbound calls, replacing a `session.rollback()` that
left a pooled connection held idle) -- verified via a standalone script
against real Postgres rather than a dedicated pytest test, since a real
proof needs a blocking-adapter-plus-concurrency test (the `_SlowAdapter`/
`threading.Event` pattern `test_engineering_connectors_postgres.py`
already establishes) that would add real timing-flakiness risk to this CI
environment's already-observed sensitivity to timing-based tests -- a
deliberate trade-off, not an absence of a viable pattern. The other three
`INSERT`/re-`SELECT`/`UPDATE` failure paths, plus the plain success path's
own audit-write failure, all have real pytest coverage via
`_fail_next_matching_execute` (a `monkeypatch.setattr` on `sqlalchemy.orm.
Session.execute` -- the same module-level-swap idiom this file already
uses repeatedly for `_adapter`, just applied one layer lower).
"""

from __future__ import annotations

import threading
from base64 import urlsafe_b64encode
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from json import dumps, loads
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

import ecc.domains.personal.gmail_oauth as gmail_oauth_module
from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import STATEMENT_TIMEOUT_MS, engine
from ecc.domains.engineering.connectors import AdapterAuthorizationError, ConnectorAccountContext
from ecc.domains.engineering.crypto import decrypt_credential
from ecc.domains.personal.gmail_adapter import REQUIRED_SCOPES, GmailAdapter
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_ALLOWED_EMAIL = "allowed@example.test"
_SCOPE_STRING = " ".join(sorted(REQUIRED_SCOPES))


def _json_response(body: Any, *, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code, headers={"content-type": "application/json"}, content=dumps(body)
    )


def _token_response(
    *,
    access_token: str = "access-1",
    refresh_token: str | None = "refresh-1",
    scope: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "access_token": access_token,
        "expires_in": 3600,
        "scope": scope if scope is not None else _SCOPE_STRING,
    }
    if refresh_token is not None:
        body["refresh_token"] = refresh_token
    return body


def _oauth_transport(
    *,
    token_body: dict[str, Any] | None = None,
    token_status: int = 200,
    profile_email: str | None = _ALLOWED_EMAIL,
    profile_status: int = 200,
    revoked_tokens: list[str] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = token_body if token_body is not None else _token_response()
            return _json_response(body, status_code=token_status)
        if request.url.path == "/gmail/v1/users/me/profile":
            if profile_status != 200:
                return _json_response({}, status_code=profile_status)
            return _json_response({"emailAddress": profile_email})
        if request.url.path == "/revoke":
            if revoked_tokens is not None:
                # POSTed as a urlencoded form body (`token=...`), not query
                # params -- parse the same way the real endpoint receives it.
                revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    return httpx.MockTransport(handler)


def _account_context(credential: str) -> ConnectorAccountContext:
    return ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id=_ALLOWED_EMAIL,
        credential=credential,
    )


# --- GmailAdapter.is_account_allowed ----------------------------------------


def test_is_account_allowed_rejects_when_allowlist_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", "")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter()
        assert adapter.is_account_allowed(_ALLOWED_EMAIL) is False
    finally:
        get_settings.cache_clear()


def test_is_account_allowed_matches_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", "  Allowed@Example.Test , other@example.test ")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter()
        assert adapter.is_account_allowed("allowed@example.test") is True
        assert adapter.is_account_allowed("ALLOWED@EXAMPLE.TEST") is True
        assert adapter.is_account_allowed("nobody@example.test") is False
        assert adapter.is_account_allowed("") is False
    finally:
        get_settings.cache_clear()


# --- GmailAdapter.authorize --------------------------------------------------


def test_authorize_always_raises() -> None:
    """`gmail` accounts connect exclusively through `get_authorization_url`/
    `handle_oauth_callback` -- `gmail_oauth.py` never calls `authorize`, and
    this method's own unconditional raise documents that this path is
    genuinely unreachable in production (`ConnectorAdapter` still declares
    it; `authorize`'s own docstring covers why an `OAuth2ConnectorAdapter`
    implementer relies on the other two methods instead). Round 6 review
    found this single-line, self-evidently-correct method had zero test
    coverage of any kind.
    """
    adapter = GmailAdapter()
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("some-credential")


# --- GmailAdapter.get_authorization_url -------------------------------------


def test_get_authorization_url_raises_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter()
        with pytest.raises(AdapterAuthorizationError):
            adapter.get_authorization_url("some-state")
    finally:
        get_settings.cache_clear()


def test_get_authorization_url_raises_when_only_client_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard (`not client_id or not redirect_uri`) has two independent
    sub-conditions, but the sibling test above only ever exercises them
    together (`client_id=""` *and* `redirect_uri=""`) -- unable to tell
    apart "either missing rejects" (the intended `or` semantics) from a
    weaker "only rejects when both are missing" (`and`) regression. Found
    by round 20 review, mutation-confirmed: changing this guard's `or` to
    `and` left all 57 then-existing tests passing. `redirect_uri` is set
    here and `client_id` alone is missing.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/oauth/callback")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter()
        with pytest.raises(AdapterAuthorizationError):
            adapter.get_authorization_url("some-state")
    finally:
        get_settings.cache_clear()


def test_get_authorization_url_raises_when_only_redirect_uri_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the `client_id`-only case above: `client_id` is set and
    `redirect_uri` alone is missing.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter()
        with pytest.raises(AdapterAuthorizationError):
            adapter.get_authorization_url("some-state")
    finally:
        get_settings.cache_clear()


def test_get_authorization_url_builds_expected_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/oauth/callback")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter()
        url = adapter.get_authorization_url("opaque-state-value")
        assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=test-client-id" in url
        assert "state=opaque-state-value" in url
        assert "access_type=offline" in url
        assert "prompt=consent" in url
        for scope in REQUIRED_SCOPES:
            assert scope.replace(":", "%3A").replace("/", "%2F") in url or scope in url
    finally:
        get_settings.cache_clear()


# --- GmailAdapter.handle_oauth_callback -------------------------------------


def test_handle_oauth_callback_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter(transport=_oauth_transport())
        authorization = adapter.handle_oauth_callback("auth-code", "state-value")
        assert authorization.external_account_id == _ALLOWED_EMAIL
        assert authorization.display_name == _ALLOWED_EMAIL
        assert authorization.granted_scopes == REQUIRED_SCOPES
        assert authorization.credential is not None
        packed = loads(authorization.credential)
        assert packed["access_token"] == "access-1"
        assert packed["refresh_token"] == "refresh-1"
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_missing_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No real `refresh_token` came back, so `_revoke_best_effort` is still
    called (the revoke-on-reject guard covers this branch too -- see
    `handle_oauth_callback`'s own comment) but with an empty string, a
    harmless no-op at Google's end -- there is nothing to revoke, and
    nothing crashes trying.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        adapter = GmailAdapter(
            transport=_oauth_transport(
                token_body=_token_response(refresh_token=None), revoked_tokens=revoked_tokens
            )
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == [""]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_missing_required_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Also asserts the just-exchanged token is revoked rather than left as
    a standing, ECC-unrecorded grant at Google -- this rejection happens
    only after a real, successful token exchange, so nothing else would
    ever clean it up (`disconnect` requires a `connector_accounts` row,
    which a rejected callback never creates). Found by review.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        adapter = GmailAdapter(
            transport=_oauth_transport(
                token_body=_token_response(scope="https://www.googleapis.com/auth/gmail.metadata"),
                revoked_tokens=revoked_tokens,
            )
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_token_endpoint_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter(transport=_oauth_transport(token_status=400))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_allowlisted_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Also asserts the just-exchanged token is revoked -- see the
    identical note on `test_handle_oauth_callback_rejects_missing_
    required_scope`. This is the more realistic trigger of the two: the
    two-layer allowlist design (design doc Decision 3) exists specifically
    to catch an ECC-allowlisted caller who authorizes a *different*,
    non-allowlisted Google account at the consent screen -- exactly the
    scenario this test exercises.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", "someone-else@example.test")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        adapter = GmailAdapter(
            transport=_oauth_transport(profile_email=_ALLOWED_EMAIL, revoked_tokens=revoked_tokens)
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter(transport=_oauth_transport())
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_when_only_client_id_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same asymmetric-coverage gap as `get_authorization_url`'s sibling
    guard (`not client_id or not client_secret`), found by round 20 review:
    the existing "not configured" test above only exercises both fields
    empty together, unable to distinguish the intended `or` ("either
    missing rejects") from a weaker `and` ("only rejects when both are
    missing") regression -- mutation-confirmed by flipping this guard's
    `or` to `and` and observing all 57 then-existing tests still pass.
    `client_secret` is set here and `client_id` alone is missing. The
    allowlist is deliberately configured to allow the transport's own
    account through: without it, a naive version of this test would still
    raise `AdapterAuthorizationError` under the `and` mutation too (via the
    unrelated allowlist-rejection branch further down the same method),
    masking the exact regression this test exists to catch -- asserting
    the specific "not configured" message (not just the exception type)
    is what actually distinguishes the two.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter(transport=_oauth_transport())
        with pytest.raises(AdapterAuthorizationError, match="not configured"):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_when_only_client_secret_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the `client_id`-only case above: `client_id` is set and
    `client_secret` alone is missing. See that test's own docstring for why
    the allowlist is configured and the error message is asserted
    specifically.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter(transport=_oauth_transport())
        with pytest.raises(AdapterAuthorizationError, match="not configured"):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_missing_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`refresh_token` did come back for real even though `access_token`
    didn't -- confirms the revoke-on-reject guard uses the real refresh
    token here, not an empty one (round 3 review's own scenario: the
    guard must cover every post-token-exchange rejection, including this
    one, which an earlier fix shape left out entirely).
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        adapter = GmailAdapter(
            transport=_oauth_transport(
                token_body=_token_response(access_token=""), revoked_tokens=revoked_tokens
            )
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_string_access_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-string truthy `access_token` (a JSON number here) would pass
    a bare `if not access_token:` check -- round 8 review, same shape as
    the `scope`/`emailAddress` gaps round 7 fixed, though this one never
    actually crashed anywhere in the method (it just gets silently
    embedded in the `Bearer` header and the persisted credential).
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = _token_response()
            body["access_token"] = 12345
            return _json_response(body)
        if request.url.path == "/revoke":
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_string_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the non-string `access_token` case above."""
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = _token_response()
            body["refresh_token"] = 67890
            return _json_response(body)
        if request.url.path == "/revoke":
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_empty_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's `not isinstance(refresh_token, str) or not refresh_token`
    shape has two independent sub-conditions, mirroring `access_token`'s own
    (which has dedicated tests for both: `..._rejects_missing_access_token`
    uses `access_token=""` for the falsy-but-str branch, `..._rejects_non_
    string_access_token` uses a JSON number for the wrong-type branch).
    `refresh_token`'s two existing tests (`..._rejects_missing_refresh_
    token` uses `None`, `..._rejects_non_string_refresh_token` uses a JSON
    number) both trip the `not isinstance(...)` half of the condition --
    neither exercises `not refresh_token` on a correctly-typed-but-empty
    string, unlike `access_token`. Found by round 19 review, the identical
    asymmetric-branch-coverage bug class round 18 closed for `scope`, one
    field over: confirmed by removing `or not refresh_token` from the guard
    and re-running the full suite -- all 55 prior tests still passed, and
    (verified via a standalone script, not merely inferred) the callback
    then *succeeds*, returning a `ConnectorAuthorization` whose packed
    credential embeds `"refresh_token": ""` -- a persisted Gmail connection
    that can never actually refresh its access token once it expires,
    accepted rather than rejected, and never revoked since it isn't treated
    as a rejection at all.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = _token_response()
            body["refresh_token"] = ""
            return _json_response(body)
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        # An empty refresh_token means there was never anything real to
        # revoke -- the same harmless-no-op contract already asserted for
        # `..._rejects_missing_refresh_token` (`refresh_token=None`).
        assert revoked_tokens == [""]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_missing_expires_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guard's `expires_in is None` sub-condition -- the key entirely
    absent from Google's token response, or present with a JSON `null` --
    had no dedicated test: every other `expires_in`-adjacent test below
    (`..._rejects_boolean_expires_in`, `..._rejects_non_numeric_expires_in`,
    `..._rejects_infinite_expires_in`, `..._rejects_huge_expires_in`)
    exercises a *present-but-wrong-type* value, never an *absent* one.
    Found by round 21 review's exhaustive per-operand sweep of this guard's
    six sub-conditions (access_token x2, refresh_token x2, expires_in x2),
    the first round to check `expires_in`'s own two sub-conditions
    independently rather than only as a group with the others.

    Confirmed by mutation: removing `or expires_in is None` from the guard
    left all 61 then-existing tests passing -- not because the branch is
    unreachable, but because the resulting `expires_in=None` still fails a
    few lines later at `float(expires_in)` (`float(None)` raises
    `TypeError`), caught by that call's own separate `except (TypeError,
    ValueError)` and re-raised as the same `AdapterAuthorizationError`
    type, just via a different message and the already-tested `..._rejects_
    non_numeric_expires_in` code path -- the identical "redundant-but-
    harmless, still worth a message-specific test" situation round 19 found
    for `email_address`. Asserting the specific message this guard raises
    (not just the exception type) is what actually distinguishes this
    branch firing from the fallback path silently masking its removal.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = _token_response()
            del body["expires_in"]
            return _json_response(body)
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(
            AdapterAuthorizationError, match="missing access_token/refresh_token/expires_in"
        ):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_boolean_expires_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`bool` is a subtype of `int` in Python -- `"expires_in": true/false`
    would otherwise pass both the presence check and `float(expires_in)`
    silently (`float(True) == 1.0`), storing a credential that claims to
    expire in ~1 second/immediately rather than being rejected the way
    every other truthy-but-wrong-type field in this response body now is.
    Round 9 review found this exact gap.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = _token_response()
            body["expires_in"] = True
            return _json_response(body)
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_numeric_expires_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        body = _token_response()
        body["expires_in"] = "not-a-number"
        adapter = GmailAdapter(
            transport=_oauth_transport(token_body=body, revoked_tokens=revoked_tokens)
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_infinite_expires_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`float("inf")` (a real risk, not exotic -- `json.loads` accepts the
    non-strict `Infinity` literal by default) passes `float(expires_in)`
    cleanly, but `datetime.fromtimestamp` on the resulting timestamp raises
    `OverflowError` -- a type the original guard didn't anticipate, and
    (before this fix) a call that only happened *after* the guard had
    already exited, on the success path, skipping revoke-on-reject
    entirely for the just-obtained grant. Round 8 review found this exact
    gap.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        body = _token_response()
        body["expires_in"] = float("inf")
        adapter = GmailAdapter(
            transport=_oauth_transport(token_body=body, revoked_tokens=revoked_tokens)
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_huge_expires_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Companion to the infinite case above -- an ordinary, syntactically
    valid huge integer (no exotic JSON literal needed) hits the exact same
    `OverflowError` once added to the current Unix timestamp.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        body = _token_response()
        body["expires_in"] = 999999999999999999999
        adapter = GmailAdapter(
            transport=_oauth_transport(token_body=body, revoked_tokens=revoked_tokens)
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_profile_lookup_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The round-3-review-found gap this whole guard restructure exists to
    close: a Gmail profile-lookup 5xx is a realistic, non-adversarial
    failure mode (arguably more likely in production than a scope
    mismatch), and the original per-branch revoke calls never covered it.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        adapter = GmailAdapter(
            transport=_oauth_transport(profile_status=500, revoked_tokens=revoked_tokens)
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_missing_email_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    try:
        adapter = GmailAdapter(
            transport=_oauth_transport(profile_email=None, revoked_tokens=revoked_tokens)
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_token_endpoint_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_profile_lookup_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The `httpx.HTTPError` branch of the profile lookup (distinct from
    `test_handle_oauth_callback_rejects_profile_lookup_error_status`'s
    non-200-response branch) -- round 4 review found this specific branch
    was undocumented-as-tested despite the module docstring and
    IMPLEMENTATION-STATUS.md both already claiming "profile-lookup network
    error or non-200 status" coverage. Production code was never actually
    at risk (the single `try/except Exception` in `handle_oauth_callback`
    covers this branch exactly like every other), but the claim needed a
    real test to back it, not just structural coverage by construction.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return _json_response(_token_response())
        if request.url.path == "/gmail/v1/users/me/profile":
            raise httpx.ConnectError("boom", request=request)
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_json_token_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 6 review found `response.json()`/`body.get(...)` previously sat
    *ahead* of the revoke-on-reject guard -- a 200 response with a non-JSON
    body (`response.json()` raising `json.JSONDecodeError`, a `ValueError`)
    would have escaped uncaught past both the guard and the router's
    `AdapterAuthorizationError`-only catch. Now inside the guard and
    converted to the app's own error type.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return httpx.Response(
                status_code=200, headers={"content-type": "application/json"}, content=b"not json"
            )
        if request.url.path == "/revoke":
            # `refresh_token` was never assigned (the failure is ahead of
            # that line) -- `_revoke_best_effort("")` is still called per
            # its own "always safe, only useful when there's something to
            # revoke" contract.
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_object_token_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the non-JSON case above -- valid JSON that isn't an
    object (a list here) previously reached `body.get(...)` and raised an
    uncaught `AttributeError` instead.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return _json_response([1, 2, 3])
        if request.url.path == "/revoke":
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_object_profile_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same shape-validation gap as the token-response tests above, for the
    profile lookup's response body -- a real token was already exchanged by
    this point, so this must revoke it.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return _json_response(_token_response())
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response(None)
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_json_profile_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fourth cell of the {non-JSON, non-object} x {token, profile}
    response-shape matrix -- round 6 added the other three
    (`test_handle_oauth_callback_rejects_non_json_token_response`,
    `..._non_object_token_response`, `..._non_object_profile_response`)
    but round 7 review found this one was missing despite the module
    docstring and IMPLEMENTATION-STATUS.md both already claiming "either
    Google endpoint" was covered. The code itself was already correct
    (`gmail_adapter.py`'s profile-lookup body parsing wraps `ValueError`
    exactly like the token response does) -- this closes the doc/test-
    completeness gap, not a functional bug.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return _json_response(_token_response())
        if request.url.path == "/gmail/v1/users/me/profile":
            return httpx.Response(
                status_code=200, headers={"content-type": "application/json"}, content=b"not json"
            )
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_null_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    """`body.get("scope", "")`'s default only applies when the key is
    absent -- a `"scope": null` response (key present, value `None`)
    previously reached `.split()` and raised an uncaught `AttributeError`.
    Round 7 review found this exact gap.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = _token_response()
            body["scope"] = None
            return _json_response(body)
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_string_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to `test_handle_oauth_callback_rejects_null_scope` above,
    covering the isinstance check's *other* branch: that test only
    exercises `raw_scope is None` (which skips the `isinstance` check
    entirely by design and falls through to the "missing required scope"
    rejection instead), so the actual `raw_scope is not None and not
    isinstance(raw_scope, str)` guard -- the one that raises "Gmail token
    exchange returned a non-string scope" -- had no test exercising a
    truthy, non-string, non-null value (a JSON number here), unlike every
    sibling field in this response body (`access_token`/`refresh_token`/
    `emailAddress` each have their own dedicated `rejects_non_string_*`
    test). Found by round 18 review: a regression to this specific guard
    (e.g. the `isinstance` check being dropped or inverted) would not have
    failed any existing test -- `(raw_scope or "").split()` on a non-string
    truthy value like an int raises an uncaught `AttributeError` that
    escapes the router's `AdapterAuthorizationError`-only catch, the same
    envelope-bypass bug class rounds 5-7 closed elsewhere in this method.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = _token_response()
            body["scope"] = 12345
            return _json_response(body)
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError, match="non-string scope"):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_non_string_email_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-string truthy `emailAddress` (a JSON number here) would pass
    a bare `if not email_address:` check and then raise an uncaught
    `AttributeError` inside `is_account_allowed`'s own `.strip()` call.
    Round 7 review found this exact gap, the same shape as the `scope`
    case above.
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return _json_response(_token_response())
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"emailAddress": 12345})
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_empty_email_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to `test_handle_oauth_callback_rejects_empty_refresh_
    token` above -- `emailAddress`'s guard has the identical two-sub-
    condition shape (`not isinstance(email_address, str) or not
    email_address`), and its two existing tests (`..._rejects_missing_
    email_address` uses `profile_email=None`, `..._rejects_non_string_
    email_address` uses a JSON number) both trip the `not isinstance(...)`
    half, leaving `not email_address` on a correctly-typed-but-empty string
    untested -- found by round 19 review, the same asymmetry as
    `refresh_token`. Unlike that case, removing this specific sub-condition
    does not change the ultimate accept/reject outcome: an empty
    `email_address` falls through to `is_account_allowed("")`, which
    already rejects on empty/blank per its own docstring -- so this test
    asserts the *specific* rejection reason (`match=`) rather than merely
    `AdapterAuthorizationError`, since a bare `pytest.raises` here would
    keep passing even with the `not email_address` guard removed (verified
    directly: the mutation still raises, just via the allowlist-rejection
    message instead of "missing emailAddress" -- a real but lower-severity
    gap than the refresh_token case, closed here for the same completeness
    reason rather than because production behavior was ever actually wrong).
    """
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            return _json_response(_token_response())
        if request.url.path == "/gmail/v1/users/me/profile":
            return _json_response({"emailAddress": ""})
        if request.url.path == "/revoke":
            revoked_tokens.append(request.content.decode().removeprefix("token="))
            return httpx.Response(200)
        raise AssertionError(f"unexpected request to {request.url}")

    try:
        adapter = GmailAdapter(transport=httpx.MockTransport(handler))
        with pytest.raises(AdapterAuthorizationError, match="missing emailAddress"):
            adapter.handle_oauth_callback("auth-code", "state-value")
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


# --- GmailAdapter.backfill / incremental_sync / handle_webhook (stubs) -----
#
# Round 22 review: unlike every sibling adapter's own identical "documented
# no-op" stub methods (`test_sandbox_adapter_handle_webhook`,
# `test_handle_webhook_is_a_documented_no_op` (datadog),
# `test_handle_webhook_ignores_non_repository_events` (github),
# `test_handle_webhook_ignores_non_push_hook_events` (gitlab) -- every one
# of them has at least one dedicated test), `GmailAdapter.backfill`/
# `incremental_sync`/`handle_webhook` had no test at all, direct or
# indirect, anywhere in this suite -- confirmed by `grep -n "backfill\|
# incremental_sync\|handle_webhook" tests/test_gmail_connector_postgres.py`
# returning nothing before this section was added. This is not merely a
# style gap: `backfill`/`incremental_sync` are reachable *today* through the
# already-shipped, fully generic `POST /api/v1/engineering/connectors/
# {account_id}/sync` endpoint (`connector_accounts.py`'s `sync_connector_
# endpoint`, which dispatches to `adapter.backfill`/`incremental_sync` for
# *any* registered provider, `gmail` included) -- yet no test, here or in
# `test_engineering_connectors_postgres.py`, had ever called `/sync` against
# a `gmail`-provider account, despite `gmail_oauth.py`'s own module
# docstring explicitly claiming "every other `connector_accounts` operation
# (list, sync, disable) reuses ... existing generic endpoints as-is" -- a
# claim item 5/6's tests below prove for list, `test_sync_*_gmail_account`
# now proves for sync (`disable` has no dedicated gmail test either, but is
# lower-value to add: unlike `sync`, `disable_connector_endpoint` is
# provider-agnostic and calls only `adapter.disconnect`, which already has
# its own direct `GmailAdapter.disconnect` coverage above).


def test_backfill_no_ops_for_a_resource_type_other_than_message() -> None:
    """Task 2 made `backfill` real for `resource_type="message"` (see
    `tests/test_gmail_connector_sync_postgres.py`) -- every other value
    (this test uses the same semantically-meaningless `"thread"` round 22
    originally picked, before `ResourceType` had any Gmail-appropriate
    value to test with at all) still zero-item-succeeds, matching every
    other adapter's "not-yet-implemented resource type" contract.
    """
    adapter = GmailAdapter()
    outcome = adapter.backfill(_account_context("irrelevant"), "thread")
    assert outcome.resource_type == "thread"
    assert outcome.items_processed == 0
    assert outcome.status == "succeeded"
    assert outcome.next_cursor is None


def test_backfill_ignores_since_for_a_resource_type_other_than_message() -> None:
    adapter = GmailAdapter()
    outcome = adapter.backfill(_account_context("irrelevant"), "thread", since=datetime.now(UTC))
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0


def test_incremental_sync_no_ops_for_a_resource_type_other_than_message() -> None:
    """See `test_backfill_no_ops_for_a_resource_type_other_than_message`'s
    own docstring -- same Task 2 change, same reasoning.
    """
    adapter = GmailAdapter()
    outcome = adapter.incremental_sync(_account_context("irrelevant"), "thread", "some-cursor")
    assert outcome.resource_type == "thread"
    assert outcome.items_processed == 0
    assert outcome.status == "succeeded"
    assert outcome.next_cursor == "some-cursor"


def test_handle_webhook_is_a_documented_stub() -> None:
    adapter = GmailAdapter()
    outcome = adapter.handle_webhook(_account_context("irrelevant"), b'{"anything": true}', {})
    assert outcome.resource_type == "thread"
    assert outcome.items_processed == 0
    assert outcome.status == "succeeded"


# --- GmailAdapter.refresh_permissions / disconnect --------------------------


def test_refresh_permissions_active_on_successful_refresh() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_token_response())

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    credential = dumps(
        {
            "access_token": "old",
            "refresh_token": "refresh-1",
            "expires_at": "2020-01-01T00:00:00+00:00",
        }
    )
    assert adapter.refresh_permissions(_account_context(credential)) == "active"


def test_refresh_permissions_permission_lost_on_invalid_grant() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"error": "invalid_grant"}, status_code=400)

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    credential = dumps(
        {
            "access_token": "old",
            "refresh_token": "revoked",
            "expires_at": "2020-01-01T00:00:00+00:00",
        }
    )
    assert adapter.refresh_permissions(_account_context(credential)) == "permission_lost"


def test_refresh_permissions_permission_lost_on_malformed_credential() -> None:
    adapter = GmailAdapter()
    assert adapter.refresh_permissions(_account_context("not-json")) == "permission_lost"


def test_refresh_permissions_permission_lost_on_non_object_credential() -> None:
    """Distinct from the malformed-JSON case above -- valid JSON that isn't
    an object (round 5's `_unpack_credential` shape-validation fix) reaches
    this same `except (ValueError, TypeError)` via the `TypeError` branch,
    not `ValueError`. Round 6 review found this specific branch had no
    test exercising it through a real caller, only the raw function in
    isolation.
    """
    adapter = GmailAdapter()
    assert adapter.refresh_permissions(_account_context("[1, 2, 3]")) == "permission_lost"


def test_refresh_permissions_fails_open_on_network_error() -> None:
    """Mirrors `test_engineering_datadog_sync_postgres.py`'s identical
    `test_refresh_permissions_fails_open_on_network_error` -- a transient
    network failure while checking permission state must never be
    misreported as `permission_lost` (which would incorrectly surface a
    healthy connection as broken); `refresh_permissions`'s own docstring
    already documents this fail-open choice, this locks it down.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    credential = dumps(
        {
            "access_token": "old",
            "refresh_token": "refresh-1",
            "expires_at": "2020-01-01T00:00:00+00:00",
        }
    )
    assert adapter.refresh_permissions(_account_context(credential)) == "active"


def test_disconnect_returns_none_for_malformed_credential() -> None:
    adapter = GmailAdapter()
    assert adapter.disconnect(_account_context("not-json")) is None


def test_disconnect_returns_none_for_non_object_credential() -> None:
    """Companion to `test_refresh_permissions_permission_lost_on_non_object_
    credential` -- same round-5 `_unpack_credential` `TypeError` branch,
    reached through `disconnect` instead.
    """
    adapter = GmailAdapter()
    assert adapter.disconnect(_account_context("[1, 2, 3]")) is None


def test_disconnect_never_raises_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    credential = dumps(
        {"access_token": "a", "refresh_token": "r", "expires_at": "2020-01-01T00:00:00+00:00"}
    )
    assert adapter.disconnect(_account_context(credential)) is None


def test_disconnect_revokes_the_stored_refresh_token() -> None:
    """The success path `test_disconnect_returns_none_for_malformed_credential`/
    `test_disconnect_never_raises_on_network_error` don't cover -- a
    well-formed credential's `refresh_token` must be the one extracted and
    POSTed to `/revoke`, not, say, the access token or an empty string.
    """
    revoked_tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/revoke"
        revoked_tokens.append(request.content.decode().removeprefix("token="))
        return httpx.Response(200)

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    credential = dumps(
        {
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "expires_at": "2020-01-01T00:00:00+00:00",
        }
    )
    assert adapter.disconnect(_account_context(credential)) is None
    assert revoked_tokens == ["refresh-1"]


# --- integration: POST /oauth/start, GET /oauth/callback -------------------


@pytest.fixture
def gmail_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Gmail Connector Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        account_id = create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=_ALLOWED_EMAIL,
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        _cleanup_workspace(workspace_id, account_id)


def _cleanup_workspace(workspace_id: UUID, account_id: UUID | None = None) -> None:
    with engine.begin() as connection:
        for table in (
            "connector_accounts",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )
        # `accounts.email` is globally unique (not scoped per workspace) --
        # left uncleaned, every test reusing the same literal `_ALLOWED_
        # EMAIL`/`"other-owner@example.test"` fixture identity would
        # collide with the previous test's still-present `accounts` row on
        # its own `create_identity` call (found by this PR's own CI run:
        # every fixture-using test after the first errored with
        # `UniqueViolation` on `accounts_email_key`).
        if account_id is not None:
            connection.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": account_id})


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


# --- _verify_state (mirrors test_identity_invitations_postgres.py's own
# "expired" vs. "wrong/tampered" separation for its structurally identical
# signed/expiring token check) ------------------------------------------


def test_verify_state_rejects_wrong_signature() -> None:
    # Structurally well-formed (decodes, splits into nonce/expires_at/
    # signature cleanly, not expired) but a signature that was never
    # produced by `_sign_state` for this `(nonce, expires_at)` pair -- the
    # genuine CSRF-forgery case, distinct from a merely-malformed value.
    auth = AuthContext(workspace_id=uuid4(), user_id=uuid4(), timezone="UTC")
    nonce = "fixed-nonce"
    expires_at = int(datetime.now(UTC).timestamp()) + 600
    wrong_signature = "0" * 64
    raw = f"{nonce}.{expires_at}.{wrong_signature}"
    state = urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    assert gmail_oauth_module._verify_state(auth, state) is False


def test_verify_state_rejects_expired_state() -> None:
    auth = AuthContext(workspace_id=uuid4(), user_id=uuid4(), timezone="UTC")
    nonce = "fixed-nonce"
    expired_at = int(datetime.now(UTC).timestamp()) - 1
    signature = gmail_oauth_module._sign_state(auth, nonce, expired_at)
    raw = f"{nonce}.{expired_at}.{signature}"
    state = urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
    # Correctly signed for this exact (nonce, expires_at) pair -- proves
    # this is the expiry check failing, not the signature check.
    assert gmail_oauth_module._verify_state(auth, state) is False


def test_verify_state_rejects_state_minted_for_a_different_session() -> None:
    minting_auth = AuthContext(workspace_id=uuid4(), user_id=uuid4(), timezone="UTC")
    other_auth = AuthContext(workspace_id=uuid4(), user_id=uuid4(), timezone="UTC")
    state = gmail_oauth_module._encode_state(minting_auth)
    assert gmail_oauth_module._verify_state(other_auth, state) is False
    assert gmail_oauth_module._verify_state(minting_auth, state) is True


def test_oauth_start_rejects_non_allowlisted_caller(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", "someone-else@example.test")
    get_settings.cache_clear()
    try:
        response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "GMAIL_ACCOUNT_NOT_ALLOWLISTED"
    finally:
        get_settings.cache_clear()


def test_oauth_start_returns_authorization_url_when_allowlisted(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    try:
        response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        assert response.status_code == 200
        body = response.json()
        assert body["authorization_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "state=" in body["authorization_url"]
    finally:
        get_settings.cache_clear()


def test_oauth_start_returns_422_when_not_configured(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allowlisted (passes the pre-redirect check), but no Gmail OAuth
    client configured -- `get_authorization_url`'s own `AdapterAuthorization
    Error` must surface as a 422 at the router, not an unhandled 500.
    """
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "")
    get_settings.cache_clear()
    try:
        response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "GMAIL_OAUTH_NOT_CONFIGURED"
    finally:
        get_settings.cache_clear()


def test_oauth_complete_redirects_to_frontend_with_a_connected_marker_on_success(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/oauth/complete")
    monkeypatch.setenv("ECC_FRONTEND_URL", "https://app.example.test")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]

        response = client.get(
            "/api/v1/personal/gmail/oauth/complete",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == "https://app.example.test/?gmail=connected"

        with engine.begin() as connection:
            provider = connection.execute(
                text("SELECT provider FROM connector_accounts WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            ).scalar_one()
        assert provider == "gmail"
    finally:
        get_settings.cache_clear()


def test_oauth_complete_redirects_to_frontend_with_the_error_code_on_failure(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_FRONTEND_URL", "https://app.example.test")
    get_settings.cache_clear()
    try:
        response = client.get(
            "/api/v1/personal/gmail/oauth/complete",
            params={"code": "auth-code", "state": "tampered-state"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == (
            "https://app.example.test/?gmail=error&code=GMAIL_OAUTH_STATE_INVALID"
        )
    finally:
        get_settings.cache_clear()


def test_oauth_complete_redirects_with_denied_marker_when_code_is_missing(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Google's own "Cancel" redirect on its consent screen carries
    `error=access_denied` and `state`, but no `code` at all -- the
    mainline rejection path, not an edge case. `code` must stay optional
    on this endpoint specifically (`/oauth/callback` above is unaffected,
    still requires it) or FastAPI 422s during dependency resolution,
    before this endpoint's own try/except ever runs, reintroducing the
    exact "stranded on raw backend JSON" bug this endpoint exists to
    close.
    """
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_FRONTEND_URL", "https://app.example.test")
    get_settings.cache_clear()
    try:
        response = client.get(
            "/api/v1/personal/gmail/oauth/complete",
            params={"error": "access_denied", "state": "whatever-state"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert (
            response.headers["location"]
            == "https://app.example.test/?gmail=error&code=GMAIL_OAUTH_DENIED"
        )
    finally:
        get_settings.cache_clear()


def test_oauth_complete_redirects_with_a_generic_error_when_callback_raises_a_non_http_exception(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient failure inside `/oauth/callback` (a DB deadlock, a
    dropped connection, an `AssertionError`) is not an `HTTPException` --
    unlike every other endpoint in this app (whose raw 500 is invisible
    to the end user, caught by the frontend's own `fetch`), this is the
    one endpoint where an unhandled exception reaches the browser
    directly via a top-level navigation. Must still redirect, not crash
    past this wrapper.
    """
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/oauth/complete")
    monkeypatch.setenv("ECC_FRONTEND_URL", "https://app.example.test")
    get_settings.cache_clear()

    class _BrokenAdapter(GmailAdapter):
        def handle_oauth_callback(self, code: str, state: str) -> object:
            raise RuntimeError("simulated transient failure")

    monkeypatch.setattr(
        gmail_oauth_module, "_adapter", _BrokenAdapter(transport=_oauth_transport())
    )
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]

        response = client.get(
            "/api/v1/personal/gmail/oauth/complete",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert (
            response.headers["location"]
            == "https://app.example.test/?gmail=error&code=GMAIL_OAUTH_FAILED"
        )
    finally:
        get_settings.cache_clear()


def test_oauth_complete_strips_a_trailing_slash_from_frontend_url_before_redirecting(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_FRONTEND_URL", "https://app.example.test/")
    get_settings.cache_clear()
    try:
        response = client.get(
            "/api/v1/personal/gmail/oauth/complete",
            params={"error": "access_denied", "state": "whatever-state"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert (
            response.headers["location"]
            == "https://app.example.test/?gmail=error&code=GMAIL_OAUTH_DENIED"
        )
    finally:
        get_settings.cache_clear()


def test_oauth_callback_rejects_invalid_state(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = gmail_test_context
    response = client.get(
        "/api/v1/personal/gmail/oauth/callback",
        params={"code": "auth-code", "state": "tampered-state"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "GMAIL_OAUTH_STATE_INVALID"


def test_oauth_callback_returns_422_with_error_envelope_on_google_rejection(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(
        gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport(token_status=400))
    )
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "GMAIL_OAUTH_FAILED"
    finally:
        get_settings.cache_clear()


def test_oauth_callback_returns_422_with_error_envelope_on_missing_scope(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`test_oauth_callback_returns_422_with_error_envelope_on_google_rejection`
    exercises the token-exchange-HTTP-status-failure case (`token_status=
    400`) end to end -- round 8 review found the module docstring's item 3
    overclaimed that every distinct *post*-exchange rejection branch
    (missing required scope among them) also had an end-to-end test
    proving the router's error envelope, when in fact only the adapter-
    level `test_handle_oauth_callback_rejects_missing_required_scope` did.
    This closes that gap for one representative, security-relevant branch
    -- the router's `except AdapterAuthorizationError` clause is generic
    (not branch-specific), so this and the token-status-400 test together
    are sufficient to prove the envelope mechanism works for both the
    pre-guard and post-guard rejection classes; the remaining adapter-
    level-only branches are not independently re-asserted at the route
    level (redundant coverage of the same generic `except` clause).
    """
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(
        gmail_oauth_module,
        "_adapter",
        GmailAdapter(transport=_oauth_transport(token_body=_token_response(scope=""))),
    )
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "GMAIL_OAUTH_FAILED"
    finally:
        get_settings.cache_clear()


def test_oauth_callback_creates_connector_account(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        assert start_response.status_code == 200
        authorization_url = start_response.json()["authorization_url"]
        state = httpx.URL(authorization_url).params["state"]

        callback_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert callback_response.status_code == 200
        body = callback_response.json()
        assert body["provider"] == "gmail"
        assert body["external_account_id"] == _ALLOWED_EMAIL
        assert "encrypted_credentials" not in body
        assert "credential" not in body
        assert sorted(body["granted_scopes"]) == sorted(REQUIRED_SCOPES)

        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT encrypted_credentials FROM connector_accounts "
                    "WHERE workspace_id = :workspace_id AND provider = 'gmail'"
                ),
                {"workspace_id": workspace_id},
            ).one()
        stored = loads(decrypt_credential(row[0]))
        assert stored["access_token"] == "access-1"
        assert stored["refresh_token"] == "refresh-1"

        # A first connect writes exactly one audit event for this account.
        with engine.begin() as connection:
            audit_count = connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE workspace_id = :workspace_id "
                    "AND aggregate_id = :aggregate_id AND event_type = 'connector_account.created'"
                ),
                {"workspace_id": workspace_id, "aggregate_id": UUID(body["id"])},
            ).scalar_one()
        assert audit_count == 1
    finally:
        get_settings.cache_clear()


def test_oauth_callback_replay_does_not_duplicate_audit_event(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reloaded callback (same code/state) must not error -- returns the
    already-connected account instead of a hard failure, and does not
    write a second audit event (the SAVEPOINT-inside-one-transaction
    guarantee `create_connector_endpoint` also relies on: a losing
    `IntegrityError` proves the winner's own transaction, including its
    audit write, already committed -- there is nothing new to record for a
    passive replay).
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        assert start_response.status_code == 200
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]

        callback_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert callback_response.status_code == 200
        body = callback_response.json()

        replay_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert replay_response.status_code == 200
        assert replay_response.json()["id"] == body["id"]
        with engine.begin() as connection:
            audit_count_after_replay = connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE workspace_id = :workspace_id "
                    "AND aggregate_id = :aggregate_id"
                ),
                {"workspace_id": workspace_id, "aggregate_id": UUID(body["id"])},
            ).scalar_one()
        assert audit_count_after_replay == 1
    finally:
        get_settings.cache_clear()


def test_oauth_callback_is_workspace_isolated(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second workspace's session must not see a connected account via
    the generic list endpoint."""
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        assert start_response.status_code == 200
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]

        callback_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert callback_response.status_code == 200
        body = callback_response.json()

        other_workspace_id = uuid4()
        other_user_id = uuid4()
        other_token = f"session-{uuid4()}"
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, timezone, created_at) "
                    "VALUES (:id, 'Other Workspace', 'UTC', :now)"
                ),
                {"id": other_workspace_id, "now": now},
            )
            other_account_id = create_identity(
                connection,
                workspace_id=other_workspace_id,
                user_id=other_user_id,
                email="other-owner@example.test",
                now=now,
            )
            connection.execute(
                text(
                    "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                    "expires_at, last_seen_at) "
                    "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
                ),
                {
                    "id": uuid4(),
                    "workspace_id": other_workspace_id,
                    "user_id": other_user_id,
                    "token_hash": sha256(other_token.encode()).hexdigest(),
                    "expires_at": now + timedelta(hours=1),
                    "now": now,
                },
            )
        try:
            other_client = TestClient(app)
            other_client.cookies.set("ecc_session", other_token)
            try:
                list_response = other_client.get("/api/v1/engineering/connectors")
                assert list_response.status_code == 200
                ids = [c["id"] for c in list_response.json()["connectors"]]
                assert body["id"] not in ids
            finally:
                other_client.close()
        finally:
            _cleanup_workspace(other_workspace_id, other_account_id)
    finally:
        get_settings.cache_clear()


def test_sync_endpoint_reused_as_is_for_a_gmail_account(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`gmail_oauth.py`'s own module docstring claims "every other
    `connector_accounts` operation (list, sync, disable) reuses ...
    existing generic endpoints as-is" -- list is proven by
    `test_oauth_callback_creates_connector_account_and_is_workspace_
    isolated` above, but round 22 review found no test anywhere ever
    exercised the real `POST /api/v1/engineering/connectors/{id}/sync`
    route against a `gmail`-provider account, despite that route being
    fully generic and already dispatching to `GmailAdapter.backfill`/
    `incremental_sync` (both real, stubbed-but-real methods, not mocked
    away) for any registered provider today. `resource_type="repository"`
    below is semantically meaningless for Gmail (Task 2 has not yet added
    a Gmail-appropriate value to `connector_accounts.ResourceType`/
    `ck_sync_cursors_resource_type`) but is the only way to exercise this
    route at all right now, since the stub ignores `resource_type`
    entirely -- proving the route itself, not yet a real Gmail sync.
    """
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        callback_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert callback_response.status_code == 200
        account_id = callback_response.json()["id"]

        backfill = client.post(
            f"/api/v1/engineering/connectors/{account_id}/sync",
            json={"run_type": "backfill", "resource_type": "repository"},
            headers=_headers(token, key=str(uuid4())),
        )
        assert backfill.status_code == 201, backfill.text
        assert backfill.json()["status"] == "succeeded"
        assert backfill.json()["items_processed"] == 0

        incremental = client.post(
            f"/api/v1/engineering/connectors/{account_id}/sync",
            json={"run_type": "incremental", "resource_type": "repository"},
            headers=_headers(token, key=str(uuid4())),
        )
        assert incremental.status_code == 201, incremental.text
        assert incremental.json()["status"] == "succeeded"
        assert incremental.json()["items_processed"] == 0
    finally:
        get_settings.cache_clear()


def test_oauth_callback_revokes_the_discarded_grant_when_racing_an_active_row(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two distinct, successfully-exchanged consent completions racing each
    other while the row is still `active` (the module docstring's own
    scenario -- a literal same-code replay can never reach this branch,
    since Google's own authorization codes are single-use) means the
    *losing* request holds a real, freshly obtained, never-to-be-persisted
    Google grant with its own distinct token pair. Round 4 review found
    the router's `IntegrityError` handler silently discarded that grant
    without revoking it at Google -- the same "obtained but never revoked"
    bug class rounds 2-3 closed inside `handle_oauth_callback` itself,
    relocated to this router branch. This asserts the *new* grant's own
    refresh token is revoked, and that the originally stored credential is
    left untouched.
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        first_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert first_response.status_code == 200
        account_id = first_response.json()["id"]

        # The "losing" request: a distinct consent completion for the same
        # Google account, with its own distinct token pair, while the row
        # above is still `active`.
        revoked_tokens: list[str] = []
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            GmailAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-3", refresh_token="refresh-3"),
                    revoked_tokens=revoked_tokens,
                )
            ),
        )
        second_start = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        second_state = httpx.URL(second_start.json()["authorization_url"]).params["state"]
        second_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code-3", "state": second_state},
        )
        assert second_response.status_code == 200
        assert second_response.json()["id"] == account_id

        assert revoked_tokens == ["refresh-3"]

        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT encrypted_credentials FROM connector_accounts WHERE id = :id"),
                {"id": account_id},
            ).one()
        stored = loads(decrypt_credential(row[0]))
        assert stored["access_token"] == "access-1"
        assert stored["refresh_token"] == "refresh-1"
    finally:
        get_settings.cache_clear()


def test_oauth_callback_releases_pool_connection_and_row_lock_before_revoking(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 23 review: every `_adapter.disconnect(...)` call inside
    `gmail_oauth_callback_endpoint`'s `IntegrityError` handler used to run
    while `create_session`'s transaction was still open -- for the
    `active`-row branch below, specifically while still holding the
    re-`SELECT ... FOR UPDATE` lock taken on the row. `GmailAdapter.
    disconnect()` is the first `disconnect()` in this registry to make a
    real, blocking outbound HTTPS call, so this held both a pooled
    connection and a row lock for up to `timeout_seconds=10.0` on the
    exact "two browser tabs" race `test_oauth_callback_revokes_the_
    discarded_grant_when_racing_an_active_row` above already proves
    correct -- just never checked for *how long* it held the lock while
    doing so. Reproduces that same scenario, but with the losing
    consent's `/revoke` call blocked on an `Event`: while blocked, a
    concurrent raw `SELECT ... FOR UPDATE NOWAIT` on the same row must
    succeed immediately (proving `create_session` already closed) rather
    than raise `LockNotAvailable`.
    """
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        first_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert first_response.status_code == 200
        account_id = first_response.json()["id"]

        entered_disconnect = threading.Event()
        unblock = threading.Event()

        class _BlockingAdapter(GmailAdapter):
            def disconnect(self, account: ConnectorAccountContext) -> None:
                entered_disconnect.set()
                unblock.wait(timeout=5)
                super().disconnect(account)

        revoked_tokens: list[str] = []
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            _BlockingAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-4", refresh_token="refresh-4"),
                    revoked_tokens=revoked_tokens,
                )
            ),
        )
        second_start = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        second_state = httpx.URL(second_start.json()["authorization_url"]).params["state"]

        def _second_callback() -> Any:
            return client.get(
                "/api/v1/personal/gmail/oauth/callback",
                params={"code": "auth-code-4", "state": second_state},
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_second_callback)
            assert entered_disconnect.wait(timeout=5), "disconnect() never entered"

            with engine.connect() as probe:
                probe.execution_options(isolation_level="AUTOCOMMIT")
                probe.execute(text("SET statement_timeout = '2s'"))
                row = (
                    probe.execute(
                        text("SELECT id FROM connector_accounts WHERE id = :id FOR UPDATE NOWAIT"),
                        {"id": UUID(account_id)},
                    )
                    .mappings()
                    .one()
                )
                assert str(row["id"]) == account_id
                probe.rollback()
                # `SET` (not `SET LOCAL`) under AUTOCOMMIT is session-scoped,
                # and this physical connection returns to the pool when this
                # `with` block exits -- `probe.rollback()` above only ends
                # the SELECT's own micro-transaction, never a plain
                # session-level `SET`. Explicitly restore the connection's
                # established baseline before checkin so a later test
                # drawing this same pooled connection doesn't inherit the 2s
                # override. (`RESET statement_timeout` does NOT work here:
                # it reverts to Postgres's boot-time/config default --
                # verified empirically to be `0` in this environment -- not
                # to the value `_set_statement_timeout` committed when the
                # connection was first created.)
                probe.execute(text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))

            unblock.set()
            second_response = future.result(timeout=5)

        assert second_response.status_code == 200
        assert second_response.json()["id"] == account_id
        assert revoked_tokens == ["refresh-4"]
    finally:
        get_settings.cache_clear()


def test_oauth_callback_reactivation_releases_pool_connection_and_row_lock_before_revoking(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 24 review: `test_oauth_callback_releases_pool_connection_and_
    row_lock_before_revoking` above only proves the lock/connection are
    released before the `active`-row branch's deferred revoke of the *new*
    grant. The reactivation branch (a `disconnected` row reconnecting) is
    a structurally distinct branch that queues a *different* credential --
    the row's own *old*, pre-update one -- into the same `pending_revokes`
    list, and this codebase's own precedent (rounds 13/16) is that proving
    a shared mechanism correct for one call site does not prove it is
    correctly wired at a sibling site. Confirmed by mutation: temporarily
    reverting just the reactivation branch's `pending_revokes.append(...)`
    to an inline `_adapter.disconnect(...)` call (reintroducing the exact
    round-23 bug for this one branch only) left the entire existing test
    file passing -- this test is what closes that gap. Same repro shape as
    the `active`-row sibling: the losing consent's `/revoke` call is
    blocked on an `Event`, and a concurrent raw `SELECT ... FOR UPDATE
    NOWAIT` on the same row must succeed immediately while blocked.
    """
    client, _workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        first_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert first_response.status_code == 200
        account_id = first_response.json()["id"]

        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE connector_accounts SET status = 'disconnected', "
                    "disconnected_at = :now, updated_at = :now WHERE id = :id"
                ),
                {"id": account_id, "now": now},
            )

        entered_disconnect = threading.Event()
        unblock = threading.Event()

        class _BlockingAdapter(GmailAdapter):
            def disconnect(self, account: ConnectorAccountContext) -> None:
                entered_disconnect.set()
                unblock.wait(timeout=5)
                super().disconnect(account)

        revoked_tokens: list[str] = []
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            _BlockingAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-5", refresh_token="refresh-5"),
                    revoked_tokens=revoked_tokens,
                )
            ),
        )
        second_start = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        second_state = httpx.URL(second_start.json()["authorization_url"]).params["state"]

        def _second_callback() -> Any:
            return client.get(
                "/api/v1/personal/gmail/oauth/callback",
                params={"code": "auth-code-5", "state": second_state},
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_second_callback)
            assert entered_disconnect.wait(timeout=5), "disconnect() never entered"

            with engine.connect() as probe:
                probe.execution_options(isolation_level="AUTOCOMMIT")
                probe.execute(text("SET statement_timeout = '2s'"))
                row = (
                    probe.execute(
                        text("SELECT id FROM connector_accounts WHERE id = :id FOR UPDATE NOWAIT"),
                        {"id": UUID(account_id)},
                    )
                    .mappings()
                    .one()
                )
                assert str(row["id"]) == account_id
                probe.rollback()
                # `SET` (not `SET LOCAL`) under AUTOCOMMIT is session-scoped,
                # and this physical connection returns to the pool when this
                # `with` block exits -- `probe.rollback()` above only ends
                # the SELECT's own micro-transaction, never a plain
                # session-level `SET`. Explicitly restore the connection's
                # established baseline before checkin so a later test
                # drawing this same pooled connection doesn't inherit the 2s
                # override. (`RESET statement_timeout` does NOT work here:
                # it reverts to Postgres's boot-time/config default --
                # verified empirically to be `0` in this environment -- not
                # to the value `_set_statement_timeout` committed when the
                # connection was first created.)
                probe.execute(text(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}"))

            unblock.set()
            second_response = future.result(timeout=5)

        assert second_response.status_code == 200
        body = second_response.json()
        assert body["id"] == account_id
        assert body["status"] == "active"
        # The *old* (pre-reactivation) credential is what gets revoked here,
        # not the newly exchanged one -- the reactivation branch's whole
        # point is to persist the new grant, not discard it.
        assert revoked_tokens == ["refresh-1"]
    finally:
        get_settings.cache_clear()


def test_oauth_callback_reactivates_disconnected_account(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `disconnected` row (e.g. via `POST /engineering/connectors/{id}/
    disable`, which for a real `gmail` account also revokes the old
    refresh token at Google) must not permanently strand the account the
    next time its owner completes a real Google consent flow -- silently
    returning the stale, disconnected row (as a naive "already connected"
    IntegrityError handler would) discards a freshly obtained, valid
    credential with no way to recover it. See `gmail_oauth_callback_
    endpoint`'s own IntegrityError-handling docstring.
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        first_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert first_response.status_code == 200
        account_id = first_response.json()["id"]

        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE connector_accounts SET status = 'disconnected', "
                    "disconnected_at = :now, updated_at = :now WHERE id = :id"
                ),
                {"id": account_id, "now": now},
            )

        # A second, real consent flow -- Google returns a genuinely new
        # token pair this time.
        revoked_tokens: list[str] = []
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            GmailAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-2", refresh_token="refresh-2"),
                    revoked_tokens=revoked_tokens,
                )
            ),
        )
        second_start = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        second_state = httpx.URL(second_start.json()["authorization_url"]).params["state"]
        second_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code-2", "state": second_state},
        )
        assert second_response.status_code == 200
        body = second_response.json()
        assert body["id"] == account_id
        assert body["status"] == "active"

        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT encrypted_credentials, status, disconnected_at "
                    "FROM connector_accounts WHERE id = :id"
                ),
                {"id": account_id},
            ).one()
        assert row[1] == "active"
        assert row[2] is None
        stored = loads(decrypt_credential(row[0]))
        assert stored["access_token"] == "access-2"
        assert stored["refresh_token"] == "refresh-2"

        # The replaced `refresh-1` credential is a real, still-possibly-live
        # Google grant this reactivation is about to overwrite -- round 4
        # review found the router discarded it here without revoking,
        # exactly the same "obtained but never revoked" bug class rounds
        # 2-3 closed inside `handle_oauth_callback` itself.
        assert revoked_tokens == ["refresh-1"]

        with engine.begin() as connection:
            reconnected_count = connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE workspace_id = :workspace_id "
                    "AND aggregate_id = :aggregate_id "
                    "AND event_type = 'connector_account.reconnected'"
                ),
                {"workspace_id": workspace_id, "aggregate_id": UUID(account_id)},
            ).scalar_one()
        assert reconnected_count == 1
    finally:
        get_settings.cache_clear()


def test_oauth_callback_reactivates_even_when_prior_credential_cannot_be_decrypted(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 review found this exact scenario -- `disable_connector_
    endpoint`'s own identical case (`test_disable_succeeds_when_credential_
    cannot_be_decrypted`, `tests/test_engineering_connectors_postgres.py`)
    already has dedicated coverage; the reactivate branch's `try:
    decrypt_credential(...) except Exception: old_credential = None`
    fallback (added alongside the revoke-on-discard fix) had none. A
    malformed/undecryptable stored credential (e.g. after a key rotation
    without re-encryption) must never block reactivation -- the best-effort
    revoke of the old, unreadable credential is simply skipped, and the
    fresh, valid, allowlist-checked new credential is still persisted.
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        first_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert first_response.status_code == 200
        account_id = first_response.json()["id"]

        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE connector_accounts SET status = 'disconnected', "
                    "disconnected_at = :now, updated_at = :now, "
                    "encrypted_credentials = :bad_credentials WHERE id = :id"
                ),
                {"id": account_id, "now": now, "bad_credentials": b"not-a-valid-fernet-token"},
            )

        revoked_tokens: list[str] = []
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            GmailAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-2", refresh_token="refresh-2"),
                    revoked_tokens=revoked_tokens,
                )
            ),
        )
        second_start = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        second_state = httpx.URL(second_start.json()["authorization_url"]).params["state"]
        second_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code-2", "state": second_state},
        )
        assert second_response.status_code == 200
        body = second_response.json()
        assert body["id"] == account_id
        assert body["status"] == "active"

        # The undecryptable old credential was never a real token to
        # revoke -- best-effort skips it silently, no `/revoke` call.
        assert revoked_tokens == []

        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT encrypted_credentials FROM connector_accounts WHERE id = :id"),
                {"id": account_id},
            ).one()
        stored = loads(decrypt_credential(row[0]))
        assert stored["refresh_token"] == "refresh-2"
    finally:
        get_settings.cache_clear()


def _fail_next_matching_execute(monkeypatch: pytest.MonkeyPatch, sql_fragment: str) -> None:
    """Round 12 review found rounds 10-11's revoke-on-unexpected-failure
    fixes were disclosed as "not practically testable through pytest's
    synchronous `TestClient`" -- overstated: `monkeypatch.setattr` on a
    SQLAlchemy internal is the exact same idiom this file already uses
    (repeatedly) for swapping `gmail_oauth_module._adapter`, just applied
    one layer lower. Patches `Session.execute` so the *first* call whose
    SQL text contains `sql_fragment` raises `OperationalError`, then
    restores real execution for everything else -- `monkeypatch`'s own
    teardown reverts this automatically at the end of the test.
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session as OrmSession

    original_execute = OrmSession.execute
    state = {"raised": False}

    def flaky_execute(
        self: OrmSession, statement: object, *args: object, **kwargs: object
    ) -> object:
        if not state["raised"] and sql_fragment in str(statement):
            state["raised"] = True
            raise OperationalError("statement", {}, Exception("simulated connection drop"))
        return original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(OrmSession, "execute", flaky_execute)


def test_oauth_callback_revokes_new_grant_when_insert_raises_unexpected_error(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 10's fix: a non-`IntegrityError` failure during the
    `connector_accounts` `INSERT` (a dropped connection, a deadlock, a
    `statement_timeout`) must still revoke the just-obtained Google grant
    rather than orphaning it. Simulates that failure directly via
    `_fail_next_matching_execute`.
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    monkeypatch.setattr(
        gmail_oauth_module,
        "_adapter",
        GmailAdapter(transport=_oauth_transport(revoked_tokens=revoked_tokens)),
    )
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]

        _fail_next_matching_execute(monkeypatch, "INSERT INTO connector_accounts")
        with pytest.raises(Exception):  # noqa: B017, PT011 -- the real, unhandled 500
            client.get(
                "/api/v1/personal/gmail/oauth/callback",
                params={"code": "auth-code", "state": state},
            )

        assert revoked_tokens == ["refresh-1"]
        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM connector_accounts WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            ).scalar_one()
        assert count == 0
    finally:
        get_settings.cache_clear()


def test_oauth_callback_revokes_new_grant_when_success_path_tail_raises(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 12's fourth guard: the plain first-time-connect success
    path's own tail (`get_connector_account` + `_write_side_effects`,
    run only when the `INSERT` succeeds with no `IntegrityError` at all)
    is a sibling of the whole `try`/`except` construct, guarded by
    neither -- unlike the reactivation branch's structurally identical
    "persist a row, build a response, write an audit event" shape, which
    round 11 already gave its own guard. Round 13 review found this
    fourth guard was the one round-12 fix left without a dedicated test,
    despite the infrastructure (`_fail_next_matching_execute`) already
    existing and having just been used three times for the same pattern.
    Fails `_write_side_effects`'s own `INSERT INTO audit_events` --
    unique to this path among the four guards tested here, since only
    the reactivation and plain-success paths ever call it, and each test
    exercises only one of the two.
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    revoked_tokens: list[str] = []
    monkeypatch.setattr(
        gmail_oauth_module,
        "_adapter",
        GmailAdapter(transport=_oauth_transport(revoked_tokens=revoked_tokens)),
    )
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]

        _fail_next_matching_execute(monkeypatch, "INSERT INTO audit_events")
        with pytest.raises(Exception):  # noqa: B017, PT011 -- the real, unhandled 500
            client.get(
                "/api/v1/personal/gmail/oauth/callback",
                params={"code": "auth-code", "state": state},
            )

        assert revoked_tokens == ["refresh-1"]
        with engine.begin() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM connector_accounts WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            ).scalar_one()
        assert count == 0
    finally:
        get_settings.cache_clear()


def test_oauth_callback_revokes_new_grant_when_integrity_error_handler_reselect_raises(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 12's fix: the `IntegrityError` handler's own re-`SELECT ...
    FOR UPDATE` -- the very first statement inside it, before any of its
    three branches runs -- had no revoke-on-failure guard even after
    rounds 10-11's fixes (both scoped to statements *after* this one).
    Simulates that failure directly. A genuinely distinct second consent
    races an already-`active` row, so the `INSERT` really does conflict
    and the `IntegrityError` handler really does run.
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        first_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert first_response.status_code == 200

        revoked_tokens: list[str] = []
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            GmailAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-3", refresh_token="refresh-3"),
                    revoked_tokens=revoked_tokens,
                )
            ),
        )
        second_start = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        second_state = httpx.URL(second_start.json()["authorization_url"]).params["state"]

        _fail_next_matching_execute(
            monkeypatch, "SELECT id, status, encrypted_credentials FROM connector_accounts"
        )
        with pytest.raises(Exception):  # noqa: B017, PT011 -- the real, unhandled 500
            client.get(
                "/api/v1/personal/gmail/oauth/callback",
                params={"code": "auth-code-3", "state": second_state},
            )

        assert revoked_tokens == ["refresh-3"]
    finally:
        get_settings.cache_clear()


def test_oauth_callback_revokes_new_grant_when_reactivation_update_raises(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 11's fix: the reactivation branch's own `UPDATE`/re-`SELECT`/
    audit-write sequence -- which persists the *new* credential -- had no
    revoke-on-failure guard of its own, since a failure raised from inside
    `except IntegrityError:` is never caught by round 10's `except
    Exception:` sibling. Simulates that failure directly.

    Round 26 review: the *old* credential is deliberately NOT expected in
    `revoked_tokens` here anymore -- round 11/12's original version of this
    test asserted `["refresh-1", "refresh-2"]`, revoking the old credential
    unconditionally even though the `UPDATE` that was supposed to replace
    it never committed (the whole transaction rolled back, so the row's
    stored credential is still the old one). That was harmless by
    coincidence in this test's own setup (a `disconnected` row, whose
    credential this codebase already treats as presumed-dead), but the
    same code path also runs for `error`/`rate_limited`/`permission_lost`
    rows, where the stored credential can still be genuinely live --
    dynamically confirmed to revoke a still-good credential at Google
    while leaving the DB row completely unchanged (still `error`, still
    pointing at the now-dead credential, no local signal anything
    happened). Fixed by only revoking the reactivation branch's old
    credential if the transaction actually commits (`pending_revokes_on_
    commit`, gated on `committed` in `gmail_oauth.py`'s own `finally`
    block) -- the new grant is still always revoked, since it was never
    going to be persisted either way once this branch is reached at all.
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        first_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert first_response.status_code == 200
        account_id = first_response.json()["id"]

        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE connector_accounts SET status = 'disconnected', "
                    "disconnected_at = :now, updated_at = :now WHERE id = :id"
                ),
                {"id": account_id, "now": now},
            )

        revoked_tokens: list[str] = []
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            GmailAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-2", refresh_token="refresh-2"),
                    revoked_tokens=revoked_tokens,
                )
            ),
        )
        second_start = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        second_state = httpx.URL(second_start.json()["authorization_url"]).params["state"]

        _fail_next_matching_execute(monkeypatch, "UPDATE connector_accounts SET")
        with pytest.raises(Exception):  # noqa: B017, PT011 -- the real, unhandled 500
            client.get(
                "/api/v1/personal/gmail/oauth/callback",
                params={"code": "auth-code-2", "state": second_state},
            )

        # `refresh-2`: the new grant, orphaned by the failed UPDATE (round
        # 11 fix) -- always revoked, since it was never persisted either
        # way. `refresh-1` (the row's own prior credential) is deliberately
        # NOT revoked here (round 26 fix): the `UPDATE` meant to replace it
        # never committed, so revoking it would kill a credential the row
        # still actually depends on -- see this test's own docstring.
        assert revoked_tokens == ["refresh-2"]

        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT status, encrypted_credentials FROM connector_accounts WHERE id = :id"),
                {"id": account_id},
            ).one()
        assert row[0] == "disconnected"
        # The row's stored credential is untouched by the failed
        # reactivation attempt -- still the original `refresh-1`.
        assert "refresh-1" in decrypt_credential(row[1])
    finally:
        get_settings.cache_clear()


def test_oauth_callback_reactivation_failure_preserves_still_live_credential_of_error_row(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 26 review: the sibling scenario the test above's original
    (`disconnected`-only) setup could not distinguish from a correctness
    bug. The reactivation branch also runs for `error`/`rate_limited`/
    `permission_lost` rows -- unlike `disconnected`, nothing has
    necessarily already revoked those rows' stored credential at Google
    (an `error` status here means an unrelated sync failure, e.g. a
    transient adapter exception -- not necessarily a bad credential).
    Dynamically confirmed pre-fix: reactivating such a row, then failing a
    later statement in the same branch (here, the branch's own re-`SELECT`
    that builds the response after a successful `UPDATE`) so the whole
    transaction rolls back, revoked the row's still-genuinely-live old
    credential at Google while leaving the DB completely unchanged (still
    `error`, still pointing at what is now a dead credential) -- silently
    turning a recoverable, working connection into a permanently broken
    one over an unrelated DB hiccup, with no local signal anything was
    wrong. Fixed by `pending_revokes_on_commit` (see `gmail_oauth.py`).
    """
    client, workspace_id, _user_id, token = gmail_test_context
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_REDIRECT_URI", "https://ecc.example.test/callback")
    get_settings.cache_clear()
    monkeypatch.setattr(gmail_oauth_module, "_adapter", GmailAdapter(transport=_oauth_transport()))
    try:
        start_response = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        state = httpx.URL(start_response.json()["authorization_url"]).params["state"]
        first_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert first_response.status_code == 200
        account_id = first_response.json()["id"]

        # A non-disconnected failure status -- e.g. an unrelated transient
        # sync failure via sync_connector_endpoint's own failure branch.
        # refresh-1 has never been revoked; it is still fully live.
        now = datetime.now(UTC)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE connector_accounts SET status = 'error', "
                    "last_error = 'transient sync failure', updated_at = :now WHERE id = :id"
                ),
                {"id": account_id, "now": now},
            )

        revoked_tokens: list[str] = []
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            GmailAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-2", refresh_token="refresh-2"),
                    revoked_tokens=revoked_tokens,
                )
            ),
        )
        second_start = client.post("/api/v1/personal/gmail/oauth/start", headers=_headers(token))
        second_state = httpx.URL(second_start.json()["authorization_url"]).params["state"]

        # Fail a statement *after* the reactivation UPDATE has already run
        # inside the same transaction (the re-SELECT that builds the
        # response), not the UPDATE itself -- proving this isn't merely
        # the same case the sibling test above already covers.
        _fail_next_matching_execute(
            monkeypatch, "granted_scopes, status, status_detail, last_synced_at, last_error,"
        )
        with pytest.raises(Exception):  # noqa: B017, PT011 -- the real, unhandled 500
            client.get(
                "/api/v1/personal/gmail/oauth/callback",
                params={"code": "auth-code-2", "state": second_state},
            )

        # Only the new, never-persisted grant is revoked -- the row's own
        # still-live credential is preserved.
        assert revoked_tokens == ["refresh-2"]

        with engine.begin() as connection:
            row = connection.execute(
                text("SELECT status, encrypted_credentials FROM connector_accounts WHERE id = :id"),
                {"id": account_id},
            ).one()
        assert row[0] == "error"
        assert "refresh-1" in decrypt_credential(row[1])
    finally:
        get_settings.cache_clear()
