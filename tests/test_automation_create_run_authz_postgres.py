"""`POST /automations/runs` authorizes the workflow it runs.

The endpoint checked only the caller's workspace role, then handed the
caller-supplied `workflow_id` to `worker.enqueue_run`, which resolves the
active version and never asks whether the caller can see it. A member could
therefore run another member's private workflow by id.

Now the active `workflow_versions` row is locked and authorized (`read` ->
404, `write` -> 403) before the idempotency cache is read, so a same-key
replay is re-authorized too.

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
from ecc.domains.automation import policy as automation_policy
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
    b_token, c_token = f"session-{uuid4()}", f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Create Run Authz', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="member")
        c_account = create_identity(connection, workspace_id=ws, user_id=c, now=now, role="member")
        _session_row(connection, ws, b, b_token, now)
        _session_row(connection, ws, c, c_token, now)
    try:
        yield World(ws=ws, a=a, b=b, c=c, c_account=c_account, b_token=b_token, c_token=c_token)
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


def _publish_workflow(w: World, workflow_id: str) -> automation_workflows.WorkflowVersion:
    """An active workflow owned by B with a usable policy, as
    `test_automation_runs_postgres.py`'s own `_publish_workflow`."""
    graph = {
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
    with SessionFactory() as session, session.begin():
        automation_workflows.create_workflow_draft(
            session,
            w.ws,
            w.b,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
        )
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
            graph=graph,
            trigger_refs=[],
            policy_ref=policy_row.id,
        )
        activated = automation_workflows.activate_workflow_version(session, w.ws, draft.id)
    assert isinstance(activated, automation_workflows.WorkflowVersion)
    return activated


def _set_visibility(version_id: UUID, visibility: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workflow_versions SET visibility = :v WHERE id = :id"),
            {"v": visibility, "id": version_id},
        )


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


def _grant(w: World, version_id: UUID, actions: list[str]) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at) "
                "VALUES (:id, :ws, :account, 'workflow_versions', :rid, :actions, :b, now())"
            ),
            {
                "id": uuid4(),
                "ws": w.ws,
                "account": w.c_account,
                "rid": version_id,
                "actions": actions,
                "b": w.b,
            },
        )


def _run_count(ws: UUID, workflow_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM workflow_runs "
                    "WHERE workspace_id = :ws AND workflow_id = :wf"
                ),
                {"ws": ws, "wf": workflow_id},
            ).scalar_one()
        )


def _post_run(token: str, workflow_id: str, key: str | None = None) -> tuple[int, Any]:
    response = _client(token).post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key),
    )
    return response.status_code, response.json()


def test_member_cannot_run_another_members_private_workflow(world: World) -> None:
    workflow_id = f"authz.private.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")

    status, body = _post_run(world.c_token, workflow_id)

    assert status == 404, body
    assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"
    assert _run_count(world.ws, workflow_id) == 0


def test_read_only_grantee_cannot_run_shared_workflow(world: World) -> None:
    workflow_id = f"authz.readonly.{uuid4().hex[:8]}"
    active = _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")
    _set_visibility(active.id, "shared_explicitly")
    _grant(world, active.id, ["read"])

    status, body = _post_run(world.c_token, workflow_id)

    assert status == 403, body
    assert body["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _run_count(world.ws, workflow_id) == 0


def test_write_grantee_can_run_shared_workflow(world: World) -> None:
    workflow_id = f"authz.writegrant.{uuid4().hex[:8]}"
    active = _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")
    _set_visibility(active.id, "shared_explicitly")
    _grant(world, active.id, ["read", "write"])

    status, body = _post_run(world.c_token, workflow_id)

    assert status == 201, body
    assert _run_count(world.ws, workflow_id) == 1


def test_owner_and_workspace_member_can_still_run(world: World) -> None:
    private_id = f"authz.own.{uuid4().hex[:8]}"
    private = _publish_workflow(world, private_id)
    _set_visibility(private.id, "private")
    status, body = _post_run(world.b_token, private_id)
    assert status == 201, body

    shared_id = f"authz.workspace.{uuid4().hex[:8]}"
    _publish_workflow(world, shared_id)  # created `workspace`-visible
    status, body = _post_run(world.c_token, shared_id)
    assert status == 201, body


def _draft_only(w: World, workflow_id: str) -> UUID:
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            w.ws,
            w.b,
            workflow_id=workflow_id,
            graph={"steps": []},
            trigger_refs=[],
            policy_ref=None,
        )
    return draft.id


