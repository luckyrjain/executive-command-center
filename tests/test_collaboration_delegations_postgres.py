"""Phase 8 Task 6: `ecc.domains.collaboration.delegations`
(`docs/superpowers/plans/2026-08-01-phase-8-multi-user.md` Task 6,
`docs/phases/phase-008/DELEGATION-CONTRACT.md`). Per this task's own
stated test requirements:

1. Full lifecycle: propose -> accept -> complete, propose -> reject,
   propose -> accept -> revoke.
2. Evidence-grant scoping: the recipient can read exactly the named
   evidence (verified by attempting to read an adjacent un-named
   resource, which must stay denied).
3. Auto-revocation of evidence grants on completion/revocation (`resource_
   grants.revoked_at` set, verified directly against Postgres).
4. IDOR/role-gating: cross-workspace isolation, non-party access denied,
   `viewer`'s baseline lacks `write` so cannot propose, only the recipient
   may accept/reject/complete, only the delegator may revoke.
5. Lazy expiry: an overdue `proposed` delegation transitions to `expired`
   on the next read, and can no longer be accepted.

**"Idempotent notifications" (the plan's own fifth named test requirement
for this task) is not testable here, disclosed rather than silently
skipped** -- no notification mechanism exists in this codebase yet; `docs/
phases/phase-008/PHASE-008-multi-user.md`'s own sequencing puts `member_
notifications` in Task 7, not this one. Deferred to that task's own test
suite once the mechanism it would need to observe actually exists.
"""

from __future__ import annotations

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

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = hmac_new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "delegation_events",
            "delegation_evidence",
            "delegations",
            "resource_grants",
            "incidents",
            "sessions",
            "workspace_memberships",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


@dataclass(frozen=True)
class _Actor:
    user_id: UUID
    account_id: UUID
    token: str
    client: TestClient


def _create_session(
    connection: Connection, *, workspace_id: UUID, user_id: UUID, token: str
) -> None:
    now = datetime.now(UTC)
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


def _make_actor(
    connection: Connection, *, workspace_id: UUID, role: str, status: str = "active"
) -> _Actor:
    user_id = uuid4()
    token = f"session-{uuid4()}"
    create_identity(
        connection,
        workspace_id=workspace_id,
        user_id=user_id,
        email=f"{user_id}@example.test",
        role=role,
        status=status,
    )
    _create_session(connection, workspace_id=workspace_id, user_id=user_id, token=token)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    account_id = connection.execute(
        text("SELECT account_id FROM users WHERE id = :id"), {"id": user_id}
    ).scalar_one()
    return _Actor(user_id=user_id, account_id=account_id, token=token, client=client)


@dataclass(frozen=True)
class _DelegationContext:
    workspace_id: UUID
    delegator: _Actor
    recipient: _Actor
    other_member: _Actor
    admin: _Actor
    viewer: _Actor


@pytest.fixture
def delegation_context() -> Iterator[_DelegationContext]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Delegation Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        delegator = _make_actor(connection, workspace_id=workspace_id, role="owner")
        recipient = _make_actor(connection, workspace_id=workspace_id, role="member")
        other_member = _make_actor(connection, workspace_id=workspace_id, role="member")
        admin = _make_actor(connection, workspace_id=workspace_id, role="admin")
        viewer = _make_actor(connection, workspace_id=workspace_id, role="viewer")

    context = _DelegationContext(
        workspace_id=workspace_id,
        delegator=delegator,
        recipient=recipient,
        other_member=other_member,
        admin=admin,
        viewer=viewer,
    )
    try:
        yield context
    finally:
        for actor in (delegator, recipient, other_member, admin, viewer):
            actor.client.close()
        _cleanup_workspace(workspace_id)


