"""Phase 8 Task 8: `ecc.domains.identity.membership_removal` and
`ecc.platform.authz`'s `POST|GET /ownership/transfers`
(`docs/superpowers/plans/2026-08-01-phase-8-multi-user.md` Task 8). Per
this task's own stated test requirements:

1. Removal blocked while unresolved sole-ownership exists
   (`test_remove_member_blocked_by_owned_resources_then_succeeds_after_
   transfer`).
2. Removal proceeds after transfer/export (same test -- the transfer
   unblocks it, and the response's own `export` field is asserted).
3. Active delegations correctly resolved on removal
   (`test_remove_member_cancels_active_delegations_and_revokes_evidence_
   grants` -- both a still-`proposed` delegation naming the removed member
   as delegator and an `accepted` one naming them as recipient).
4. Historical `owner_id`/`created_by` attribution survives removal
   unchanged (same test -- the removed member's `users` row is asserted to
   still exist after removal, per Decision 1's "never delete the anchor").

Plus the two structural invariants this task's own module docstring
discloses (last-active-owner cannot be demoted/removed), member-listing
visibility, self-removal, session revocation (`PERMISSION-CONTRACT.md`'s
"second, independent propagation path"), and `POST|GET /ownership/
transfers`'s own authorization/validation surface.
"""

from __future__ import annotations

import threading
import time
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
            "member_notifications",
            "ownership_transfers",
            "delegations",
            "resource_grants",
            "incidents",
            "workflow_runs",
            "workflow_versions",
            "workflow_definitions",
            "event_outbox",
            "audit_events",
            "idempotency_records",
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
class _MembershipContext:
    workspace_id: UUID
    owner: _Actor
    admin: _Actor
    member_a: _Actor
    member_b: _Actor
    viewer: _Actor


@pytest.fixture
def membership_context() -> Iterator[_MembershipContext]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Membership Removal Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        owner = _make_actor(connection, workspace_id=workspace_id, role="owner")
        admin = _make_actor(connection, workspace_id=workspace_id, role="admin")
        member_a = _make_actor(connection, workspace_id=workspace_id, role="member")
        member_b = _make_actor(connection, workspace_id=workspace_id, role="member")
        viewer = _make_actor(connection, workspace_id=workspace_id, role="viewer")

    context = _MembershipContext(
        workspace_id=workspace_id,
        owner=owner,
        admin=admin,
        member_a=member_a,
        member_b=member_b,
        viewer=viewer,
    )
    try:
        yield context
    finally:
        for actor in (owner, admin, member_a, member_b, viewer):
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


