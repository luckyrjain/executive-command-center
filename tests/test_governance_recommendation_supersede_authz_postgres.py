"""`POST /api/v1/recommendations` supersedes only the pending recommendations
on its target that the caller could itself read and write.

`create_recommendation`'s supersede UPDATE matched every `proposed`/
`pending_confirmation` recommendation on the target by workspace alone,
never by each row's own `owner_id`/`visibility`. A member creating a
recommendation against a workspace-visible task flipped another member's
*private* pending recommendation on that task to `superseded` (bumping its
version and recording a `recommendation.superseded` event for it in the
caller's name), a write the caller could never have made through the
recommendation endpoints themselves.

Now the supersede carries the same read and write visibility filter the
list endpoints use: rows the caller can see and change are superseded as
before; another member's private row, or one shared with the caller
read-only, stays live and untouched.

B (`admin`) is the caller; C (`member`) owns the target task.
"""

from __future__ import annotations

from collections.abc import Iterator
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

ACTION = {"operation": "set_priority", "value": "high"}


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Recommendation Supersede Authz",
        ("resource_grants", "recommendation_feedback", "recommendations", "tasks"),
    ) as w:
        yield w


def _seed_task(conn: Connection, w: RaceWorld) -> UUID:
    task_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO tasks (id, workspace_id, owner_id, title, status, created_by, "
            "updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :o, 'Shared task', 'captured', :o, :o, :now, :now, "
            "'workspace')"
        ),
        {"id": task_id, "ws": w.ws, "o": w.c, "now": datetime.now(UTC)},
    )
    return task_id


def _seed_recommendation(
    conn: Connection,
    w: RaceWorld,
    *,
    task_id: UUID,
    owner: UUID,
    visibility: str,
    status: str = "proposed",
) -> UUID:
    recommendation_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
            "target_id, proposed_action, expected_version, rationale, confidence, status, "
            "source, created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'owner_detected', 'task', :target_id, "
            "CAST(:action AS jsonb), 1, 'Owner rationale', 0.9, :status, "
            "'rule', :o, :o, :now, :now, :o, :vis)"
        ),
        {
            "id": recommendation_id,
            "ws": w.ws,
            "o": owner,
            "now": now,
            "target_id": task_id,
            "action": dumps(ACTION),
            "status": status,
            "vis": visibility,
        },
    )
    return recommendation_id


def _grant(conn: Connection, w: RaceWorld, recommendation_id: UUID, actions: list[str]) -> None:
    conn.execute(
        text(
            "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, resource_type, "
            "resource_id, actions, granted_by, created_at) "
            "VALUES (:id, :ws, (SELECT account_id FROM users WHERE id = :b), "
            "'recommendations', :rid, :actions, :c, :now)"
        ),
        {
            "id": uuid4(),
            "ws": w.ws,
            "b": w.b,
            "c": w.c,
            "rid": recommendation_id,
            "actions": actions,
            "now": datetime.now(UTC),
        },
    )


def _create(w: RaceWorld, task_id: UUID) -> dict[str, Any]:
    with TestClient(app) as client:
        client.cookies.set("ecc_session", w.b_token)
        response = client.post(
            "/api/v1/recommendations",
            headers=headers(w.b_token),
            json={
                "recommendation_type": "probe",
                "target_type": "task",
                "target_id": str(task_id),
                "proposed_action": ACTION,
                "expected_version": 1,
                "rationale": "Caller rationale.",
                "confidence": 0.5,
                "evidence_ids": [],
                "source": "rule",
            },
        )
    assert response.status_code == 201, response.json()
    return dict(response.json())


def _superseded_event_ids(ws: UUID) -> set[UUID]:
    with engine.connect() as connection:
        return set(
            connection.execute(
                text(
                    "SELECT aggregate_id FROM audit_events "
                    "WHERE workspace_id = :ws AND event_type = 'recommendation.superseded'"
                ),
                {"ws": ws},
            ).scalars()
        )


def test_create_leaves_other_members_private_pending_recommendation_live(
    world: RaceWorld,
) -> None:
    with engine.begin() as connection:
        task_id = _seed_task(connection, world)
        c_private = _seed_recommendation(
            connection, world, task_id=task_id, owner=world.c, visibility="private"
        )
        c_private_pending = _seed_recommendation(
            connection,
            world,
            task_id=task_id,
            owner=world.c,
            visibility="private",
            status="pending_confirmation",
        )
        c_shared_read_only = _seed_recommendation(
            connection, world, task_id=task_id, owner=world.c, visibility="shared_explicitly"
        )
        _grant(connection, world, c_shared_read_only, ["read"])
    untouched = (c_private, c_private_pending, c_shared_read_only)
    before = {rid: row_snapshot("recommendations", rid) for rid in untouched}

    created = _create(world, task_id)

    assert created["status"] == "proposed"
    for rid in untouched:
        assert row_snapshot("recommendations", rid) == before[rid]
    assert _superseded_event_ids(world.ws).isdisjoint(untouched)


def test_create_still_supersedes_recommendations_the_caller_can_write(
    world: RaceWorld,
) -> None:
    """Control: a workspace-visible one by C, B's own private one, and one
    shared with B for read+write are all superseded, each with its event."""
    with engine.begin() as connection:
        task_id = _seed_task(connection, world)
        c_workspace = _seed_recommendation(
            connection, world, task_id=task_id, owner=world.c, visibility="workspace"
        )
        b_private = _seed_recommendation(
            connection, world, task_id=task_id, owner=world.b, visibility="private"
        )
        c_shared_writable = _seed_recommendation(
            connection, world, task_id=task_id, owner=world.c, visibility="shared_explicitly"
        )
        _grant(connection, world, c_shared_writable, ["read", "write"])
    superseded = (c_workspace, b_private, c_shared_writable)

    _create(world, task_id)

    for rid in superseded:
        row = row_snapshot("recommendations", rid)
        assert row["status"] == "superseded"
        assert row["version"] == 2
        assert row["updated_by"] == world.b
    assert _superseded_event_ids(world.ws) == set(superseded)
