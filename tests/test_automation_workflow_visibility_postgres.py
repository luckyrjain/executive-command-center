"""A private workflow stays invisible through its policies, runs and approvals.

Three leaks, all ignoring the visibility of `workflow_versions`:

- `POST /automations/policies` checked only that the `workflow_id` family
  existed. A member got 201 for another member's private workflow (and
  bound a policy to it) but 404 for an id that never existed, so the
  status code told them whether the private workflow existed.
- `worker.enqueue_run` inserts every run with `visibility='workspace'`, and
  run reads authorized only the run row. Every member could list, open and
  cancel the runs of another member's private workflow, scheduled runs
  included.
- `approval_requests` rows are inserted with `visibility='workspace'` and
  the inbox authorized only that row, so every member could list (run_id,
  digest, categories), approve or reject the approvals of those runs,
  advancing or failing a run they could not see.

Now policy create needs read (404) then write (403) on a version of the
workflow before the idempotency cache is read, and a run is visible only
while its pinned version is readable too, and an approval only while its
run is.

World: one workspace with A (`owner`), B (`member`, the workflow's owner)
and C (`member`, the caller).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import approvals as automation_approvals
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapter_contract import ACTION_TYPES
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@dataclass
class World:
    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    c_account: UUID
    a_token: str
    b_token: str
    c_token: str


def _session_row(
    connection: Connection, ws: UUID, user_id: UUID, token: str, now: datetime
) -> None:
    connection.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
            "expires_at, last_seen_at) "
            "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
        ),
        {
            "id": uuid4(),
            "workspace_id": ws,
            "user_id": user_id,
            "token_hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "last_seen_at": now,
        },
    )


def _workspace_tables() -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema = 'public' AND c.column_name = 'workspace_id' "
                    "AND t.table_type = 'BASE TABLE' AND c.table_name <> 'workspaces'"
                )
            ).scalars()
        )


def _delete_workspace(ws: UUID) -> None:
    """Deletes every workspace-scoped row, retrying in passes so foreign
    keys resolve without a hand-maintained order."""
    with engine.connect() as connection:
        account_ids = list(
            connection.execute(
                text("SELECT account_id FROM users WHERE workspace_id = :ws"), {"ws": ws}
            ).scalars()
        )
    pending = _workspace_tables()
    for _ in range(10):
        failed: list[str] = []
        with engine.begin() as connection:
            for table in pending:
                savepoint = connection.begin_nested()
                try:
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                        {"ws": ws},
                    )
                    savepoint.commit()
                except Exception:  # noqa: BLE001 -- FK order, retried next pass
                    savepoint.rollback()
                    failed.append(table)
        if not failed:
            break
        pending = failed
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
        connection.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids})


@pytest.fixture
def world() -> Iterator[World]:
    ws, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    a_token, b_token, c_token = (f"session-{uuid4()}" for _ in range(3))
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Workflow Visibility', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="member")
        c_account = create_identity(connection, workspace_id=ws, user_id=c, now=now, role="member")
        _session_row(connection, ws, a, a_token, now)
        _session_row(connection, ws, b, b_token, now)
        _session_row(connection, ws, c, c_token, now)
    try:
        yield World(
            ws=ws,
            a=a,
            b=b,
            c=c,
            c_account=c_account,
            a_token=a_token,
            b_token=b_token,
            c_token=c_token,
        )
    finally:
        _delete_workspace(ws)


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": key or str(uuid4()),
    }


def _client(token: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    return client


_GRAPH = {
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


def _draft_workflow(w: World, workflow_id: str) -> automation_workflows.WorkflowVersion:
    """A draft owned by B, with no active version."""
    with SessionFactory() as session, session.begin():
        return automation_workflows.create_workflow_draft(
            session,
            w.ws,
            w.b,
            workflow_id=workflow_id,
            graph=_GRAPH,
            trigger_refs=[],
            policy_ref=None,
        )


def _publish_workflow(w: World, workflow_id: str) -> automation_workflows.WorkflowVersion:
    """An active workflow owned by B with a usable policy, as
    `test_automation_runs_postgres.py`'s own `_publish_workflow`."""
    _draft_workflow(w, workflow_id)
    with SessionFactory() as session, session.begin():
        policy_row = automation_policy.create_policy(
            session,
            w.ws,
            w.b,
            workflow_id=workflow_id,
            action_types=sorted(ACTION_TYPES),
            data_classes=["sensitive"],
            value_limit=Decimal("1000000"),
            count_limit=1000,
            rate_limit=None,
            schedule=None,
            approval_mode="bounded_recurring",
        )
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            w.ws,
            w.b,
            workflow_id=workflow_id,
            graph=_GRAPH,
            trigger_refs=[],
            policy_ref=policy_row.id,
        )
        activated = automation_workflows.activate_workflow_version(session, w.ws, draft.id)
    assert isinstance(activated, automation_workflows.WorkflowVersion)
    return activated


