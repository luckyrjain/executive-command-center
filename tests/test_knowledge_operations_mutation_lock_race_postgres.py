"""Lock-before-authorize sweep for knowledge entity operations and
resolution candidates: merge/reverse/split and candidate confirm/reject/defer
authorized the caller *before* taking `SELECT ... FOR UPDATE` on the rows
they then write. An ownership transfer (locks the row, rewrites `owner_id`,
does not bump `version`) that committed while the request waited on that row
lock left the request writing to a row the caller could no longer see.

Now every involved row is locked first (merge/reverse/split keep their
existing operation-row-then-sorted-entity lock order) and authorization is
evaluated afterwards, on the committed post-transfer rows, so the waiting
request answers 404 and writes nothing. For the multi-row operations one
transferred row is enough to show the bug, so reverse/split are raced twice:
once on the operation row and once on the merged-away source entity.
See `lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race, race_world, row_snapshot
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
    "entity_operations",
    "timeline_entries",
    "retrieval_documents",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Knowledge Operations Lock Race",
        (
            "embedding_projections",
            "retrieval_documents",
            "timeline_entries",
            "entity_operations",
            "resolution_candidates",
            "entity_aliases",
            "pkos_nodes",
        ),
    ) as w:
        yield w


@dataclass(frozen=True)
class Seeded:
    target_id: UUID
    source_id: UUID
    candidate_id: UUID
    operation_id: UUID | None


def _seed_node(conn: Connection, w: RaceWorld, now: datetime, name: str, status: str) -> UUID:
    node_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, status, owner_id, visibility) "
            "VALUES (:id, :ws, 'person', :name, :now, :now, :status, :b, 'private')"
        ),
        {"id": node_id, "ws": w.ws, "name": name, "now": now, "status": status, "b": w.b},
    )
    return node_id


def _seed(conn: Connection, w: RaceWorld, *, merged: bool, candidate_status: str) -> Seeded:
    """Two private entities owned by B plus a private resolution candidate
    naming them; with `merged`, the source is already redirected and a
    private, active merge operation row (owned by B) records it -- the state
    `merge_entities` leaves behind when it had nothing to rehome."""
    now = datetime.now(UTC) - timedelta(minutes=1)
    target_id = _seed_node(conn, w, now, "Race Target", "active")
    source_id = _seed_node(conn, w, now, "Race Source", "redirected" if merged else "active")
    candidate_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO resolution_candidates (id, workspace_id, left_entity_id, "
            "right_entity_id, score, factors_json, resolver_version, status, created_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :left, :right, 0.9, CAST('{}' AS jsonb), 'race-test', "
            ":status, :now, :b, 'private')"
        ),
        {
            "id": candidate_id,
            "ws": w.ws,
            "left": target_id,
            "right": source_id,
            "status": candidate_status,
            "now": now,
            "b": w.b,
        },
    )
    operation_id = None
    if merged:
        operation_id = uuid4()
        entities = {"source_entity_id": str(source_id), "target_entity_id": str(target_id)}
        conn.execute(
            text(
                "INSERT INTO entity_operations (id, workspace_id, operation_type, status, "
                "inputs_json, outputs_json, actor_id, reason, version, created_at, "
                "updated_at, owner_id, visibility) "
                "VALUES (:id, :ws, 'merge', 'active', CAST(:inputs AS jsonb), "
                "CAST(:outputs AS jsonb), :b, 'race seed', 1, :now, :now, :b, 'private')"
            ),
            {
                "id": operation_id,
                "ws": w.ws,
                "inputs": dumps({"candidate_id": str(candidate_id), **entities}),
                "outputs": dumps(
                    {
                        **entities,
                        "rehomed_alias_ids": [],
                        "rehomed_edge_ids": [],
                        "invalidated_edge_ids": [],
                    }
                ),
                "b": w.b,
                "now": now,
            },
        )
    return Seeded(target_id, source_id, candidate_id, operation_id)


_RACED_TABLE = {
    "operation": "entity_operations",
    "source": "pkos_nodes",
    "candidate": "resolution_candidates",
}


@dataclass(frozen=True)
class Case:
    merged: bool
    candidate_status: str
    raced: str  # key of _RACED_TABLE
    path: str
    body: dict[str, Any]
    ok_status: int
    not_found: str

    @property
    def table(self) -> str:
        return _RACED_TABLE[self.raced]

    def raced_id(self, s: Seeded) -> UUID:
        if self.raced == "operation":
            assert s.operation_id is not None
            return s.operation_id
        return s.source_id if self.raced == "source" else s.candidate_id

    def url(self, s: Seeded) -> str:
        return self.path.format(op=s.operation_id, cand=s.candidate_id)

    def payload(self, s: Seeded) -> dict[str, Any]:
        if self.path == _MERGE:
            return {
                "candidate_id": str(s.candidate_id),
                "target_entity_id": str(s.target_id),
                "expected_target_version": 1,
                "expected_source_version": 1,
                "reason": "race probe",
            }
        return self.body


_REASON = {"reason": "race probe"}
_MERGE = "/api/v1/knowledge/entities/merge"
_REVERSE = "/api/v1/knowledge/entity-operations/{op}/reverse"
_SPLIT = "/api/v1/knowledge/entity-operations/{op}/split"
_CANDIDATE = "/api/v1/knowledge/resolution/candidates/{cand}"
_DEFER = {"deferred_until": (datetime.now(UTC) + timedelta(days=7)).isoformat()}

CASES: dict[str, Case] = {
    "merge_source": Case(False, "confirmed", "source", _MERGE, {}, 201, "ENTITY_NOT_FOUND"),
    "merge_candidate": Case(
        False, "confirmed", "candidate", _MERGE, {}, 201, "CANDIDATE_NOT_FOUND"
    ),
    "reverse_operation": Case(
        True, "confirmed", "operation", _REVERSE, _REASON, 201, "OPERATION_NOT_FOUND"
    ),
    "reverse_source": Case(True, "confirmed", "source", _REVERSE, _REASON, 201, "ENTITY_NOT_FOUND"),
    "split_operation": Case(
        True, "confirmed", "operation", _SPLIT, _REASON, 201, "OPERATION_NOT_FOUND"
    ),
    "split_source": Case(True, "confirmed", "source", _SPLIT, _REASON, 201, "ENTITY_NOT_FOUND"),
    "candidate_confirm": Case(
        False, "open", "candidate", _CANDIDATE + "/confirm", _REASON, 200, "CANDIDATE_NOT_FOUND"
    ),
    "candidate_reject": Case(
        False, "open", "candidate", _CANDIDATE + "/reject", _REASON, 200, "CANDIDATE_NOT_FOUND"
    ),
    "candidate_defer": Case(
        False, "open", "candidate", _CANDIDATE + "/defer", _DEFER, 200, "CANDIDATE_NOT_FOUND"
    ),
}


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


def _involved(s: Seeded) -> list[tuple[str, UUID]]:
    rows = [
        ("pkos_nodes", s.target_id),
        ("pkos_nodes", s.source_id),
        ("resolution_candidates", s.candidate_id),
    ]
    if s.operation_id is not None:
        rows.append(("entity_operations", s.operation_id))
    return rows


def _seed_case(w: RaceWorld, case: Case) -> Seeded:
    with engine.begin() as connection:
        return _seed(connection, w, merged=case.merged, candidate_status=case.candidate_status)


def _run(w: RaceWorld, case: Case, s: Seeded, *, transfer: bool) -> tuple[Any, Any]:
    return race(
        w,
        table=case.table,
        row_id=case.raced_id(s),
        send=lambda client: client.post(
            case.url(s), headers=headers(w.b_token), json=case.payload(s)
        ),
        transfer=transfer,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    seeded = _seed_case(world, case)
    raced = (case.table, case.raced_id(seeded))
    others_before = {row: row_snapshot(*row) for row in _involved(seeded) if row != raced}
    counts_before = _side_effect_counts(world.ws)

    response, before = _run(world, case, seeded, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(*raced) == {**before, "owner_id": world.c}
    assert {row: row_snapshot(*row) for row in others_before} == others_before
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    case = CASES[name]
    seeded = _seed_case(world, case)
    counts_before = _side_effect_counts(world.ws)

    response, _ = _run(world, case, seeded, transfer=False)

    assert response.status_code == case.ok_status, response.text
    assert _side_effect_counts(world.ws)["audit_events"] > counts_before["audit_events"]


@pytest.mark.parametrize("candidate_status", ["open", "confirmed"])
def test_merge_with_unreadable_candidate_is_not_found(
    world: RaceWorld, candidate_status: str
) -> None:
    """Without any race: a private candidate owned by someone else is
    invisible to B, so merging through it answers the same 404 as a missing
    candidate -- not CANDIDATE_NOT_CONFIRMED (which would reveal an open
    candidate's state) and not a merge of B's own entities (which would let
    B use a confirmation B cannot see)."""
    seeded = _seed_case(world, Case(False, candidate_status, "candidate", _MERGE, {}, 201, ""))
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE resolution_candidates SET owner_id = :c WHERE id = :id"),
            {"c": world.c, "id": seeded.candidate_id},
        )
    involved_before = {row: row_snapshot(*row) for row in _involved(seeded)}
    counts_before = _side_effect_counts(world.ws)

    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        response = client.post(
            _MERGE,
            headers=headers(world.b_token),
            json=CASES["merge_candidate"].payload(seeded),
        )
    finally:
        client.close()

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "CANDIDATE_NOT_FOUND"
    assert {row: row_snapshot(*row) for row in involved_before} == involved_before
    assert _side_effect_counts(world.ws) == counts_before
