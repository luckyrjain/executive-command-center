"""Authorization of `POST /automations/policies` against its `workflow_id`
family.

`create_policy_endpoint` used to check only that some `workflow_definitions`
row with that `workflow_id` existed. Any writer could create a policy for
another member's private workflow, and the `404`-vs-`201` difference told
them whether a hidden family existed. The idempotency cache was also read
before that check, so a same-key replay was never re-authorized.

A policy is authority over its workflow family, so creating one is
authorized as an edit of that family: the caller must be able to read (else
`404 WORKFLOW_NOT_FOUND`, the same answer an unknown `workflow_id` gets) and
write (else `403 INSUFFICIENT_ROLE`) its latest and active versions. This
runs under the row locks and before the cache (ADR-0014).
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
from lock_race_support import RaceWorld, headers, race, race_world
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
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
_SEEDED_TABLES = (
    "resource_grants",
    "automation_policies",
    "workflow_versions",
    "workflow_definitions",
)
_POLICIES = "/api/v1/automations/policies"


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Automation Policy Family Authz", _SEEDED_TABLES) as w:
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
    workflow_id = f"family-{uuid4().hex[:12]}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
                "created_at, updated_at, owner_id, visibility) "
                "VALUES (:id, :ws, :wf, :o, now(), now(), :o, 'workspace')"
            ),
            {"id": uuid4(), "ws": w.ws, "wf": workflow_id, "o": owner},
        )
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


def _side_effects(w: RaceWorld) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: int(
                conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": w.ws},
                ).scalar_one()
            )
            for table in ("automation_policies", "audit_events", "event_outbox")
        }


def _create(
    client: TestClient,
    token: str,
    workflow_id: str,
    *,
    request_headers: dict[str, str] | None = None,
) -> Any:
    return client.post(
        _POLICIES,
        headers=request_headers or headers(token),
        json={
            "workflow_id": workflow_id,
            "value_limit": "0",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
    )


def _assert_refused(response: Any, status_code: int, code: str) -> None:
    assert response.status_code == status_code, response.text
    assert response.json()["error"]["code"] == code


def test_member_cannot_create_a_policy_for_another_members_private_family(
    world: RaceWorld,
) -> None:
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="private")
    c_token = _session_token(world, world.c)
    effects_before = _side_effects(world)
    with _client(c_token) as client:
        response = _create(client, c_token, workflow_id)

    _assert_refused(response, 404, "WORKFLOW_NOT_FOUND")
    assert _side_effects(world) == effects_before


def test_hidden_family_refusal_matches_unknown_workflow(world: RaceWorld) -> None:
    """No existence oracle: a hidden family and a `workflow_id` that does
    not exist answer the same body."""
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="private")
    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        hidden = _create(client, c_token, workflow_id)
        unknown = _create(client, c_token, f"missing-{uuid4().hex[:12]}")

    for response in (hidden, unknown):
        _assert_refused(response, 404, "WORKFLOW_NOT_FOUND")
    hidden_error, unknown_error = hidden.json()["error"], unknown.json()["error"]
    hidden_error.pop("request_id")
    unknown_error.pop("request_id")
    assert hidden_error == unknown_error


def test_hidden_active_version_blocks_policy_even_when_latest_is_visible(
    world: RaceWorld,
) -> None:
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="private")
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
    effects_before = _side_effects(world)
    with _client(c_token) as client:
        response = _create(client, c_token, workflow_id)

    _assert_refused(response, 404, "WORKFLOW_NOT_FOUND")
    assert _side_effects(world) == effects_before


def test_read_only_grant_on_the_family_is_403(world: RaceWorld) -> None:
    workflow_id, version_id = _seed_family(world, owner=world.b, visibility="shared_explicitly")
    _grant_read(world, grantee=world.c, version_id=version_id)
    c_token = _session_token(world, world.c)
    effects_before = _side_effects(world)
    with _client(c_token) as client:
        response = _create(client, c_token, workflow_id)

    _assert_refused(response, 403, "INSUFFICIENT_ROLE")
    assert _side_effects(world) == effects_before


def test_member_may_create_a_policy_for_a_workspace_visible_family(world: RaceWorld) -> None:
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="workspace")
    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        response = _create(client, c_token, workflow_id)

    assert response.status_code == 201, response.text
    assert response.json()["workflow_id"] == workflow_id


def test_owner_may_create_a_policy_for_own_private_family(world: RaceWorld) -> None:
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="private")
    with _client(world.b_token) as client:
        response = _create(client, world.b_token, workflow_id)

    assert response.status_code == 201, response.text


def test_create_replay_is_reauthorized_against_the_family(world: RaceWorld) -> None:
    """C creates a policy for B's workspace-visible family; B then makes the
    family private. The same-key replay must not hand C the cached policy."""
    workflow_id, version_id = _seed_family(world, owner=world.b, visibility="workspace")
    c_token = _session_token(world, world.c)
    request_headers = headers(c_token)
    with _client(c_token) as client:
        first = _create(client, c_token, workflow_id, request_headers=request_headers)
        assert first.status_code == 201, first.text
        replay = _create(client, c_token, workflow_id, request_headers=request_headers)
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == first.json()["id"]

        with engine.begin() as conn:
            conn.execute(
                text("UPDATE workflow_versions SET visibility = 'private' WHERE id = :id"),
                {"id": version_id},
            )
        effects_before = _side_effects(world)
        refused = _create(client, c_token, workflow_id, request_headers=request_headers)

    _assert_refused(refused, 404, "WORKFLOW_NOT_FOUND")
    assert _side_effects(world) == effects_before


def test_family_made_private_while_create_waits_is_refused(world: RaceWorld) -> None:
    """Lock before authorizing: a visibility change committed while the
    request waits on the version row lock is what the checks see."""
    workflow_id, version_id = _seed_family(world, owner=world.a, visibility="workspace")
    effects_before = _side_effects(world)

    def make_private(conn: Connection) -> None:
        conn.execute(
            text("UPDATE workflow_versions SET visibility = 'private' WHERE id = :id"),
            {"id": version_id},
        )

    response, _ = race(
        world,
        table="workflow_versions",
        row_id=version_id,
        send=lambda client: _create(client, world.b_token, workflow_id),
        transfer=True,
        mutate=make_private,
    )

    _assert_refused(response, 404, "WORKFLOW_NOT_FOUND")
    assert _side_effects(world)["automation_policies"] == effects_before["automation_policies"]
