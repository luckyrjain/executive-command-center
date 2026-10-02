"""Authorization of `POST /automations/workflows` against an existing
`workflow_id` family, and of publish against the active version it retires.

`create_workflow_endpoint` used to append a new draft to any `workflow_id`
family in the workspace without authorizing the caller against it. The caller
owns that new draft, so publishing it passed authz on the draft and then
retired the family's active version -- someone else's, possibly private --
unchecked. A member could take over another member's private workflow, and
the response's `version` (previous + 1, or 1) told them whether a hidden
family existed.

Now, when the family exists, the caller must be able to read (else `404
WORKFLOW_NOT_FOUND`) and write (else `403 INSUFFICIENT_ROLE`) both its latest
version and its active version. Every way of failing the read check answers
the same 404, which is also what an unknown `version_id` answers, and no
version number is disclosed. A `policy_ref` the caller cannot read answers
`404 POLICY_NOT_FOUND`, exactly like one that does not exist. Publish
requires write on the version it retires. A same-key replay is re-authorized
before the cache (ADR-0014).
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
_WORKFLOWS = "/api/v1/automations/workflows"


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Automation Workflow Family Authz", _SEEDED_TABLES) as w:
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


def _insert_family(conn: Connection, w: RaceWorld, owner: UUID, workflow_id: str) -> None:
    conn.execute(
        text(
            "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, :o, now(), now(), :o, 'workspace')"
        ),
        {"id": uuid4(), "ws": w.ws, "wf": workflow_id, "o": owner},
    )


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


def _seed_family(
    w: RaceWorld, *, owner: UUID, visibility: str, status: str = "active"
) -> tuple[str, UUID]:
    workflow_id = f"family-{uuid4().hex[:12]}"
    with engine.begin() as conn:
        _insert_family(conn, w, owner, workflow_id)
        version_id = _insert_version(
            conn,
            w,
            workflow_id=workflow_id,
            version=1,
            owner=owner,
            visibility=visibility,
            status=status,
        )
    return workflow_id, version_id


def _seed_policy(w: RaceWorld, *, owner: UUID, visibility: str) -> UUID:
    workflow_id, _ = _seed_family(w, owner=owner, visibility="workspace", status="draft")
    policy_id = uuid4()
    with engine.begin() as conn:
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
                "o": owner,
                "vis": visibility,
            },
        )
    return policy_id


def _grant_read(w: RaceWorld, *, grantee: UUID, resource_type: str, resource_id: UUID) -> None:
    with engine.begin() as conn:
        account_id = conn.execute(
            text("SELECT account_id FROM users WHERE id = :id"), {"id": grantee}
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at) "
                "VALUES (:id, :ws, :grantee, :rt, :rid, ARRAY['read'], :by, now())"
            ),
            {
                "id": uuid4(),
                "ws": w.ws,
                "grantee": account_id,
                "rt": resource_type,
                "rid": resource_id,
                "by": w.a,
            },
        )


def _family_rows(w: RaceWorld) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                text(
                    "SELECT id, workflow_id, version, status, owner_id FROM workflow_versions "
                    "WHERE workspace_id = :ws ORDER BY workflow_id, version"
                ),
                {"ws": w.ws},
            ).mappings()
        ]


def _side_effects(w: RaceWorld) -> dict[str, int]:
    with engine.connect() as conn:
        return {
            table: int(
                conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": w.ws},
                ).scalar_one()
            )
            for table in ("workflow_definitions", "audit_events", "event_outbox")
        }


def _create(
    client: TestClient,
    token: str,
    workflow_id: str,
    *,
    policy_ref: UUID | None = None,
    request_headers: dict[str, str] | None = None,
) -> Any:
    body: dict[str, Any] = {"workflow_id": workflow_id, "graph": _GRAPH, "trigger_refs": []}
    if policy_ref is not None:
        body["policy_ref"] = str(policy_ref)
    return client.post(_WORKFLOWS, headers=request_headers or headers(token), json=body)


def _assert_refused(response: Any, status_code: int, code: str) -> None:
    assert response.status_code == status_code, response.text
    body = response.json()
    assert body["error"]["code"] == code
    assert "version" not in body


# ---------------------------------------------------------------------------
# Create against an existing family
# ---------------------------------------------------------------------------


def test_member_cannot_append_to_another_members_private_family(world: RaceWorld) -> None:
    """The reported takeover: C appends v2 to B's private family. Refused
    with the same 404 an unknown workflow answers, and nothing is written."""
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="private")
    c_token = _session_token(world, world.c)
    rows_before, effects_before = _family_rows(world), _side_effects(world)
    with _client(c_token) as client:
        response = _create(client, c_token, workflow_id)

    _assert_refused(response, 404, "WORKFLOW_NOT_FOUND")
    assert _family_rows(world) == rows_before
    assert _side_effects(world) == effects_before


def test_hidden_family_refusal_matches_unknown_version_404(world: RaceWorld) -> None:
    """No oracle beyond the one a taken slug inherently is: the refusal body
    is the same 404 a `GET` of an unknown or invisible version answers, and
    discloses no version number."""
    workflow_id, version_id = _seed_family(world, owner=world.b, visibility="private")
    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        refused = _create(client, c_token, workflow_id)
        hidden_get = client.get(f"{_WORKFLOWS}/{version_id}")
        unknown_get = client.get(f"{_WORKFLOWS}/{uuid4()}")

    for response in (refused, hidden_get, unknown_get):
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"
    assert refused.json()["error"].keys() == unknown_get.json()["error"].keys()


def test_hidden_active_version_blocks_append_even_when_latest_is_visible(
    world: RaceWorld,
) -> None:
    """Latest and active are both checked: a visible draft on top (C's own,
    left by pre-fix data) does not let C append over B's private active v1."""
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
    rows_before = _family_rows(world)
    with _client(c_token) as client:
        response = _create(client, c_token, workflow_id)

    _assert_refused(response, 404, "WORKFLOW_NOT_FOUND")
    assert _family_rows(world) == rows_before


