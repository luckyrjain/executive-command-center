"""Authorization before the idempotency cache for the automation mutation
endpoints that lock the target row before authorizing: workflow
publish/disable, policy revoke, approval approve/reject and run
cancel/pause/resume.

Each used to serve a cached response for a same-`Idempotency-Key` replay
before any per-row authorization, so a caller who had since lost access
(demoted to `viewer`, suspended, or no longer able to see the row after an
ownership transfer) could replay a cached success. The cache is now read
only after the locked read (404) and write (403) checks pass, and still
before the state checks, so a still-authorized replay of a successful
transition gets its cached 200 rather than a 409.
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
from lock_race_support import RaceWorld, headers, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.automation import workflows as automation_workflows
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_DIGEST = "a" * 64
_GRAPH: dict[str, Any] = {
    "steps": [
        {
            "step_id": "s1",
            "step_type": "condition",
            "input_mapping": {},
            "on_success": "succeeded",
            "on_failure": "failed",
        }
    ]
}
_SIDE_EFFECT_TABLES = ("audit_events", "event_outbox", "idempotency_records")
# Every automation table a seed below writes, children first.
_SEEDED_TABLES = (
    "approval_requests",
    "workflow_run_steps",
    "workflow_runs",
    "automation_policies",
    "workflow_versions",
    "workflow_definitions",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Automation Idempotent Replay Authz", _SEEDED_TABLES) as w:
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


# ---------------------------------------------------------------------------
# Seeds: each inserts the target row and its parents, all owned by `owner`
# with `visibility`, and returns the target row's id.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Owner:
    user_id: UUID
    visibility: str


def _insert_family(conn: Connection, w: RaceWorld, o: Owner, *, status: str) -> tuple[str, UUID]:
    workflow_id = f"replay-{uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, :o, now(), now(), :o, :vis)"
        ),
        {"id": uuid4(), "ws": w.ws, "wf": workflow_id, "o": o.user_id, "vis": o.visibility},
    )
    version_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workflow_versions (id, workspace_id, workflow_id, version, graph, "
            "trigger_refs, policy_ref, definition_hash, status, created_by, updated_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 1, CAST(:graph AS jsonb), '[]'::jsonb, NULL, :hash, "
            ":status, :o, :o, now(), now(), :o, :vis)"
        ),
        {
            "id": version_id,
            "ws": w.ws,
            "wf": workflow_id,
            "graph": dumps(_GRAPH),
            "hash": automation_workflows.compute_definition_hash(
                graph=_GRAPH, trigger_refs=[], policy_ref=None
            ),
            "status": status,
            "o": o.user_id,
            "vis": o.visibility,
        },
    )
    return workflow_id, version_id


def _seed_version(status: str) -> Callable[[Connection, RaceWorld, Owner], UUID]:
    def seed(conn: Connection, w: RaceWorld, o: Owner) -> UUID:
        return _insert_family(conn, w, o, status=status)[1]

    return seed


def _seed_policy(conn: Connection, w: RaceWorld, o: Owner) -> UUID:
    workflow_id, _ = _insert_family(conn, w, o, status="draft")
    policy_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO automation_policies (id, workspace_id, workflow_id, value_limit, "
            "count_limit, approval_mode, expires_at, created_by, updated_by, created_at, "
            "updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 0, 0, 'per_run', :expires, :o, :o, now(), now(), :o, "
            ":vis)"
        ),
        {
            "id": policy_id,
            "ws": w.ws,
            "wf": workflow_id,
            "expires": datetime.now(UTC) + timedelta(days=30),
            "o": o.user_id,
            "vis": o.visibility,
        },
    )
    return policy_id


def _insert_run(conn: Connection, w: RaceWorld, o: Owner, *, status: str) -> UUID:
    workflow_id, _ = _insert_family(conn, w, o, status="active")
    run_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workflow_runs (id, workspace_id, workflow_id, workflow_version, "
            "status, current_step_index, queued_at, created_by, created_at, updated_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 1, :status, 0, now(), :o, now(), now(), :o, :vis)"
        ),
        {
            "id": run_id,
            "ws": w.ws,
            "wf": workflow_id,
            "status": status,
            "o": o.user_id,
            "vis": o.visibility,
        },
    )
    return run_id


def _seed_run(status: str) -> Callable[[Connection, RaceWorld, Owner], UUID]:
    def seed(conn: Connection, w: RaceWorld, o: Owner) -> UUID:
        return _insert_run(conn, w, o, status=status)

    return seed


def _seed_approval(conn: Connection, w: RaceWorld, o: Owner) -> UUID:
    """A pending approval gating a `waiting_approval` run at step 0."""
    run_id = _insert_run(conn, w, o, status="waiting_approval")
    approval_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO approval_requests (id, workspace_id, run_id, step_index, "
            "action_digest, requested_at, expires_at, created_at, updated_at, owner_id, "
            "visibility) "
            "VALUES (:id, :ws, :run, 0, :digest, :now, :expires, :now, :now, :o, :vis)"
        ),
        {
            "id": approval_id,
            "ws": w.ws,
            "run": run_id,
            "digest": _DIGEST,
            "now": now,
            "expires": now + timedelta(days=1),
            "o": o.user_id,
            "vis": o.visibility,
        },
    )
    return approval_id


@dataclass(frozen=True)
class Case:
    table: str
    seed: Callable[[Connection, RaceWorld, Owner], UUID]
    path: str
    body: dict[str, Any] | None
    not_found: str


_WF = "/api/v1/automations/workflows/{id}"
_APPROVALS = "/api/v1/automations/approvals/{id}"
_RUNS = "/api/v1/automations/runs/{id}"

CASES: dict[str, Case] = {
    "workflow_publish": Case(
        "workflow_versions", _seed_version("draft"), _WF + "/publish", None, "WORKFLOW_NOT_FOUND"
    ),
    "workflow_disable": Case(
        "workflow_versions", _seed_version("active"), _WF + "/disable", None, "WORKFLOW_NOT_FOUND"
    ),
    "policy_revoke": Case(
        "automation_policies",
        _seed_policy,
        "/api/v1/automations/policies/{id}/revoke",
        None,
        "POLICY_NOT_FOUND",
    ),
    "approval_approve": Case(
        "approval_requests",
        _seed_approval,
        _APPROVALS + "/approve",
        {"action_digest": _DIGEST},
        "APPROVAL_NOT_FOUND",
    ),
    "approval_reject": Case(
        "approval_requests", _seed_approval, _APPROVALS + "/reject", None, "APPROVAL_NOT_FOUND"
    ),
    "run_cancel": Case(
        "workflow_runs", _seed_run("queued"), _RUNS + "/cancel", None, "RUN_NOT_FOUND"
    ),
    "run_pause": Case(
        "workflow_runs", _seed_run("running"), _RUNS + "/pause", None, "RUN_NOT_FOUND"
    ),
    "run_resume": Case(
        "workflow_runs", _seed_run("paused"), _RUNS + "/resume", None, "RUN_NOT_FOUND"
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


def _seeded_state(ws: UUID) -> dict[str, list[dict[str, Any]]]:
    """Every row of every seeded automation table -- a refused replay must
    change none of them."""
    with engine.connect() as connection:
        return {
            table: [
                dict(row)
                for row in connection.execute(
                    text(
                        f"SELECT * FROM {table} WHERE workspace_id = :ws ORDER BY id"  # noqa: S608
                    ),
                    {"ws": ws},
                ).mappings()
            ]
            for table in _SEEDED_TABLES
        }


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


def _write_then_replay(
    client: TestClient, case: Case, row_id: UUID, request_headers: dict[str, str]
) -> None:
    """The first call succeeds; a same-key replay while still authorized
    gets the cached 200 even though the row has already transitioned (so a
    fresh attempt would 409)."""
    path = case.path.format(id=row_id)
    first = client.post(path, headers=request_headers, json=case.body)
    assert first.status_code == 200, first.text
    replay = client.post(path, headers=request_headers, json=case.body)
    assert replay.status_code == 200, replay.text
    assert _without_request_id(replay.json()) == _without_request_id(first.json())


def _set_membership(w: RaceWorld, user_id: UUID, column: str, value: str) -> None:
    assert column in {"role", "status"}
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE workspace_memberships SET {column} = :value "  # noqa: S608
                "WHERE workspace_id = :ws AND users_id = :user_id"
            ),
            {"value": value, "ws": w.ws, "user_id": user_id},
        )


@pytest.mark.parametrize("name", list(CASES))
def test_idempotent_replay_after_demotion_is_refused_by_the_locked_write_check(
    world: RaceWorld, name: str
) -> None:
    """C (`member`) writes a workspace-visible row owned by A, then is
    demoted to `viewer`: it can still read the row, so only the locked
    write check -- which now runs before the cache -- refuses the replay."""
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, Owner(world.a, "workspace"))
    c_token = _session_token(world, world.c)
    request_headers = headers(c_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        _write_then_replay(client, case, row_id, request_headers)

        _set_membership(world, world.c, "role", "viewer")
        counts_before = _side_effect_counts(world.ws)
        state_before = _seeded_state(world.ws)
        refused = client.post(case.path.format(id=row_id), headers=request_headers, json=case.body)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before
    assert _seeded_state(world.ws) == state_before


def _transfer_to_c(w: RaceWorld, case: Case, row_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
            {"c": w.c, "id": row_id},
        )


def _suspend_b(w: RaceWorld, _case: Case, _row_id: UUID) -> None:
    _set_membership(w, w.b, "status", "suspended")


LOSSES: dict[str, Callable[[RaceWorld, Case, UUID], None]] = {
    "ownership_transfer": _transfer_to_c,
    "suspended": _suspend_b,
}


@pytest.mark.parametrize("loss", list(LOSSES))
@pytest.mark.parametrize("name", list(CASES))
def test_idempotent_replay_is_authorized_before_the_cache_is_served(
    world: RaceWorld, name: str, loss: str
) -> None:
    """B (`admin`) writes its own private row, then loses read access to it
    (the row is transferred to C, or B is suspended): the same key must no
    longer replay the cached success."""
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, Owner(world.b, "private"))
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        _write_then_replay(client, case, row_id, request_headers)

        LOSSES[loss](world, case, row_id)
        counts_before = _side_effect_counts(world.ws)
        state_before = _seeded_state(world.ws)
        refused = client.post(case.path.format(id=row_id), headers=request_headers, json=case.body)
    finally:
        client.close()

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == case.not_found
    assert _side_effect_counts(world.ws) == counts_before
    assert _seeded_state(world.ws) == state_before
    assert row_snapshot(case.table, row_id) == next(
        row for row in state_before[case.table] if row["id"] == row_id
    )
