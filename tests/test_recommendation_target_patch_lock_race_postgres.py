"""Lock-before-authorize for the recommendation-confirm column patch:
`execute_target`'s generic branch (task `set_priority`/`set_pinned`,
commitment `set_importance`/`set_pinned`, risk `set_probability`/
`set_impact`/`set_pinned`) authorized the target and then ran a plain
`UPDATE` with no prior row lock and no `owner_id` in its WHERE. An ownership
transfer (locks the row, rewrites `owner_id`, does not bump `version`) that
committed while that UPDATE waited on the row lock let the confirm write to a
target the confirming user could no longer see.

Now the target is locked first and authorized afterwards, on the committed
post-transfer row, so the confirm answers 404 and writes nothing.

Without the fix the request blocks in the UPDATE, not in a `FOR UPDATE`
select, so the shared harness's waiter probe is widened here to accept
either; that is what lets the reverted-fix run show the write landing
instead of only a probe timeout.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
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


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[RaceWorld]:
    monkeypatch.setattr(lock_race_support, "wait_for_lock_waiter", _wait_for_update_or_lock)
    with race_world(
        "Recommendation Target Patch Lock Race",
        ("recommendation_feedback", "recommendations", "tasks", "commitments", "risks"),
    ) as w:
        yield w


def _wait_for_update_or_lock(table: str) -> None:
    deadline = time.monotonic() + lock_race_support.WAIT_SECONDS
    while True:
        with engine.connect() as probe:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event IN ('transactionid', 'tuple') "
                    "AND (query ~* :lock OR query ~* :update)"
                ),
                {"lock": f"FROM {table}\\s.*FOR UPDATE", "update": f"UPDATE {table}\\s"},
            ).scalar_one()
        if waiting >= 1:
            return
        if time.monotonic() > deadline:
            raise AssertionError(f"confirm never blocked on the {table} row lock")
        time.sleep(0.05)


def _seed_task(conn: Connection, w: RaceWorld, now: datetime, archived: bool) -> UUID:
    task_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO tasks (id, workspace_id, owner_id, title, status, created_by, "
            "updated_by, created_at, updated_at, visibility, archived_at, pre_archive_status) "
            "VALUES (:id, :ws, :b, 'Race task', 'captured', :b, :b, :now, :now, 'private', "
            ":archived_at, :pre)"
        ),
        {
            "id": task_id,
            "ws": w.ws,
            "b": w.b,
            "now": now,
            "archived_at": now if archived else None,
            "pre": "captured" if archived else None,
        },
    )
    return task_id


def _seed_commitment(conn: Connection, w: RaceWorld, now: datetime, archived: bool) -> UUID:
    commitment_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
            "created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :b, 'Race commitment', 'made_by_me', :b, :b, :now, :now, "
            "'private')"
        ),
        {"id": commitment_id, "ws": w.ws, "b": w.b, "now": now},
    )
    return commitment_id


def _seed_risk(conn: Connection, w: RaceWorld, now: datetime, archived: bool) -> UUID:
    risk_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO risks (id, workspace_id, description, probability, impact, status, "
            "owner_id, created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, 'Race risk', 3, 3, 'identified', :b, :b, :b, :now, :now, "
            "'private')"
        ),
        {"id": risk_id, "ws": w.ws, "b": w.b, "now": now},
    )
    return risk_id


def _seed_recommendation(
    conn: Connection,
    w: RaceWorld,
    now: datetime,
    *,
    target_type: str,
    target_id: UUID,
    action: dict[str, Any],
) -> UUID:
    recommendation_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
            "target_id, proposed_action, expected_version, rationale, confidence, status, "
            "source, created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'race_detected', :target_type, :target_id, "
            "CAST(:action AS jsonb), 1, 'Race rationale', 0.9, 'pending_confirmation', "
            "'rule', :b, :b, :now, :now, :b, 'private')"
        ),
        {
            "id": recommendation_id,
            "ws": w.ws,
            "b": w.b,
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
    seed: Callable[[Connection, RaceWorld, datetime, bool], UUID]
    action: dict[str, Any]
    column: str


CASES: dict[str, Case] = {
    "task_set_priority": Case(
        "tasks",
        "task",
        _seed_task,
        {"operation": "set_priority", "value": "high"},
        "manual_priority",
    ),
    "commitment_set_importance": Case(
        "commitments",
        "commitment",
        _seed_commitment,
        {"operation": "set_importance", "value": "high"},
        "importance",
    ),
    "risk_set_probability": Case(
        "risks", "risk", _seed_risk, {"operation": "set_probability", "value": 5}, "probability"
    ),
}

_BODY = {"expected_version": 1, "target_expected_version": 1}


def _seed(case: Case, w: RaceWorld, *, archived: bool = False) -> tuple[UUID, UUID]:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        target_id = case.seed(connection, w, now, archived)
        recommendation_id = _seed_recommendation(
            connection,
            w,
            now,
            target_type=case.target_type,
            target_id=target_id,
            action=case.action,
        )
    return target_id, recommendation_id


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
                "audit_events",
                "event_outbox",
                "idempotency_records",
                "recommendation_feedback",
            )
        }


def _confirm(w: RaceWorld, recommendation_id: UUID) -> Callable[[TestClient], Any]:
    return lambda client: client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=headers(w.b_token),
        json=_BODY,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_confirm_waiting_on_target_lock_rechecks_authorization(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    target_id, recommendation_id = _seed(case, world)
    counts_before = _side_effect_counts(world.ws)
    recommendation_before = row_snapshot("recommendations", recommendation_id)

    response, before = race(
        world,
        table=case.table,
        row_id=target_id,
        send=_confirm(world, recommendation_id),
        transfer=True,
    )

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "TARGET_NOT_FOUND"
    assert row_snapshot(case.table, target_id) == {**before, "owner_id": world.c}
    assert row_snapshot("recommendations", recommendation_id) == recommendation_before
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_confirm_waiting_on_target_lock_without_transfer_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change applies the
    patch -- the 404 above comes from the transfer, not from the wait."""
    case = CASES[name]
    target_id, recommendation_id = _seed(case, world)

    response, before = race(
        world,
        table=case.table,
        row_id=target_id,
        send=_confirm(world, recommendation_id),
        transfer=False,
    )

    assert response.status_code == 200, response.text
    after = row_snapshot(case.table, target_id)
    assert after[case.column] == case.action["value"]
    assert after["version"] == before["version"] + 1


def test_confirm_against_archived_target_keeps_its_answer(world: RaceWorld) -> None:
    """The new lock has no `archived_at` filter on purpose, so an archived
    target still reaches the UPDATE and its `target_version` fallback and
    keeps answering exactly as before (404 TARGET_NOT_FOUND), unpatched."""
    target_id, recommendation_id = _seed(CASES["task_set_priority"], world, archived=True)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        response = _confirm(world, recommendation_id)(client)
    finally:
        client.close()

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "TARGET_NOT_FOUND"
    assert row_snapshot("tasks", target_id)["manual_priority"] == "medium"