def test_invisible_inactive_workflow_answers_like_an_unknown_id(world: World) -> None:
    """No active version: an id that never existed and a private draft the
    caller cannot see both answer 404, so the status reveals nothing."""
    status, unknown = _post_run(world.c_token, f"authz.missing.{uuid4().hex[:8]}")
    assert status == 404, unknown
    assert unknown["error"]["code"] == "WORKFLOW_NOT_FOUND"

    private_id = f"authz.privdraft.{uuid4().hex[:8]}"
    _set_visibility(_draft_only(world, private_id), "private")
    status, private = _post_run(world.c_token, private_id)
    assert status == 404, private
    assert private["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_visible_inactive_workflow_is_still_409(world: World) -> None:
    workflow_id = f"authz.draft.{uuid4().hex[:8]}"
    _draft_only(world, workflow_id)  # created `workspace`-visible

    status, body = _post_run(world.c_token, workflow_id)

    assert status == 409, body
    assert body["error"]["code"] == "WORKFLOW_NOT_ACTIVE"


def test_same_key_replay_is_reauthorized(world: World) -> None:
    workflow_id = f"authz.replay.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    key = str(uuid4())

    status, first = _post_run(world.c_token, workflow_id, key)
    assert status == 201, first

    _set_family_visibility(world.ws, workflow_id, "private")
    status, replay = _post_run(world.c_token, workflow_id, key)

    assert status == 404, replay
    assert replay["error"]["code"] == "WORKFLOW_NOT_FOUND"
    assert _run_count(world.ws, workflow_id) == 1


def test_authorized_same_key_replay_returns_cached_run(world: World) -> None:
    workflow_id = f"authz.replayok.{uuid4().hex[:8]}"
    _publish_workflow(world, workflow_id)
    key = str(uuid4())

    status, first = _post_run(world.c_token, workflow_id, key)
    assert status == 201, first
    status, replay = _post_run(world.c_token, workflow_id, key)

    assert status == 201, replay
    assert replay["id"] == first["id"]
    assert _run_count(world.ws, workflow_id) == 1


def test_authorized_replay_after_disable_returns_cached_run(world: World) -> None:
    """No active version any more, but the caller can still read the
    (now disabled) version: the replay gets its cached 201, a new key the
    409. The throwaway v1 draft is made unreadable so only the disabled
    version keeps the workflow visible."""
    workflow_id = f"authz.disabled.{uuid4().hex[:8]}"
    active = _publish_workflow(world, workflow_id)
    _set_family_visibility(world.ws, workflow_id, "private")
    _set_visibility(active.id, "workspace")
    key = str(uuid4())

    status, first = _post_run(world.c_token, workflow_id, key)
    assert status == 201, first
    with SessionFactory() as session, session.begin():
        disabled = automation_workflows.disable_workflow_version(session, world.ws, active.id)
    assert isinstance(disabled, automation_workflows.WorkflowVersion)

    status, replay = _post_run(world.c_token, workflow_id, key)
    assert status == 201, replay
    assert replay["id"] == first["id"]

    status, fresh = _post_run(world.c_token, workflow_id)
    assert status == 409, fresh
    assert fresh["error"]["code"] == "WORKFLOW_NOT_ACTIVE"
    assert _run_count(world.ws, workflow_id) == 1


# ---------------------------------------------------------------------------
# The active row lock: real concurrency (request thread + a separate
# connection holding the lock), waiting observed in pg_stat_activity.
# ---------------------------------------------------------------------------

_WAIT_SECONDS = 15


def _blocked_on(holder_pid: int) -> int:
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)] "
                    "AND query ~* 'FROM workflow_versions\\s.*FOR SHARE'"
                ),
                {"holder": holder_pid},
            ).scalar_one()
        )


