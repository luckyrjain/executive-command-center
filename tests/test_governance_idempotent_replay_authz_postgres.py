"""Authorization before the idempotency cache for the risk PATCH/archive/
restore paths and the recommendation publish/reject/defer/pin/confirm
transitions.

Each of these read the `Idempotency-Key` cache right after the membership
and idempotency locks, before any authorization on the row it writes, so a
caller who had since lost access (suspended, demoted to `viewer`, or no
longer able to see the row after an ownership transfer) could replay a
same-key request and get the cached success. The cache is now read only
after the locked read (404) and write (403) checks pass, and still before
the version/state checks, so a still-authorized replay gets the cached 200
rather than a 409 from a row its own first request already changed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race_world
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SIDE_EFFECT_TABLES = (
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "recommendation_feedback",
    "tasks",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Governance Idempotent Replay",
        ("recommendation_feedback", "recommendations", "tasks", "risks"),
    ) as w:
        yield w


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


def _seed_risk(conn: Connection, w: RaceWorld, owner: UUID, *, archived: bool) -> UUID:
    risk_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO risks (id, workspace_id, description, probability, impact, status, "
            "owner_id, created_by, updated_by, created_at, updated_at, visibility, archived_at, "
            "pre_archive_status) "
            "VALUES (:id, :ws, 'Replay risk', 3, 3, 'identified', :owner, :owner, :owner, "
            ":now, :now, 'private', :archived_at, :pre)"
        ),
        {
            "id": risk_id,
            "ws": w.ws,
            "owner": owner,
            "now": now,
            "archived_at": now if archived else None,
            "pre": "identified" if archived else None,
        },
    )
    return risk_id


def _seed_recommendation(conn: Connection, w: RaceWorld, owner: UUID, *, status: str) -> UUID:
    recommendation_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
            "target_id, proposed_action, expected_version, rationale, confidence, status, "
            "source, created_by, updated_by, created_at, updated_at, owner_id, visibility, "
            "proposed_fields) "
            "VALUES (:id, :ws, 'replay_detected', 'task', NULL, CAST(:action AS jsonb), NULL, "
            "'Replay rationale', 0.9, :status, 'rule', :owner, :owner, :now, :now, :owner, "
            "'private', CAST(:fields AS jsonb))"
        ),
        {
            "id": recommendation_id,
            "ws": w.ws,
            "owner": owner,
            "now": now,
            "status": status,
            "action": dumps({"operation": "create", "value": None}),
            "fields": dumps({"title": "Replay task"}),
        },
    )
    return recommendation_id


@dataclass(frozen=True)
class Case:
    table: str
    method: str
    path: str
    body: Callable[[], dict[str, Any]]
    not_found: str
    seed: Callable[[Connection, RaceWorld, UUID], UUID]


def _risk(*, archived: bool = False) -> Callable[[Connection, RaceWorld, UUID], UUID]:
    return lambda conn, w, owner: _seed_risk(conn, w, owner, archived=archived)


def _rec(status: str) -> Callable[[Connection, RaceWorld, UUID], UUID]:
    return lambda conn, w, owner: _seed_recommendation(conn, w, owner, status=status)


def _v1() -> dict[str, Any]:
    return {"expected_version": 1}


_RISKS = "/api/v1/risks/{id}"
_RECS = "/api/v1/recommendations/{id}"

CASES: dict[str, Case] = {
    "risk_patch": Case(
        "risks",
        "PATCH",
        _RISKS,
        lambda: {"expected_version": 1, "description": "replay probe"},
        "RISK_NOT_FOUND",
        _risk(),
    ),
    "risk_archive": Case("risks", "POST", _RISKS + "/archive", _v1, "RISK_NOT_FOUND", _risk()),
    "risk_restore": Case(
        "risks", "POST", _RISKS + "/restore", _v1, "RISK_NOT_FOUND", _risk(archived=True)
    ),
    "recommendation_publish": Case(
        "recommendations",
        "POST",
        _RECS + "/publish",
        _v1,
        "RECOMMENDATION_NOT_FOUND",
        _rec("proposed"),
    ),
    "recommendation_reject": Case(
        "recommendations",
        "POST",
        _RECS + "/reject",
        lambda: {"expected_version": 1, "reason": "replay probe"},
        "RECOMMENDATION_NOT_FOUND",
        _rec("pending_confirmation"),
    ),
    "recommendation_defer": Case(
        "recommendations",
        "POST",
        _RECS + "/defer",
        lambda: {
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
        lambda: {"expected_version": 1, "pinned": True},
        "RECOMMENDATION_NOT_FOUND",
        _rec("proposed"),
    ),
    "recommendation_confirm": Case(
        "recommendations",
        "POST",
        _RECS + "/confirm",
        _v1,
        "RECOMMENDATION_NOT_FOUND",
        _rec("pending_confirmation"),
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


def _row(table: str, row_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(f"SELECT * FROM {table} WHERE id = :id"),  # noqa: S608
                {"id": row_id},
            )
            .mappings()
            .one()
        )


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


def _write_then_replay(
    client: TestClient, case: Case, row_id: UUID, request_headers: dict[str, str], body: Any
) -> None:
    """The first write plus a still-authorized same-key replay, which must
    get the cached 200 although the row is no longer in its pre-write state
    (a version/state check ahead of the cache would answer 409)."""
    path = case.path.format(id=row_id)
    first = client.request(case.method, path, headers=request_headers, json=body)
    assert first.status_code == 200, first.text
    replay = client.request(case.method, path, headers=request_headers, json=body)
    assert replay.status_code == 200, replay.text
    assert _without_request_id(replay.json()) == _without_request_id(first.json())


@pytest.mark.parametrize("how", ["suspended", "ownership_transferred"])
@pytest.mark.parametrize("name", list(CASES))
def test_replay_after_losing_read_access_is_refused_404(
    world: RaceWorld, name: str, how: str
) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, world.b)
    body = case.body()
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        _write_then_replay(client, case, row_id, request_headers, body)
        with engine.begin() as connection:
            if how == "suspended":
                connection.execute(
                    text(
                        "UPDATE workspace_memberships SET status = 'suspended' "
                        "WHERE workspace_id = :ws AND users_id = :b"
                    ),
                    {"ws": world.ws, "b": world.b},
                )
            else:
                # Transferred to C; still private, so B can no longer see it.
                connection.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"c": world.c, "id": row_id},
                )
        counts_before = _side_effect_counts(world.ws)
        row_before = _row(case.table, row_id)
        refused = client.request(
            case.method, case.path.format(id=row_id), headers=request_headers, json=body
        )
    finally:
        client.close()

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == case.not_found
    assert _side_effect_counts(world.ws) == counts_before
    assert _row(case.table, row_id) == row_before


@pytest.mark.parametrize("name", list(CASES))
def test_replay_after_demotion_to_viewer_is_refused_403(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, world.a)
        # Workspace-visible and owned by A: C (`member`) writes it by role
        # alone, and as a `viewer` can still read it.
        connection.execute(
            text(f"UPDATE {case.table} SET visibility = 'workspace' WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        )
    c_token = _session_token(world, world.c)
    body = case.body()
    request_headers = headers(c_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        _write_then_replay(client, case, row_id, request_headers, body)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace_memberships SET role = 'viewer' "
                    "WHERE workspace_id = :ws AND users_id = :c"
                ),
                {"ws": world.ws, "c": world.c},
            )
        counts_before = _side_effect_counts(world.ws)
        row_before = _row(case.table, row_id)
        refused = client.request(
            case.method, case.path.format(id=row_id), headers=request_headers, json=body
        )
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before
    assert _row(case.table, row_id) == row_before