def _set_family_visibility(ws: UUID, workflow_id: str, visibility: str) -> None:
    """Every version of the workflow, so no stray draft stays readable."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_versions SET visibility = :v "
                "WHERE workspace_id = :ws AND workflow_id = :wf"
            ),
            {"v": visibility, "ws": ws, "wf": workflow_id},
        )


def _grant_family(w: World, workflow_id: str, actions: list[str]) -> None:
    """Grants C `actions` on every version of the workflow."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at) "
                "SELECT gen_random_uuid(), :ws, :account, 'workflow_versions', id, "
                ":actions, :b, now() FROM workflow_versions "
                "WHERE workspace_id = :ws AND workflow_id = :wf"
            ),
            {"ws": w.ws, "account": w.c_account, "actions": actions, "b": w.b, "wf": workflow_id},
        )


def _policy_count(ws: UUID, workflow_id: str, created_by: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM automation_policies "
                    "WHERE workspace_id = :ws AND workflow_id = :wf AND created_by = :by"
                ),
                {"ws": ws, "wf": workflow_id, "by": created_by},
            ).scalar_one()
        )


def _post_policy(token: str, workflow_id: str, key: str | None = None) -> tuple[int, Any]:
    response = _client(token).post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "action_types": ["note.create"],
            "data_classes": ["internal"],
            "value_limit": "100",
            "count_limit": 5,
            "approval_mode": "bounded_recurring",
        },
        headers=_headers(token, key),
    )
    return response.status_code, response.json()


def _enqueue(w: World, workflow_id: str, actor: UUID) -> UUID:
    """Enqueues as the scheduler does: `enqueue_run` in the actor's name."""
    with SessionFactory() as session, session.begin():
        run = automation_worker.enqueue_run(session, w.ws, actor, workflow_id=workflow_id)
    assert isinstance(run, automation_worker.WorkflowRun)
    return run.id


def _listed_run_ids(token: str, status: str | None = None) -> set[str]:
    params = {"status": status} if status else None
    response = _client(token).get("/api/v1/automations/runs", params=params)
    assert response.status_code == 200, response.text
    return {run["id"] for run in response.json()["runs"]}


def _get_run(token: str, run_id: UUID) -> int:
    return _client(token).get(f"/api/v1/automations/runs/{run_id}").status_code


def _mutate_run(token: str, run_id: UUID, action: str = "cancel") -> tuple[int, Any]:
    response = _client(token).post(
        f"/api/v1/automations/runs/{run_id}/{action}", headers=_headers(token)
    )
    return response.status_code, response.json()


def _run_status(ws: UUID, run_id: UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT status FROM workflow_runs WHERE workspace_id = :ws AND id = :id"),
                {"ws": ws, "id": run_id},
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# POST /automations/policies
# ---------------------------------------------------------------------------


def test_policy_create_for_private_workflow_is_indistinguishable_from_missing(
    world: World,
) -> None:
    workflow_id = f"vis.private.{uuid4().hex[:8]}"
    _draft_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")

    private_status, private_body = _post_policy(world.c_token, workflow_id)
    missing_status, missing_body = _post_policy(world.c_token, f"vis.missing.{uuid4().hex[:8]}")

    assert private_status == missing_status == 404, private_body
    assert private_body["error"]["code"] == missing_body["error"]["code"] == "WORKFLOW_NOT_FOUND"
    assert _policy_count(world.ws, workflow_id, world.c) == 0


def test_policy_create_for_workspace_workflow_succeeds(world: World) -> None:
    workflow_id = f"vis.workspace.{uuid4().hex[:8]}"
    _draft_workflow(world, workflow_id)

    status, body = _post_policy(world.c_token, workflow_id)

    assert status == 201, body
    assert _policy_count(world.ws, workflow_id, world.c) == 1


def test_policy_create_owner_of_private_workflow_succeeds(world: World) -> None:
    workflow_id = f"vis.own.{uuid4().hex[:8]}"
    _draft_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")

    status, body = _post_policy(world.b_token, workflow_id)

    assert status == 201, body


def test_policy_create_with_read_only_grant_is_forbidden(world: World) -> None:
    workflow_id = f"vis.readgrant.{uuid4().hex[:8]}"
    _draft_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "shared_explicitly")
    _grant_family(world, workflow_id, ["read"])

    status, body = _post_policy(world.c_token, workflow_id)

    assert status == 403, body
    assert body["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _policy_count(world.ws, workflow_id, world.c) == 0


def test_policy_create_with_write_grant_succeeds(world: World) -> None:
    workflow_id = f"vis.writegrant.{uuid4().hex[:8]}"
    _draft_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "shared_explicitly")
    _grant_family(world, workflow_id, ["read", "write"])

    status, body = _post_policy(world.c_token, workflow_id)

    assert status == 201, body


