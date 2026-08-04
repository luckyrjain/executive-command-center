"""Phase 10 Gmail Connector Task 1 (`backend/ecc/domains/personal/
gmail_adapter.py`, `backend/ecc/domains/personal/gmail_oauth.py`).

Covers, per the implementation plan's own Task 1 test list
(`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`):

1. Allowlist rejects a non-listed account before any Google call
   (`GmailAdapter.is_account_allowed`, and `POST /oauth/start`'s own
   pre-redirect check).
2. Authorization-URL generation (`GmailAdapter.get_authorization_url`).
3. Callback/code-exchange against a mocked token-endpoint response
   (`GmailAdapter.handle_oauth_callback`, and the real `GET /oauth/
   callback` route end to end).
4. Refresh-token renewal (`GmailAdapter.refresh_permissions`).
5. Encrypted-field-never-returned-in-list-view (`ConnectorAccountResponse`
   never carries `encrypted_credentials`, for a `gmail` row exactly like
   any other provider).
6. Workspace isolation (a `gmail` connector account created in one
   workspace is invisible to a session in a different workspace).
"""

from __future__ import annotations

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
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            body = token_body if token_body is not None else _token_response()
            return _json_response(body, status_code=token_status)
        if request.url.path == "/gmail/v1/users/me/profile":
            if profile_status != 200:
                return _json_response({}, status_code=profile_status)
            return _json_response({"emailAddress": profile_email})
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
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter(
            transport=_oauth_transport(token_body=_token_response(refresh_token=None))
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
    finally:
        get_settings.cache_clear()


def test_handle_oauth_callback_rejects_missing_required_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", _ALLOWED_EMAIL)
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter(
            transport=_oauth_transport(
                token_body=_token_response(scope="https://www.googleapis.com/auth/gmail.metadata")
            )
        )
        with pytest.raises(AdapterAuthorizationError):
            adapter.handle_oauth_callback("auth-code", "state-value")
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
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_ID", "cid")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("ECC_GMAIL_OAUTH_ALLOWLIST", "someone-else@example.test")
    get_settings.cache_clear()
    try:
        adapter = GmailAdapter(transport=_oauth_transport(profile_email=_ALLOWED_EMAIL))
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
        create_identity(
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
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
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


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}


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
        assert response.json()["detail"] == "GMAIL_ACCOUNT_NOT_ALLOWLISTED"
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


def test_oauth_callback_rejects_invalid_state(
    gmail_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = gmail_test_context
    response = client.get(
        "/api/v1/personal/gmail/oauth/callback",
        params={"code": "auth-code", "state": "tampered-state"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "GMAIL_OAUTH_STATE_INVALID"


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

        # A reloaded callback (same code/state) must not error -- returns
        # the already-connected account instead of a hard failure.
        replay_response = client.get(
            "/api/v1/personal/gmail/oauth/callback",
            params={"code": "auth-code", "state": state},
        )
        assert replay_response.status_code == 200
        assert replay_response.json()["id"] == body["id"]

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
            create_identity(
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
            _cleanup_workspace(other_workspace_id)
    finally:
        get_settings.cache_clear()