def _fire(token: str, workflow_id: str, result: dict[str, Any]) -> threading.Thread:
    def run() -> None:
        try:
            result["response"] = _post_run(token, workflow_id)
        except BaseException as exc:  # surfaced on the main thread
            result["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_run_waiting_on_active_row_lock_sees_committed_visibility(world: World) -> None:
    """A visibility change (as an ownership transfer would: FOR UPDATE on
    the row) commits while the request waits on the active row: the
    request must authorize against the committed row and answer 404."""
    workflow_id = f"authz.race.{uuid4().hex[:8]}"
    active = _publish_workflow(world, workflow_id)
    result: dict[str, Any] = {}
    holder = engine.connect()
    holder_tx = holder.begin()
    thread: threading.Thread | None = None
    try:
        holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
        holder.execute(
            text("SELECT id FROM workflow_versions WHERE id = :id FOR UPDATE"), {"id": active.id}
        )
        holder.execute(
            text(
                "UPDATE workflow_versions SET visibility = 'private' "
                "WHERE workspace_id = :ws AND workflow_id = :wf"
            ),
            {"ws": world.ws, "wf": workflow_id},
        )
        thread = _fire(world.c_token, workflow_id, result)
        deadline = time.monotonic() + _WAIT_SECONDS
        while _blocked_on(holder_pid) < 1:
            assert time.monotonic() < deadline, "run request never blocked on the row lock"
            time.sleep(0.05)
        holder_tx.commit()
    finally:
        if holder_tx.is_active:
            holder_tx.rollback()
        holder.close()
        if thread is not None:
            thread.join(timeout=_WAIT_SECONDS)
    assert thread is not None and not thread.is_alive(), "run request never finished"
    if "error" in result:
        raise result["error"]
    status, body = result["response"]
    assert status == 404, body
    assert body["error"]["code"] == "WORKFLOW_NOT_FOUND"
    assert _run_count(world.ws, workflow_id) == 0


def test_run_waiting_on_active_row_lock_sees_committed_publish(world: World) -> None:
    """A publish (as activate_workflow_version: retire the active row,
    promote a draft) commits while the request waits on the active row.
    The waiting statement then matches neither row, so the request must
    re-read and run the newly active version rather than fail closed
    with a spurious 409."""
    workflow_id = f"authz.publish.{uuid4().hex[:8]}"
    active = _publish_workflow(world, workflow_id)
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            world.ws,
            world.b,
            workflow_id=workflow_id,
            graph=active.graph,
            trigger_refs=[],
            policy_ref=active.policy_ref,
        )
    result: dict[str, Any] = {}
    holder = engine.connect()
    holder_tx = holder.begin()
    thread: threading.Thread | None = None
    try:
        holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
        holder.execute(
            text("SELECT id FROM workflow_versions WHERE id = :id FOR UPDATE"), {"id": active.id}
        )
        holder.execute(
            text("UPDATE workflow_versions SET status = 'retired' WHERE id = :id"),
            {"id": active.id},
        )
        holder.execute(
            text("UPDATE workflow_versions SET status = 'active' WHERE id = :id"),
            {"id": draft.id},
        )
        thread = _fire(world.c_token, workflow_id, result)
        deadline = time.monotonic() + _WAIT_SECONDS
        while _blocked_on(holder_pid) < 1:
            assert time.monotonic() < deadline, "run request never blocked on the row lock"
            time.sleep(0.05)
        holder_tx.commit()
    finally:
        if holder_tx.is_active:
            holder_tx.rollback()
        holder.close()
        if thread is not None:
            thread.join(timeout=_WAIT_SECONDS)
    assert thread is not None and not thread.is_alive(), "run request never finished"
    if "error" in result:
        raise result["error"]
    status, body = result["response"]
    assert status == 201, body
    assert body["workflow_version"] == draft.version
    assert _run_count(world.ws, workflow_id) == 1


def test_concurrent_runs_do_not_block_each_other(world: World) -> None:
    """The lock is FOR SHARE: another run holding it does not block this one."""
    workflow_id = f"authz.shared.{uuid4().hex[:8]}"
    active = _publish_workflow(world, workflow_id)
    result: dict[str, Any] = {}
    with engine.connect() as holder, holder.begin():
        holder.execute(
            text("SELECT id FROM workflow_versions WHERE id = :id FOR SHARE"), {"id": active.id}
        )
        thread = _fire(world.c_token, workflow_id, result)
        thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "run request blocked behind another FOR SHARE holder"
    if "error" in result:
        raise result["error"]
    status, body = result["response"]
    assert status == 201, body
