"""Spec A S1.13 refresh canary: `GmailAdapter.ensure_fresh_credential`
classifies a failed token refresh (`invalid_grant` vs `other`) into
`ecc_gmail_refresh_rejected_total{error, since_reconnect}`, with
`since_reconnect` bucketed from the latest `connector_account.reconnected`/
`.created` audit row (`connector_accounts.latest_reconnect_at`) -- never
`connector_accounts.updated_at`, which every refresh bumps. The canary is
observational only: the exception callers see is unchanged, and a canary
failure counts nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ecc import observability
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.engineering import connector_accounts as connector_accounts_module
from ecc.domains.engineering.connector_accounts import latest_reconnect_at
from ecc.domains.engineering.connectors import AdapterAuthorizationError
from ecc.domains.personal.gmail_adapter import GmailAdapter, refresh_since_reconnect_bucket

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_REFRESH_TOKEN = "refresh-token-SECRET-canary"
_ACCESS_TOKEN = "access-token-SECRET-canary"
_EMAIL = "canary-owner@example.test"
_EXPIRED = "2020-01-01T00:00:00+00:00"


def _credential(*, expires_at: str = _EXPIRED) -> str:
    return dumps(
        {"access_token": _ACCESS_TOKEN, "refresh_token": _REFRESH_TOKEN, "expires_at": expires_at}
    )


def _count(error: str, since_reconnect: str) -> float:
    return observability.gmail_refresh_rejected_total._values.get((error, since_reconnect), 0.0)


def _all_counts() -> dict[tuple[str, ...], float]:
    return dict(observability.gmail_refresh_rejected_total._values)


def _adapter(handler: Callable[[httpx.Request], httpx.Response]) -> GmailAdapter:
    return GmailAdapter(transport=httpx.MockTransport(handler))


def _responding(status_code: int, content: bytes, content_type: str) -> GmailAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=status_code, headers={"content-type": content_type}, content=content
        )

    return _adapter(handler)


def _json(status_code: int, body: Any) -> GmailAdapter:
    return _responding(status_code, dumps(body).encode(), "application/json")


# ---------------------------------------------------------------------------
# since_reconnect bucket boundaries
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        (timedelta(seconds=-30), "lt_1h"),  # small clock skew
        (timedelta(0), "lt_1h"),
        (timedelta(minutes=59, seconds=59), "lt_1h"),
        (timedelta(hours=1), "1h_24h"),
        (timedelta(hours=23, minutes=59, seconds=59), "1h_24h"),
        (timedelta(hours=24), "gt_24h"),
        (timedelta(days=400), "gt_24h"),
    ],
)
def test_since_reconnect_bucket_boundaries(age: timedelta, expected: str) -> None:
    assert refresh_since_reconnect_bucket(_NOW - age, _NOW) == expected


def test_since_reconnect_bucket_unknown_without_timestamp() -> None:
    assert refresh_since_reconnect_bucket(None, _NOW) == "unknown"


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------


def _recent() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=5)


@pytest.mark.parametrize(
    ("adapter_factory", "expected_error"),
    [
        (
            lambda: _json(400, {"error": "invalid_grant", "error_description": "Bad"}),
            "invalid_grant",
        ),
        (lambda: _json(401, {"error": "invalid_grant"}), "invalid_grant"),
        (lambda: _json(400, {"error": "invalid_client"}), "other"),
        (lambda: _json(400, ["invalid_grant"]), "other"),
        (lambda: _responding(400, b"<html>invalid_grant</html>", "text/html"), "other"),
        (lambda: _json(500, {}), "other"),
        (lambda: _responding(200, b"not json", "text/plain"), "other"),
        (lambda: _json(200, {"access_token": "x"}), "other"),
    ],
    ids=[
        "400-invalid_grant",
        "401-invalid_grant",
        "400-other-json-error",
        "400-non-object-json",
        "400-non-json",
        "500",
        "200-non-json",
        "200-missing-expires_in",
    ],
)
def test_refresh_failure_is_classified_and_counted(
    adapter_factory: Callable[[], GmailAdapter], expected_error: str
) -> None:
    adapter = adapter_factory()
    before = _count(expected_error, "lt_1h")
    with pytest.raises(AdapterAuthorizationError):
        adapter.ensure_fresh_credential(_credential(), reconnected_at=_recent)
    assert _count(expected_error, "lt_1h") == before + 1


def test_transport_error_counts_as_other() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    before = _count("other", "unknown")
    with pytest.raises(AdapterAuthorizationError, match="Gmail token refresh failed"):
        _adapter(handler).ensure_fresh_credential(_credential())
    assert _count("other", "unknown") == before + 1


def test_deeply_nested_error_body_counts_as_other_and_still_raises() -> None:
    """A pathological 400 body makes `response.json()` raise `RecursionError`
    (not `ValueError`); classification must not let it escape."""
    adapter = _responding(400, b"[" * 200_000, "application/json")
    before = _count("other", "lt_1h")
    with pytest.raises(AdapterAuthorizationError) as excinfo:
        adapter.ensure_fresh_credential(_credential(), reconnected_at=_recent)
    assert str(excinfo.value) == "Gmail refresh token was rejected -- reconnect this Gmail account"
    assert _count("other", "lt_1h") == before + 1


def test_invalid_grant_keeps_the_existing_error_message_and_type() -> None:
    adapter = _json(400, {"error": "invalid_grant"})
    with pytest.raises(AdapterAuthorizationError) as excinfo:
        adapter.ensure_fresh_credential(_credential())
    assert str(excinfo.value) == "Gmail refresh token was rejected -- reconnect this Gmail account"


def test_no_reconnect_lookup_buckets_unknown() -> None:
    adapter = _json(400, {"error": "invalid_grant"})
    before = _count("invalid_grant", "unknown")
    with pytest.raises(AdapterAuthorizationError):
        adapter.ensure_fresh_credential(_credential(), reconnected_at=lambda: None)
    assert _count("invalid_grant", "unknown") == before + 1


def test_old_reconnect_buckets_gt_24h() -> None:
    adapter = _json(400, {"error": "invalid_grant"})
    before = _count("invalid_grant", "gt_24h")
    with pytest.raises(AdapterAuthorizationError):
        adapter.ensure_fresh_credential(
            _credential(), reconnected_at=lambda: datetime.now(UTC) - timedelta(days=3)
        )
    assert _count("invalid_grant", "gt_24h") == before + 1


def test_successful_refresh_counts_nothing_and_never_looks_up() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=dumps({"access_token": "new", "expires_in": 3600}).encode(),
        )

    def lookup() -> datetime | None:
        raise AssertionError("lookup must only run on a failed refresh")

    before = _all_counts()
    _adapter(handler).ensure_fresh_credential(_credential(), reconnected_at=lookup)
    assert _all_counts() == before


def test_fresh_credential_counts_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no network call expected")

    before = _all_counts()
    fresh = _credential(expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    assert _adapter(handler).ensure_fresh_credential(fresh) == fresh
    assert _all_counts() == before


def test_malformed_stored_credential_is_not_a_refresh_rejection() -> None:
    """No token-endpoint call happened -- not the canary's concern."""
    before = _all_counts()
    with pytest.raises(AdapterAuthorizationError):
        _json(400, {"error": "invalid_grant"}).ensure_fresh_credential("not-json")
    assert _all_counts() == before