def test_read_only_grant_on_the_family_is_403(world: RaceWorld) -> None:
    """Visible but not writable (`shared_explicitly` with a read-only grant):
    403, since the read check already established C can see the family."""
    workflow_id, version_id = _seed_family(world, owner=world.b, visibility="shared_explicitly")
    _grant_read(world, grantee=world.c, resource_type="workflow_versions", resource_id=version_id)
    c_token = _session_token(world, world.c)
    rows_before = _family_rows(world)
    with _client(c_token) as client:
        response = _create(client, c_token, workflow_id)

    _assert_refused(response, 403, "INSUFFICIENT_ROLE")
    assert _family_rows(world) == rows_before


def test_member_may_append_to_a_workspace_visible_family(world: RaceWorld) -> None:
    """Editing a workspace-visible workflow stays allowed for any writer."""
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="workspace")
    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        response = _create(client, c_token, workflow_id)

    assert response.status_code == 201, response.text
    assert response.json()["version"] == 2


def test_owner_may_append_to_own_private_family(world: RaceWorld) -> None:
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="private")
    with _client(world.b_token) as client:
        response = _create(client, world.b_token, workflow_id)

    assert response.status_code == 201, response.text
    assert response.json()["version"] == 2


# ---------------------------------------------------------------------------
# policy_ref
# ---------------------------------------------------------------------------


def test_invisible_policy_ref_is_indistinguishable_from_unknown(world: RaceWorld) -> None:
    private_policy = _seed_policy(world, owner=world.b, visibility="private")
    c_token = _session_token(world, world.c)
    rows_before = _family_rows(world)
    with _client(c_token) as client:
        hidden = _create(client, c_token, f"new-{uuid4().hex[:12]}", policy_ref=private_policy)
        unknown = _create(client, c_token, f"new-{uuid4().hex[:12]}", policy_ref=uuid4())

    for response in (hidden, unknown):
        _assert_refused(response, 404, "POLICY_NOT_FOUND")
    assert _family_rows(world) == rows_before


def test_visible_policy_ref_is_accepted(world: RaceWorld) -> None:
    policy = _seed_policy(world, owner=world.b, visibility="workspace")
    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        response = _create(client, c_token, f"new-{uuid4().hex[:12]}", policy_ref=policy)

    assert response.status_code == 201, response.text
    assert response.json()["policy_ref"] == str(policy)


# ---------------------------------------------------------------------------
# Publish retiring another member's active version
# ---------------------------------------------------------------------------


def test_publish_cannot_retire_an_active_version_the_caller_cannot_write(
    world: RaceWorld,
) -> None:
    """Pre-fix data (or a family made workspace-visible later): C owns a
    draft v2 in a family whose active v1 is B's and private. Publishing v2
    would retire v1, so it is refused and v1 stays active."""
    workflow_id, active_id = _seed_family(world, owner=world.b, visibility="private")
    with engine.begin() as conn:
        draft_id = _insert_version(
            conn,
            world,
            workflow_id=workflow_id,
            version=2,
            owner=world.c,
            visibility="workspace",
            status="draft",
        )
    c_token = _session_token(world, world.c)
    rows_before, effects_before = _family_rows(world), _side_effects(world)
    with _client(c_token) as client:
        response = client.post(f"{_WORKFLOWS}/{draft_id}/publish", headers=headers(c_token))

    _assert_refused(response, 403, "INSUFFICIENT_ROLE")
    assert _family_rows(world) == rows_before
    assert _side_effects(world) == effects_before
    assert {row["id"]: row["status"] for row in rows_before}[active_id] == "active"


