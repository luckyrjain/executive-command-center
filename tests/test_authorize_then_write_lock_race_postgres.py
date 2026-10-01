"""Lock-before-authorize, second shape: endpoints that authorized the caller
and then wrote with a plain `UPDATE ... WHERE id = :id` (or inserted a child
row) without ever locking the row the check was about.

An ownership transfer (`authz_grants`: locks the row, rewrites `owner_id`,
does not bump `version`) holding the row lock made the request's write wait,
then let it proceed once the transfer committed -- writing to (or hanging a
new row off) a resource the caller could no longer see:

- `POST /engineering/incidents/{id}/resolve` and
  `POST /engineering/decisions/{id}/decide` (plain guarded UPDATE);
- `POST /ai/runs/{id}/cancel` (plain guarded UPDATE);
- `POST /knowledge/entities/{id}/relationships` (edge INSERT against an
  unlocked source entity -- the FK check waited, then went ahead);
- `POST /delegations` (delegation INSERT against an unlocked obligation --
  nothing waited at all);
- `POST /delegations/{id}/accept`, whose evidence re-check ran before the
  visibility-widening UPDATE and grant INSERT waited on the transfer, and so
  granted the recipient a resource the delegator no longer controlled.

Now each endpoint locks the row (or parent row) first, workspace-scoped, and
authorizes against the committed post-transfer row. Unlike
`lock_race_support.race`, the harness below does not require the request to
block on a `FOR UPDATE`: pre-fix code blocks on a plain UPDATE/INSERT or not
at all, and must still reach its (wrong) answer so the test fails on it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from lock_race_support import WAIT_SECONDS, RaceWorld, headers, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SIDE_EFFECT_TABLES = (
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "pkos_edges",
    "delegations",
    "resource_grants",
    "member_notifications",
)


@pytest.fixture
def isolation(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bool], None]]:
    """Call with `True` to turn `ECC_PERSONAL_DATA_ISOLATION` on for the test."""

    def enable(on: bool) -> None:
        if on:
            monkeypatch.setenv("ECC_PERSONAL_DATA_ISOLATION", "true")
            get_settings.cache_clear()

    try:
        yield enable
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Authorize Then Write Lock Race",
        (
            "member_notifications",
            "resource_grants",
            "delegations",
            "timeline_entries",
            "retrieval_documents",
            "embedding_projections",
            "pkos_edges",
            "pkos_evidence",
            "pkos_nodes",
            "incidents",
            "engineering_decisions",
            "ai_runs",
        ),
    ) as w:
        yield w


def _account_id(conn: Connection, w: RaceWorld, users_id: UUID) -> UUID:
    return UUID(
        str(
            conn.execute(
                text("SELECT account_id FROM users WHERE workspace_id = :ws AND id = :id"),
                {"ws": w.ws, "id": users_id},
            ).scalar_one()
        )
    )


def _a_token(conn: Connection, w: RaceWorld, now: datetime) -> str:
    token = f"session-{uuid4()}"
    conn.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
            "expires_at, last_seen_at) "
            "VALUES (:id, :ws, :a, :token_hash, :expires_at, :now)"
        ),
        {
            "id": uuid4(),
            "ws": w.ws,
            "a": w.a,
            "token_hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "now": now,
        },
    )
    return token


def _seed_incident(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    incident_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO incidents (id, workspace_id, title, severity, status, detected_at, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'Race incident', 'high', 'open', :detected, "
            ":b, :b, :now, :now, :b, 'private')"
        ),
        {"id": incident_id, "ws": w.ws, "detected": now - timedelta(hours=1), "now": now, "b": w.b},
    )
    return incident_id


def _seed_decision(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    decision_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO engineering_decisions (id, workspace_id, title, status, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'Race decision', 'proposed', :b, :b, :created, :now, "
            ":b, 'private')"
        ),
        {"id": decision_id, "ws": w.ws, "created": now - timedelta(hours=1), "now": now, "b": w.b},
    )
    return decision_id


def _seed_run(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    run_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO ai_runs (id, workspace_id, actor_id, task_type, data_class, status, "
            "started_at, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :b, 'attention.explain_item', 'internal', 'running', "
            ":now, :now, :now, :b, 'private')"
        ),
        {"id": run_id, "ws": w.ws, "now": now, "b": w.b},
    )
    return run_id


def _seed_node(conn: Connection, w: RaceWorld, now: datetime, *, visibility: str) -> UUID:
    node_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, status, owner_id, visibility) "
            "VALUES (:id, :ws, 'person', :name, :now, :now, 'active', :b, :visibility)"
        ),
        {
            "id": node_id,
            "ws": w.ws,
            "name": f"Race node {node_id}",
            "now": now,
            "b": w.b,
            "visibility": visibility,
        },
    )
    return node_id


def _seed_evidence(
    conn: Connection, w: RaceWorld, now: datetime, node_id: UUID, *, visibility: str = "workspace"
) -> UUID:
    evidence_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, source_ref, "
            "sha256, captured_at, owner_id, visibility) "
            "VALUES (:id, :ws, :node, 'manual', 'race-ref', :sha, :now, :b, :visibility)"
        ),
        {
            "id": evidence_id,
            "ws": w.ws,
            "node": node_id,
            "sha": "0" * 64,
            "now": now,
            "b": w.b,
            "visibility": visibility,
        },
    )
    return evidence_id


def _seed_delegation(conn: Connection, w: RaceWorld, now: datetime, obligation_id: UUID) -> UUID:
    """A still-`proposed` delegation from B to A of B's incident."""
    delegation_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO delegations (id, workspace_id, delegator_account_id, "
            "recipient_account_id, obligation_type, obligation_resource_id, "
            "expected_outcome, due_at, status, created_at, updated_at) "
            "VALUES (:id, :ws, :delegator, :recipient, 'incidents', :obligation, "
            "'Race outcome', :due, 'proposed', :now, :now)"
        ),
        {
            "id": delegation_id,
            "ws": w.ws,
            "delegator": _account_id(conn, w, w.b),
            "recipient": _account_id(conn, w, w.a),
            "obligation": obligation_id,
            "due": now + timedelta(days=1),
            "now": now,
        },
    )
    conn.execute(
        text(
            "INSERT INTO delegation_evidence (id, delegation_id, resource_type, resource_id, "
            "created_at) VALUES (:id, :delegation, 'incidents', :obligation, :now)"
        ),
        {"id": uuid4(), "delegation": delegation_id, "obligation": obligation_id, "now": now},
    )
    return delegation_id