def test_canary_failure_is_swallowed_and_counts_nothing(caplog: pytest.LogCaptureFixture) -> None:
    def broken_lookup() -> datetime | None:
        raise RuntimeError(f"db down {_EMAIL}")

    before = _all_counts()
    with caplog.at_level(logging.WARNING, logger="ecc.domains.personal.gmail_adapter"):
        with pytest.raises(AdapterAuthorizationError, match="reconnect this Gmail account"):
            _json(400, {"error": "invalid_grant"}).ensure_fresh_credential(
                _credential(), reconnected_at=broken_lookup
            )
    assert _all_counts() == before
    assert "Gmail refresh canary failed to record (RuntimeError)" in caplog.text
    assert _EMAIL not in caplog.text


def test_canary_logs_carry_no_token_email_or_response_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    body = {"error": "invalid_grant", "error_description": f"Token for {_EMAIL} revoked"}
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AdapterAuthorizationError):
            _json(400, body).ensure_fresh_credential(_credential(), reconnected_at=_recent)
    assert "error=invalid_grant since_reconnect=lt_1h" in caplog.text
    for secret in (_REFRESH_TOKEN, _ACCESS_TOKEN, _EMAIL, "error_description", "revoked"):
        assert secret not in caplog.text


def test_counter_is_exported_in_metrics() -> None:
    with pytest.raises(AdapterAuthorizationError):
        _json(400, {"error": "invalid_grant"}).ensure_fresh_credential(
            _credential(), reconnected_at=_recent
        )
    rendered = observability.render_metrics()
    assert "# TYPE ecc_gmail_refresh_rejected_total counter" in rendered
    assert 'ecc_gmail_refresh_rejected_total{error="invalid_grant",since_reconnect="lt_1h"}' in (
        rendered
    )


# ---------------------------------------------------------------------------
# latest_reconnect_at (Postgres)
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Refresh Canary Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"canary-{user_id}@example.test",
            now=now,
        )
    try:
        yield workspace_id, user_id
    finally:
        with engine.begin() as connection:
            account_ids = [
                row[0]
                for row in connection.execute(
                    text("SELECT account_id FROM users WHERE workspace_id = :ws"),
                    {"ws": workspace_id},
                )
            ]
            for table in (
                "audit_events",
                "connector_accounts",
                "workspace_memberships",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": workspace_id},
                )
            connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
            if account_ids:
                connection.execute(
                    text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids}
                )


