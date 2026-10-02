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

A grant revoked while the create waits on the row lock is honoured too:
the supersede locks its candidate rows and only then authorizes each one,
because a revoke locks the recommendation row without changing it, so a
filter evaluated inside the locking statement would still see the grant.
Rows the caller can read but not write are not locked at all.

B (`admin`) is the caller; C (`member`) owns the target task.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import lock_race_support
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


def _select_or_update_waiters(table: str, *, holder_pid: int) -> int:
    """Backends blocked by `holder_pid` on a row lock while running the
    supersede's locking `SELECT id FROM <table>` or, before #383, its plain
    `UPDATE <table>`. Matched on the statement's start, not on a trailing
    `FOR UPDATE` like the shared probe: `pg_stat_activity.query` is cut at
    `track_activity_query_size` (1 kB by default), and the SELECT with its
    two inlined authz filters is longer than that."""
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event IN ('transactionid', 'tuple') "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)] "
                    "AND query ~* :pattern"
                ),
                {"holder": holder_pid, "pattern": f"^\\s*(SELECT id FROM|UPDATE) {table}\\s"},
            ).scalar_one()
        )


def _wait_for_select_or_update_waiter(table: str, *, holder_pid: int) -> None:
    deadline = time.monotonic() + lock_race_support.WAIT_SECONDS
    while True:
        if _select_or_update_waiters(table, holder_pid=holder_pid) >= 1:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"create never blocked on the {table} row lock")
        time.sleep(0.05)


def _create_request(w: RaceWorld, task_id: UUID) -> Any:
    def send(client: TestClient) -> Any:
        return client.post(
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

    return send


@pytest.mark.parametrize("revoke", [True, False])
def test_grant_revoked_while_the_create_waits_on_the_row_lock_is_honoured(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch, revoke: bool
) -> None:
    """The holder locks C's recommendation `FOR UPDATE`, as
    `revoke_grant_endpoint` does, and (when `revoke`) revokes B's read+write
    grant in the same transaction while B's create waits on that lock. Once
    it commits, B can no longer write the row, so it must stay live. Control
    (no revoke): the same wait still supersedes it."""
    monkeypatch.setattr(
        lock_race_support, "wait_for_lock_waiter", _wait_for_select_or_update_waiter
    )
    with engine.begin() as connection:
        task_id = _seed_task(connection, world)
        c_shared = _seed_recommendation(
            connection, world, task_id=task_id, owner=world.c, visibility="shared_explicitly"
        )
        _grant(connection, world, c_shared, ["read", "write"])

    def revoke_grant(holder: Connection) -> object:
        return holder.execute(
            text(
                "UPDATE resource_grants SET revoked_at = now() "
                "WHERE resource_type = 'recommendations' AND resource_id = :rid"
            ),
            {"rid": c_shared},
        )

    response, before = race(
        world,
        table="recommendations",
        row_id=c_shared,
        send=_create_request(world, task_id),
        transfer=False,
        mutate=revoke_grant if revoke else None,
    )

    assert response.status_code == 201, response.text
    after = row_snapshot("recommendations", c_shared)
    if revoke:
        assert after == before
        assert c_shared not in _superseded_event_ids(world.ws)
    else:
        assert after["status"] == "superseded"
        assert after["updated_by"] == world.b
        assert _superseded_event_ids(world.ws) == {c_shared}


def test_create_does_not_lock_a_row_shared_with_the_caller_read_only(
    world: RaceWorld,
) -> None:
    """C's recommendation shared with B read-only is never superseded by B,
    so B's create must not take its row lock either: with the row held
    `FOR UPDATE` elsewhere, the create finishes without queuing behind it.
    Before, the locking SELECT filtered on read access only, so B's create
    locked (here: waited on) a row it could never supersede."""
    with engine.begin() as connection:
        task_id = _seed_task(connection, world)
        c_read_only = _seed_recommendation(
            connection, world, task_id=task_id, owner=world.c, visibility="shared_explicitly"
        )
        _grant(connection, world, c_read_only, ["read"])
    before = row_snapshot("recommendations", c_read_only)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = _create_request(world, task_id)(client)
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    holder = engine.connect()
    holder_tx = holder.begin()
    thread = threading.Thread(target=fire)
    try:
        holder_pid = lock_race_support.holder_backend_pid(holder)
        holder.execute(
            text("SELECT id FROM recommendations WHERE id = :id FOR UPDATE"),
            {"id": c_read_only},
        )
        thread.start()
        deadline = time.monotonic() + lock_race_support.WAIT_SECONDS
        while thread.is_alive():
            assert _select_or_update_waiters("recommendations", holder_pid=holder_pid) == 0, (
                "create queued behind the lock on a row it cannot supersede"
            )
            assert time.monotonic() < deadline, "create never finished"
            thread.join(timeout=0.05)
    finally:
        holder_tx.rollback()
        holder.close()
        thread.join(timeout=lock_race_support.WAIT_SECONDS)
        client.close()
    if "error" in result:
        raise result["error"]
    assert result["response"].status_code == 201, result["response"].text
    assert row_snapshot("recommendations", c_read_only) == before