def test_policy_create_checks_the_active_version_not_any_version(world: World) -> None:
    """A stray workspace-visible draft does not reopen a workflow whose
    active version is private."""
    workflow_id = f"vis.mixed.{uuid4().hex[:8]}"
    active = _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "workspace")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workflow_versions SET visibility = 'private' WHERE id = :id"),
            {"id": active.id},
        )

    status, body = _post_policy(world.c_token, workflow_id)

    assert status == 404, body
    assert _policy_count(world.ws, workflow_id, world.c) == 0


def test_policy_create_replay_is_reauthorized(world: World) -> None:
    """ADR-0014: the cache is read only after authorization, so a same-key
    replay after the workflow went private answers 404, not the cached 201."""
    workflow_id = f"vis.replay.{uuid4().hex[:8]}"
    _draft_workflow(world, workflow_id)
    key = str(uuid4())

    first_status, first_body = _post_policy(world.c_token, workflow_id, key)
    assert first_status == 201, first_body

    _set_family_visibility(world.ws, workflow_id, "private")
    replay_status, replay_body = _post_policy(world.c_token, workflow_id, key)

    assert replay_status == 404, replay_body
    assert replay_body["error"]["code"] == "WORKFLOW_NOT_FOUND"


def _blocked_by(holder_pid: int) -> bool:
    """A fresh connection per poll: `pg_stat_activity` is a snapshot that
    stays fixed for the rest of the reading transaction."""
    with engine.connect() as probe:
        return bool(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)]"
                ),
                {"holder": holder_pid},
            ).scalar_one()
        )


def test_policy_create_does_not_deadlock_with_draft_create(world: World) -> None:
    """Draft create locks the family row `FOR UPDATE`, then the latest
    version. Policy create used to lock the version first and then wait on
    the family (its insert's foreign key), so the two deadlocked. Here a
    holder takes draft create's locks in its order around a policy create:
    the policy create must queue on the family, holding no version lock."""
    workflow_id = f"vis.deadlock.{uuid4().hex[:8]}"
    draft = _draft_workflow(world, workflow_id)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["status"], result["body"] = _post_policy(world.c_token, workflow_id)
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    holder = engine.connect()
    holder_tx = holder.begin()
    thread = threading.Thread(target=fire)
    try:
        holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
        holder.execute(
            text(
                "SELECT id FROM workflow_definitions "
                "WHERE workspace_id = :ws AND workflow_id = :wf FOR UPDATE"
            ),
            {"ws": world.ws, "wf": workflow_id},
        )
        thread.start()
        deadline = time.monotonic() + 15
        while not _blocked_by(holder_pid):
            assert time.monotonic() < deadline, "policy create never queued on the family"
            time.sleep(0.05)
        holder.execute(text("SET LOCAL lock_timeout = '5s'"))
        holder.execute(
            text("SELECT id FROM workflow_versions WHERE id = :id FOR UPDATE"), {"id": draft.id}
        )
        holder_tx.commit()
    finally:
        if holder_tx.is_active:
            holder_tx.rollback()
        holder.close()
        thread.join(timeout=15)

    assert not thread.is_alive()
    if "error" in result:
        raise result["error"]
    assert result["status"] == 201, result["body"]


# ---------------------------------------------------------------------------
# Run reads and mutations
# ---------------------------------------------------------------------------