def _insert_connector(workspace_id: UUID, user_id: UUID, *, updated_at: datetime) -> UUID:
    account_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :ws, 'gmail', :external, 'Canary', ARRAY[]::text[],
                    :credential, 'active', 1, :user_id, :user_id, :created_at, :updated_at
                )
                """
            ),
            {
                "id": account_id,
                "ws": workspace_id,
                "external": f"canary-{account_id}@example.test",
                "credential": b"not-a-real-ciphertext",
                "user_id": user_id,
                "created_at": updated_at - timedelta(days=30),
                "updated_at": updated_at,
            },
        )
    return account_id


def _insert_audit(
    workspace_id: UUID,
    user_id: UUID,
    account_id: UUID,
    event_type: str,
    occurred_at: datetime,
    *,
    aggregate_type: str = "connector_account",
) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id, occurred_at
                ) VALUES (
                    :id, :ws, :event_type, :aggregate_type, :aggregate_id, 1, :actor_id,
                    :request_id, :correlation_id, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "ws": workspace_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": account_id,
                "actor_id": user_id,
                "request_id": uuid4(),
                "correlation_id": uuid4(),
                "occurred_at": occurred_at,
            },
        )


def _lookup(workspace_id: UUID, account_id: UUID) -> datetime | None:
    with SessionFactory() as session, session.begin():
        return latest_reconnect_at(session, workspace_id, account_id)


def test_latest_reconnect_at_newer_reconnected_beats_older_created(
    workspace: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = workspace
    now = datetime.now(UTC)
    account_id = _insert_connector(workspace_id, user_id, updated_at=now)
    created = now - timedelta(days=10)
    reconnected = now - timedelta(minutes=20)
    _insert_audit(workspace_id, user_id, account_id, "connector_account.created", created)
    _insert_audit(workspace_id, user_id, account_id, "connector_account.reconnected", reconnected)
    assert _lookup(workspace_id, account_id) == reconnected
    assert refresh_since_reconnect_bucket(_lookup(workspace_id, account_id), now) == "lt_1h"


def test_latest_reconnect_at_newer_created_wins_too(workspace: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = workspace
    now = datetime.now(UTC)
    account_id = _insert_connector(workspace_id, user_id, updated_at=now)
    reconnected = now - timedelta(days=5)
    created = now - timedelta(hours=3)
    _insert_audit(workspace_id, user_id, account_id, "connector_account.reconnected", reconnected)
    _insert_audit(workspace_id, user_id, account_id, "connector_account.created", created)
    assert _lookup(workspace_id, account_id) == created


def test_latest_reconnect_at_ignores_updated_at_and_unrelated_rows(
    workspace: tuple[UUID, UUID],
) -> None:
    """`updated_at` was bumped a minute ago (as a successful refresh does),
    and newer audit rows exist for other event types, another account, and
    another aggregate type -- none of them move the reconnect time."""
    workspace_id, user_id = workspace
    now = datetime.now(UTC)
    account_id = _insert_connector(workspace_id, user_id, updated_at=now - timedelta(minutes=1))
    other_account = _insert_connector(workspace_id, user_id, updated_at=now)
    created = now - timedelta(days=3)
    _insert_audit(workspace_id, user_id, account_id, "connector_account.created", created)
    _insert_audit(workspace_id, user_id, account_id, "connector_account.disabled", now)
    _insert_audit(workspace_id, user_id, other_account, "connector_account.reconnected", now)
    _insert_audit(
        workspace_id,
        user_id,
        account_id,
        "connector_account.reconnected",
        now,
        aggregate_type="something_else",
    )
    assert _lookup(workspace_id, account_id) == created
    assert refresh_since_reconnect_bucket(_lookup(workspace_id, account_id), now) == "gt_24h"


def test_latest_reconnect_at_none_without_audit_row(workspace: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = workspace
    account_id = _insert_connector(workspace_id, user_id, updated_at=datetime.now(UTC))
    assert _lookup(workspace_id, account_id) is None
    assert refresh_since_reconnect_bucket(_lookup(workspace_id, account_id), _NOW) == "unknown"


def test_latest_reconnect_at_scoped_to_workspace(workspace: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = workspace
    now = datetime.now(UTC)
    account_id = _insert_connector(workspace_id, user_id, updated_at=now)
    _insert_audit(workspace_id, user_id, account_id, "connector_account.created", now)
    assert _lookup(uuid4(), account_id) is None


def test_latest_reconnect_at_failure_leaves_the_outer_transaction_usable(
    workspace: tuple[UUID, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lookup runs inside `_run_connector_sync`'s phase-1 transaction;
    a failing query must roll back only its own savepoint."""
    workspace_id, user_id = workspace
    account_id = _insert_connector(workspace_id, user_id, updated_at=datetime.now(UTC))
    monkeypatch.setattr(connector_accounts_module, "text", lambda _sql: text("SELECT 1/0"))
    with SessionFactory() as session, session.begin():
        with pytest.raises(DBAPIError):
            latest_reconnect_at(session, workspace_id, account_id)
        assert session.execute(text("SELECT 1")).scalar_one() == 1