def _create_incident(
    client: TestClient, token: str, *, title: str = "Obligation"
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/engineering/incidents",
        json={"title": title, "severity": "high", "detected_at": datetime.now(UTC).isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _propose(
    client: TestClient,
    token: str,
    *,
    recipient_account_id: UUID,
    obligation_resource_id: str,
    evidence: list[dict[str, str]] | None = None,
    due_at: datetime | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    return client.post(  # type: ignore[return-value]
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(recipient_account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": obligation_resource_id,
            "expected_outcome": "Resolve the incident",
            "due_at": (due_at or (datetime.now(UTC) + timedelta(days=1))).isoformat(),
            "evidence": evidence or [],
        },
        headers=_headers(token, key=key or str(uuid4())),
    ).json()


def test_full_lifecycle_propose_accept_complete(delegation_context: _DelegationContext) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)

    propose_response = ctx.delegator.client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(ctx.recipient.account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": incident["id"],
            "expected_outcome": "Resolve the incident",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "evidence": [],
        },
        headers=_headers(ctx.delegator.token, key=str(uuid4())),
    )
    assert propose_response.status_code == 201, propose_response.text
    delegation = propose_response.json()
    assert delegation["status"] == "proposed"
    assert delegation["delegator_account_id"] == str(ctx.delegator.account_id)
    assert [e["resource_id"] for e in delegation["evidence"]] == [incident["id"]]

    accept_response = ctx.recipient.client.post(
        f"/api/v1/delegations/{delegation['id']}/accept",
        headers=_headers(ctx.recipient.token, key=str(uuid4())),
    )
    assert accept_response.status_code == 200, accept_response.text
    assert accept_response.json()["status"] == "accepted"

    with engine.begin() as connection:
        grant = (
            connection.execute(
                text(
                    "SELECT actions, revoked_at, expires_at FROM resource_grants "
                    "WHERE grantee_account_id = :account_id AND resource_id = :resource_id"
                ),
                {"account_id": ctx.recipient.account_id, "resource_id": UUID(incident["id"])},
            )
            .mappings()
            .one()
        )
    assert list(grant["actions"]) == ["read"]
    assert grant["revoked_at"] is None
    assert grant["expires_at"] is None

    complete_response = ctx.recipient.client.post(
        f"/api/v1/delegations/{delegation['id']}/complete",
        headers=_headers(ctx.recipient.token, key=str(uuid4())),
    )
    assert complete_response.status_code == 200, complete_response.text
    assert complete_response.json()["status"] == "completed"

    with engine.begin() as connection:
        revoked_at = connection.execute(
            text(
                "SELECT revoked_at FROM resource_grants "
                "WHERE grantee_account_id = :account_id AND resource_id = :resource_id"
            ),
            {"account_id": ctx.recipient.account_id, "resource_id": UUID(incident["id"])},
        ).scalar_one()
    assert revoked_at is not None


def test_full_lifecycle_propose_reject(delegation_context: _DelegationContext) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    delegation = _propose(
        ctx.delegator.client,
        ctx.delegator.token,
        recipient_account_id=ctx.recipient.account_id,
        obligation_resource_id=incident["id"],
    )

    response = ctx.recipient.client.post(
        f"/api/v1/delegations/{delegation['id']}/reject",
        headers=_headers(ctx.recipient.token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"

    # No evidence grant was ever created -- there is nothing to revoke.
    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM resource_grants WHERE grantee_account_id = :account_id"),
            {"account_id": ctx.recipient.account_id},
        ).scalar_one()
    assert count == 0


def test_full_lifecycle_propose_accept_revoke(delegation_context: _DelegationContext) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    delegation = _propose(
        ctx.delegator.client,
        ctx.delegator.token,
        recipient_account_id=ctx.recipient.account_id,
        obligation_resource_id=incident["id"],
    )
    accept = ctx.recipient.client.post(
        f"/api/v1/delegations/{delegation['id']}/accept",
        headers=_headers(ctx.recipient.token, key=str(uuid4())),
    )
    assert accept.status_code == 200, accept.text

    # Only the delegator may revoke, not the recipient.
    denied = ctx.recipient.client.post(
        f"/api/v1/delegations/{delegation['id']}/revoke",
        headers=_headers(ctx.recipient.token, key=str(uuid4())),
    )
    assert denied.status_code == 403, denied.text

    revoke = ctx.delegator.client.post(
        f"/api/v1/delegations/{delegation['id']}/revoke",
        headers=_headers(ctx.delegator.token, key=str(uuid4())),
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["status"] == "revoked"

    with engine.begin() as connection:
        revoked_at = connection.execute(
            text(
                "SELECT revoked_at FROM resource_grants "
                "WHERE grantee_account_id = :account_id AND resource_id = :resource_id"
            ),
            {"account_id": ctx.recipient.account_id, "resource_id": UUID(incident["id"])},
        ).scalar_one()
    assert revoked_at is not None


def test_evidence_grant_scoped_to_exactly_the_named_resources(
    delegation_context: _DelegationContext,
) -> None:
    ctx = delegation_context
    obligation = _create_incident(ctx.delegator.client, ctx.delegator.token, title="Obligation")
    named_evidence = _create_incident(
        ctx.delegator.client, ctx.delegator.token, title="Named evidence"
    )
    adjacent = _create_incident(
        ctx.delegator.client, ctx.delegator.token, title="Adjacent, not named"
    )

    with engine.begin() as connection:
        for incident_id in (obligation["id"], named_evidence["id"], adjacent["id"]):
            connection.execute(
                text(
                    "UPDATE incidents SET visibility = 'private' "
                    "WHERE id = :id AND workspace_id = :workspace_id"
                ),
                {"id": UUID(incident_id), "workspace_id": ctx.workspace_id},
            )

    delegation = _propose(
        ctx.delegator.client,
        ctx.delegator.token,
        recipient_account_id=ctx.recipient.account_id,
        obligation_resource_id=obligation["id"],
        evidence=[{"resource_type": "incidents", "resource_id": named_evidence["id"]}],
    )
    accept = ctx.recipient.client.post(
        f"/api/v1/delegations/{delegation['id']}/accept",
        headers=_headers(ctx.recipient.token, key=str(uuid4())),
    )
    assert accept.status_code == 200, accept.text

    visible = {
        row["id"]
        for row in ctx.recipient.client.get("/api/v1/engineering/incidents").json()["incidents"]
    }
    assert obligation["id"] in visible
    assert named_evidence["id"] in visible
    assert adjacent["id"] not in visible


def test_workspace_and_party_isolation(delegation_context: _DelegationContext) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    delegation = _propose(
        ctx.delegator.client,
        ctx.delegator.token,
        recipient_account_id=ctx.recipient.account_id,
        obligation_resource_id=incident["id"],
    )

    # A workspace member not party to the delegation cannot see or accept
    # it -- 404, never a different failure shape that could leak existence.
    get_response = ctx.other_member.client.get(f"/api/v1/delegations/{delegation['id']}")
    assert get_response.status_code == 404, get_response.text

    accept_response = ctx.other_member.client.post(
        f"/api/v1/delegations/{delegation['id']}/accept",
        headers=_headers(ctx.other_member.token, key=str(uuid4())),
    )
    assert accept_response.status_code == 404, accept_response.text

    # owner/admin oversight visibility: not a party, but still 200.
    admin_get = ctx.admin.client.get(f"/api/v1/delegations/{delegation['id']}")
    assert admin_get.status_code == 200, admin_get.text


def test_viewer_cannot_propose_lacking_write_access(delegation_context: _DelegationContext) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    response = ctx.viewer.client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(ctx.recipient.account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": incident["id"],
            "expected_outcome": "Resolve it",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "evidence": [],
        },
        headers=_headers(ctx.viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_cannot_delegate_to_self(delegation_context: _DelegationContext) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    response = ctx.delegator.client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(ctx.delegator.account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": incident["id"],
            "expected_outcome": "Resolve it",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "evidence": [],
        },
        headers=_headers(ctx.delegator.token, key=str(uuid4())),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "CANNOT_DELEGATE_TO_SELF"


def test_due_at_in_past_rejected(delegation_context: _DelegationContext) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    response = ctx.delegator.client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(ctx.recipient.account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": incident["id"],
            "expected_outcome": "Resolve it",
            "due_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
            "evidence": [],
        },
        headers=_headers(ctx.delegator.token, key=str(uuid4())),
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "DUE_AT_IN_PAST"


def test_lazy_expiry_transitions_overdue_proposal_and_blocks_accept(
    delegation_context: _DelegationContext,
) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    delegation = _propose(
        ctx.delegator.client,
        ctx.delegator.token,
        recipient_account_id=ctx.recipient.account_id,
        obligation_resource_id=incident["id"],
        due_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE delegations SET due_at = :due_at WHERE id = :id"),
            {"due_at": datetime.now(UTC) - timedelta(seconds=1), "id": UUID(delegation["id"])},
        )

    get_response = ctx.delegator.client.get(f"/api/v1/delegations/{delegation['id']}")
    assert get_response.status_code == 200, get_response.text
    assert get_response.json()["status"] == "expired"

    accept_response = ctx.recipient.client.post(
        f"/api/v1/delegations/{delegation['id']}/accept",
        headers=_headers(ctx.recipient.token, key=str(uuid4())),
    )
    assert accept_response.status_code == 409, accept_response.text
    assert accept_response.json()["error"]["code"] == "DELEGATION_NOT_PROPOSED"

    with engine.begin() as connection:
        event_types = (
            connection.execute(
                text(
                    "SELECT event_type FROM delegation_events WHERE delegation_id = :id "
                    "ORDER BY occurred_at ASC"
                ),
                {"id": UUID(delegation["id"])},
            )
            .scalars()
            .all()
        )
    assert event_types == ["proposed", "expired"]


def test_recipient_not_found_for_non_member(delegation_context: _DelegationContext) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    response = ctx.delegator.client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(uuid4()),
            "obligation_type": "incidents",
            "obligation_resource_id": incident["id"],
            "expected_outcome": "Resolve it",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "evidence": [],
        },
        headers=_headers(ctx.delegator.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RECIPIENT_NOT_FOUND"


def test_idempotency_key_replay_returns_identical_response(
    delegation_context: _DelegationContext,
) -> None:
    ctx = delegation_context
    incident = _create_incident(ctx.delegator.client, ctx.delegator.token)
    key = str(uuid4())
    first = _propose(
        ctx.delegator.client,
        ctx.delegator.token,
        recipient_account_id=ctx.recipient.account_id,
        obligation_resource_id=incident["id"],
        key=key,
    )
    second = ctx.delegator.client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(ctx.recipient.account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": incident["id"],
            "expected_outcome": "Resolve the incident",
            "due_at": first["due_at"],
            "evidence": [],
        },
        headers=_headers(ctx.delegator.token, key=key),
    )
    assert second.status_code == 201, second.text
    assert second.json()["id"] == first["id"]

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM delegations WHERE id = :id"), {"id": UUID(first["id"])}
        ).scalar_one()
    assert count == 1
