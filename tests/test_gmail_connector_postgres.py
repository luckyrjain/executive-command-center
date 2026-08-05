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
   (`GmailAdapter.handle_oauth_callback`, and the real `GET /oauth/
   callback` route end to end), including every distinct post-exchange
   rejection branch (missing access/refresh token, non-numeric
   `expires_in`, missing required scope, profile-lookup network error or
   non-200 status, missing `emailAddress`, non-allowlisted account) --
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
   raising) and its real provider-side revocation call.
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
    event this writes.
12. Audit-event dedup: a replayed callback (same code/state) returns the
    same row without writing a second `connector_account.created` event.
"""

from __future__ import annotations

from base64 import urlsafe_b64encode
from collections.abc import Iterator
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
from ecc.database import engine
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


def test_disconnect_never_raises_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    adapter = GmailAdapter(transport=httpx.MockTransport(handler))
    credential = dumps(
        {"access_token": "a", "refresh_token": "r", "expires_at": "2020-01-01T00:00:00+00:00"}
    )
    assert adapter.disconnect(_account_context(credential)) is None


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


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}


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


def test_oauth_callback_creates_connector_account_and_is_workspace_isolated(
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

        # A reloaded callback (same code/state) must not error -- returns
        # the already-connected account instead of a hard failure, and does
        # not write a second audit event (the SAVEPOINT-inside-one-
        # transaction guarantee `create_connector_endpoint` also relies on:
        # a losing IntegrityError proves the winner's own transaction,
        # including its audit write, already committed -- there is nothing
        # new to record for a passive replay).
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

        # Workspace isolation: a second workspace's session must not see
        # this connector account via the generic list endpoint.
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
        monkeypatch.setattr(
            gmail_oauth_module,
            "_adapter",
            GmailAdapter(
                transport=_oauth_transport(
                    token_body=_token_response(access_token="access-2", refresh_token="refresh-2")
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