def test_runs_of_private_workflow_are_hidden_from_other_members(world: World) -> None:
    """B's own run of B's private workflow, enqueued the way the scheduler
    does (in the trigger creator's name)."""
    workflow_id = f"vis.runs.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")
    run_id = _enqueue(world, workflow_id, world.b)

    assert str(run_id) not in _listed_run_ids(world.c_token)
    assert _get_run(world.c_token, run_id) == 404
    assert str(run_id) not in _listed_run_ids(world.c_token, status="queued")
    for action in ("cancel", "pause"):
        status, body = _mutate_run(world.c_token, run_id, action)
        assert status == 404, (action, body)
        assert body["error"]["code"] == "RUN_NOT_FOUND"
    assert _run_status(world.ws, run_id) == "queued"

    # A workspace owner role does not see into a member's private workflow.
    assert str(run_id) not in _listed_run_ids(world.a_token)
    assert _get_run(world.a_token, run_id) == 404

    assert str(run_id) in _listed_run_ids(world.b_token)
    assert str(run_id) in _listed_run_ids(world.b_token, status="queued")
    assert _get_run(world.b_token, run_id) == 200


def test_runs_of_workspace_workflow_stay_visible(world: World) -> None:
    workflow_id = f"vis.wsruns.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    run_id = _enqueue(world, workflow_id, world.b)

    assert str(run_id) in _listed_run_ids(world.c_token)
    assert _get_run(world.c_token, run_id) == 200


def test_run_follows_its_pinned_version_going_private(world: World) -> None:
    """A run C started while the workflow was workspace-visible disappears
    for C once B makes it private: the run's detail is the workflow's."""
    workflow_id = f"vis.later.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    run_id = _enqueue(world, workflow_id, world.c)
    assert _get_run(world.c_token, run_id) == 200

    _set_family_visibility(world.ws, workflow_id, "private")

    assert str(run_id) not in _listed_run_ids(world.c_token)
    assert _get_run(world.c_token, run_id) == 404
    status, _ = _mutate_run(world.c_token, run_id)
    assert status == 404
    assert _run_status(world.ws, run_id) == "queued"


def test_run_of_shared_workflow_is_visible_to_grantee(world: World) -> None:
    workflow_id = f"vis.shared.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "shared_explicitly")
    _grant_family(world, workflow_id, ["read"])
    run_id = _enqueue(world, workflow_id, world.b)

    assert str(run_id) in _listed_run_ids(world.c_token)
    assert _get_run(world.c_token, run_id) == 200


# ---------------------------------------------------------------------------
# Approval inbox
# ---------------------------------------------------------------------------

_DIGEST = "a" * 64


def _pending_approval(w: World, workflow_id: str, actor: UUID) -> tuple[UUID, UUID]:
    """A run paused on step 0 with a pending approval, as the dispatch gate
    leaves it (`approval_requests` rows are always `visibility='workspace'`)."""
    run_id = _enqueue(w, workflow_id, actor)
    with SessionFactory() as session, session.begin():
        session.execute(
            text(
                "UPDATE workflow_runs SET status = 'waiting_approval', current_step_index = 0 "
                "WHERE workspace_id = :ws AND id = :id"
            ),
            {"ws": w.ws, "id": run_id},
        )
        approval = automation_approvals.create_approval_request(
            session, w.ws, run_id, 0, _DIGEST, frozenset({"person-directed"})
        )
    return run_id, approval.id


def _listed_approval_ids(token: str, status: str | None = None) -> set[str]:
    params = {"status": status} if status else None
    response = _client(token).get("/api/v1/automations/approvals", params=params)
    assert response.status_code == 200, response.text
    return {approval["id"] for approval in response.json()["approvals"]}


def _decide(
    token: str, approval_id: UUID, decision: str, key: str | None = None
) -> tuple[int, Any]:
    response = _client(token).post(
        f"/api/v1/automations/approvals/{approval_id}/{decision}",
        json={"action_digest": _DIGEST} if decision == "approve" else None,
        headers=_headers(token, key),
    )
    return response.status_code, response.json()


def _approval_status(ws: UUID, approval_id: UUID) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text("SELECT status FROM approval_requests WHERE workspace_id = :ws AND id = :id"),
                {"ws": ws, "id": approval_id},
            ).scalar_one()
        )


