"""Sibling sweep of the `_mutate_attention` lock-before-authorize fix: the
risk review, waiting-link patch/fulfil/cancel, planning-constraint archive
and every plan mutation (accept, supersede, propose, block move/remove)
authorized the caller *before* taking `SELECT ... FOR UPDATE` on the row
they then write. An ownership transfer (`authz_grants` transfer locks the
row, rewrites `owner_id`, and does not bump `version`) that committed while
the request waited on that row lock left the request writing to a row the
caller could no longer see.

Now each row is locked first and authorization is evaluated afterwards, on
the committed post-transfer row, so the waiting request answers 404 and
writes nothing.

Concurrency is real (request thread + separate connection); "is waiting" is
observed in `pg_stat_activity`, not assumed from timing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

settings = get_settings()
_WAIT_SECONDS = 15


@dataclass
class RaceWorld:
    """One workspace: A (`owner`), B (`admin`, the racing caller and the
    rows' original owner) and C (`member`, the transfer target)."""

    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    b_token: str


def _session(connection: Connection, ws: UUID, user_id: UUID, token: str, now: datetime) -> None:
    connection.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
            "expires_at, last_seen_at) "
            "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
        ),
        {
            "id": uuid4(),
            "workspace_id": ws,
            "user_id": user_id,
            "token_hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "last_seen_at": now,
        },
    )


@pytest.fixture
def race_world() -> Iterator[RaceWorld]:
    ws, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    b_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Attention Sibling Lock Race', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="admin")
        create_identity(connection, workspace_id=ws, user_id=c, now=now, role="member")
        _session(connection, ws, b, b_token, now)
    try:
        yield RaceWorld(ws=ws, a=a, b=b, c=c, b_token=b_token)
    finally:
        with engine.begin() as connection:
            account_ids = list(
                connection.execute(
                    text("SELECT account_id FROM users WHERE workspace_id = :ws"), {"ws": ws}
                ).scalars()
            )
            for table in (
                "risk_reviews",
                "risks",
                "waiting_links",
                "pkos_nodes",
                "plan_blocks",
                "plans",
                "planning_constraints",
                "attention_items",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "sessions",
                "workspace_memberships",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                )
            connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
            connection.execute(
                text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids}
            )


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }


# ---------------------------------------------------------------------------
# Seeds: each inserts one private row owned by B and returns its id (plus,
# for plan blocks, the block id the request path needs).
# ---------------------------------------------------------------------------


def _seed_risk(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    risk_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO risks (id, workspace_id, description, probability, impact, "
            "owner_id, created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, 'Race risk', 3, 3, :b, :b, :b, :now, :now, 'private')"
        ),
        {"id": risk_id, "ws": w.ws, "b": w.b, "now": now},
    )
    return {"id": risk_id}


def _seed_waiting_link(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    node_id, link_id = uuid4(), uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, owner_id) "
            "VALUES (:id, :ws, 'person', 'Race Counterparty', :now, :now, :b)"
        ),
        {"id": node_id, "ws": w.ws, "b": w.b, "now": now},
    )
    conn.execute(
        text(
            "INSERT INTO waiting_links (id, workspace_id, subject_type, subject_id, "
            "counterparty_entity_id, direction, since_at, created_by, updated_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'knowledge_entity', :node, :node, 'waiting_on_them', "
            ":now, :b, :b, :now, :now, :b, 'private')"
        ),
        {"id": link_id, "ws": w.ws, "node": node_id, "b": w.b, "now": now},
    )
    return {"id": link_id}


def _seed_constraint(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    constraint_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO planning_constraints (id, workspace_id, user_id, kind, label, "
            "hardness, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :b, 'preference', 'Race constraint', 'soft', "
            ":now, :now, :b, 'private')"
        ),
        {"id": constraint_id, "ws": w.ws, "b": w.b, "now": now},
    )
    return {"id": constraint_id}


def _seed_plan(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    plan_id, block_id = uuid4(), uuid4()
    today = date.today()
    conn.execute(
        text(
            "INSERT INTO plans (id, workspace_id, user_id, period_start, period_end, "
            "policy_version, capacity_minutes, created_by, updated_by, created_at, "
            "updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :b, :start, :end, 1, 480, :b, :b, :now, :now, :b, 'private')"
        ),
        {
            "id": plan_id,
            "ws": w.ws,
            "b": w.b,
            "now": now,
            "start": today,
            "end": today + timedelta(days=6),
        },
    )
    starts = datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC).replace(hour=10)
    conn.execute(
        text(
            "INSERT INTO plan_blocks (id, workspace_id, plan_id, source_type, starts_at, "
            "ends_at, rationale, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :plan, 'constraint', :starts, :ends, 'Race block', "
            ":now, :now, :b, 'private')"
        ),
        {
            "id": block_id,
            "ws": w.ws,
            "plan": plan_id,
            "starts": starts,
            "ends": starts + timedelta(hours=1),
            "now": now,
            "b": w.b,
        },
    )
    return {"id": plan_id, "block": block_id}


@dataclass(frozen=True)
class Case:
    table: str
    seed: Callable[[Connection, RaceWorld, datetime], dict[str, UUID]]
    method: str
    path: str  # formatted with the seed's ids
    body: Callable[[dict[str, UUID]], dict[str, Any]]
    not_found: str


def _v1(_: dict[str, UUID]) -> dict[str, Any]:
    return {"expected_version": 1}


def _move_body(_: dict[str, UUID]) -> dict[str, Any]:
    starts = datetime.combine(date.today() + timedelta(days=1), datetime.min.time(), UTC)
    starts = starts.replace(hour=14)
    return {
        "expected_version": 1,
        "starts_at": starts.isoformat(),
        "ends_at": (starts + timedelta(hours=1)).isoformat(),
    }


