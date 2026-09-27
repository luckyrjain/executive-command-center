"""Spec A S1.7 -- reactivating a `disconnected` engineering connector through
`POST /api/v1/engineering/connectors` goes through `authz.authorize` on that
existing row, exactly like `/sync` and `/disable`:

- caller cannot read the row (`private` / `shared_explicitly` without a
  grant) -> `404 CONNECTOR_NOT_FOUND`, row unchanged, nothing about the row
  in the body, a `denied` `connector_account.enrollment_refused` audit
  (reason `not_found`) + `ecc_connector_enrollment_refused_total`;
- caller can read but not write (read-only grant) -> `403
  INSUFFICIENT_ROLE`, same guarantees, reason `access_denied`;
- `workspace`-visible row -> any member with write may reactivate it
  (decision C15-a, today's behavior); the row's owner may always
  reactivate; a read+write grant allows it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.crypto import decrypt_credential
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_CREATE_URL = "/api/v1/engineering/connectors"
_REFUSED_EVENT = "connector_account.enrollment_refused"


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = hmac_new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@dataclass(frozen=True)
class _Actor:
    user_id: UUID
    account_id: UUID
    token: str
    client: TestClient


def _make_actor(connection: Connection, *, workspace_id: UUID, role: str) -> _Actor:
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    account_id = create_identity(
        connection,
        workspace_id=workspace_id,
        user_id=user_id,
        email=f"{user_id}@example.test",
        role=role,
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
    return _Actor(user_id=user_id, account_id=account_id, token=token, client=client)


@dataclass(frozen=True)
class _Ctx:
    workspace_id: UUID
    row_owner: _Actor
    other: _Actor


@pytest.fixture
def ctx() -> Iterator[_Ctx]:
    """One workspace, two `member`-role actors: `row_owner` creates (and owns)
    the connector, `other` is the second member attempting reactivation."""
    workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Reactivation Authz Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": datetime.now(UTC)},
        )
        row_owner = _make_actor(connection, workspace_id=workspace_id, role="member")
        other = _make_actor(connection, workspace_id=workspace_id, role="member")
    context = _Ctx(workspace_id=workspace_id, row_owner=row_owner, other=other)
    try:
        yield context
    finally:
        row_owner.client.close()
        other.client.close()
        with engine.begin() as connection:
            for table in (
                "resource_grants",
                "sync_runs",
                "sync_cursors",
                "connector_accounts",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "sessions",
                "workspace_memberships",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": workspace_id},
                )
            connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})


def _connect(actor: _Actor, credential: str) -> Any:
    return actor.client.post(
        _CREATE_URL,
        json={"provider": "sandbox", "credential": credential},
        headers=_headers(actor.token, key=str(uuid4())),
    )


def _disconnected_row(ctx: _Ctx, credential: str, *, visibility: str) -> UUID:
    """`row_owner` connects then disables a sandbox connector, then its
    visibility is set directly (no API narrows a connector's visibility)."""
    created = _connect(ctx.row_owner, credential)
    assert created.status_code == 201, created.text
    account_id = UUID(created.json()["id"])
    disabled = ctx.row_owner.client.post(
        f"{_CREATE_URL}/{account_id}/disable",
        headers=_headers(ctx.row_owner.token, key=str(uuid4())),
    )
    assert disabled.status_code == 200, disabled.text
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE connector_accounts SET visibility = :visibility WHERE id = :id"),
            {"visibility": visibility, "id": account_id},
        )
    return account_id


def _grant(ctx: _Ctx, account_id: UUID, grantee: _Actor, actions: list[str]) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO resource_grants (
                    id, workspace_id, grantee_account_id, resource_type, resource_id,
                    actions, granted_by, expires_at, created_at
                ) VALUES (
                    :id, :ws, :grantee, 'connector_accounts', :resource_id,
                    :actions, :granted_by, NULL, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "ws": ctx.workspace_id,
                "grantee": grantee.account_id,
                "resource_id": account_id,
                "actions": actions,
                "granted_by": ctx.row_owner.user_id,
                "now": datetime.now(UTC),
            },
        )


def _row_snapshot(account_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT status, version, updated_by, updated_at, disconnected_at, "
                    "display_name, encrypted_credentials, owner_id, visibility "
                    "FROM connector_accounts WHERE id = :id"
                ),
                {"id": account_id},
            )
            .mappings()
            .one()
        )


def _audit_rows(workspace_id: UUID, event_type: str) -> list[Any]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT aggregate_type, aggregate_id, authorization_result, "
                    "failure_code, metadata, actor_id FROM audit_events "
                    "WHERE workspace_id = :ws AND event_type = :event_type"
                ),
                {"ws": workspace_id, "event_type": event_type},
            )
        )


def _refused_count(reason: str) -> float:
    return observability.connector_enrollment_refused_total._values.get(("sandbox", reason), 0.0)


