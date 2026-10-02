"""`POST /api/v1/recommendations` with a non-create operation must authorize
reading its target before touching it.

`create_recommendation` read the target's version (`target_version`) with
only a workspace filter, never the target's own visibility. A member could
name another member's private task/commitment/risk and learn from the
answer that it exists (409 `TARGET_VERSION_CONFLICT` vs 404) and its exact
version (201 once `expected_version` matched), and the 201 path also
superseded the owner's own pending recommendations on that row.

Now a target the caller cannot read answers the same 404 as a nonexistent
id, whatever `expected_version` says, and writes nothing. A same-key replay
by a caller who has since lost sight of the target is refused too, since
the idempotency cache is read only after the check.

B (`admin`) is the caller; C (`member`) owns the private rows.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Recommendation Create Target Authz",
        ("recommendation_feedback", "recommendations", "tasks", "commitments", "risks"),
    ) as w:
        yield w


def _seed_task(conn: Connection, w: RaceWorld, owner: UUID, visibility: str) -> UUID:
    task_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO tasks (id, workspace_id, owner_id, title, status, created_by, "
            "updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :o, 'Hidden task', 'captured', :o, :o, :now, :now, :vis)"
        ),
        {"id": task_id, "ws": w.ws, "o": owner, "now": datetime.now(UTC), "vis": visibility},
    )
    return task_id


def _seed_commitment(conn: Connection, w: RaceWorld, owner: UUID, visibility: str) -> UUID:
    commitment_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
            "created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :o, 'Hidden commitment', 'made_by_me', :o, :o, :now, :now, "
            ":vis)"
        ),
        {"id": commitment_id, "ws": w.ws, "o": owner, "now": datetime.now(UTC), "vis": visibility},
    )
    return commitment_id


def _seed_risk(conn: Connection, w: RaceWorld, owner: UUID, visibility: str) -> UUID:
    risk_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO risks (id, workspace_id, description, probability, impact, status, "
            "owner_id, created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, 'Hidden risk', 3, 3, 'identified', :o, :o, :o, :now, :now, "
            ":vis)"
        ),
        {"id": risk_id, "ws": w.ws, "o": owner, "now": datetime.now(UTC), "vis": visibility},
    )
    return risk_id


def _seed_owner_recommendation(
    conn: Connection, w: RaceWorld, *, target_type: str, target_id: UUID, action: dict[str, Any]
) -> UUID:
    """C's own pending recommendation on C's private target, which a
    successful create by B would have superseded."""
    recommendation_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
            "target_id, proposed_action, expected_version, rationale, confidence, status, "
            "source, created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'owner_detected', :target_type, :target_id, "
            "CAST(:action AS jsonb), 1, 'Owner rationale', 0.9, 'proposed', "
            "'rule', :c, :c, :now, :now, :c, 'private')"
        ),
        {
            "id": recommendation_id,
            "ws": w.ws,
            "c": w.c,
            "now": now,
            "target_type": target_type,
            "target_id": target_id,
            "action": dumps(action),
        },
    )
    return recommendation_id


@dataclass(frozen=True)
class Case:
    table: str
    target_type: str
    seed: Callable[[Connection, RaceWorld, UUID, str], UUID]
    action: dict[str, Any]


CASES: dict[str, Case] = {
    "task": Case("tasks", "task", _seed_task, {"operation": "set_priority", "value": "high"}),
    "commitment": Case(
        "commitments",
        "commitment",
        _seed_commitment,
        {"operation": "set_importance", "value": "high"},
    ),
    "risk": Case("risks", "risk", _seed_risk, {"operation": "set_probability", "value": 5}),
}


def _body(case: Case, target_id: UUID, expected_version: int) -> dict[str, Any]:
    return {
        "recommendation_type": "probe",
        "target_type": case.target_type,
        "target_id": str(target_id),
        "proposed_action": case.action,
        "expected_version": expected_version,
        "rationale": "Probe rationale.",
        "confidence": 0.5,
        "evidence_ids": [],
        "source": "rule",
    }


def _post(
    w: RaceWorld, body: dict[str, Any], *, key: str | None = None
) -> tuple[int, dict[str, Any]]:
    request_headers = headers(w.b_token)
    if key is not None:
        request_headers["Idempotency-Key"] = key
    with TestClient(app) as client:
        client.cookies.set("ecc_session", w.b_token)
        response = client.post("/api/v1/recommendations", headers=request_headers, json=body)
    return response.status_code, response.json()


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in (
                "recommendations",
                "audit_events",
                "event_outbox",
                "idempotency_records",
            )
        }


def _error(body: dict[str, Any]) -> dict[str, Any]:
    # `request_id` differs between any two responses; everything else
    # must match exactly.
    return {key: value for key, value in body["error"].items() if key != "request_id"}


@pytest.mark.parametrize("name", list(CASES))
def test_create_against_unreadable_target_is_indistinguishable_from_missing(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        target_id = case.seed(connection, world, world.c, "private")
        owner_rec_id = _seed_owner_recommendation(
            connection, world, target_type=case.target_type, target_id=target_id, action=case.action
        )
    target_before = row_snapshot(case.table, target_id)
    owner_rec_before = row_snapshot("recommendations", owner_rec_id)
    counts_before = _side_effect_counts(world.ws)

    missing_status, missing_body = _post(world, _body(case, uuid4(), 1))
    assert missing_status == 404, missing_body
    assert missing_body["error"]["code"] == "TARGET_NOT_FOUND"

    # The right version (1) and a wrong one (99) both answer exactly the
    # missing-id 404: neither existence nor version leaks.
    for expected_version in (1, 99):
        status, body = _post(world, _body(case, target_id, expected_version))
        assert status == 404, body
        assert _error(body) == _error(missing_body)

    assert row_snapshot(case.table, target_id) == target_before
    assert row_snapshot("recommendations", owner_rec_id) == owner_rec_before
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_create_against_readable_target_still_works(world: RaceWorld, name: str) -> None:
    """Control: a workspace-visible target owned by someone else is readable
    by B, so the create proceeds and supersedes C's pending one."""
    case = CASES[name]
    with engine.begin() as connection:
        target_id = case.seed(connection, world, world.c, "workspace")
        owner_rec_id = _seed_owner_recommendation(
            connection, world, target_type=case.target_type, target_id=target_id, action=case.action
        )

    status, body = _post(world, _body(case, target_id, 99))
    assert status == 409, body
    assert body["error"]["code"] == "TARGET_VERSION_CONFLICT"

    status, body = _post(world, _body(case, target_id, 1))
    assert status == 201, body
    assert body["target_id"] == str(target_id)
    assert row_snapshot("recommendations", owner_rec_id)["status"] == "superseded"


def test_replay_after_losing_sight_of_target_is_refused(world: RaceWorld) -> None:
    case = CASES["task"]
    with engine.begin() as connection:
        target_id = case.seed(connection, world, world.c, "workspace")
    key = str(uuid4())
    body = _body(case, target_id, 1)

    status, first = _post(world, body, key=key)
    assert status == 201, first
    status, replay = _post(world, body, key=key)
    assert status == 201, replay
    assert replay["id"] == first["id"]

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE tasks SET visibility = 'private' WHERE id = :id"), {"id": target_id}
        )
    counts_before = _side_effect_counts(world.ws)

    status, refused = _post(world, body, key=key)
    assert status == 404, refused
    assert refused["error"]["code"] == "TARGET_NOT_FOUND"
    assert _side_effect_counts(world.ws) == counts_before
