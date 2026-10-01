"""Knowledge writes against a concurrent member removal or role change
(ADR-0014).

Every write transaction in `knowledge/*` -- entities, claims,
relationships, evidence, notes, entity operations and resolution
candidates -- now takes the shared membership lock first
(`authz.lock_membership_for_write`) and authorizes after it. The creates
whose only role gate ran before the transaction (entity create, and the
identity person/organization wrappers over it; note create; entity merge;
resolution-candidate create) re-check the role in-transaction
(`role_action="write"`). A removal or demotion holding the lock makes the
write wait, and once it commits the write answers 403/404 and writes
nothing. See `membership_lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from membership_lock_race_support import (
    ROLE_GATED,
    Case,
    Change,
    Ids,
    RaceWorld,
    Seed,
    assert_proceeds,
    assert_refused,
    race_world,
    refusal_params,
    row_refusals,
    seed,
    seed_nothing,
)
from sqlalchemy import Connection, text

from ecc.config import get_settings

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SEEDED_TABLES = (
    "knowledge_claims",
    "embedding_projections",
    "retrieval_documents",
    "timeline_entries",
    "entity_operations",
    "resolution_candidates",
    "entity_aliases",
    "pkos_edges",
    "pkos_evidence",
    "pkos_nodes",
    "notes",
)
_WRITE_TABLES = (*_SEEDED_TABLES, "audit_events", "event_outbox", "idempotency_records")


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Knowledge Membership Race", _SEEDED_TABLES) as w:
        yield w


def _node(conn: Connection, w: RaceWorld, now: datetime, *, status: str = "active") -> UUID:
    node_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, status, owner_id, visibility) "
            "VALUES (:id, :ws, 'person', :name, :now, :now, :status, :a, 'workspace')"
        ),
        {
            "id": node_id,
            "ws": w.ws,
            "name": f"Race node {node_id}",
            "now": now,
            "status": status,
            "a": w.a,
        },
    )
    return node_id


def _evidence(conn: Connection, w: RaceWorld, now: datetime, node_id: UUID) -> UUID:
    evidence_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, source_ref, "
            "sha256, captured_at, owner_id, visibility) "
            "VALUES (:id, :ws, :node, 'manual', 'race-ref', :sha, :now, :a, 'workspace')"
        ),
        {"id": evidence_id, "ws": w.ws, "node": node_id, "sha": "0" * 64, "now": now, "a": w.a},
    )
    return evidence_id


def _seed_entity(status: str) -> Seed:
    def seed_entity(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
        return {"id": _node(conn, w, now, status=status)}

    return seed_entity


def _seed_claim(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    node_id = _node(conn, w, now)
    evidence_id = _evidence(conn, w, now, node_id)
    claim_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO knowledge_claims (id, workspace_id, subject_id, predicate, "
            "value_json, source_id, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :node, 'role', CAST(:value AS jsonb), :ev, "
            ":now, :now, :a, 'workspace')"
        ),
        {
            "id": claim_id,
            "ws": w.ws,
            "node": node_id,
            "value": '{"v": "old"}',
            "ev": evidence_id,
            "now": now,
            "a": w.a,
        },
    )
    return {"id": node_id, "ev": evidence_id, "claim": claim_id}


def _seed_relationship_ends(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    source = _node(conn, w, now)
    target = _node(conn, w, now)
    return {"id": source, "to": target, "ev": _evidence(conn, w, now, source)}


def _seed_edge(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    ends = _seed_relationship_ends(conn, w, now)
    edge_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_edges (id, workspace_id, source_node_id, target_node_id, "
            "edge_type, evidence_id, owner_id, visibility) "
            "VALUES (:id, :ws, :source, :target, 'RELATES_TO', :ev, :a, 'workspace')"
        ),
        {
            "id": edge_id,
            "ws": w.ws,
            "source": ends["id"],
            "target": ends["to"],
            "ev": ends["ev"],
            "a": w.a,
        },
    )
    return {"id": edge_id}


def _seed_evidence(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"id": _evidence(conn, w, now, _node(conn, w, now))}


def _seed_note(archived: bool) -> Seed:
    def seed_note(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
        note_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO notes (id, workspace_id, owner_id, title, body, created_by, "
                "updated_by, created_at, updated_at, visibility, archived_at, "
                "pre_archive_status) "
                "VALUES (:id, :ws, :a, 'Race note', 'race body', :a, :a, :now, :now, "
                "'workspace', :archived_at, :pre)"
            ),
            {
                "id": note_id,
                "ws": w.ws,
                "a": w.a,
                "now": now,
                "archived_at": now if archived else None,
                "pre": "active" if archived else None,
            },
        )
        return {"id": note_id}

    return seed_note


def _seed_pair(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"left": _node(conn, w, now), "right": _node(conn, w, now)}


def _seed_candidate(*, status: str, merged: bool) -> Seed:
    """Two workspace entities owned by A and a resolution candidate naming
    them; with `merged`, the source is already redirected and an active
    merge operation (owned by A) records it -- the state `merge_entities`
    leaves behind when it had nothing to rehome."""

    def seed_candidate(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
        now -= timedelta(minutes=1)
        target = _node(conn, w, now)
        source = _node(conn, w, now, status="redirected" if merged else "active")
        candidate_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO resolution_candidates (id, workspace_id, left_entity_id, "
                "right_entity_id, score, factors_json, resolver_version, status, created_at, "
                "owner_id, visibility) "
                "VALUES (:id, :ws, :left, :right, 0.9, CAST('{}' AS jsonb), 'race-test', "
                ":status, :now, :a, 'workspace')"
            ),
            {
                "id": candidate_id,
                "ws": w.ws,
                "left": target,
                "right": source,
                "status": status,
                "now": now,
                "a": w.a,
            },
        )
        ids = {"id": candidate_id, "target": target, "source": source}
        if merged:
            operation_id = uuid4()
            entities = {"source_entity_id": str(source), "target_entity_id": str(target)}
            conn.execute(
                text(
                    "INSERT INTO entity_operations (id, workspace_id, operation_type, status, "
                    "inputs_json, outputs_json, actor_id, reason, version, created_at, "
                    "updated_at, owner_id, visibility) "
                    "VALUES (:id, :ws, 'merge', 'active', CAST(:inputs AS jsonb), "
                    "CAST(:outputs AS jsonb), :a, 'race seed', 1, :now, :now, :a, 'workspace')"
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
                    "a": w.a,
                    "now": now,
                },
            )
            ids["id"] = operation_id
        return ids

    return seed_candidate


def _v1(_: Ids) -> dict[str, Any]:
    return {"expected_version": 1}


def _reason(_: Ids) -> dict[str, Any]:
    return {"reason": "race probe"}


def _claim_body(ids: Ids) -> dict[str, Any]:
    return {"predicate": "role", "value": {"v": "race"}, "source_id": str(ids["ev"])}


def _merge_body(ids: Ids) -> dict[str, Any]:
    return {
        "candidate_id": str(ids["id"]),
        "target_entity_id": str(ids["target"]),
        "expected_target_version": 1,
        "expected_source_version": 1,
        "reason": "race probe",
    }


_ENTITY_ROW = row_refusals("ENTITY_NOT_FOUND")
_NOTE_ROW = row_refusals("NOTE_NOT_FOUND")
_OPERATION_ROW = row_refusals("OPERATION_NOT_FOUND")
_CANDIDATE_ROW = row_refusals("CANDIDATE_NOT_FOUND")
_ENTITIES = "/api/v1/knowledge/entities"
_NOTES = "/api/v1/notes"
_CANDIDATES = "/api/v1/knowledge/resolution/candidates"
_OPERATIONS = "/api/v1/knowledge/entity-operations"
_OPEN_CANDIDATE = _seed_candidate(status="open", merged=False)
_MERGED = _seed_candidate(status="confirmed", merged=True)

CASES: dict[str, Case] = {
    "entity_create": Case(
        seed_nothing,
        "POST",
        _ENTITIES,
        lambda _: {"kind": "topic", "canonical_name": "Race entity"},
        201,
        ROLE_GATED,
    ),
    "person_create": Case(
        seed_nothing,
        "POST",
        "/api/v1/identity/people",
        lambda _: {"canonical_name": "Race person"},
        201,
        ROLE_GATED,
    ),
    "entity_patch": Case(
        _seed_entity("active"),
        "PATCH",
        _ENTITIES + "/{id}",
        lambda _: {"expected_version": 1, "canonical_name": "Race probe"},
        200,
        _ENTITY_ROW,
    ),
    "entity_archive": Case(
        _seed_entity("active"), "POST", _ENTITIES + "/{id}/archive", _v1, 200, _ENTITY_ROW
    ),
    "entity_restore": Case(
        _seed_entity("archived"), "POST", _ENTITIES + "/{id}/restore", _v1, 200, _ENTITY_ROW
    ),
    "claim_create": Case(
        _seed_claim, "POST", _ENTITIES + "/{id}/claims", _claim_body, 201, _ENTITY_ROW
    ),
    "claim_supersede": Case(
        _seed_claim,
        "POST",
        _ENTITIES + "/{id}/claims/{claim}/supersede",
        _claim_body,
        201,
        _ENTITY_ROW,
    ),
    "relationship_create": Case(
        _seed_relationship_ends,
        "POST",
        _ENTITIES + "/{id}/relationships",
        lambda ids: {
            "relationship_type": "RELATES_TO",
            "to_entity_id": str(ids["to"]),
            "evidence_id": str(ids["ev"]),
        },
        201,
        _ENTITY_ROW,
    ),
    "relationship_invalidate": Case(
        _seed_edge,
        "POST",
        "/api/v1/knowledge/relationships/{id}/invalidate",
        lambda _: {},
        200,
        row_refusals("RELATIONSHIP_NOT_FOUND"),
    ),
    "evidence_delete": Case(
        _seed_evidence,
        "POST",
        "/api/v1/evidence/{id}/delete",
        _reason,
        200,
        row_refusals("EVIDENCE_NOT_FOUND"),
    ),
    "note_create": Case(
        seed_nothing, "POST", _NOTES, lambda _: {"body": "Race note"}, 201, ROLE_GATED
    ),
    "note_patch": Case(
        _seed_note(False),
        "PATCH",
        _NOTES + "/{id}",
        lambda _: {"expected_version": 1, "title": "Race probe"},
        200,
        _NOTE_ROW,
    ),
    "note_archive": Case(_seed_note(False), "POST", _NOTES + "/{id}/archive", _v1, 200, _NOTE_ROW),
    "note_restore": Case(_seed_note(True), "POST", _NOTES + "/{id}/restore", _v1, 200, _NOTE_ROW),
    "entity_merge": Case(
        _seed_candidate(status="confirmed", merged=False),
        "POST",
        _ENTITIES + "/merge",
        _merge_body,
        201,
        ROLE_GATED,
    ),
    "operation_reverse": Case(
        _MERGED, "POST", _OPERATIONS + "/{id}/reverse", _reason, 201, _OPERATION_ROW
    ),
    "operation_split": Case(
        _MERGED, "POST", _OPERATIONS + "/{id}/split", _reason, 201, _OPERATION_ROW
    ),
    "candidate_create": Case(
        _seed_pair,
        "POST",
        _CANDIDATES,
        lambda ids: {"left_entity_id": str(ids["left"]), "right_entity_id": str(ids["right"])},
        201,
        ROLE_GATED,
    ),
    "candidate_confirm": Case(
        _OPEN_CANDIDATE, "POST", _CANDIDATES + "/{id}/confirm", _reason, 200, _CANDIDATE_ROW
    ),
    "candidate_reject": Case(
        _OPEN_CANDIDATE, "POST", _CANDIDATES + "/{id}/reject", _reason, 200, _CANDIDATE_ROW
    ),
    "candidate_defer": Case(
        _OPEN_CANDIDATE,
        "POST",
        _CANDIDATES + "/{id}/defer",
        lambda _: {"deferred_until": (datetime.now(UTC) + timedelta(days=7)).isoformat()},
        200,
        _CANDIDATE_ROW,
    ),
}


@pytest.mark.parametrize(("name", "change"), refusal_params(CASES))
def test_write_waiting_on_membership_lock_rechecks_authorization(
    world: RaceWorld, name: str, change: Change
) -> None:
    case = CASES[name]
    assert_refused(world, case, seed(world, case), change, _WRITE_TABLES)


@pytest.mark.parametrize("name", list(CASES))
def test_write_waiting_on_membership_lock_without_change_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    assert_proceeds(world, case, seed(world, case))