def test_publish_may_retire_a_workspace_visible_active_version(world: RaceWorld) -> None:
    workflow_id, active_id = _seed_family(world, owner=world.b, visibility="workspace")
    c_token = _session_token(world, world.c)
    with _client(c_token) as client:
        draft = _create(client, c_token, workflow_id)
        assert draft.status_code == 201, draft.text
        response = client.post(
            f"{_WORKFLOWS}/{draft.json()['id']}/publish", headers=headers(c_token)
        )

    assert response.status_code == 200, response.text
    statuses = {row["id"]: row["status"] for row in _family_rows(world)}
    assert statuses[active_id] == "retired"


# ---------------------------------------------------------------------------
# Same-key replay is re-authorized (ADR-0014)
# ---------------------------------------------------------------------------


def test_create_replay_is_reauthorized_against_the_family(world: RaceWorld) -> None:
    """C appends v2 to B's workspace-visible family; B then makes v2 its own
    private row. The same-key replay must not hand C the cached v2."""
    workflow_id, _ = _seed_family(world, owner=world.b, visibility="workspace")
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
                text(
                    "UPDATE workflow_versions SET owner_id = :b, visibility = 'private' "
                    "WHERE id = :id"
                ),
                {"b": world.b, "id": UUID(first.json()["id"])},
            )
        rows_before = _family_rows(world)
        refused = _create(client, c_token, workflow_id, request_headers=request_headers)

    _assert_refused(refused, 404, "WORKFLOW_NOT_FOUND")
    assert _family_rows(world) == rows_before


def test_create_replay_is_reauthorized_against_the_policy(world: RaceWorld) -> None:
    policy = _seed_policy(world, owner=world.b, visibility="workspace")
    c_token = _session_token(world, world.c)
    request_headers = headers(c_token)
    workflow_id = f"new-{uuid4().hex[:12]}"
    with _client(c_token) as client:
        first = _create(
            client, c_token, workflow_id, policy_ref=policy, request_headers=request_headers
        )
        assert first.status_code == 201, first.text

        with engine.begin() as conn:
            conn.execute(
                text("UPDATE automation_policies SET visibility = 'private' WHERE id = :id"),
                {"id": policy},
            )
        refused = _create(
            client, c_token, workflow_id, policy_ref=policy, request_headers=request_headers
        )

    _assert_refused(refused, 404, "POLICY_NOT_FOUND")


# ---------------------------------------------------------------------------
# Lock before authorizing: a change committed while the request waits on the
# row lock is what the checks see
# ---------------------------------------------------------------------------


