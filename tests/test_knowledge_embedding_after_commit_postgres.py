"""Knowledge writes compute embeddings only after their write transaction
commits (ADR-0014: the membership lock is never held across a model call).

`embeddings.embed_after_commit` wraps each write transaction that changes
a retrieval document; `defer_embedding` records the entity inside it, and
once the transaction has committed and released its locks the model runs
with no transaction open, followed by a short, content-hash-guarded upsert
(`embed_committed`). The fake provider below probes Postgres from a
separate connection while `embed()` runs, so the assertions are about the
real lock state, not about call order.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from membership_lock_race_support import (
    Case,
    Ids,
    RaceWorld,
    client_for,
    headers,
    race_world,
    seed,
    seed_nothing,
)
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.knowledge import embeddings
from ecc.domains.knowledge.embeddings import EMBEDDING_DIMENSIONS, embed_committed
from ecc.platform.connector_security import membership_mutation_lock_key

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_TABLES = (
    "knowledge_claims",
    "embedding_projections",
    "retrieval_documents",
    "timeline_entries",
    "pkos_evidence",
    "pkos_nodes",
)
_VECTOR = [0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0]

# Granted or waiting advisory locks on the workspace's membership key, held
# by any backend. A bigint advisory key shows in pg_locks split into
# classid (high 32 bits) and objid (low 32 bits) with objsubid = 1.
_MEMBERSHIP_LOCKS = text(
    "SELECT count(*) FROM pg_locks l, (SELECT hashtextextended(:key, 0) AS h) k "
    "WHERE l.locktype = 'advisory' AND l.objsubid = 1 "
    "AND l.classid::bigint = ((k.h >> 32) & 4294967295) "
    "AND l.objid::bigint = (k.h & 4294967295)"
)
_OPEN_TRANSACTIONS = text(
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND pid <> pg_backend_pid() AND state LIKE 'idle in transaction%'"
)


class ProbingProvider:
    """Records, for every `embed()` call, how many membership advisory
    locks any backend holds for the workspace and how many transactions
    are open in this database, both read from a separate connection."""

    def __init__(self, ws: UUID) -> None:
        self.key = membership_mutation_lock_key(ws)
        self.calls: list[dict[str, int]] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        with engine.connect() as probe:
            self.calls.append(
                {
                    "membership_locks": int(
                        probe.execute(_MEMBERSHIP_LOCKS, {"key": self.key}).scalar_one()
                    ),
                    "open_transactions": int(probe.execute(_OPEN_TRANSACTIONS).scalar_one()),
                }
            )
        return [_VECTOR for _ in texts]


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Knowledge Embedding After Commit", _TABLES) as w:
        try:
            yield w
        finally:
            embeddings.set_provider_for_testing(None)


def _node(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    node_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, status, owner_id, visibility) "
            "VALUES (:id, :ws, 'person', 'Embed node', :now, :now, 'active', :a, 'workspace')"
        ),
        {"id": node_id, "ws": w.ws, "now": now, "a": w.a},
    )
    return node_id


def _evidence(conn: Connection, w: RaceWorld, now: datetime, node_id: UUID) -> UUID:
    evidence_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, source_ref, "
            "sha256, captured_at, owner_id, visibility) "
            "VALUES (:id, :ws, :node, 'manual', 'embed-ref', :sha, :now, :a, 'workspace')"
        ),
        {"id": evidence_id, "ws": w.ws, "node": node_id, "sha": "0" * 64, "now": now, "a": w.a},
    )
    return evidence_id


def _seed_entity(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"id": _node(conn, w, now)}


def _seed_entity_with_evidence(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    node_id = _node(conn, w, now)
    return {"id": node_id, "ev": _evidence(conn, w, now, node_id)}


CASES: dict[str, Case] = {
    "entity_create": Case(
        seed_nothing,
        "POST",
        "/api/v1/knowledge/entities",
        lambda _: {"kind": "topic", "canonical_name": "Embed entity", "summary": "first"},
        201,
    ),
    "entity_patch": Case(
        _seed_entity,
        "PATCH",
        "/api/v1/knowledge/entities/{id}",
        lambda _: {"expected_version": 1, "canonical_name": "Embed renamed"},
        200,
    ),
    "claim_create": Case(
        _seed_entity_with_evidence,
        "POST",
        "/api/v1/knowledge/entities/{id}/claims",
        lambda ids: {"predicate": "role", "value": {"v": "x"}, "source_id": str(ids["ev"])},
        201,
    ),
}


def _send(w: RaceWorld, case: Case, ids: Ids, request_headers: dict[str, str]) -> Any:
    client = client_for(w)
    try:
        return client.request(
            case.method, case.path.format(**ids), headers=request_headers, json=case.body(ids)
        )
    finally:
        client.close()


def _entity_id(name: str, ids: Ids, response: Any) -> UUID:
    return UUID(response.json()["id"]) if name == "entity_create" else ids["id"]


def _projection(ws: UUID, entity_id: UUID) -> dict[str, Any] | None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT e.content_hash, d.title, d.body FROM embedding_projections e "
                    "JOIN retrieval_documents d ON d.id = e.document_id "
                    "WHERE d.workspace_id = :ws AND d.entity_id = :entity_id"
                ),
                {"ws": ws, "entity_id": entity_id},
            )
            .mappings()
            .one_or_none()
        )
    return dict(row) if row is not None else None


def test_probe_detects_a_held_membership_lock(world: RaceWorld) -> None:
    """The lock probe is not vacuous: it sees the membership lock while a
    connection holds it."""
    provider = ProbingProvider(world.ws)
    with engine.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
            {"key": provider.key},
        )
        provider.embed(["probe"])
    assert provider.calls[0]["membership_locks"] == 1
    assert provider.calls[0]["open_transactions"] >= 1


@pytest.mark.parametrize("name", list(CASES))
def test_embedding_runs_after_commit_with_no_lock_held_and_still_lands(
    world: RaceWorld, name: str
) -> None:
    provider = ProbingProvider(world.ws)
    embeddings.set_provider_for_testing(provider)
    case = CASES[name]
    ids = seed(world, case)

    response = _send(world, case, ids, headers(world.b_token))

    assert response.status_code == case.ok_status, response.text
    assert provider.calls == [{"membership_locks": 0, "open_transactions": 0}]
    projection = _projection(world.ws, _entity_id(name, ids, response))
    assert projection is not None
    assert projection["content_hash"] == embeddings._content_hash(
        projection["title"], projection["body"]
    )


def test_idempotent_replay_embeds_nothing_and_answers_the_same(world: RaceWorld) -> None:
    provider = ProbingProvider(world.ws)
    embeddings.set_provider_for_testing(provider)
    case = CASES["entity_create"]
    request_headers = headers(world.b_token)

    first = _send(world, case, {}, request_headers)
    replay = _send(world, case, {}, {**request_headers, "X-Correlation-ID": str(uuid4())})

    assert first.status_code == replay.status_code == 201
    per_request = {"request_id", "correlation_id"}
    assert {k: v for k, v in replay.json().items() if k not in per_request} == {
        k: v for k, v in first.json().items() if k not in per_request
    }
    assert len(provider.calls) == 1


def test_failed_embed_does_not_change_the_response(world: RaceWorld) -> None:
    class _Failing:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("model crashed")

    embeddings.set_provider_for_testing(_Failing())
    response = _send(world, CASES["entity_create"], {}, headers(world.b_token))

    assert response.status_code == 201, response.text
    assert _projection(world.ws, UUID(response.json()["id"])) is None


def test_stale_vector_never_overwrites_a_newer_document(world: RaceWorld) -> None:
    """A write that commits new content while the model is embedding the
    old content makes the post-commit upsert skip (`superseded`); the newer
    write's own post-commit step embeds the newer content."""
    embeddings.set_provider_for_testing(None)
    response = _send(world, CASES["entity_create"], {}, headers(world.b_token))
    entity_id = UUID(response.json()["id"])

    class _ConcurrentWrite:
        def embed(self, texts: list[str]) -> list[list[float]]:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "UPDATE retrieval_documents SET body = 'newer body' "
                        "WHERE workspace_id = :ws AND entity_id = :entity_id"
                    ),
                    {"ws": world.ws, "entity_id": entity_id},
                )
            return [_VECTOR for _ in texts]

    embeddings.set_provider_for_testing(_ConcurrentWrite())
    with SessionFactory() as session:
        result = embed_committed(session, world.ws, entity_id)

    assert (result.written, result.reason) == (False, "superseded")
    assert _projection(world.ws, entity_id) is None