def _propose_delegation(
    client: TestClient,
    token: str,
    *,
    recipient_account_id: UUID,
    obligation_resource_id: str,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(recipient_account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": obligation_resource_id,
            "expected_outcome": "Resolve the incident",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "evidence": [],
        },
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# GET|PATCH|DELETE /identity/workspaces/{id}/members(/{user_id})
# ---------------------------------------------------------------------------


def test_list_members_visible_to_any_active_role(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.viewer.client.get(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members",
        headers=_headers(ctx.viewer.token),
    )
    assert response.status_code == 200, response.text
    user_ids = {m["user_id"] for m in response.json()["members"]}
    assert user_ids == {
        str(ctx.owner.user_id),
        str(ctx.admin.user_id),
        str(ctx.member_a.user_id),
        str(ctx.member_b.user_id),
        str(ctx.viewer.user_id),
    }


def test_patch_member_role_updates_role(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.owner.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        json={"role": "admin"},
        headers=_headers(ctx.owner.token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin"

    with engine.begin() as connection:
        role = connection.execute(
            text("SELECT role FROM workspace_memberships WHERE users_id = :id"),
            {"id": ctx.member_a.user_id},
        ).scalar_one()
    assert role == "admin"


def test_patch_role_blocked_for_last_active_owner(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.owner.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.owner.user_id}",
        json={"role": "admin"},
        headers=_headers(ctx.owner.token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "LAST_OWNER_CANNOT_BE_DEMOTED"


def test_admin_cannot_promote_a_member_to_owner(membership_context: _MembershipContext) -> None:
    """Found untested by the second whole-phase review, despite the guard
    itself being fixed during the first: an `admin` promoting someone to
    `owner` unilaterally is the exact privilege-escalation gap that round
    closed."""
    ctx = membership_context
    response = ctx.admin.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        json={"role": "owner"},
        headers=_headers(ctx.admin.token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_owner_can_promote_a_member_to_owner(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.owner.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        json={"role": "owner"},
        headers=_headers(ctx.owner.token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "owner"


def test_admin_cannot_demote_an_owner(membership_context: _MembershipContext) -> None:
    """Second whole-phase review: the promotion guard above only stopped an
    `admin` from *promoting* someone to `owner` -- nothing stopped them
    from *demoting* an existing owner instead, the same trust boundary
    applied asymmetrically. This must 403 before ever reaching the
    separate `LAST_OWNER_CANNOT_BE_DEMOTED` 409 check (`ctx.owner` is the
    workspace's only owner here, so that check would otherwise fire
    first -- this proves the role guard is checked first)."""
    ctx = membership_context
    response = ctx.admin.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.owner.user_id}",
        json={"role": "member"},
        headers=_headers(ctx.admin.token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_remove_member_blocked_by_owned_resources_then_succeeds_after_transfer(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    incident = _create_incident(ctx.member_a.client, ctx.member_a.token, title="Owned by A")

    blocked = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert blocked.status_code == 409, blocked.text
    error = blocked.json()["error"]
    assert error["code"] == "OWNED_RESOURCES_BLOCK_REMOVAL"
    assert {"resource_type": "incidents", "count": 1} in error["details"]["owned_resources"]

    transfer = ctx.owner.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "to_account_id": str(ctx.member_b.account_id),
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert transfer.status_code == 201, transfer.text
    assert transfer.json()["from_account_id"] == str(ctx.member_a.account_id)
    assert transfer.json()["to_account_id"] == str(ctx.member_b.account_id)
    assert transfer.json()["status"] == "completed"

    removal = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert removal.status_code == 200, removal.text
    export = removal.json()["export"]
    assert export["account_id"] == str(ctx.member_a.account_id)
    assert export["removed_at"] is not None

    with engine.begin() as connection:
        status_row = (
            connection.execute(
                text("SELECT status, removed_at FROM workspace_memberships WHERE users_id = :id"),
                {"id": ctx.member_a.user_id},
            )
            .mappings()
            .one()
        )
        assert status_row["status"] == "removed"
        assert status_row["removed_at"] is not None

        # Historical attribution survives -- the `users` FK anchor is never
        # deleted (Decision 1), and the transferred incident's new owner_id
        # correctly points at member_b's own users_id.
        still_a_user = connection.execute(
            text("SELECT 1 FROM users WHERE id = :id"), {"id": ctx.member_a.user_id}
        ).scalar_one_or_none()
        assert still_a_user == 1

        new_owner = connection.execute(
            text("SELECT owner_id FROM incidents WHERE id = :id"), {"id": UUID(incident["id"])}
        ).scalar_one()
    assert new_owner == ctx.member_b.user_id


def test_remove_member_blocked_for_last_active_owner(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    response = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.owner.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "LAST_OWNER_CANNOT_BE_REMOVED"


def test_admin_cannot_remove_an_owner(membership_context: _MembershipContext) -> None:
    """Symmetric with `test_admin_cannot_demote_an_owner` -- an admin
    removing an owner is just as much an unapproved strip of that owner's
    authority as demoting them. Must 403 before the separate
    `LAST_OWNER_CANNOT_BE_REMOVED` 409 check (same single-owner fixture)."""
    ctx = membership_context
    response = ctx.admin.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.owner.user_id}",
        headers=_headers(ctx.admin.token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_remove_member_self_removal_allowed(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.member_b.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_b.user_id}",
        headers=_headers(ctx.member_b.token),
    )
    assert response.status_code == 200, response.text


def test_remove_member_forbidden_for_non_owner_admin_non_self(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    response = ctx.member_a.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_b.user_id}",
        headers=_headers(ctx.member_a.token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_remove_member_cancels_active_delegations_and_revokes_evidence_grants(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    incident_1 = _create_incident(ctx.owner.client, ctx.owner.token, title="Obligation 1")
    incident_2 = _create_incident(ctx.owner.client, ctx.owner.token, title="Obligation 2")

    # Still `proposed` -- member_a is the delegator.
    delegation_1 = _propose_delegation(
        ctx.member_a.client,
        ctx.member_a.token,
        recipient_account_id=ctx.member_b.account_id,
        obligation_resource_id=incident_1["id"],
    )

    # `accepted` -- member_a is the recipient, so accepting created a
    # resource_grants row naming member_a as grantee.
    delegation_2 = _propose_delegation(
        ctx.member_b.client,
        ctx.member_b.token,
        recipient_account_id=ctx.member_a.account_id,
        obligation_resource_id=incident_2["id"],
    )
    accept = ctx.member_a.client.post(
        f"/api/v1/delegations/{delegation_2['id']}/accept",
        headers=_headers(ctx.member_a.token, key=str(uuid4())),
    )
    assert accept.status_code == 200, accept.text

    removal = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert removal.status_code == 200, removal.text

    with engine.begin() as connection:
        status_1 = connection.execute(
            text("SELECT status FROM delegations WHERE id = :id"),
            {"id": UUID(delegation_1["id"])},
        ).scalar_one()
        status_2 = connection.execute(
            text("SELECT status FROM delegations WHERE id = :id"),
            {"id": UUID(delegation_2["id"])},
        ).scalar_one()
        grant_revoked_at = connection.execute(
            text(
                "SELECT revoked_at FROM resource_grants "
                "WHERE grantee_account_id = :account_id AND resource_id = :resource_id"
            ),
            {"account_id": ctx.member_a.account_id, "resource_id": UUID(incident_2["id"])},
        ).scalar_one()
        event_types_1 = set(
            connection.execute(
                text(
                    "SELECT event_type FROM delegation_events "
                    "WHERE delegation_id = :id AND actor_account_id IS NULL"
                ),
                {"id": UUID(delegation_1["id"])},
            )
            .scalars()
            .all()
        )
    assert status_1 == "cancelled"
    assert status_2 == "cancelled"
    assert grant_revoked_at is not None
    assert "cancelled" in event_types_1


def _chained_lock_waiters(holder_pid: int) -> int:
    """Backends waiting on a lock held by `holder_pid` -- directly, or behind
    a waiter that is (a second waiter on the same row blocks on the first
    one's tuple lock, not on the holder)."""
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND (pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)] "
                    "OR EXISTS (SELECT 1 FROM unnest(pg_blocking_pids(pid)) AS b(pid) "
                    "WHERE pg_blocking_pids(b.pid) @> ARRAY[CAST(:holder AS integer)]))"
                ),
                {"holder": holder_pid},
            ).scalar_one()
        )


def _describe(results: dict[str, Any], name: str) -> str:
    if f"{name}_error" in results:
        return f"{name} raised {results[f'{name}_error']!r}"
    response = results.get(name)
    return (
        f"{name} returned {getattr(response, 'status_code', None)}: {getattr(response, 'text', '')}"
    )


def _race_two(
    *,
    lock_run_id: UUID | None = None,
    lock_delegation_id: UUID | None = None,
    first: tuple[str, Any],
    second: tuple[str, Any],
) -> dict[str, Any]:
    """Holds a run (or delegation) row FOR UPDATE, fires `first`, waits until it is
    queued behind the holder, fires `second`, waits until it is queued too,
    then commits. A request that finishes instead of queueing fails the test
    at once with its own response."""
    results: dict[str, Any] = {}

    def fire(name: str, send: Any) -> None:
        try:
            results[name] = send()
        except BaseException as exc:  # surfaced on the main thread below
            results[f"{name}_error"] = exc

    threads = [threading.Thread(target=fire, args=request) for request in (first, second)]

    def wait_queued(count: int) -> None:
        deadline = time.monotonic() + 15
        while _chained_lock_waiters(holder_pid) < count:
            for (name, _), thread in zip((first, second), threads[:count], strict=False):
                if not thread.is_alive():
                    raise AssertionError(
                        f"{name} finished instead of queueing: {_describe(results, name)}"
                    )
            if time.monotonic() > deadline:
                raise AssertionError(f"expected {count} requests queued behind the holder")
            time.sleep(0.05)

    holder = engine.connect()
    holder_tx = holder.begin()
    try:
        holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
        if lock_run_id is not None:
            holder.execute(
                text("SELECT id FROM workflow_runs WHERE id = :id FOR UPDATE"),
                {"id": lock_run_id},
            )
        else:
            holder.execute(
                text("SELECT id FROM delegations WHERE id = :id FOR UPDATE"),
                {"id": lock_delegation_id},
            )
        threads[0].start()
        wait_queued(1)
        threads[1].start()
        wait_queued(2)
        holder_tx.commit()
    finally:
        if holder_tx.is_active:
            holder_tx.rollback()
        holder.close()
        for thread in threads:
            if thread.ident is not None:  # a setup failure must not be masked
                thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads), "a request never finished"
    for name, _ in (first, second):
        if f"{name}_error" in results:
            raise results[f"{name}_error"]
    return results


def _seed_race_workflow(ctx: _MembershipContext, now: datetime) -> None:
    """A workflow owned by the owner, so nothing about it blocks removal."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
                "created_at, updated_at) VALUES (:id, :ws, 'race.workflow', :owner, :now, :now)"
            ),
            {"id": uuid4(), "ws": ctx.workspace_id, "owner": ctx.owner.user_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO workflow_versions (id, workspace_id, workflow_id, version, graph, "
                "definition_hash, status, created_by, updated_by, created_at, updated_at) "
                "VALUES (:id, :ws, 'race.workflow', 1, '{}'::jsonb, :hash, 'active', "
                ":owner, :owner, :now, :now)"
            ),
            {
                "id": uuid4(),
                "ws": ctx.workspace_id,
                "hash": "0" * 64,
                "owner": ctx.owner.user_id,
                "now": now,
            },
        )


def _seed_run(ctx: _MembershipContext, *, created_by: UUID, visibility: str, now: datetime) -> UUID:
    """A queued run owned by the owner (never blocks removal as an owned
    resource); removal cancels it only when `created_by` is the member."""
    run_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workflow_runs (id, workspace_id, workflow_id, workflow_version, "
                "status, queued_at, created_by, created_at, updated_at, owner_id, visibility) "
                "VALUES (:id, :ws, 'race.workflow', 1, 'queued', :now, :by, :now, :now, "
                ":owner, :visibility)"
            ),
            {
                "id": run_id,
                "ws": ctx.workspace_id,
                "by": created_by,
                "owner": ctx.owner.user_id,
                "visibility": visibility,
                "now": now,
            },
        )
    return run_id


def _seed_delegation(
    ctx: _MembershipContext,
    *,
    status: str,
    evidence: list[UUID],
    now: datetime,
    recipient: _Actor | None = None,
    due_at: datetime | None = None,
    delegation_id: UUID | None = None,
) -> UUID:
    """Owner -> `recipient` (member_b by default) delegation with `evidence`
    runs, inserted in that order."""
    delegation_id = delegation_id or uuid4()
    recipient = recipient or ctx.member_b
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO delegations (id, workspace_id, delegator_account_id, "
                "recipient_account_id, obligation_type, obligation_resource_id, "
                "expected_outcome, due_at, status, created_at, updated_at) "
                "VALUES (:id, :ws, :owner_account, :b_account, 'workflow_runs', :run, "
                "'Race outcome', :due, :status, :now, :now)"
            ),
            {
                "id": delegation_id,
                "ws": ctx.workspace_id,
                "owner_account": ctx.owner.account_id,
                "b_account": recipient.account_id,
                "run": evidence[0],
                "due": due_at or now + timedelta(days=1),
                "status": status,
                "now": now,
            },
        )
        for offset, run_id in enumerate(evidence):
            connection.execute(
                text(
                    "INSERT INTO delegation_evidence (id, delegation_id, resource_type, "
                    "resource_id, created_at) VALUES (:id, :d, 'workflow_runs', :run, :at)"
                ),
                {
                    "id": uuid4(),
                    "d": delegation_id,
                    "run": run_id,
                    "at": now + timedelta(seconds=offset),
                },
            )
    return delegation_id


def _remove_member_b(ctx: _MembershipContext) -> tuple[str, Any]:
    return (
        "removal",
        lambda: ctx.owner.client.delete(
            f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_b.user_id}",
            headers=_headers(ctx.owner.token),
        ),
    )


def test_remove_member_and_evidence_grant_revoke_do_not_deadlock(
    membership_context: _MembershipContext,
) -> None:
    """Removal cancels the member's runs (locks `workflow_runs` rows) and
    revokes their delegation evidence grants (updates `resource_grants`).
    A grant revoke locks the resource, then the grant. Had removal updated
    the grant first and then waited on the run, a revoke of an evidence
    grant on that run (holding the run, waiting on the grant) deadlocked
    against it.

    The revoke queues on the run behind a holder first, then the removal;
    the revoke gets the run first, so it succeeds and removal then finds the
    grant already revoked. Neither may deadlock (a 500)."""
    ctx = membership_context
    now = datetime.now(UTC)
    _seed_race_workflow(ctx, now)
    run_id = _seed_run(ctx, created_by=ctx.member_b.user_id, visibility="workspace", now=now)
    delegation_id = _seed_delegation(ctx, status="accepted", evidence=[run_id], now=now)
    grant_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at, delegation_id) "
                "VALUES (:id, :ws, :b_account, 'workflow_runs', :run, ARRAY['read'], "
                ":owner, :now, :d)"
            ),
            {
                "id": grant_id,
                "ws": ctx.workspace_id,
                "b_account": ctx.member_b.account_id,
                "run": run_id,
                "owner": ctx.owner.user_id,
                "now": now,
                "d": delegation_id,
            },
        )
    revoke_client = TestClient(app)
    revoke_client.cookies.set("ecc_session", ctx.owner.token)
    try:
        results = _race_two(
            lock_run_id=run_id,
            first=(
                "revoke",
                lambda: revoke_client.delete(
                    f"/api/v1/sharing/grants/{grant_id}", headers=_headers(ctx.owner.token)
                ),
            ),
            second=_remove_member_b(ctx),
        )
    finally:
        revoke_client.close()

    assert results["removal"].status_code == 200, results["removal"].text
    assert results["revoke"].status_code == 200, results["revoke"].text
    with engine.connect() as connection:
        run_status = connection.execute(
            text("SELECT status FROM workflow_runs WHERE id = :id"), {"id": run_id}
        ).scalar_one()
        grant_revoked_at = connection.execute(
            text("SELECT revoked_at FROM resource_grants WHERE id = :id"), {"id": grant_id}
        ).scalar_one()
    assert run_status == "cancelled"
    assert grant_revoked_at is not None


def test_remove_member_and_delegation_accept_do_not_deadlock(
    membership_context: _MembershipContext,
) -> None:
    """Accept locks the delegation, then each private evidence resource (to
    share it), then inserts grants. Removal must lock the member's
    delegations before it cancels their runs: had it locked a run first and
    then waited on the delegation, an accept holding the delegation and
    waiting on that run deadlocked against it.

    The accept is paused on its first evidence run (held by the holder)
    while holding the delegation; the removal then queues. On release the
    accept shares both runs and commits, then removal cancels the member's
    run and the now-accepted delegation, revoking its grants."""
    ctx = membership_context
    now = datetime.now(UTC)
    _seed_race_workflow(ctx, now)
    paused_on = _seed_run(ctx, created_by=ctx.owner.user_id, visibility="private", now=now)
    members_run = _seed_run(ctx, created_by=ctx.member_b.user_id, visibility="private", now=now)
    delegation_id = _seed_delegation(
        ctx, status="proposed", evidence=[paused_on, members_run], now=now
    )

    results = _race_two(
        lock_run_id=paused_on,
        first=(
            "accept",
            lambda: ctx.member_b.client.post(
                f"/api/v1/delegations/{delegation_id}/accept",
                headers=_headers(ctx.member_b.token, key=str(uuid4())),
            ),
        ),
        second=_remove_member_b(ctx),
    )

    assert results["accept"].status_code == 200, results["accept"].text
    assert results["removal"].status_code == 200, results["removal"].text
    with engine.connect() as connection:
        delegation_status = connection.execute(
            text("SELECT status FROM delegations WHERE id = :id"), {"id": delegation_id}
        ).scalar_one()
        live_grants = connection.execute(
            text(
                "SELECT count(*) FROM resource_grants "
                "WHERE delegation_id = :d AND revoked_at IS NULL"
            ),
            {"d": delegation_id},
        ).scalar_one()
        run_status = connection.execute(
            text("SELECT status FROM workflow_runs WHERE id = :id"), {"id": members_run}
        ).scalar_one()
    assert delegation_status == "cancelled"
    assert live_grants == 0
    assert run_status == "cancelled"


def test_remove_member_revokes_sessions(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    removal = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert removal.status_code == 200, removal.text

    with engine.begin() as connection:
        revoked_at = connection.execute(
            text(
                "SELECT revoked_at FROM sessions "
                "WHERE workspace_id = :workspace_id AND user_id = :user_id"
            ),
            {"workspace_id": ctx.workspace_id, "user_id": ctx.member_a.user_id},
        ).scalar_one()
    assert revoked_at is not None

    # The already-issued cookie no longer authenticates anything.
    denied = ctx.member_a.client.get(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members",
        headers=_headers(ctx.member_a.token),
    )
    assert denied.status_code == 401, denied.text


# ---------------------------------------------------------------------------
# POST|GET /ownership/transfers
# ---------------------------------------------------------------------------


def test_ownership_transfer_requires_owner_admin_or_resource_owner(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    response = ctx.member_a.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "to_account_id": str(ctx.member_b.account_id),
        },
        headers=_headers(ctx.member_a.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_ownership_transfer_rejects_ungrantable_resource_type(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    response = ctx.owner.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "personal_domains",
            "resource_id": str(uuid4()),
            "to_account_id": str(ctx.member_b.account_id),
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "RESOURCE_TYPE_NOT_GRANTABLE"


def test_ownership_transfer_requires_active_recipient_membership(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    response = ctx.owner.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "to_account_id": str(uuid4()),
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RECIPIENT_NOT_FOUND"


def test_list_ownership_transfers_role_scoped(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    transfer = ctx.owner.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "to_account_id": str(ctx.member_b.account_id),
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert transfer.status_code == 201, transfer.text
    transfer_id = transfer.json()["id"]

    owner_view = ctx.owner.client.get(
        "/api/v1/ownership/transfers", headers=_headers(ctx.owner.token)
    )
    assert transfer_id in {t["id"] for t in owner_view.json()["transfers"]}

    recipient_view = ctx.member_b.client.get(
        "/api/v1/ownership/transfers", headers=_headers(ctx.member_b.token)
    )
    assert transfer_id in {t["id"] for t in recipient_view.json()["transfers"]}

    uninvolved_view = ctx.member_a.client.get(
        "/api/v1/ownership/transfers", headers=_headers(ctx.member_a.token)
    )
    assert transfer_id not in {t["id"] for t in uninvolved_view.json()["transfers"]}


def test_remove_member_and_lazy_expiry_do_not_deadlock(
    membership_context: _MembershipContext,
) -> None:
    """Removal locks the member's live delegations in `id` order. Listing
    delegations lazily expires the caller's overdue proposed ones in one bulk
    UPDATE; unless that also locks in `id` order it scans in physical order,
    and two overdue delegations stored in the reverse of their `id` order
    deadlocked the two.

    The larger-id delegation is stored first. The holder locks it; the
    listing queues on it first, then the removal (which by then holds the
    smaller-id one). On release both must finish."""
    ctx = membership_context
    now = datetime.now(UTC)
    _seed_race_workflow(ctx, now)
    run_id = _seed_run(ctx, created_by=ctx.owner.user_id, visibility="workspace", now=now)
    low, high = sorted([uuid4(), uuid4()])
    overdue = now - timedelta(hours=1)
    for delegation_id in (high, low):  # physical order: high first
        _seed_delegation(
            ctx,
            status="proposed",
            evidence=[run_id],
            now=now,
            due_at=overdue,
            delegation_id=delegation_id,
        )

    results = _race_two(
        lock_delegation_id=high,
        first=(
            "listing",
            lambda: ctx.member_b.client.get(
                "/api/v1/delegations", headers=_headers(ctx.member_b.token)
            ),
        ),
        second=_remove_member_b(ctx),
    )

    assert results["listing"].status_code == 200, results["listing"].text
    assert results["removal"].status_code == 200, results["removal"].text
    with engine.connect() as connection:
        statuses = set(
            connection.execute(
                text("SELECT status FROM delegations WHERE id = ANY(:ids)"),
                {"ids": [low, high]},
            ).scalars()
        )
    # Expired by the listing (it got the rows first), never left proposed.
    assert statuses == {"expired"}


def test_concurrent_accepts_of_shared_evidence_do_not_deadlock(
    membership_context: _MembershipContext,
) -> None:
    """Two delegations naming the same two private runs, in opposite
    orders, accepted at once. Each accept locks its delegation, then its
    evidence rows to share them; unless both lock the evidence in one
    canonical order they deadlock.

    The holder locks the smaller-id run; the first accept (evidence listed
    small-then-large) queues on it; the second (large-then-small) queues
    behind. On release both must be accepted."""
    ctx = membership_context
    now = datetime.now(UTC)
    _seed_race_workflow(ctx, now)
    runs = sorted(
        _seed_run(ctx, created_by=ctx.owner.user_id, visibility="private", now=now)
        for _ in range(2)
    )
    to_a = _seed_delegation(ctx, status="proposed", evidence=runs, now=now, recipient=ctx.member_a)
    to_b = _seed_delegation(
        ctx, status="proposed", evidence=list(reversed(runs)), now=now, recipient=ctx.member_b
    )

    def accept(actor: _Actor, delegation_id: UUID) -> Any:
        return actor.client.post(
            f"/api/v1/delegations/{delegation_id}/accept",
            headers=_headers(actor.token, key=str(uuid4())),
        )

    results = _race_two(
        lock_run_id=runs[0],
        first=("accept_a", lambda: accept(ctx.member_a, to_a)),
        second=("accept_b", lambda: accept(ctx.member_b, to_b)),
    )

    assert results["accept_a"].status_code == 200, results["accept_a"].text
    assert results["accept_b"].status_code == 200, results["accept_b"].text