@dataclass(frozen=True)
class Seeded:
    """`row_id` is the row the race locks (and, for `transfer=True`, re-owns
    to C): the mutated row itself, or the parent authorization is evaluated
    against. `token` is the caller's session (B unless the case says)."""

    row_id: UUID
    path: str
    body: dict[str, Any]
    token: str | None = None


@dataclass(frozen=True)
class Case:
    table: str
    seed: Callable[[Connection, RaceWorld, datetime], Seeded]
    not_found: str | None
    ok_status: int
    # Which statements count as "the request is blocked" (regex over the
    # waiting query): pre-fix code waits in a plain UPDATE of the row, or --
    # for an edge -- in the INSERT's foreign-key check on the locked node.
    wait_on: str = ""
    # `ECC_PERSONAL_DATA_ISOLATION` on: the flag that makes cited evidence
    # an authorization boundary (`authz.cited_evidence_readable`).
    isolation: bool = False


def _incident(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    incident_id = _seed_incident(conn, w, now)
    return Seeded(
        incident_id,
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        {"resolved_at": now.isoformat()},
    )


def _decision(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    decision_id = _seed_decision(conn, w, now)
    return Seeded(
        decision_id,
        f"/api/v1/engineering/decisions/{decision_id}/decide",
        {"decided_at": now.isoformat(), "rationale": "race probe"},
    )


def _run(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    run_id = _seed_run(conn, w, now)
    return Seeded(run_id, f"/api/v1/ai/runs/{run_id}/cancel", {})


def _relationship(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    source = _seed_node(conn, w, now, visibility="private")
    target = _seed_node(conn, w, now, visibility="workspace")
    evidence_id = _seed_evidence(conn, w, now, target)
    return Seeded(
        source,
        f"/api/v1/knowledge/entities/{source}/relationships",
        {"relationship_type": "OWNS", "to_entity_id": str(target), "evidence_id": str(evidence_id)},
    )


def _relationship_target(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    """The raced row is the target entity, which is authorized read-only."""
    source = _seed_node(conn, w, now, visibility="workspace")
    target = _seed_node(conn, w, now, visibility="private")
    evidence_id = _seed_evidence(conn, w, now, source)
    return Seeded(
        target,
        f"/api/v1/knowledge/entities/{source}/relationships",
        {"relationship_type": "OWNS", "to_entity_id": str(target), "evidence_id": str(evidence_id)},
    )


def _relationship_evidence(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    """The raced row is the cited evidence, which is authorized read-only."""
    source = _seed_node(conn, w, now, visibility="workspace")
    target = _seed_node(conn, w, now, visibility="workspace")
    evidence_id = _seed_evidence(conn, w, now, source, visibility="private")
    return Seeded(
        evidence_id,
        f"/api/v1/knowledge/entities/{source}/relationships",
        {"relationship_type": "OWNS", "to_entity_id": str(target), "evidence_id": str(evidence_id)},
    )


def _delegation_create(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    incident_id = _seed_incident(conn, w, now)
    return Seeded(
        incident_id,
        "/api/v1/delegations",
        {
            "recipient_account_id": str(_account_id(conn, w, w.a)),
            "obligation_type": "incidents",
            "obligation_resource_id": str(incident_id),
            "expected_outcome": "Race outcome",
            "due_at": (now + timedelta(days=1)).isoformat(),
        },
    )


def _delegation_evidence(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    """The raced row is an explicitly named evidence item (read-only)."""
    incident_id = _seed_incident(conn, w, now)
    decision_id = _seed_decision(conn, w, now)
    return Seeded(
        decision_id,
        "/api/v1/delegations",
        {
            "recipient_account_id": str(_account_id(conn, w, w.a)),
            "obligation_type": "incidents",
            "obligation_resource_id": str(incident_id),
            "expected_outcome": "Race outcome",
            "due_at": (now + timedelta(days=1)).isoformat(),
            "evidence": [
                {"resource_type": "engineering_decisions", "resource_id": str(decision_id)}
            ],
        },
    )


def _delegation_accept(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    incident_id = _seed_incident(conn, w, now)
    delegation_id = _seed_delegation(conn, w, now, incident_id)
    return Seeded(
        incident_id, f"/api/v1/delegations/{delegation_id}/accept", {}, _a_token(conn, w, now)
    )


CASES: dict[str, Case] = {
    "incident_resolve": Case("incidents", _incident, "INCIDENT_NOT_FOUND", 200),
    "decision_decide": Case("engineering_decisions", _decision, "DECISION_NOT_FOUND", 200),
    "ai_run_cancel": Case("ai_runs", _run, "AI_RUN_NOT_FOUND", 200),
    "relationship_create": Case(
        "pkos_nodes", _relationship, "ENTITY_NOT_FOUND", 201, "pkos_nodes|pkos_edges"
    ),
    "relationship_create_target": Case(
        "pkos_nodes", _relationship_target, "ENTITY_NOT_FOUND", 201, "pkos_nodes|pkos_edges"
    ),
    "relationship_create_evidence": Case(
        "pkos_evidence",
        _relationship_evidence,
        "EVIDENCE_NOT_FOUND",
        201,
        "pkos_evidence|pkos_edges",
        isolation=True,
    ),
    "delegation_create": Case("incidents", _delegation_create, "OBLIGATION_NOT_FOUND", 201),
    "delegation_create_evidence": Case(
        "engineering_decisions", _delegation_evidence, "EVIDENCE_NOT_FOUND", 201
    ),
    # The recipient's accept still succeeds either way -- the delegation
    # itself is theirs to accept -- but the evidence the delegator lost to
    # the transfer must not be granted (asserted below).
    "delegation_accept": Case("incidents", _delegation_accept, None, 200),
}


def _lock_waiters(pattern: str) -> int:
    """Backends in this database blocked on a lock in a statement touching
    `pattern`'s tables -- any statement, not only `FOR UPDATE`, since pre-fix
    code waits in a plain UPDATE or INSERT."""
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND query ~* :pattern"
                ),
                {"pattern": f"\\m({pattern})\\M"},
            ).scalar_one()
        )


def _race(
    w: RaceWorld, case: Case, s: Seeded, *, transfer: bool
) -> tuple[httpx.Response, dict[str, Any]]:
    """Holds `case.table`'s `s.row_id` lock in a separate transaction
    (optionally transferring the row to C there), fires the request, waits
    until it is blocked on a lock *or* has already finished (pre-fix
    delegation create never touched the row), then commits."""
    client = TestClient(app)
    client.cookies.set("ecc_session", s.token or w.b_token)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.post(
                s.path, headers=headers(s.token or w.b_token), json=s.body
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
                {"id": s.row_id},
            )
            before = row_snapshot(case.table, s.row_id)
            if transfer:
                holder.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"id": s.row_id, "c": w.c},
                )
            thread.start()
            deadline = time.monotonic() + WAIT_SECONDS
            while thread.is_alive() and _lock_waiters(case.wait_on or case.table) < 1:
                if time.monotonic() > deadline:
                    raise AssertionError("request neither blocked nor finished")
                time.sleep(0.05)
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            thread.join(timeout=WAIT_SECONDS)
        assert not thread.is_alive(), "mutation request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"], before
    finally:
        client.close()


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in _SIDE_EFFECT_TABLES
        }


def _seed(world: RaceWorld, case: Case) -> Seeded:
    with engine.begin() as connection:
        return case.seed(connection, world, datetime.now(UTC))


@pytest.mark.parametrize("name", [n for n, c in CASES.items() if c.not_found is not None])
def test_write_waiting_on_transfer_rechecks_authorization(
    world: RaceWorld, isolation: Callable[[bool], None], name: str
) -> None:
    case = CASES[name]
    isolation(case.isolation)
    seeded = _seed(world, case)
    counts_before = _side_effect_counts(world.ws)

    response, before = _race(world, case, seeded, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, seeded.row_id) == {**before, "owner_id": world.c}
    assert _side_effect_counts(world.ws) == counts_before


def test_accept_waiting_on_evidence_transfer_does_not_grant_it(world: RaceWorld) -> None:
    case = CASES["delegation_accept"]
    seeded = _seed(world, case)

    response, before = _race(world, case, seeded, transfer=True)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    # Neither widened out of `private` nor granted: the delegator (B) lost
    # the incident to C before the grant could be made on B's authority.
    assert row_snapshot(case.table, seeded.row_id) == {**before, "owner_id": world.c}
    assert _side_effect_counts(world.ws)["resource_grants"] == 0


@pytest.mark.parametrize("name", list(CASES))
def test_write_waiting_on_lock_without_transfer_still_proceeds(
    world: RaceWorld, isolation: Callable[[bool], None], name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    denials above come from the transfer, not from the wait itself."""
    case = CASES[name]
    isolation(case.isolation)
    seeded = _seed(world, case)

    response, _ = _race(world, case, seeded, transfer=False)

    assert response.status_code == case.ok_status, response.text
    if name == "delegation_accept":
        assert _side_effect_counts(world.ws)["resource_grants"] == 1