def test_approvals_for_private_workflow_runs_are_hidden(world: World) -> None:
    workflow_id = f"vis.appr.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")
    run_id, approval_id = _pending_approval(world, workflow_id, world.b)

    assert str(approval_id) not in _listed_approval_ids(world.c_token)
    assert str(approval_id) not in _listed_approval_ids(world.c_token, status="pending")
    assert str(approval_id) not in _listed_approval_ids(world.a_token)

    assert str(approval_id) in _listed_approval_ids(world.b_token)
    assert str(approval_id) in _listed_approval_ids(world.b_token, status="pending")


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_deciding_a_private_workflow_runs_approval_is_not_found(
    world: World, decision: str
) -> None:
    workflow_id = f"vis.decide.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")
    run_id, approval_id = _pending_approval(world, workflow_id, world.b)

    for token in (world.c_token, world.a_token):
        status, body = _decide(token, approval_id, decision)
        assert status == 404, (decision, body)
        assert body["error"]["code"] == "APPROVAL_NOT_FOUND"
    assert _approval_status(world.ws, approval_id) == "pending"
    assert _run_status(world.ws, run_id) == "waiting_approval"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_deciding_a_workspace_workflow_runs_approval_succeeds(world: World, decision: str) -> None:
    workflow_id = f"vis.wsdecide.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    run_id, approval_id = _pending_approval(world, workflow_id, world.b)

    assert str(approval_id) in _listed_approval_ids(world.c_token)
    status, body = _decide(world.c_token, approval_id, decision)
    assert status == 200, body
    assert _run_status(world.ws, run_id) == "queued"


def test_approval_of_shared_workflow_run_is_visible_to_grantee(world: World) -> None:
    workflow_id = f"vis.apprshared.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "shared_explicitly")
    _grant_family(world, workflow_id, ["read"])
    _run_id, approval_id = _pending_approval(world, workflow_id, world.b)

    assert str(approval_id) in _listed_approval_ids(world.c_token)
    status, body = _decide(world.c_token, approval_id, "reject")
    assert status == 200, body


def test_approval_follows_its_runs_pinned_version_going_private(world: World) -> None:
    workflow_id = f"vis.apprlater.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    run_id, approval_id = _pending_approval(world, workflow_id, world.c)
    assert str(approval_id) in _listed_approval_ids(world.c_token)

    _set_family_visibility(world.ws, workflow_id, "private")

    assert str(approval_id) not in _listed_approval_ids(world.c_token)
    status, _ = _decide(world.c_token, approval_id, "reject")
    assert status == 404
    assert _run_status(world.ws, run_id) == "waiting_approval"


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_decision_replay_is_reauthorized_against_the_run(world: World, decision: str) -> None:
    """ADR-0014: the cache is read only after the run check, so a same-key
    replay after the workflow went private answers 404, not the cached 200."""
    workflow_id = f"vis.apprreplay.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    _run_id, approval_id = _pending_approval(world, workflow_id, world.b)
    key = str(uuid4())

    first_status, first_body = _decide(world.c_token, approval_id, decision, key)
    assert first_status == 200, first_body

    _set_family_visibility(world.ws, workflow_id, "private")
    replay_status, replay_body = _decide(world.c_token, approval_id, decision, key)

    assert replay_status == 404, replay_body
    assert replay_body["error"]["code"] == "APPROVAL_NOT_FOUND"


def test_decision_waits_for_an_in_flight_visibility_change(world: World) -> None:
    """The pinned version is locked `FOR SHARE` before it is checked, so a
    decision racing a visibility change queues behind it and then sees it,
    rather than authorizing against the pre-change row."""
    workflow_id = f"vis.apprrace.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    run_id, approval_id = _pending_approval(world, workflow_id, world.b)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["status"], result["body"] = _decide(world.c_token, approval_id, "approve")
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    holder = engine.connect()
    holder_tx = holder.begin()
    thread = threading.Thread(target=fire)
    try:
        holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
        holder.execute(
            text(
                "UPDATE workflow_versions SET visibility = 'private' "
                "WHERE workspace_id = :ws AND workflow_id = :wf"
            ),
            {"ws": world.ws, "wf": workflow_id},
        )
        thread.start()
        deadline = time.monotonic() + 15
        while not _blocked_by(holder_pid):
            assert time.monotonic() < deadline, "decision never queued on the version"
            time.sleep(0.05)
        holder_tx.commit()
    finally:
        if holder_tx.is_active:
            holder_tx.rollback()
        holder.close()
        thread.join(timeout=15)

    assert not thread.is_alive()
    if "error" in result:
        raise result["error"]
    assert result["status"] == 404, result["body"]
    assert _approval_status(world.ws, approval_id) == "pending"
    assert _run_status(world.ws, run_id) == "waiting_approval"
