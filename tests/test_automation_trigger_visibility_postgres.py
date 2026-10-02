"""Who can read a `triggers` row.

`create_trigger` stores every trigger with `owner_id = created_by` and
`visibility = 'workspace'`, and `GET /automations/triggers` authorized only
that row. Every member was shown the triggers of another member's private
workflow: its `workflow_id`, schedule expression, timezone and event filter.

A trigger is listed only while the caller can read both the trigger row and
its workflow's governing version (the active version, else the latest), the
rule `GET /automations/policies` applies to policies. The check runs on
every read, so a workflow made private later hides its triggers from then
on, and a family with no version hides them.

World (`lock_race_support.race_world`): A (`owner`), B (`admin`, the
workflow's owner) and C (`member`, the caller).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, race_world
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import triggers as automation_triggers
from ecc.domains.automation import workflows as automation_workflows
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

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
_SEEDED_TABLES = ("resource_grants", "triggers", "workflow_versions", "workflow_definitions")
_TRIGGERS = "/api/v1/automations/triggers"


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Automation Trigger Visibility", _SEEDED_TABLES) as w:
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


def _client(token: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    return client


def _insert_family(conn: Connection, w: RaceWorld, *, owner: UUID) -> str:
    workflow_id = f"family-{uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, :o, now(), now(), :o, 'workspace')"
        ),
        {"id": uuid4(), "ws": w.ws, "wf": workflow_id, "o": owner},
    )
    return workflow_id


def _insert_version(
    conn: Connection,
    w: RaceWorld,
    *,
    workflow_id: str,
    version: int,
    owner: UUID,
    visibility: str,
    status: str,
) -> UUID:
    version_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workflow_versions (id, workspace_id, workflow_id, version, graph, "
            "trigger_refs, policy_ref, definition_hash, status, created_by, updated_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, :version, CAST(:graph AS jsonb), '[]'::jsonb, NULL, "
            ":hash, :status, :o, :o, now(), now(), :o, :vis)"
        ),
        {
            "id": version_id,
            "ws": w.ws,
            "wf": workflow_id,
            "version": version,
            "graph": dumps(_GRAPH),
            "hash": automation_workflows.compute_definition_hash(
                graph=_GRAPH, trigger_refs=[], policy_ref=None
            ),
            "status": status,
            "o": owner,
            "vis": visibility,
        },
    )
    return version_id


def _seed_family(w: RaceWorld, *, owner: UUID, visibility: str) -> tuple[str, UUID]:
    with engine.begin() as conn:
        workflow_id = _insert_family(conn, w, owner=owner)
        version_id = _insert_version(
            conn,
            w,
            workflow_id=workflow_id,
            version=1,
            owner=owner,
            visibility=visibility,
            status="active",
        )
    return workflow_id, version_id


def _create_trigger(w: RaceWorld, workflow_id: str) -> UUID:
    """No HTTP create path exists: workflows are authored with bare
    `trigger_refs`, and trigger rows come from `create_trigger` (seed and
    scheduler setup), stamped with the creator as owner."""
    with SessionFactory() as session:
        trigger = automation_triggers.create_trigger(
            session,
            w.ws,
            w.b,
            workflow_id=workflow_id,
            trigger_type="schedule",
            schedule_expression="0 9 * * 1",
            timezone="UTC",
        )
        session.commit()
    return trigger.id


def _set_visibility(version_id: UUID, visibility: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE workflow_versions SET visibility = :vis WHERE id = :id"),
            {"id": version_id, "vis": visibility},
        )


def _grant_read(w: RaceWorld, *, grantee: UUID, version_id: UUID) -> None:
    with engine.begin() as conn:
        account_id = conn.execute(
            text("SELECT account_id FROM users WHERE id = :id"), {"id": grantee}
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at) "
                "VALUES (:id, :ws, :grantee, 'workflow_versions', :rid, ARRAY['read'], "
                ":by, now())"
            ),
            {"id": uuid4(), "ws": w.ws, "grantee": account_id, "rid": version_id, "by": w.a},
        )


def _listed(client: TestClient, workflow_id: str | None = None) -> set[UUID]:
    params = {"workflow_id": workflow_id} if workflow_id is not None else None
    response = client.get(_TRIGGERS, params=params)
    assert response.status_code == 200, response.text
    return {UUID(t["id"]) for t in response.json()["triggers"]}


def test_trigger_on_another_members_private_workflow_is_not_listed(world: RaceWorld) -> None:
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="private")
    trigger_id = _create_trigger(world, workflow_id)

    with _client(world.b_token) as client:
        assert trigger_id in _listed(client)
        assert _listed(client, workflow_id) == {trigger_id}

    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        assert trigger_id not in _listed(client)
        assert _listed(client, workflow_id) == set()


def test_workspace_owner_role_does_not_see_a_private_workflows_trigger(
    world: RaceWorld,
) -> None:
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="private")
    trigger_id = _create_trigger(world, workflow_id)

    a_token = _session_token(world, world.a)
    with _client(a_token) as client:
        assert trigger_id not in _listed(client)


def test_trigger_on_a_workspace_visible_workflow_is_listed(world: RaceWorld) -> None:
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="workspace")
    trigger_id = _create_trigger(world, workflow_id)

    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        assert _listed(client, workflow_id) == {trigger_id}


def test_trigger_is_hidden_once_its_workflow_turns_private(world: RaceWorld) -> None:
    """Evaluated per read, not copied at creation."""
    workflow_id, version_id = _seed_family(world, owner=world.b, visibility="workspace")
    trigger_id = _create_trigger(world, workflow_id)

    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        assert _listed(client, workflow_id) == {trigger_id}
        _set_visibility(version_id, "private")
        assert _listed(client, workflow_id) == set()


def test_read_grant_on_the_workflow_lists_its_trigger(world: RaceWorld) -> None:
    workflow_id, version_id = _seed_family(world, owner=world.b, visibility="shared_explicitly")
    trigger_id = _create_trigger(world, workflow_id)
    _grant_read(world, grantee=world.c, version_id=version_id)

    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        assert _listed(client, workflow_id) == {trigger_id}


def test_visible_draft_does_not_reopen_a_private_active_version(world: RaceWorld) -> None:
    workflow_id, active_id = _seed_family(world, owner=world.b, visibility="private")
    trigger_id = _create_trigger(world, workflow_id)
    with engine.begin() as conn:
        _insert_version(
            conn,
            world,
            workflow_id=workflow_id,
            version=2,
            owner=world.c,
            visibility="workspace",
            status="draft",
        )

    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        assert _listed(client, workflow_id) == set()
    with _client(world.b_token) as client:
        assert _listed(client, workflow_id) == {trigger_id}


def test_trigger_of_a_family_with_no_version_is_hidden(world: RaceWorld) -> None:
    with engine.begin() as conn:
        workflow_id = _insert_family(conn, world, owner=world.b)
    trigger_id = _create_trigger(world, workflow_id)

    with _client(world.b_token) as client:
        assert trigger_id not in _listed(client)
