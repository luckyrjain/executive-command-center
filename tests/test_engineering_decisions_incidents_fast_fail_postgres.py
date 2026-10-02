"""Read fast-fail before the row lock, and authorization before the
idempotency cache, for `POST /engineering/incidents/{id}/resolve` and
`POST /engineering/decisions/{id}/decide`.

Lock-before-authorize (#324) made these take the membership, idempotency
and row locks before deciding, so a caller who cannot even see the row
queued behind whoever held it before getting its 404, and the delay told
them the row existed and was busy. Each now runs an unlocked read check
right after the shared membership lock, ahead of the idempotency and row
locks: such a caller gets its 404 while the row is still locked, without
waiting. The authoritative check stays on the locked row
(`test_authorize_then_write_lock_race_postgres.py`).

The idempotency cache used to be served before any authorization, so a
caller who had since lost access could replay a cached success. It is now
read only after the locked read/write checks pass.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import (
    WAIT_SECONDS,
    RaceWorld,
    headers,
    holder_backend_pid,
    lock_waiters,
    race_world,
    row_snapshot,
)
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FAST_SECONDS = 5
_SIDE_EFFECT_TABLES = ("audit_events", "event_outbox", "idempotency_records")


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Decisions Incidents Fast Fail",
        ("incident_changes", "decision_changes", "incidents", "engineering_decisions"),
    ) as w:
        yield w


def _c_token(w: RaceWorld) -> str:
    """A session for C (`member`), who cannot see B's private rows."""
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
                "user_id": w.c,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    return token


def _seed_incident(conn: Connection, w: RaceWorld) -> UUID:
    incident_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO incidents (id, workspace_id, title, severity, status, detected_at, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'Fast-fail incident', 'high', 'open', :detected, "
            ":b, :b, :now, :now, :b, 'private')"
        ),
        {"id": incident_id, "ws": w.ws, "detected": now - timedelta(hours=1), "now": now, "b": w.b},
    )
    return incident_id


def _seed_decision(conn: Connection, w: RaceWorld) -> UUID:
    decision_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO engineering_decisions (id, workspace_id, title, status, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'Fast-fail decision', 'proposed', :b, :b, :created, :now, "
            ":b, 'private')"
        ),
        {"id": decision_id, "ws": w.ws, "created": now - timedelta(hours=1), "now": now, "b": w.b},
    )
    return decision_id


@dataclass(frozen=True)
class Case:
    table: str
    path: str
    body: Callable[[], dict[str, Any]]
    not_found: str
    seed: Callable[[Connection, RaceWorld], UUID]


CASES: dict[str, Case] = {
    "incident_resolve": Case(
        "incidents",
        "/api/v1/engineering/incidents/{id}/resolve",
        lambda: {"resolved_at": datetime.now(UTC).isoformat()},
        "INCIDENT_NOT_FOUND",
        _seed_incident,
    ),
    "decision_decide": Case(
        "engineering_decisions",
        "/api/v1/engineering/decisions/{id}/decide",
        lambda: {"decided_at": datetime.now(UTC).isoformat(), "rationale": "fast-fail probe"},
        "DECISION_NOT_FOUND",
        _seed_decision,
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


@pytest.mark.parametrize("name", list(CASES))
def test_caller_who_cannot_see_the_row_is_refused_without_waiting_on_its_lock(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world)
    c_token = _c_token(world)
    counts_before = _side_effect_counts(world.ws)

    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    result: dict[str, Any] = {}
    finished = threading.Event()

    def fire() -> None:
        try:
            result["response"] = client.post(
                case.path.format(id=row_id), headers=headers(c_token), json=case.body()
            )
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc
        finally:
            finished.set()

    holder = engine.connect()
    holder_tx = holder.begin()
    thread = threading.Thread(target=fire)
    try:
        holder_pid = holder_backend_pid(holder)
        holder.execute(
            text(f"SELECT id FROM {case.table} WHERE id = :id FOR UPDATE"),  # noqa: S608
            {"id": row_id},
        )
        before = row_snapshot(case.table, row_id)
        thread.start()
        # Answered while the row is still locked: the request never queued.
        answered_while_locked = finished.wait(timeout=_FAST_SECONDS)
        waiting_on_holder = lock_waiters(case.table, holder_pid=holder_pid)
    finally:
        holder_tx.rollback()
        holder.close()
        thread.join(timeout=WAIT_SECONDS)
        client.close()
    assert not thread.is_alive(), "request never finished"
    if "error" in result:
        raise result["error"]

    assert answered_while_locked, "request waited on the row lock before being refused"
    assert waiting_on_holder == 0
    response = result["response"]
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == before
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_nonexistent_id_is_refused_with_the_same_404(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    c_token = _c_token(world)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        response = client.post(
            case.path.format(id=uuid4()), headers=headers(c_token), json=case.body()
        )
    finally:
        client.close()

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


@pytest.mark.parametrize("name", list(CASES))
def test_idempotent_replay_is_authorized_before_the_cache_is_served(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world)
    body = case.body()
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        first = client.post(case.path.format(id=row_id), headers=request_headers, json=body)
        assert first.status_code == 200, first.text

        # Still a member: the same key replays the cached response even
        # though the row is no longer in its pre-write state.
        replay = client.post(case.path.format(id=row_id), headers=request_headers, json=body)
        assert replay.status_code == 200, replay.text
        assert _without_request_id(replay.json()) == _without_request_id(first.json())

        # B loses access: the same key must no longer replay the success.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace_memberships SET status = 'suspended' "
                    "WHERE workspace_id = :ws AND users_id = :b"
                ),
                {"ws": world.ws, "b": world.b},
            )
        counts_before = _side_effect_counts(world.ws)
        refused = client.post(case.path.format(id=row_id), headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == case.not_found
    assert _side_effect_counts(world.ws) == counts_before