CASES: dict[str, Case] = {
    "risk_review": Case(
        "risks",
        _seed_risk,
        "POST",
        "/api/v1/risks/{id}/review",
        lambda _: {"expected_version": 1, "outcome": "no_change"},
        "RISK_NOT_FOUND",
    ),
    "waiting_patch": Case(
        "waiting_links",
        _seed_waiting_link,
        "PATCH",
        "/api/v1/waiting/{id}",
        lambda _: {"expected_version": 1, "note": "race probe"},
        "WAITING_LINK_NOT_FOUND",
    ),
    "waiting_fulfil": Case(
        "waiting_links",
        _seed_waiting_link,
        "POST",
        "/api/v1/waiting/{id}/fulfil",
        _v1,
        "WAITING_LINK_NOT_FOUND",
    ),
    "waiting_cancel": Case(
        "waiting_links",
        _seed_waiting_link,
        "POST",
        "/api/v1/waiting/{id}/cancel",
        _v1,
        "WAITING_LINK_NOT_FOUND",
    ),
    "constraint_archive": Case(
        "planning_constraints",
        _seed_constraint,
        "POST",
        "/api/v1/planning/constraints/{id}/archive",
        lambda _: {},
        "PLANNING_CONSTRAINT_NOT_FOUND",
    ),
    "plan_accept": Case(
        "plans", _seed_plan, "POST", "/api/v1/plans/{id}/accept", _v1, "PLAN_NOT_FOUND"
    ),
    "plan_supersede": Case(
        "plans", _seed_plan, "POST", "/api/v1/plans/{id}/supersede", _v1, "PLAN_NOT_FOUND"
    ),
    "plan_propose": Case(
        "plans", _seed_plan, "POST", "/api/v1/plans/{id}/propose", _v1, "PLAN_NOT_FOUND"
    ),
    "plan_block_move": Case(
        "plans",
        _seed_plan,
        "POST",
        "/api/v1/plans/{id}/blocks/{block}/move",
        _move_body,
        "PLAN_NOT_FOUND",
    ),
    "plan_block_remove": Case(
        "plans",
        _seed_plan,
        "POST",
        "/api/v1/plans/{id}/blocks/{block}/remove",
        _v1,
        "PLAN_NOT_FOUND",
    ),
}


def _lock_waiters(table: str) -> int:
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event IN ('transactionid', 'tuple') "
                    "AND query ~* :pattern"
                ),
                {"pattern": f"FROM {table}\\s.*FOR UPDATE"},
            ).scalar_one()
        )


def _wait_for_lock_waiter(table: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while _lock_waiters(table) < 1:
        if time.monotonic() > deadline:
            raise AssertionError(f"mutation never blocked on the {table} row lock")
        time.sleep(0.05)


def _row_snapshot(table: str, row_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(f"SELECT * FROM {table} WHERE id = :id"),  # noqa: S608 -- CASES literal
                {"id": row_id},
            )
            .mappings()
            .one()
        )


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            name: int(connection.execute(text(sql), {"ws": ws}).scalar_one())
            for name, sql in {
                "audit_events": "SELECT count(*) FROM audit_events WHERE workspace_id = :ws",
                "risk_reviews": "SELECT count(*) FROM risk_reviews WHERE workspace_id = :ws",
                "waiting_links": "SELECT count(*) FROM waiting_links WHERE workspace_id = :ws",
                "plans": "SELECT count(*) FROM plans WHERE workspace_id = :ws",
                "moved_blocks": "SELECT count(*) FROM plan_blocks "
                "WHERE workspace_id = :ws AND updated_at <> created_at",
            }.items()
        }


def _race(
    w: RaceWorld, case: Case, ids: dict[str, UUID], *, transfer: bool
) -> tuple[Any, dict[str, Any]]:
    """Holds the row lock in a separate transaction (optionally transferring
    the row to C there), fires B's mutation, waits until it is blocked on
    that lock, then commits."""
    client = TestClient(app)
    client.cookies.set("ecc_session", w.b_token)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.request(
                case.method,
                case.path.format(**ids),
                headers=_headers(w.b_token),
                json=case.body(ids),
            )
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    try:
        holder = engine.connect()
        holder_tx = holder.begin()
        thread = threading.Thread(target=fire)
        try:
            holder.execute(
                text(f"SELECT id FROM {case.table} WHERE id = :id FOR UPDATE"),  # noqa: S608
                {"id": ids["id"]},
            )
            before = _row_snapshot(case.table, ids["id"])
            if transfer:
                holder.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"id": ids["id"], "c": w.c},
                )
            thread.start()
            _wait_for_lock_waiter(case.table)
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "mutation request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"], before
    finally:
        client.close()


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(
    race_world: RaceWorld, name: str
) -> None:
    w, case = race_world, CASES[name]
    with engine.begin() as connection:
        ids = case.seed(connection, w, datetime.now(UTC))
    counts_before = _side_effect_counts(w.ws)

    response, before = _race(w, case, ids, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    after = _row_snapshot(case.table, ids["id"])
    assert after == {**before, "owner_id": w.c}
    assert _side_effect_counts(w.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    race_world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change does not 404 --
    the 404 above comes from the transfer, not from the wait itself."""
    w, case = race_world, CASES[name]
    with engine.begin() as connection:
        ids = case.seed(connection, w, datetime.now(UTC))

    response, _ = _race(w, case, ids, transfer=False)

    assert response.status_code != 404, response.text
    assert response.status_code != 403, response.text
