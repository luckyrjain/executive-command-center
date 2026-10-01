"""Delegation and morning-brief writes against a concurrent member removal
or role change (ADR-0014).

Every write transaction in `collaboration/delegations.py` and
`platform/dashboard_briefs.py` now takes the shared membership lock first
(`authz.lock_membership_for_write`) and re-checks active membership in it
(`role_action="read"`). A removal or demotion holding the lock makes the
write wait, and once it commits the write answers 403 and writes nothing.

Demotion does not revoke these writes, by existing policy: a delegation is
party-gated (only its recipient may accept/reject/complete it, only its
delegator may revoke it), not role-gated, and a morning brief is the
caller's own per-user data. Proposing a delegation is the exception: it
needs `write` on the obligation, which a viewer lacks. See
`membership_lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from membership_lock_race_support import (
    FORBIDDEN,
    ROLE_GATED,
    Case,
    Change,
    Ids,
    RaceWorld,
    Refusal,
    Seed,
    assert_proceeds,
    assert_refused,
    race,
    race_world,
    refusal_params,
    seed,
    seed_nothing,
)
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_WRITE_TABLES = (
    "delegations",
    "resource_grants",
    "member_notifications",
    "tasks",
    "morning_briefs",
    "audit_events",
    "event_outbox",
    "idempotency_records",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    # `delegation_events`/`delegation_evidence` have no `workspace_id`; they
    # cascade from `delegations`.
    with race_world(
        "Collaboration Briefs Membership Race",
        ("resource_grants", "member_notifications", "delegations", "morning_briefs", "tasks"),
    ) as w:
        yield w


def _delegation_children(ws: UUID) -> tuple[str | None, ...]:
    """`delegation_events`/`delegation_evidence` carry no `workspace_id`, so
    the harness's fingerprint cannot see them; this covers them through
    their parent."""
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                text(
                    f"SELECT md5(string_agg(c::text, '|' ORDER BY c::text)) "  # noqa: S608
                    f"FROM {table} c JOIN delegations d ON d.id = c.delegation_id "
                    "WHERE d.workspace_id = :ws"
                ),
                {"ws": ws},
            ).scalar_one()
            for table in ("delegation_events", "delegation_evidence")
        )


def _account(conn: Connection, ws: UUID, user_id: UUID) -> UUID:
    return conn.execute(
        text("SELECT account_id FROM users WHERE workspace_id = :ws AND id = :u"),
        {"ws": ws, "u": user_id},
    ).scalar_one()


def _insert_task(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    task_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO tasks (id, workspace_id, owner_id, title, created_by, updated_by, "
            "created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :a, 'Race obligation', :a, :a, :now, :now, 'workspace')"
        ),
        {"id": task_id, "ws": w.ws, "a": w.a, "now": now},
    )
    return task_id


def _seed_obligation(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"task": _insert_task(conn, w, now), "recipient": _account(conn, w.ws, w.a)}


def _delegation_seeder(*, b_is: str, status: str) -> Seed:
    """A delegation of an A-owned, `workspace`-visible task (with the task
    as its evidence) between A and B, B being the `recipient` or the
    `delegator`."""

    def _seed(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
        task_id = _insert_task(conn, w, now)
        a_account, b_account = _account(conn, w.ws, w.a), _account(conn, w.ws, w.b)
        delegator, recipient = (
            (a_account, b_account) if b_is == "recipient" else (b_account, a_account)
        )
        delegation_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO delegations (id, workspace_id, delegator_account_id, "
                "recipient_account_id, obligation_type, obligation_resource_id, "
                "expected_outcome, due_at, status, created_at, updated_at) "
                "VALUES (:id, :ws, :delegator, :recipient, 'tasks', :task, "
                "'Race outcome', :due, :status, :now, :now)"
            ),
            {
                "id": delegation_id,
                "ws": w.ws,
                "delegator": delegator,
                "recipient": recipient,
                "task": task_id,
                "due": now + timedelta(days=7),
                "status": status,
                "now": now,
            },
        )
        conn.execute(
            text(
                "INSERT INTO delegation_evidence (id, delegation_id, resource_type, "
                "resource_id, created_at) VALUES (:id, :d, 'tasks', :task, :now)"
            ),
            {"id": uuid4(), "d": delegation_id, "task": task_id, "now": now},
        )
        return {"id": delegation_id}

    return _seed


def _empty(_: Ids) -> dict[str, Any]:
    return {}


def _create_body(ids: Ids) -> dict[str, Any]:
    return {
        "recipient_account_id": str(ids["recipient"]),
        "obligation_type": "tasks",
        "obligation_resource_id": str(ids["task"]),
        "expected_outcome": "Race outcome",
        "due_at": (datetime.now().astimezone() + timedelta(days=7)).isoformat(),
    }


# Party-gated or per-user writes: demotion does not revoke them, removal
# does (`role_action="read"`).
_MEMBERSHIP_GATED: dict[Change, Refusal | None] = {"demote": None, "remove": FORBIDDEN}

CASES: dict[str, Case] = {
    "delegation_create": Case(
        _seed_obligation, "POST", "/api/v1/delegations", _create_body, 201, ROLE_GATED
    ),
    "delegation_accept": Case(
        _delegation_seeder(b_is="recipient", status="proposed"),
        "POST",
        "/api/v1/delegations/{id}/accept",
        _empty,
        200,
        _MEMBERSHIP_GATED,
    ),
    "delegation_reject": Case(
        _delegation_seeder(b_is="recipient", status="proposed"),
        "POST",
        "/api/v1/delegations/{id}/reject",
        _empty,
        200,
        _MEMBERSHIP_GATED,
    ),
    "delegation_revoke": Case(
        _delegation_seeder(b_is="delegator", status="accepted"),
        "POST",
        "/api/v1/delegations/{id}/revoke",
        _empty,
        200,
        _MEMBERSHIP_GATED,
    ),
    "delegation_complete": Case(
        _delegation_seeder(b_is="recipient", status="accepted"),
        "POST",
        "/api/v1/delegations/{id}/complete",
        _empty,
        200,
        _MEMBERSHIP_GATED,
    ),
    "brief_refresh": Case(
        seed_nothing, "POST", "/api/v1/briefs/morning", lambda _: None, 200, _MEMBERSHIP_GATED
    ),
    # A GET that lazily generates the brief (no brief exists yet).
    "brief_get_generates": Case(
        seed_nothing, "GET", "/api/v1/briefs/morning", lambda _: None, 200, _MEMBERSHIP_GATED
    ),
}


@pytest.mark.parametrize(("name", "change"), refusal_params(CASES))
def test_write_waiting_on_membership_lock_rechecks_authorization(
    world: RaceWorld, name: str, change: Change
) -> None:
    case = CASES[name]
    ids = seed(world, case)
    children = _delegation_children(world.ws)
    assert_refused(world, case, ids, change, _WRITE_TABLES)
    assert _delegation_children(world.ws) == children


@pytest.mark.parametrize("name", list(CASES))
def test_write_waiting_on_membership_lock_without_change_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    assert_proceeds(world, case, seed(world, case))


@pytest.mark.parametrize(
    "name",
    [name for name, case in CASES.items() if case.refusals.get("demote") is None],
)
def test_viewer_demotion_does_not_block_party_or_own_brief_write(
    world: RaceWorld, name: str
) -> None:
    """`role_action="read"`: a delegation party demoted to viewer may still
    act on it, and a viewer may still generate their own brief. Pinned so
    that stays a deliberate policy, not an accident of the lock."""
    case = CASES[name]

    response, blocked = race(world, case, seed(world, case), change="demote")

    assert response.status_code == case.ok_status, response.text
    assert blocked
