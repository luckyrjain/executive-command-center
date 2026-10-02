"""Authorization before the idempotency cache, for the knowledge mutations.

Every knowledge write below used to read its idempotency cache right after
taking the membership and idempotency locks, before any authorization of the
row (or parent entity) it writes. A caller who had since lost access --
demoted to `viewer`, or no longer able to see the row after an ownership
transfer -- could replay the same `Idempotency-Key` and get the cached
success back.

Each now reads the cache only after the locked read (404) and write (403)
checks, and before its state/version checks, so a still-authorized replay of
a successful write -- which finds the row already changed -- still gets the
cached response rather than a 409.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from hashlib import sha256
from json import dumps
from typing import Any, Self
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race_world
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.knowledge import resolution
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SIDE_EFFECT_TABLES = ("audit_events", "event_outbox", "idempotency_records")


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Knowledge Idempotent Replay Authz",
        (
            "embedding_projections",
            "retrieval_documents",
            "timeline_entries",
            "knowledge_claims",
            "pkos_edges",
            "entity_operations",
            "resolution_candidates",
            "entity_aliases",
            "pkos_evidence",
            "pkos_nodes",
            "notes",
        ),
    ) as w:
        yield w


Ids = dict[str, UUID]


@dataclass(frozen=True)
class Access:
    """Who owns every seeded row the request authorizes against, and with
    what visibility."""

    owner: UUID
    visibility: str


Seeder = Callable[[Connection, RaceWorld, Access], Ids]


def _now() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=1)


def _seed_node(conn: Connection, w: RaceWorld, a: Access, *, status: str = "active") -> UUID:
    node_id = uuid4()
    now = _now()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, status, owner_id, visibility) "
            "VALUES (:id, :ws, 'person', :name, :now, :now, :status, :owner, :visibility)"
        ),
        {
            "id": node_id,
            "ws": w.ws,
            "name": f"Replay node {node_id}",
            "now": now,
            "status": status,
            "owner": a.owner,
            "visibility": a.visibility,
        },
    )
    return node_id


def _seed_evidence(conn: Connection, w: RaceWorld, a: Access, node_id: UUID) -> UUID:
    evidence_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, source_ref, "
            "sha256, captured_at, owner_id, visibility) "
            "VALUES (:id, :ws, :node, 'manual', 'replay-ref', :sha, :now, :owner, :visibility)"
        ),
        {
            "id": evidence_id,
            "ws": w.ws,
            "node": node_id,
            "sha": "0" * 64,
            "now": _now(),
            "owner": a.owner,
            "visibility": a.visibility,
        },
    )
    return evidence_id


def _note(*, archived: bool) -> Seeder:
    def seed(conn: Connection, w: RaceWorld, a: Access) -> Ids:
        note_id = uuid4()
        now = _now()
        conn.execute(
            text(
                "INSERT INTO notes (id, workspace_id, owner_id, title, body, created_by, "
                "updated_by, created_at, updated_at, visibility, archived_at, "
                "pre_archive_status) "
                "VALUES (:id, :ws, :owner, 'Replay note', 'replay body', :owner, :owner, "
                ":now, :now, :visibility, :archived_at, :pre)"
            ),
            {
                "id": note_id,
                "ws": w.ws,
                "owner": a.owner,
                "now": now,
                "visibility": a.visibility,
                "archived_at": now if archived else None,
                "pre": "active" if archived else None,
            },
        )
        return {"row": note_id}

    return seed


def _node(*, status: str) -> Seeder:
    def seed(conn: Connection, w: RaceWorld, a: Access) -> Ids:
        return {"row": _seed_node(conn, w, a, status=status)}

    return seed


def _claim_world(conn: Connection, w: RaceWorld, a: Access) -> Ids:
    node_id = _seed_node(conn, w, a)
    evidence_id = _seed_evidence(conn, w, a, node_id)
    claim_id = uuid4()
    now = _now()
    conn.execute(
        text(
            "INSERT INTO knowledge_claims (id, workspace_id, subject_id, predicate, "
            "value_json, source_id, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :node, 'role', CAST(:value AS jsonb), :ev, "
            ":now, :now, :owner, :visibility)"
        ),
        {
            "id": claim_id,
            "ws": w.ws,
            "node": node_id,
            "value": '{"v": "old"}',
            "ev": evidence_id,
            "now": now,
            "owner": a.owner,
            "visibility": a.visibility,
        },
    )
    return {"row": node_id, "evidence": evidence_id, "claim": claim_id}


def _relationship_world(conn: Connection, w: RaceWorld, a: Access) -> Ids:
    source = _seed_node(conn, w, a)
    target = _seed_node(conn, w, a)
    return {"source": source, "target": target, "evidence": _seed_evidence(conn, w, a, source)}


def _edge_world(conn: Connection, w: RaceWorld, a: Access) -> Ids:
    ids = _relationship_world(conn, w, a)
    edge_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_edges (id, workspace_id, source_node_id, target_node_id, "
            "edge_type, evidence_id, owner_id, visibility) "
            "VALUES (:id, :ws, :source, :target, 'RELATES_TO', :ev, :owner, :visibility)"
        ),
        {
            "id": edge_id,
            "ws": w.ws,
            "source": ids["source"],
            "target": ids["target"],
            "ev": ids["evidence"],
            "owner": a.owner,
            "visibility": a.visibility,
        },
    )
    return {"row": edge_id}


def _evidence_world(conn: Connection, w: RaceWorld, a: Access) -> Ids:
    node_id = _seed_node(conn, w, a)
    return {"row": _seed_evidence(conn, w, a, node_id)}


def _pair(conn: Connection, w: RaceWorld, a: Access) -> Ids:
    return {"target": _seed_node(conn, w, a), "source": _seed_node(conn, w, a)}


def _operations_world(*, merged: bool, candidate_status: str) -> Seeder:
    """Two entities plus a resolution candidate naming them; with `merged`,
    the source is already redirected and an active merge operation records
    it -- the state `merge_entities` leaves behind when it had nothing to
    rehome (see test_knowledge_operations_mutation_lock_race_postgres)."""

    def seed(conn: Connection, w: RaceWorld, a: Access) -> Ids:
        now = _now()
        target_id = _seed_node(conn, w, a)
        source_id = _seed_node(conn, w, a, status="redirected" if merged else "active")
        candidate_id = uuid4()
        conn.execute(
            text(
                "INSERT INTO resolution_candidates (id, workspace_id, left_entity_id, "
                "right_entity_id, score, factors_json, resolver_version, status, created_at, "
                "owner_id, visibility) "
                "VALUES (:id, :ws, :left, :right, 0.9, CAST('{}' AS jsonb), 'replay-test', "
                ":status, :now, :owner, :visibility)"
            ),
            {
                "id": candidate_id,
                "ws": w.ws,
                "left": target_id,
                "right": source_id,
                "status": candidate_status,
                "now": now,
                "owner": a.owner,
                "visibility": a.visibility,
            },
        )
        ids = {"target": target_id, "source": source_id, "candidate": candidate_id}
        if merged:
            operation_id = uuid4()
            entities = {"source_entity_id": str(source_id), "target_entity_id": str(target_id)}
            conn.execute(
                text(
                    "INSERT INTO entity_operations (id, workspace_id, operation_type, status, "
                    "inputs_json, outputs_json, actor_id, reason, version, created_at, "
                    "updated_at, owner_id, visibility) "
                    "VALUES (:id, :ws, 'merge', 'active', CAST(:inputs AS jsonb), "
                    "CAST(:outputs AS jsonb), :owner, 'replay seed', 1, :now, :now, :owner, "
                    ":visibility)"
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
                    "owner": a.owner,
                    "now": now,
                    "visibility": a.visibility,
                },
            )
            ids["operation"] = operation_id
        return ids

    return seed


@dataclass(frozen=True)
class Case:
    seed: Seeder
    method: str
    path: Callable[[Ids], str]
    body: Callable[[Ids], dict[str, Any]]
    ok_status: int
    # The row handed to C in the lost-read test (table, key into the seeded
    # ids), and the 404 code that answers once B can no longer see it.
    revoke: tuple[str, str]
    not_found: str


_NOTES = "/api/v1/notes/{}"
_ENTITIES = "/api/v1/knowledge/entities/{}"
_OPS = "/api/v1/knowledge/entity-operations/{}"
_CANDIDATES = "/api/v1/knowledge/resolution/candidates"
_REASON = {"reason": "replay probe"}


def _v1(_: Ids) -> dict[str, Any]:
    return {"expected_version": 1}


def _reason(_: Ids) -> dict[str, Any]:
    return _REASON


def _claim_body(ids: Ids) -> dict[str, Any]:
    return {"predicate": "role", "value": {"v": "replay"}, "source_id": str(ids["evidence"])}


def _relationship_body(ids: Ids) -> dict[str, Any]:
    return {
        "relationship_type": "WORKS_ON",
        "to_entity_id": str(ids["target"]),
        "evidence_id": str(ids["evidence"]),
    }


def _merge_body(ids: Ids) -> dict[str, Any]:
    return {
        "candidate_id": str(ids["candidate"]),
        "target_entity_id": str(ids["target"]),
        "expected_target_version": 1,
        "expected_source_version": 1,
        "reason": "replay probe",
    }


def _defer_body(_: Ids) -> dict[str, Any]:
    return {"deferred_until": (datetime.now(UTC) + timedelta(days=7)).isoformat()}


def _note_case(path: str, body: Callable[[Ids], dict[str, Any]], *, archived: bool) -> Case:
    return Case(
        _note(archived=archived),
        "PATCH" if not path else "POST",
        lambda ids: _NOTES.format(ids["row"]) + path,
        body,
        200,
        ("notes", "row"),
        "NOTE_NOT_FOUND",
    )


def _entity_case(path: str, body: Callable[[Ids], dict[str, Any]], *, status: str) -> Case:
    return Case(
        _node(status=status),
        "PATCH" if not path else "POST",
        lambda ids: _ENTITIES.format(ids["row"]) + path,
        body,
        200,
        ("pkos_nodes", "row"),
        "ENTITY_NOT_FOUND",
    )


def _operation_case(action: str, revoke: tuple[str, str], not_found: str) -> Case:
    return Case(
        _operations_world(merged=True, candidate_status="confirmed"),
        "POST",
        lambda ids: _OPS.format(ids["operation"]) + f"/{action}",
        _reason,
        201,
        revoke,
        not_found,
    )


def _candidate_case(
    action: str, body: Callable[[Ids], dict[str, Any]], revoke: tuple[str, str]
) -> Case:
    return Case(
        _operations_world(merged=False, candidate_status="open"),
        "POST",
        lambda ids: f"{_CANDIDATES}/{ids['candidate']}/{action}",
        body,
        200,
        revoke,
        "CANDIDATE_NOT_FOUND",
    )


_CANDIDATE_ROW = ("resolution_candidates", "candidate")
_SOURCE_ENTITY = ("pkos_nodes", "source")

CASES: dict[str, Case] = {
    "note_patch": _note_case(
        "", lambda _: {"expected_version": 1, "title": "replay probe"}, archived=False
    ),
    "note_archive": _note_case("/archive", _v1, archived=False),
    "note_restore": _note_case("/restore", _v1, archived=True),
    "entity_patch": _entity_case(
        "", lambda _: {"expected_version": 1, "canonical_name": "replay probe"}, status="active"
    ),
    "entity_archive": _entity_case("/archive", _v1, status="active"),
    "entity_restore": _entity_case("/restore", _v1, status="archived"),
    "claim_create": Case(
        _claim_world,
        "POST",
        lambda ids: _ENTITIES.format(ids["row"]) + "/claims",
        _claim_body,
        201,
        ("pkos_nodes", "row"),
        "ENTITY_NOT_FOUND",
    ),
    "claim_supersede": Case(
        _claim_world,
        "POST",
        lambda ids: _ENTITIES.format(ids["row"]) + f"/claims/{ids['claim']}/supersede",
        _claim_body,
        201,
        ("pkos_nodes", "row"),
        "ENTITY_NOT_FOUND",
    ),
    "relationship_create_source_lost": Case(
        _relationship_world,
        "POST",
        lambda ids: _ENTITIES.format(ids["source"]) + "/relationships",
        _relationship_body,
        201,
        _SOURCE_ENTITY,
        "ENTITY_NOT_FOUND",
    ),
    "relationship_create_target_lost": Case(
        _relationship_world,
        "POST",
        lambda ids: _ENTITIES.format(ids["source"]) + "/relationships",
        _relationship_body,
        201,
        ("pkos_nodes", "target"),
        "ENTITY_NOT_FOUND",
    ),
    "relationship_invalidate": Case(
        _edge_world,
        "POST",
        lambda ids: f"/api/v1/knowledge/relationships/{ids['row']}/invalidate",
        lambda _: {},
        200,
        ("pkos_edges", "row"),
        "RELATIONSHIP_NOT_FOUND",
    ),
    "evidence_delete": Case(
        _evidence_world,
        "POST",
        lambda ids: f"/api/v1/evidence/{ids['row']}/delete",
        _reason,
        200,
        ("pkos_evidence", "row"),
        "EVIDENCE_NOT_FOUND",
    ),
    "merge": Case(
        _operations_world(merged=False, candidate_status="confirmed"),
        "POST",
        lambda _: "/api/v1/knowledge/entities/merge",
        _merge_body,
        201,
        _SOURCE_ENTITY,
        "ENTITY_NOT_FOUND",
    ),
    "reverse_operation_lost": _operation_case(
        "reverse", ("entity_operations", "operation"), "OPERATION_NOT_FOUND"
    ),
    "reverse_source_lost": _operation_case("reverse", _SOURCE_ENTITY, "ENTITY_NOT_FOUND"),
    "split_operation_lost": _operation_case(
        "split", ("entity_operations", "operation"), "OPERATION_NOT_FOUND"
    ),
    "split_source_lost": _operation_case("split", _SOURCE_ENTITY, "ENTITY_NOT_FOUND"),
    "candidate_create": Case(
        _pair,
        "POST",
        lambda _: _CANDIDATES,
        lambda ids: {"left_entity_id": str(ids["target"]), "right_entity_id": str(ids["source"])},
        201,
        _SOURCE_ENTITY,
        "ENTITY_NOT_FOUND",
    ),
    "candidate_confirm": _candidate_case("confirm", _reason, _CANDIDATE_ROW),
    "candidate_reject": _candidate_case("reject", _reason, _CANDIDATE_ROW),
    "candidate_reject_entity_lost": _candidate_case("reject", _reason, _SOURCE_ENTITY),
    "candidate_defer": _candidate_case("defer", _defer_body, _CANDIDATE_ROW),
}


def _session_token(w: RaceWorld, user_id: UUID) -> str:
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :ws, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "ws": w.ws,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    return token


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


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


def _seed(w: RaceWorld, case: Case, access: Access) -> Ids:
    with engine.begin() as connection:
        return case.seed(connection, w, access)


@pytest.mark.parametrize("name", list(CASES))
def test_still_authorized_replay_after_the_write_landed_gets_the_cached_response(
    world: RaceWorld, name: str
) -> None:
    """The row has moved on (version bumped, archived, superseded, merged,
    reversed, decided...) by the time the replay arrives; the replay must
    still get the original response, not the state check's 409/422."""
    case = CASES[name]
    ids = _seed(world, case, Access(world.b, "private"))
    request_headers = headers(world.b_token)
    body = case.body(ids)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        first = client.request(case.method, case.path(ids), headers=request_headers, json=body)
        assert first.status_code == case.ok_status, first.text
        counts_before = _side_effect_counts(world.ws)
        replay = client.request(case.method, case.path(ids), headers=request_headers, json=body)
    finally:
        client.close()

    assert replay.status_code == case.ok_status, replay.text
    assert _without_request_id(replay.json()) == _without_request_id(first.json())
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_idempotent_replay_after_losing_read_access_is_not_found(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    ids = _seed(world, case, Access(world.b, "private"))
    request_headers = headers(world.b_token)
    body = case.body(ids)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        first = client.request(case.method, case.path(ids), headers=request_headers, json=body)
        assert first.status_code == case.ok_status, first.text

        # The row B wrote through is handed to C and stays private: B (an
        # admin) can no longer see it, so the same key must not replay the
        # success.
        table, key = case.revoke
        with engine.begin() as connection:
            connection.execute(
                text(f"UPDATE {table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                {"c": world.c, "id": ids[key]},
            )
        counts_before = _side_effect_counts(world.ws)
        refused = client.request(case.method, case.path(ids), headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == case.not_found
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_idempotent_replay_after_demotion_to_viewer_is_refused(world: RaceWorld, name: str) -> None:
    """Rows owned by A and workspace-visible: C (`member`) writes them by
    role alone, and once demoted to `viewer` can still read them -- so only
    the write check ahead of the cache refuses the replay. (For merge and
    candidate creation the membership lock's `role_action="write"` already
    refused a viewer ahead of the cache; they stay here as a regression
    guard.)"""
    case = CASES[name]
    ids = _seed(world, case, Access(world.a, "workspace"))
    c_token = _session_token(world, world.c)
    request_headers = headers(c_token)
    body = case.body(ids)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        first = client.request(case.method, case.path(ids), headers=request_headers, json=body)
        assert first.status_code == case.ok_status, first.text

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace_memberships SET role = 'viewer' "
                    "WHERE workspace_id = :ws AND users_id = :c"
                ),
                {"ws": world.ws, "c": world.c},
            )
        counts_before = _side_effect_counts(world.ws)
        refused = client.request(case.method, case.path(ids), headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before


def _clock_at(moment: datetime) -> type[datetime]:
    """A `datetime` whose `now()` reads `moment`, for the module under test."""

    class _Clock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            return cls.fromtimestamp(moment.timestamp(), tz)

    return _Clock


def test_defer_replay_after_deferred_until_has_passed_gets_the_cached_response(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`deferred_until` must be in the future, but that check sits below the
    cache: a same-key replay sent once the requested time has passed still
    gets the cached 200, not 422 DEFER_UNTIL_MUST_BE_FUTURE. A fresh key with
    the same body, on the same moved clock, does get the 422."""
    case = CASES["candidate_defer"]
    ids = _seed(world, case, Access(world.b, "private"))
    deferred_until = datetime.now(UTC) + timedelta(minutes=5)
    body = {"deferred_until": deferred_until.isoformat()}
    path = case.path(ids)
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        first = client.post(path, headers=request_headers, json=body)
        assert first.status_code == 200, first.text

        # The endpoint's clock moves past the requested time.
        monkeypatch.setattr(resolution, "datetime", _clock_at(deferred_until + timedelta(hours=1)))
        counts_before = _side_effect_counts(world.ws)
        replay = client.post(path, headers=request_headers, json=body)
        counts_after_replay = _side_effect_counts(world.ws)
        fresh = client.post(path, headers=headers(world.b_token), json=body)
    finally:
        client.close()

    assert replay.status_code == 200, replay.text
    assert _without_request_id(replay.json()) == _without_request_id(first.json())
    assert counts_after_replay == counts_before
    assert fresh.status_code == 422, fresh.text
    assert fresh.json()["error"]["code"] == "DEFER_UNTIL_MUST_BE_FUTURE"
