"""Risk and recommendation writes against a concurrent member removal or
role change (ADR-0014).

Every write transaction in `governance/*` now takes the shared membership
lock first (`authz.lock_membership_for_write`) and authorizes after it:
the risk create/patch/archive/restore paths, recommendation generation,
the publish/reject/defer/pin transitions, confirm (including the task,
commitment and risk writes its target execution makes inside the same
transaction), and `GET /recommendations/{id}`, whose expiry flip is a write
attributed to the caller. The two creates re-check the role
in-transaction (`role_action="write"`). A removal or demotion holding the
lock makes the write wait, and once it commits the write answers 403/404
and writes nothing. See `membership_lock_race_support` for the harness.
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
    Refusal,
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

_SEEDED_TABLES = ("recommendation_feedback", "recommendations", "tasks", "commitments", "risks")
_WRITE_TABLES = (*_SEEDED_TABLES, "audit_events", "event_outbox", "idempotency_records")


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Governance Membership Race", _SEEDED_TABLES) as w:
        yield w


def _insert_risk(conn: Connection, w: RaceWorld, now: datetime, *, archived: bool = False) -> UUID:
    risk_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO risks (id, workspace_id, description, probability, impact, status, "
            "owner_id, created_by, updated_by, created_at, updated_at, visibility, archived_at, "
            "pre_archive_status) "
            "VALUES (:id, :ws, 'Race risk', 3, 3, 'identified', :a, :a, :a, :now, :now, "
            "'workspace', :archived_at, :pre)"
        ),
        {
            "id": risk_id,
            "ws": w.ws,
            "a": w.a,
            "now": now,
            "archived_at": now if archived else None,
            "pre": "identified" if archived else None,
        },
    )
    return risk_id


def _insert_task(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    task_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO tasks (id, workspace_id, owner_id, title, created_by, updated_by, "
            "created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :a, 'Race task', :a, :a, :now, :now, 'workspace')"
        ),
        {"id": task_id, "ws": w.ws, "a": w.a, "now": now},
    )
    return task_id


def _insert_commitment(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    commitment_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
            "status, created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :a, 'Race commitment', 'made_by_me', 'confirmed', "
            ":a, :a, :now, :now, 'workspace')"
        ),
        {"id": commitment_id, "ws": w.ws, "a": w.a, "now": now},
    )
    return commitment_id


def _insert_recommendation(
    conn: Connection,
    w: RaceWorld,
    now: datetime,
    *,
    status: str,
    target_type: str = "task",
    target_id: UUID | None = None,
    action: dict[str, Any] | None = None,
    expected_version: int | None = None,
    proposed_fields: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
) -> UUID:
    recommendation_id = uuid4()
    is_create = target_id is None
    conn.execute(
        text(
            "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
            "target_id, proposed_action, expected_version, rationale, confidence, status, "
            "source, created_by, updated_by, created_at, updated_at, owner_id, visibility, "
            "proposed_fields, expires_at) "
            "VALUES (:id, :ws, 'race_detected', :target_type, :target_id, "
            "CAST(:action AS jsonb), :expected_version, 'Race rationale', 0.9, :status, 'rule', "
            ":a, :a, :now, :now, :a, 'workspace', CAST(:fields AS jsonb), :expires_at)"
        ),
        {
            "id": recommendation_id,
            "ws": w.ws,
            "a": w.a,
            "now": now,
            "status": status,
            "target_type": target_type,
            "target_id": target_id,
            "action": dumps(action or {"operation": "create", "value": None}),
            "expected_version": expected_version,
            "fields": (dumps(proposed_fields or {"title": "Race task"}) if is_create else None),
            "expires_at": expires_at,
        },
    )
    return recommendation_id


def _seed_risk(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"id": _insert_risk(conn, w, now)}


def _seed_archived_risk(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"id": _insert_risk(conn, w, now, archived=True)}


def _seed_task(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"task": _insert_task(conn, w, now)}


def _seed_rec(status: str) -> Any:
    def seed_one(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
        return {"id": _insert_recommendation(conn, w, now, status=status)}

    return seed_one


def _seed_expired_rec(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {
        "id": _insert_recommendation(
            conn, w, now, status="proposed", expires_at=now - timedelta(minutes=1)
        )
    }


def _seed_confirm_commitment(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    commitment_id = _insert_commitment(conn, w, now)
    return {
        "id": _insert_recommendation(
            conn,
            w,
            now,
            status="pending_confirmation",
            target_type="commitment",
            target_id=commitment_id,
            action={"operation": "set_importance", "value": "high"},
            expected_version=1,
        )
    }


def _seed_confirm_risk(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    risk_id = _insert_risk(conn, w, now)
    return {
        "id": _insert_recommendation(
            conn,
            w,
            now,
            status="pending_confirmation",
            target_type="risk",
            target_id=risk_id,
            action={"operation": "set_status", "value": "monitoring"},
            expected_version=1,
        )
    }


def _v1(_: Ids) -> dict[str, Any]:
    return {"expected_version": 1}


def _v1_target(_: Ids) -> dict[str, Any]:
    return {"expected_version": 1, "target_expected_version": 1}


_RISKS = "/api/v1/risks"
_RECS = "/api/v1/recommendations"
_RISK_ROW = row_refusals("RISK_NOT_FOUND")
_REC_ROW = row_refusals("RECOMMENDATION_NOT_FOUND")
# A viewer may still read a recommendation (and, as before, flip an expired
# one to `expired`); only removal revokes the read.
_REC_READ: dict[Change, Refusal | None] = {
    "demote": None,
    "remove": (404, "RECOMMENDATION_NOT_FOUND"),
}

CASES: dict[str, Case] = {
    "risk_create": Case(
        seed_nothing,
        "POST",
        _RISKS,
        lambda _: {"description": "Race risk", "probability": 3, "impact": 3},
        201,
        ROLE_GATED,
    ),
    "risk_patch": Case(
        _seed_risk,
        "PATCH",
        _RISKS + "/{id}",
        lambda _: {"expected_version": 1, "description": "Race probe"},
        200,
        _RISK_ROW,
    ),
    "risk_archive": Case(_seed_risk, "POST", _RISKS + "/{id}/archive", _v1, 200, _RISK_ROW),
    "risk_restore": Case(
        _seed_archived_risk, "POST", _RISKS + "/{id}/restore", _v1, 200, _RISK_ROW
    ),
    "recommendation_create_new_row": Case(
        seed_nothing,
        "POST",
        _RECS,
        lambda _: {
            "recommendation_type": "race_detected",
            "target_type": "task",
            "proposed_action": {"operation": "create", "value": None},
            "proposed_fields": {"title": "Race task"},
            "rationale": "Race rationale",
            "confidence": 0.9,
        },
        201,
        ROLE_GATED,
    ),
    "recommendation_create_for_target": Case(
        _seed_task,
        "POST",
        _RECS,
        lambda ids: {
            "recommendation_type": "race_detected",
            "target_type": "task",
            "target_id": str(ids["task"]),
            "proposed_action": {"operation": "set_pinned", "value": True},
            "expected_version": 1,
            "rationale": "Race rationale",
            "confidence": 0.9,
        },
        201,
        ROLE_GATED,
    ),
    "recommendation_publish": Case(
        _seed_rec("proposed"), "POST", _RECS + "/{id}/publish", _v1, 200, _REC_ROW
    ),
    "recommendation_reject": Case(
        _seed_rec("pending_confirmation"),
        "POST",
        _RECS + "/{id}/reject",
        lambda _: {"expected_version": 1, "reason": "Race probe"},
        200,
        _REC_ROW,
    ),
    "recommendation_defer": Case(
        _seed_rec("proposed"),
        "POST",
        _RECS + "/{id}/defer",
        lambda _: {
            "expected_version": 1,
            "defer_until": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
        200,
        _REC_ROW,
    ),
    "recommendation_pin": Case(
        _seed_rec("proposed"),
        "POST",
        _RECS + "/{id}/pin",
        lambda _: {"expected_version": 1, "pinned": True},
        200,
        _REC_ROW,
    ),
    "recommendation_confirm_task_create": Case(
        _seed_rec("pending_confirmation"), "POST", _RECS + "/{id}/confirm", _v1, 200, _REC_ROW
    ),
    "recommendation_confirm_commitment_importance": Case(
        _seed_confirm_commitment, "POST", _RECS + "/{id}/confirm", _v1_target, 200, _REC_ROW
    ),
    "recommendation_confirm_risk_status": Case(
        _seed_confirm_risk, "POST", _RECS + "/{id}/confirm", _v1_target, 200, _REC_ROW
    ),
    "recommendation_get_expiring": Case(
        _seed_expired_rec, "GET", _RECS + "/{id}", lambda _: None, 200, _REC_READ
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
