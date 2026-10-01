"""Lock-before-authorize sweep for risks and recommendations: the risk
PATCH/archive/restore paths, `set_risk_status_write` (the confirm cascade's
risk `set_status` branch), the recommendation publish/reject/defer/pin
transitions and confirm all authorized the caller *before* taking
`SELECT ... FOR UPDATE` on the row they then write. An ownership transfer
(locks the row, rewrites `owner_id`, does not bump `version`) that committed
while the request waited on that row lock left the request writing to a row
the caller could no longer see.

Now each row is locked first and authorization is evaluated afterwards, on
the committed post-transfer row, so the waiting request answers 404 and
writes nothing. See `lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from lock_race_support import RaceWorld, headers, race, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Governance Lock Race",
        ("recommendation_feedback", "recommendations", "tasks", "risks"),
    ) as w:
        yield w


def _seed_risk(conn: Connection, w: RaceWorld, now: datetime, *, archived: bool = False) -> UUID:
    risk_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO risks (id, workspace_id, description, probability, impact, status, "
            "owner_id, created_by, updated_by, created_at, updated_at, visibility, archived_at, "
            "pre_archive_status) "
            "VALUES (:id, :ws, 'Race risk', 3, 3, 'identified', :b, :b, :b, :now, :now, "
            "'private', :archived_at, :pre)"
        ),
        {
            "id": risk_id,
            "ws": w.ws,
            "b": w.b,
            "now": now,
            "archived_at": now if archived else None,
            "pre": "identified" if archived else None,
        },
    )
    return risk_id


def _seed_recommendation(
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
) -> UUID:
    recommendation_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
            "target_id, proposed_action, expected_version, rationale, confidence, status, "
            "source, created_by, updated_by, created_at, updated_at, owner_id, visibility, "
            "proposed_fields) "
            "VALUES (:id, :ws, 'race_detected', :target_type, :target_id, "
            "CAST(:action AS jsonb), :expected_version, 'Race rationale', 0.9, :status, 'rule', "
            ":b, :b, :now, :now, :b, 'private', CAST(:fields AS jsonb))"
        ),
        {
            "id": recommendation_id,
            "ws": w.ws,
            "b": w.b,
            "now": now,
            "status": status,
            "target_type": target_type,
            "target_id": target_id,
            "action": dumps(action or {"operation": "create", "value": None}),
            "expected_version": expected_version,
            "fields": dumps(
                proposed_fields if proposed_fields is not None else {"title": "Race task"}
            ),
        },
    )
    return recommendation_id


@dataclass(frozen=True)
class Case:
    table: str
    method: str
    path: str
    body: dict[str, Any]
    not_found: str
    seed: Callable[[Connection, RaceWorld, datetime], UUID]
    # For the confirm-cascade case the raced row (a risk) is not the row the
    # URL names (the recommendation): `seed` returns the raced row and
    # `url_id` maps it to the URL's id.
    url_id: Callable[[UUID], UUID] = field(default=lambda row_id: row_id)


_V1 = {"expected_version": 1}
_RISKS = "/api/v1/risks/{id}"
_RECS = "/api/v1/recommendations/{id}"
_cascade_urls: dict[UUID, UUID] = {}


def _seed_cascade(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    """A pending set_status recommendation targeting a risk: the raced row
    is the risk, locked by `set_risk_status_write` during confirm."""
    risk_id = _seed_risk(conn, w, now)
    _cascade_urls[risk_id] = _seed_recommendation(
        conn,
        w,
        now,
        status="pending_confirmation",
        target_type="risk",
        target_id=risk_id,
        action={"operation": "set_status", "value": "monitoring"},
        expected_version=1,
    )
    return risk_id


def _rec(status: str) -> Callable[[Connection, RaceWorld, datetime], UUID]:
    return lambda conn, w, now: _seed_recommendation(conn, w, now, status=status)


CASES: dict[str, Case] = {
    "risk_patch": Case(
        "risks",
        "PATCH",
        _RISKS,
        {"expected_version": 1, "description": "race probe"},
        "RISK_NOT_FOUND",
        _seed_risk,
    ),
    "risk_archive": Case("risks", "POST", _RISKS + "/archive", _V1, "RISK_NOT_FOUND", _seed_risk),
    "risk_restore": Case(
        "risks",
        "POST",
        _RISKS + "/restore",
        _V1,
        "RISK_NOT_FOUND",
        lambda conn, w, now: _seed_risk(conn, w, now, archived=True),
    ),
    "recommendation_publish": Case(
        "recommendations",
        "POST",
        _RECS + "/publish",
        _V1,
        "RECOMMENDATION_NOT_FOUND",
        _rec("proposed"),
    ),
    "recommendation_reject": Case(
        "recommendations",
        "POST",
        _RECS + "/reject",
        {"expected_version": 1, "reason": "race probe"},
        "RECOMMENDATION_NOT_FOUND",
        _rec("pending_confirmation"),
    ),
    "recommendation_defer": Case(
        "recommendations",
        "POST",
        _RECS + "/defer",
        {
            "expected_version": 1,
            "defer_until": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
        },
        "RECOMMENDATION_NOT_FOUND",
        _rec("proposed"),
    ),
    "recommendation_pin": Case(
        "recommendations",
        "POST",
        _RECS + "/pin",
        {"expected_version": 1, "pinned": True},
        "RECOMMENDATION_NOT_FOUND",
        _rec("proposed"),
    ),
    "recommendation_confirm_create": Case(
        "recommendations",
        "POST",
        _RECS + "/confirm",
        _V1,
        "RECOMMENDATION_NOT_FOUND",
        _rec("pending_confirmation"),
    ),
    "recommendation_confirm_risk_status": Case(
        "risks",
        "POST",
        _RECS + "/confirm",
        {"expected_version": 1, "target_expected_version": 1},
        "RISK_NOT_FOUND",
        _seed_cascade,
        url_id=lambda risk_id: _cascade_urls[risk_id],
    ),
}

_SIDE_EFFECT_TABLES = (
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "recommendation_feedback",
    "tasks",
)


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


def _recommendation_snapshots(ws: UUID) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text("SELECT * FROM recommendations WHERE workspace_id = :ws ORDER BY id"),
                {"ws": ws},
            ).mappings()
        ]


def _run(w: RaceWorld, case: Case, row_id: UUID, *, transfer: bool) -> tuple[Any, Any]:
    return race(
        w,
        table=case.table,
        row_id=row_id,
        send=lambda client: client.request(
            case.method,
            case.path.format(id=case.url_id(row_id)),
            headers=headers(w.b_token),
            json=case.body,
        ),
        transfer=transfer,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, datetime.now(UTC))
    counts_before = _side_effect_counts(world.ws)
    recommendations_before = (
        _recommendation_snapshots(world.ws) if case.table != "recommendations" else None
    )

    response, before = _run(world, case, row_id, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == {**before, "owner_id": world.c}
    assert _side_effect_counts(world.ws) == counts_before
    if recommendations_before is not None:
        # The cascade's recommendation (not the raced row) is rolled back too.
        assert _recommendation_snapshots(world.ws) == recommendations_before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, datetime.now(UTC))

    response, before = _run(world, case, row_id, transfer=False)

    assert response.status_code == 200, response.text
    assert row_snapshot(case.table, row_id)["version"] > before["version"]