def _make_private(table: str, row_id: UUID) -> Any:
    def mutate(conn: Connection) -> None:
        conn.execute(
            text(f"UPDATE {table} SET visibility = 'private' WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        )

    return mutate


def test_family_made_private_while_create_waits_is_refused(world: RaceWorld) -> None:
    workflow_id, version_id = _seed_family(world, owner=world.a, visibility="workspace")
    rows_before = _family_rows(world)
    response, _ = race(
        world,
        table="workflow_versions",
        row_id=version_id,
        send=lambda client: _create(client, world.b_token, workflow_id),
        transfer=True,
        mutate=_make_private("workflow_versions", version_id),
    )

    _assert_refused(response, 404, "WORKFLOW_NOT_FOUND")
    assert [row["id"] for row in _family_rows(world)] == [row["id"] for row in rows_before]


def test_active_version_made_private_while_publish_waits_is_refused(world: RaceWorld) -> None:
    workflow_id, active_id = _seed_family(world, owner=world.a, visibility="workspace")
    with engine.begin() as conn:
        draft_id = _insert_version(
            conn,
            world,
            workflow_id=workflow_id,
            version=2,
            owner=world.b,
            visibility="workspace",
            status="draft",
        )
    response, _ = race(
        world,
        table="workflow_versions",
        row_id=active_id,
        send=lambda client: client.post(
            f"{_WORKFLOWS}/{draft_id}/publish", headers=headers(world.b_token)
        ),
        transfer=True,
        mutate=_make_private("workflow_versions", active_id),
    )

    _assert_refused(response, 403, "INSUFFICIENT_ROLE")
    statuses = {row["id"]: row["status"] for row in _family_rows(world)}
    assert statuses == {active_id: "active", draft_id: "draft"}


def test_policy_made_private_while_create_waits_is_refused(world: RaceWorld) -> None:
    policy = _seed_policy(world, owner=world.a, visibility="workspace")
    rows_before = _family_rows(world)
    response, _ = race(
        world,
        table="automation_policies",
        row_id=policy,
        send=lambda client: _create(
            client, world.b_token, f"new-{uuid4().hex[:12]}", policy_ref=policy
        ),
        transfer=True,
        mutate=_make_private("automation_policies", policy),
    )

    _assert_refused(response, 404, "POLICY_NOT_FOUND")
    assert _family_rows(world) == rows_before


# ---------------------------------------------------------------------------
# A publish committing while the request waits on the active row: the newly
# active version is the one authorized
# ---------------------------------------------------------------------------


def _publish(retire_id: UUID, activate_id: UUID) -> Any:
    """What `activate_workflow_version` writes, run on the holder connection
    that already locks `retire_id`."""

    def mutate(conn: Connection) -> None:
        conn.execute(
            text("UPDATE workflow_versions SET status = 'retired' WHERE id = :id"),
            {"id": retire_id},
        )
        conn.execute(
            text("UPDATE workflow_versions SET status = 'active' WHERE id = :id"),
            {"id": activate_id},
        )

    return mutate


@pytest.mark.parametrize(("visibility", "status_code"), [("private", 404), ("workspace", 201)])
def test_publish_committing_while_create_waits_authorizes_the_new_active_version(
    world: RaceWorld, visibility: str, status_code: int
) -> None:
    """A's v1 is active, A's v2 (`visibility`) a draft, A's v3 the visible
    latest. B's append waits on v1 while a publish retires v1 and activates
    v2. Waking up, the locking select skips v1 and cannot see v2 as active;
    v2 must still be checked, so a private v2 refuses B with 404."""
    workflow_id, v1 = _seed_family(world, owner=world.a, visibility="workspace")
    with engine.begin() as conn:
        v2 = _insert_version(
            conn,
            world,
            workflow_id=workflow_id,
            version=2,
            owner=world.a,
            visibility=visibility,
            status="draft",
        )
        _insert_version(
            conn,
            world,
            workflow_id=workflow_id,
            version=3,
            owner=world.a,
            visibility="workspace",
            status="draft",
        )
    response, _ = race(
        world,
        table="workflow_versions",
        row_id=v1,
        send=lambda client: _create(client, world.b_token, workflow_id),
        transfer=False,
        mutate=_publish(v1, v2),
    )

    assert response.status_code == status_code, response.text
    if status_code == 404:
        _assert_refused(response, 404, "WORKFLOW_NOT_FOUND")
        assert [row["version"] for row in _family_rows(world)] == [1, 2, 3]
    else:
        assert response.json()["version"] == 4


@pytest.mark.parametrize(("visibility", "status_code"), [("private", 403), ("workspace", 200)])
def test_publish_committing_while_publish_waits_authorizes_the_new_active_version(
    world: RaceWorld, visibility: str, status_code: int
) -> None:
    """B publishes its draft v3 while another publish retires A's v1 and
    activates A's v2 (`visibility`). B's publish would retire v2, so it needs
    write on v2: a private v2 refuses B and stays active."""
    workflow_id, v1 = _seed_family(world, owner=world.a, visibility="workspace")
    with engine.begin() as conn:
        v2 = _insert_version(
            conn,
            world,
            workflow_id=workflow_id,
            version=2,
            owner=world.a,
            visibility=visibility,
            status="draft",
        )
        v3 = _insert_version(
            conn,
            world,
            workflow_id=workflow_id,
            version=3,
            owner=world.b,
            visibility="workspace",
            status="draft",
        )
    response, _ = race(
        world,
        table="workflow_versions",
        row_id=v1,
        send=lambda client: client.post(
            f"{_WORKFLOWS}/{v3}/publish", headers=headers(world.b_token)
        ),
        transfer=False,
        mutate=_publish(v1, v2),
    )

    assert response.status_code == status_code, response.text
    statuses = {row["id"]: row["status"] for row in _family_rows(world)}
    if status_code == 403:
        _assert_refused(response, 403, "INSUFFICIENT_ROLE")
        assert statuses == {v1: "retired", v2: "active", v3: "draft"}
    else:
        assert statuses == {v1: "retired", v2: "retired", v3: "active"}
