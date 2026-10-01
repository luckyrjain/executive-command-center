"""Claim create, claim supersede and relationship create lock the entity row
(`pkos_nodes`) because it is their authorization boundary: the lock holds an
ownership transfer (`SELECT ... FOR UPDATE` in `authz_grants`) off until they
commit. They never write the entity row itself, only child rows that
reference it, so the lock they take must conflict with that transfer lock
but not with the `FOR KEY SHARE` every foreign-key child insert takes on the
entity.

With `FOR UPDATE` it did conflict, and that closed a lock cycle with evidence
deletion: the request holds the entity lock and waits to `FOR KEY SHARE` the
evidence row its new claim/edge cites, while `delete_evidence` holds that
evidence row `FOR UPDATE` and then inserts a child row of the same entity
(its projection refresh), waiting on the entity lock. Postgres breaks the
cycle by aborting one side with a deadlock error.

The test reproduces that interleaving for real: a separate connection holds
the evidence row `FOR UPDATE` (what `delete_evidence` holds), the request
blocks citing it, and the holder then inserts a timeline entry for the entity
(a child-row insert, like the projection refresh). That insert must go
through while the request is still waiting.

`test_entity_lock_still_blocks_an_ownership_transfer` pins the other
direction: the lock must still hold the transfer off.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race_world
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_WAIT_SECONDS = 15
_ENTITIES = "/api/v1/knowledge/entities"


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Knowledge Child Write Lock Mode",
        (
            "knowledge_claims",
            "timeline_entries",
            "retrieval_documents",
            "embedding_projections",
            "pkos_edges",
            "pkos_evidence",
            "pkos_nodes",
        ),
    ) as w:
        yield w


@dataclass(frozen=True)
class Seeded:
    entity_id: UUID
    evidence_id: UUID
    other_entity_id: UUID
    claim_id: UUID


def _node(conn: Connection, w: RaceWorld, now: datetime, name: str, kind: str) -> UUID:
    node_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'person', :name, :now, :now, :b, 'private')"
        ),
        {"id": node_id, "ws": w.ws, "name": name, "now": now, "b": w.b},
    )
    return node_id


def _seed(w: RaceWorld) -> Seeded:
    now = datetime.now(UTC)
    evidence_id, claim_id = uuid4(), uuid4()
    with engine.begin() as conn:
        entity_id = _node(conn, w, now, "Child Write Subject", "person")
        other_id = _node(conn, w, now, "Child Write Target", "project")
        conn.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at, owner_id, visibility) "
                "VALUES (:id, :ws, :node, 'manual', 'race-ref', :sha, :now, :b, 'workspace')"
            ),
            {
                "id": evidence_id,
                "ws": w.ws,
                "node": entity_id,
                "sha": "0" * 64,
                "now": now,
                "b": w.b,
            },
        )
        conn.execute(
            text(
                "INSERT INTO knowledge_claims (id, workspace_id, subject_id, predicate, "
                "value_json, source_id, created_at, updated_at, owner_id, visibility) "
                "VALUES (:id, :ws, :node, 'role', CAST('{\"v\": \"old\"}' AS jsonb), :ev, "
                ":now, :now, :b, 'private')"
            ),
            {
                "id": claim_id,
                "ws": w.ws,
                "node": entity_id,
                "ev": evidence_id,
                "now": now,
                "b": w.b,
            },
        )
    return Seeded(entity_id, evidence_id, other_id, claim_id)


@dataclass(frozen=True)
class Case:
    path: Callable[[Seeded], str]
    body: Callable[[Seeded], dict[str, Any]]
    # The statement the request blocks on while the holder has the evidence
    # row locked: its `FOR KEY SHARE` on the cited evidence (the foreign key
    # check of the claim insert), or the `FOR SHARE` relationship create takes
    # on it before inserting.
    blocked_on: str


def _claim_body(s: Seeded) -> dict[str, Any]:
    return {"predicate": "role", "value": {"v": "new"}, "source_id": str(s.evidence_id)}


CASES: dict[str, Case] = {
    "claim_create": Case(
        lambda s: f"{_ENTITIES}/{s.entity_id}/claims", _claim_body, "INSERT INTO knowledge_claims"
    ),
    "claim_supersede": Case(
        lambda s: f"{_ENTITIES}/{s.entity_id}/claims/{s.claim_id}/supersede",
        _claim_body,
        "INSERT INTO knowledge_claims",
    ),
    "relationship_create": Case(
        lambda s: f"{_ENTITIES}/{s.entity_id}/relationships",
        lambda s: {
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(s.other_entity_id),
            "evidence_id": str(s.evidence_id),
        },
        "FROM pkos_evidence.*FOR SHARE",
    ),
}


def _blocked_on_holder(holder_pid: int, statement: str) -> bool:
    with engine.connect() as probe:
        return bool(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)] "
                    "AND query ~* :pattern"
                ),
                {"holder": holder_pid, "pattern": statement},
            ).scalar_one()
        )


def _wait_blocked(holder_pid: int, statement: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while not _blocked_on_holder(holder_pid, statement):
        if time.monotonic() > deadline:
            raise AssertionError(f"request never blocked on `{statement}`")
        time.sleep(0.05)


@pytest.mark.parametrize("name", list(CASES))
def test_child_row_insert_on_the_entity_is_not_blocked_by_the_request(
    world: RaceWorld, name: str
) -> None:
    """The holder owns the cited evidence row (what `delete_evidence` holds);
    the request blocks on it holding the entity lock. The holder's own insert
    of a child row of that entity must not wait on the request."""
    case = CASES[name]
    seeded = _seed(world)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.post(
                case.path(seeded), headers=headers(world.b_token), json=case.body(seeded)
            )
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    thread = threading.Thread(target=fire)
    holder = engine.connect()
    holder_tx = holder.begin()
    insert_error: BaseException | None = None
    try:
        holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
        holder.execute(
            text("SELECT id FROM pkos_evidence WHERE id = :id FOR UPDATE"),
            {"id": seeded.evidence_id},
        )
        thread.start()
        _wait_blocked(holder_pid, case.blocked_on)
        # Fail fast instead of waiting out the deadlock detector if the
        # request's entity lock is in the way.
        holder.execute(text("SET LOCAL lock_timeout = '3s'"))
        try:
            holder.execute(
                text(
                    "INSERT INTO timeline_entries (id, workspace_id, entity_id, effective_at, "
                    "recorded_at, event_type, summary, owner_id) "
                    "VALUES (:id, :ws, :entity, now(), now(), 'race.child_insert', 'probe', :b)"
                ),
                {"id": uuid4(), "ws": world.ws, "entity": seeded.entity_id, "b": world.b},
            )
        except DBAPIError as exc:
            insert_error = exc
            holder_tx.rollback()
        if holder_tx.is_active:
            holder_tx.commit()
    finally:
        if holder_tx.is_active:
            holder_tx.rollback()
        holder.close()
        if thread.ident is not None:
            thread.join(timeout=_WAIT_SECONDS)
        client.close()

    assert insert_error is None, (
        "a child-row insert for the entity waited on the request's entity lock "
        f"(lock cycle with evidence deletion): {insert_error}"
    )
    assert not thread.is_alive(), "request never finished"
    if "error" in result:
        raise result["error"]
    assert result["response"].status_code == 201, result["response"].text


@pytest.mark.parametrize("name", list(CASES))
def test_entity_lock_still_blocks_an_ownership_transfer(world: RaceWorld, name: str) -> None:
    """The lighter lock must still conflict with the transfer's `FOR UPDATE`:
    while the request is in flight on the entity, a transfer of it waits."""
    case = CASES[name]
    seeded = _seed(world)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.post(
                case.path(seeded), headers=headers(world.b_token), json=case.body(seeded)
            )
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    thread = threading.Thread(target=fire)
    holder = engine.connect()
    holder_tx = holder.begin()
    transfer_blocked = False
    try:
        holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
        holder.execute(
            text("SELECT id FROM pkos_evidence WHERE id = :id FOR UPDATE"),
            {"id": seeded.evidence_id},
        )
        thread.start()
        _wait_blocked(holder_pid, case.blocked_on)
        # The request is parked holding its entity lock; a transfer's lock on
        # the entity must now wait.
        with engine.connect() as other:
            other.execute(text("SET lock_timeout = '300ms'"))
            try:
                other.execute(
                    text("SELECT id FROM pkos_nodes WHERE id = :id FOR UPDATE"),
                    {"id": seeded.entity_id},
                )
            except DBAPIError:
                transfer_blocked = True
        holder_tx.commit()
    finally:
        if holder_tx.is_active:
            holder_tx.rollback()
        holder.close()
        if thread.ident is not None:
            thread.join(timeout=_WAIT_SECONDS)
        client.close()

    assert transfer_blocked, "an ownership transfer's entity lock was not held off"
    assert not thread.is_alive(), "request never finished"
    if "error" in result:
        raise result["error"]
    assert result["response"].status_code == 201, result["response"].text
