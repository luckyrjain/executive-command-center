"""Lock-before-authorize sweep for the core knowledge mutations: note PATCH
and archive/restore, entity PATCH and archive/restore, relationship
invalidate and evidence delete authorized the caller *before* taking
`SELECT ... FOR UPDATE` on the row they then write. An ownership transfer
(locks the row, rewrites `owner_id`, does not bump `version`) that committed
while the request waited on that row lock left the request writing to a row
the caller could no longer see.

Claim create/supersede authorize against the subject entity (`pkos_nodes`)
but never locked it at all, so a transfer of the entity could commit between
the check and the write. They now lock the entity row first too.

Now each row is locked first and authorization is evaluated afterwards, on
the committed post-transfer row, so the waiting request answers 404 and
writes nothing. See `lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from lock_race_support import RaceWorld, headers, race, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SIDE_EFFECT_TABLES = (
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "knowledge_claims",
    "timeline_entries",
    "retrieval_documents",
    "embedding_projections",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Knowledge Core Lock Race",
        (
            "knowledge_claims",
            "timeline_entries",
            "retrieval_documents",
            "embedding_projections",
            "pkos_edges",
            "pkos_evidence",
            "pkos_nodes",
            "notes",
        ),
    ) as w:
        yield w


@dataclass(frozen=True)
class Seeded:
    """The row the race locks (and, for `transfer=True`, re-owns to C) plus
    the ids the request path needs."""

    row_id: UUID
    evidence_id: UUID | None = None
    claim_id: UUID | None = None


def _seed_note(conn: Connection, w: RaceWorld, now: datetime, *, archived: bool) -> UUID:
    note_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO notes (id, workspace_id, owner_id, title, body, created_by, "
            "updated_by, created_at, updated_at, visibility, archived_at, pre_archive_status) "
            "VALUES (:id, :ws, :b, 'Race note', 'race body', :b, :b, :now, :now, "
            "'private', :archived_at, :pre)"
        ),
        {
            "id": note_id,
            "ws": w.ws,
            "b": w.b,
            "now": now,
            "archived_at": now if archived else None,
            "pre": "active" if archived else None,
        },
    )
    return note_id


def _seed_node(
    conn: Connection,
    w: RaceWorld,
    now: datetime,
    *,
    status: str = "active",
    visibility: str = "private",
) -> UUID:
    node_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, status, owner_id, visibility) "
            "VALUES (:id, :ws, 'person', :name, :now, :now, :status, :b, :visibility)"
        ),
        {
            "id": node_id,
            "ws": w.ws,
            "name": f"Race node {node_id}",
            "now": now,
            "status": status,
            "b": w.b,
            "visibility": visibility,
        },
    )
    return node_id


def _seed_evidence(
    conn: Connection, w: RaceWorld, now: datetime, node_id: UUID, *, visibility: str
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


def _seed_claim(
    conn: Connection, w: RaceWorld, now: datetime, node_id: UUID, evidence_id: UUID
) -> UUID:
    claim_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO knowledge_claims (id, workspace_id, subject_id, predicate, "
            "value_json, source_id, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :node, 'role', CAST(:value AS jsonb), :ev, "
            ":now, :now, :b, 'private')"
        ),
        {
            "id": claim_id,
            "ws": w.ws,
            "node": node_id,
            "value": '{"v": "old"}',
            "ev": evidence_id,
            "now": now,
            "b": w.b,
        },
    )
    return claim_id


Seeder = Callable[[Connection, RaceWorld, datetime], Seeded]


def _note(archived: bool) -> Seeder:
    def seed(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
        return Seeded(_seed_note(conn, w, now, archived=archived))

    return seed


def _node(status: str) -> Seeder:
    def seed(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
        return Seeded(_seed_node(conn, w, now, status=status))

    return seed


def _seed_edge_world(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    source = _seed_node(conn, w, now, visibility="workspace")
    target = _seed_node(conn, w, now, visibility="workspace")
    evidence_id = _seed_evidence(conn, w, now, source, visibility="workspace")
    edge_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_edges (id, workspace_id, source_node_id, target_node_id, "
            "edge_type, evidence_id, owner_id, visibility) "
            "VALUES (:id, :ws, :source, :target, 'RELATES_TO', :ev, :b, 'private')"
        ),
        {
            "id": edge_id,
            "ws": w.ws,
            "source": source,
            "target": target,
            "ev": evidence_id,
            "b": w.b,
        },
    )
    return Seeded(edge_id)


def _seed_evidence_world(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    node_id = _seed_node(conn, w, now, visibility="workspace")
    return Seeded(_seed_evidence(conn, w, now, node_id, visibility="private"))


def _seed_claim_world(conn: Connection, w: RaceWorld, now: datetime) -> Seeded:
    """The raced row is the claim's subject entity (the authorization
    boundary), not the claim."""
    node_id = _seed_node(conn, w, now)
    evidence_id = _seed_evidence(conn, w, now, node_id, visibility="workspace")
    claim_id = _seed_claim(conn, w, now, node_id, evidence_id)
    return Seeded(node_id, evidence_id=evidence_id, claim_id=claim_id)


def _claim_body(s: Seeded) -> dict[str, Any]:
    return {"predicate": "role", "value": {"v": "race"}, "source_id": str(s.evidence_id)}


def _v1(_: Seeded) -> dict[str, Any]:
    return {"expected_version": 1}


def _version_bumped(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return bool(after["version"] == before["version"] + 1)


def _unchanged(before: dict[str, Any], after: dict[str, Any]) -> bool:
    return before == after


@dataclass(frozen=True)
class Case:
    table: str
    seed: Seeder
    method: str
    path: Callable[[Seeded], str]
    body: Callable[[Seeded], dict[str, Any]]
    not_found: str
    ok_status: int
    # Control case: how the raced row looks once the write really landed.
    landed: Callable[[dict[str, Any], dict[str, Any]], bool]


_NOTES = "/api/v1/notes/{}"
_ENTITIES = "/api/v1/knowledge/entities/{}"

CASES: dict[str, Case] = {
    "note_patch": Case(
        "notes",
        _note(False),
        "PATCH",
        lambda s: _NOTES.format(s.row_id),
        lambda _: {"expected_version": 1, "title": "race probe"},
        "NOTE_NOT_FOUND",
        200,
        _version_bumped,
    ),
    "note_archive": Case(
        "notes",
        _note(False),
        "POST",
        lambda s: _NOTES.format(s.row_id) + "/archive",
        _v1,
        "NOTE_NOT_FOUND",
        200,
        _version_bumped,
    ),
    "note_restore": Case(
        "notes",
        _note(True),
        "POST",
        lambda s: _NOTES.format(s.row_id) + "/restore",
        _v1,
        "NOTE_NOT_FOUND",
        200,
        _version_bumped,
    ),
    "entity_patch": Case(
        "pkos_nodes",
        _node("active"),
        "PATCH",
        lambda s: _ENTITIES.format(s.row_id),
        lambda _: {"expected_version": 1, "canonical_name": "race probe"},
        "ENTITY_NOT_FOUND",
        200,
        _version_bumped,
    ),
    "entity_archive": Case(
        "pkos_nodes",
        _node("active"),
        "POST",
        lambda s: _ENTITIES.format(s.row_id) + "/archive",
        _v1,
        "ENTITY_NOT_FOUND",
        200,
        _version_bumped,
    ),
    "entity_restore": Case(
        "pkos_nodes",
        _node("archived"),
        "POST",
        lambda s: _ENTITIES.format(s.row_id) + "/restore",
        _v1,
        "ENTITY_NOT_FOUND",
        200,
        _version_bumped,
    ),
    "relationship_invalidate": Case(
        "pkos_edges",
        _seed_edge_world,
        "POST",
        lambda s: f"/api/v1/knowledge/relationships/{s.row_id}/invalidate",
        lambda _: {},
        "RELATIONSHIP_NOT_FOUND",
        200,
        lambda _before, after: after["status"] == "invalidated",
    ),
    "evidence_delete": Case(
        "pkos_evidence",
        _seed_evidence_world,
        "POST",
        lambda s: f"/api/v1/evidence/{s.row_id}/delete",
        lambda _: {"reason": "race probe"},
        "EVIDENCE_NOT_FOUND",
        200,
        lambda _before, after: after["evidence_state"] == "deleted",
    ),
    "claim_create": Case(
        "pkos_nodes",
        _seed_claim_world,
        "POST",
        lambda s: _ENTITIES.format(s.row_id) + "/claims",
        _claim_body,
        "ENTITY_NOT_FOUND",
        201,
        _unchanged,
    ),
    "claim_supersede": Case(
        "pkos_nodes",
        _seed_claim_world,
        "POST",
        lambda s: _ENTITIES.format(s.row_id) + f"/claims/{s.claim_id}/supersede",
        _claim_body,
        "ENTITY_NOT_FOUND",
        201,
        _unchanged,
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


def _claim_snapshot(s: Seeded) -> dict[str, Any] | None:
    return row_snapshot("knowledge_claims", s.claim_id) if s.claim_id is not None else None


def _run(w: RaceWorld, case: Case, s: Seeded, *, transfer: bool) -> tuple[Any, Any]:
    return race(
        w,
        table=case.table,
        row_id=s.row_id,
        send=lambda client: client.request(
            case.method, case.path(s), headers=headers(w.b_token), json=case.body(s)
        ),
        transfer=transfer,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        seeded = case.seed(connection, world, datetime.now(UTC))
    counts_before = _side_effect_counts(world.ws)
    claim_before = _claim_snapshot(seeded)

    response, before = _run(world, case, seeded, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, seeded.row_id) == {**before, "owner_id": world.c}
    assert _claim_snapshot(seeded) == claim_before
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    case = CASES[name]
    with engine.begin() as connection:
        seeded = case.seed(connection, world, datetime.now(UTC))
    claims_before = _side_effect_counts(world.ws)["knowledge_claims"]

    response, before = _run(world, case, seeded, transfer=False)

    assert response.status_code == case.ok_status, response.text
    assert case.landed(before, row_snapshot(case.table, seeded.row_id))
    if seeded.claim_id is not None:
        assert _side_effect_counts(world.ws)["knowledge_claims"] == claims_before + 1
    if name == "claim_supersede":
        superseded = _claim_snapshot(seeded)
        assert superseded is not None
        assert str(superseded["superseded_by"]) == response.json()["id"]