def _assert_refused(
    ctx: _Ctx,
    account_id: UUID,
    response: Any,
    before: dict[str, Any],
    *,
    status_code: int,
    code: str,
    reason: str,
) -> None:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["error"]["code"] == code
    # Nothing about the existing row leaks into the refusal body.
    assert str(account_id) not in response.text
    assert before["display_name"] not in response.text
    assert "disconnected" not in response.text
    # Row untouched: still disconnected, same version/actor/credential.
    assert _row_snapshot(account_id) == before
    # Refusal audited (denied, own txn), keyed on the row, no row data.
    rows = _audit_rows(ctx.workspace_id, _REFUSED_EVENT)
    assert len(rows) == 1
    aggregate_type, aggregate_id, authorization_result, failure_code, metadata, actor_id = rows[0]
    assert aggregate_type == "connector_account"
    assert aggregate_id == account_id
    assert authorization_result == "denied"
    assert failure_code == reason
    assert actor_id == ctx.other.user_id
    assert metadata == {"reason": reason, "provider": "sandbox"}
    assert "@" not in json.dumps(metadata)
    # No reconnect was recorded.
    reconnected = _audit_rows(ctx.workspace_id, "connector_account.reconnected")
    assert reconnected == []


@pytest.mark.parametrize("visibility", ["shared_explicitly", "private"])
def test_reactivation_of_row_caller_cannot_read_is_404_and_audited(
    ctx: _Ctx, visibility: str
) -> None:
    credential = f"token-hidden-{visibility}"
    account_id = _disconnected_row(ctx, credential, visibility=visibility)
    before = _row_snapshot(account_id)
    metric_before = _refused_count("not_found")

    response = _connect(ctx.other, credential)

    _assert_refused(
        ctx,
        account_id,
        response,
        before,
        status_code=404,
        code="CONNECTOR_NOT_FOUND",
        reason="not_found",
    )
    assert _refused_count("not_found") == metric_before + 1


def test_reactivation_with_read_only_grant_is_403_and_audited(ctx: _Ctx) -> None:
    credential = "token-read-only-grant"
    account_id = _disconnected_row(ctx, credential, visibility="shared_explicitly")
    _grant(ctx, account_id, ctx.other, ["read"])
    before = _row_snapshot(account_id)
    metric_before = _refused_count("access_denied")

    response = _connect(ctx.other, credential)

    _assert_refused(
        ctx,
        account_id,
        response,
        before,
        status_code=403,
        code="INSUFFICIENT_ROLE",
        reason="access_denied",
    )
    assert _refused_count("access_denied") == metric_before + 1


def _assert_reactivated_by(account_id: UUID, response: Any, actor: _Actor, credential: str) -> None:
    assert response.status_code == 201, response.text
    assert response.json()["id"] == str(account_id)
    assert response.json()["status"] == "active"
    row = _row_snapshot(account_id)
    assert row["status"] == "active"
    assert row["updated_by"] == actor.user_id
    assert row["version"] == 3  # 1: created, 2: disabled, 3: reactivated
    assert decrypt_credential(bytes(row["encrypted_credentials"])) == credential


def test_reactivation_with_read_write_grant_is_allowed(ctx: _Ctx) -> None:
    credential = "token-read-write-grant"
    account_id = _disconnected_row(ctx, credential, visibility="shared_explicitly")
    _grant(ctx, account_id, ctx.other, ["read", "write"])

    response = _connect(ctx.other, credential)

    _assert_reactivated_by(account_id, response, ctx.other, credential)
    assert _audit_rows(ctx.workspace_id, _REFUSED_EVENT) == []


def test_reactivation_of_workspace_row_by_another_member_is_allowed(ctx: _Ctx) -> None:
    """Decision C15-a: a `workspace`-visible engineering connector stays
    reactivatable by any member whose role has write (today's behavior);
    the reconnect is audited with the reactivating actor."""
    credential = "token-workspace-row"
    account_id = _disconnected_row(ctx, credential, visibility="workspace")

    response = _connect(ctx.other, credential)

    _assert_reactivated_by(account_id, response, ctx.other, credential)
    assert _audit_rows(ctx.workspace_id, _REFUSED_EVENT) == []
    reconnected = _audit_rows(ctx.workspace_id, "connector_account.reconnected")
    assert len(reconnected) == 1
    assert reconnected[0][1] == account_id
    assert reconnected[0][5] == ctx.other.user_id


@pytest.mark.parametrize("role", ["admin", "owner"])
def test_admin_or_workspace_owner_cannot_reactivate_another_members_private_row(
    ctx: _Ctx, role: str
) -> None:
    """Workspace role never overrides `private` visibility: an `admin` or
    workspace `owner` reactivating another member's private row gets the
    same 404 + audit as any other caller who cannot read it."""
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workspace_memberships SET role = :role WHERE users_id = :users_id"),
            {"role": role, "users_id": ctx.other.user_id},
        )
    credential = f"token-private-vs-{role}"
    account_id = _disconnected_row(ctx, credential, visibility="private")
    before = _row_snapshot(account_id)
    metric_before = _refused_count("not_found")

    response = _connect(ctx.other, credential)

    _assert_refused(
        ctx,
        account_id,
        response,
        before,
        status_code=404,
        code="CONNECTOR_NOT_FOUND",
        reason="not_found",
    )
    assert _refused_count("not_found") == metric_before + 1


@pytest.mark.parametrize("visibility", ["private", "shared_explicitly", "workspace"])
def test_row_owner_may_always_reactivate(ctx: _Ctx, visibility: str) -> None:
    credential = f"token-owner-{visibility}"
    account_id = _disconnected_row(ctx, credential, visibility=visibility)

    response = _connect(ctx.row_owner, credential)

    _assert_reactivated_by(account_id, response, ctx.row_owner, credential)
    assert _audit_rows(ctx.workspace_id, _REFUSED_EVENT) == []
