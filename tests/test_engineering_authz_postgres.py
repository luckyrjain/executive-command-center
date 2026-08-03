"""Phase 8 Task 3: `ecc.platform.authz`'s authorization engine, exercised
end to end through the `engineering` domain (`incidents`/
`connector_accounts`), the reference domain this task wires (see that
module's own docstring). Per this task's own stated test requirements
(`docs/superpowers/plans/2026-08-01-phase-8-multi-user.md` Task 3):

1. The full role x resource x action matrix for `engineering`: `owner`/
   `admin`/`member`/`viewer`/a suspended membership, against create/
   mutate/list endpoints on both `incidents` (`decisions_incidents.py`,
   the "no acting-user ambiguity, plain workspace visibility" resource)
   and `connector_accounts` (`connector_accounts.py`, the two-step
   read-then-write 404-then-403 pattern).
2. IDOR/confused-deputy: a second, wholly isolated workspace's owner
   guessing another workspace's real resource id gets `404`, never `403`
   (existence-hiding, per Decision 2's own "deny is 404, not 403" rule).
3. Revocation-propagation (Decision 4): suspending a membership makes the
   very next request deny, no cache, no sleep -- `authz.py` never
   memoizes across requests, so this is a real regression test for that
   guarantee, not merely an assertion about the design.
4. A background-job re-check test: two sequential `authz.authorize()`
   calls with a revocation between them (mirroring how a background/sync
   job must re-check immediately before each side-effecting step rather
   than caching authority from job start, this module's own docstring).
5. `resource_grants`: `shared_explicitly` visibility only grants access
   with a matching active grant; an expired/revoked grant denies; no
   grant can ever be created against an `UNGRANTABLE_RESOURCE_TYPES`
   (Phase 7 personal-domain) `resource_type`.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = hmac_new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "resource_grants",
            "incident_changes",
            "decision_changes",
            "incidents",
            "engineering_decisions",
            "sync_runs",
            "sync_cursors",
            "changes",
            "repositories",
            "connector_accounts",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "workspace_memberships",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


@dataclass(frozen=True)
class _Actor:
    user_id: UUID
    account_id: UUID
    token: str
    client: TestClient


def _create_session(
    connection: Connection, *, workspace_id: UUID, user_id: UUID, token: str
) -> None:
    now = datetime.now(UTC)
    connection.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
            "expires_at, last_seen_at) "
            "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "user_id": user_id,
            "token_hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "now": now,
        },
    )


def _make_actor(
    connection: Connection, *, workspace_id: UUID, role: str, status: str = "active"
) -> _Actor:
    user_id = uuid4()
    token = f"session-{uuid4()}"
    create_identity(
        connection,
        workspace_id=workspace_id,
        user_id=user_id,
        email=f"{user_id}@example.test",
        role=role,
        status=status,
    )
    _create_session(connection, workspace_id=workspace_id, user_id=user_id, token=token)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    account_id = connection.execute(
        text("SELECT account_id FROM users WHERE id = :id"), {"id": user_id}
    ).scalar_one()
    return _Actor(user_id=user_id, account_id=account_id, token=token, client=client)


@dataclass(frozen=True)
class _AuthzContext:
    workspace_id: UUID
    owner: _Actor
    admin: _Actor
    member: _Actor
    viewer: _Actor
    suspended: _Actor

    def all_actors(self) -> tuple[_Actor, ...]:
        return (self.owner, self.admin, self.member, self.viewer, self.suspended)

    def actor(self, role_name: str) -> _Actor:
        result: _Actor = getattr(self, role_name)
        return result


@pytest.fixture
def authz_context() -> Iterator[_AuthzContext]:
    """One workspace with five memberships: `owner`/`admin`/`member`/
    `viewer` (all `active`) plus a `suspended` `member` -- the full role
    matrix this task's own test requirements name, in one shared fixture
    rather than five near-identical ones.
    """
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Authz Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        owner = _make_actor(connection, workspace_id=workspace_id, role="owner")
        admin = _make_actor(connection, workspace_id=workspace_id, role="admin")
        member = _make_actor(connection, workspace_id=workspace_id, role="member")
        viewer = _make_actor(connection, workspace_id=workspace_id, role="viewer")
        suspended = _make_actor(
            connection, workspace_id=workspace_id, role="member", status="suspended"
        )

    context = _AuthzContext(
        workspace_id=workspace_id,
        owner=owner,
        admin=admin,
        member=member,
        viewer=viewer,
        suspended=suspended,
    )
    try:
        yield context
    finally:
        for actor in context.all_actors():
            actor.client.close()
        _cleanup_workspace(workspace_id)


def _create_incident(
    client: TestClient, token: str, *, title: str = "Test incident"
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/engineering/incidents",
        json={
            "title": title,
            "severity": "high",
            "detected_at": datetime.now(UTC).isoformat(),
        },
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _create_connector(
    client: TestClient, token: str, *, credential: str = "token-authz"
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": credential},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _create_decision(
    client: TestClient, token: str, *, title: str = "Test decision"
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/engineering/decisions",
        json={"title": title},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


# --- 1a. Role x action matrix -- incidents ----------------------------------


def test_incident_create_owner_admin_member_succeed_viewer_and_suspended_denied(
    authz_context: _AuthzContext,
) -> None:
    for role_name in ("owner", "admin", "member"):
        actor: _Actor = authz_context.actor(role_name)
        body = _create_incident(actor.client, actor.token, title=f"Created by {role_name}")
        assert body["title"] == f"Created by {role_name}"

    for role_name in ("viewer", "suspended"):
        actor = authz_context.actor(role_name)
        response = actor.client.post(
            "/api/v1/engineering/incidents",
            json={
                "title": f"Denied for {role_name}",
                "severity": "low",
                "detected_at": datetime.now(UTC).isoformat(),
            },
            headers=_headers(actor.token, key=str(uuid4())),
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_incident_resolve_owner_admin_member_succeed_viewer_denied(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer

    # viewer: workspace-visibility read is allowed, write is not.
    incident = _create_incident(owner.client, owner.token, title="For viewer denial")
    response = viewer.client.post(
        f"/api/v1/engineering/incidents/{incident['id']}/resolve",
        json={"resolved_at": datetime.now(UTC).isoformat()},
        headers=_headers(viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    # owner/admin/member each resolve their own freshly-created incident
    # (workspace visibility + role's own write permission -- not
    # dependent on being the resource's own owner_id).
    for role_name in ("owner", "admin", "member"):
        actor: _Actor = authz_context.actor(role_name)
        incident = _create_incident(owner.client, owner.token, title=f"Resolved by {role_name}")
        response = actor.client.post(
            f"/api/v1/engineering/incidents/{incident['id']}/resolve",
            json={"resolved_at": datetime.now(UTC).isoformat()},
            headers=_headers(actor.token, key=str(uuid4())),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "resolved"


def test_incident_list_visible_to_every_active_role_not_suspended(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    incident = _create_incident(owner.client, owner.token, title="Listed for everyone")

    for role_name in ("owner", "admin", "member", "viewer"):
        actor: _Actor = authz_context.actor(role_name)
        response = actor.client.get("/api/v1/engineering/incidents")
        assert response.status_code == 200, response.text
        ids = {row["id"] for row in response.json()["incidents"]}
        assert incident["id"] in ids, f"{role_name} should see workspace-visible incident"

    # `visible_resource_filter_sql` (unlike `authorize()`/`require_role_
    # action`) never 403s an inactive membership on a list endpoint -- it
    # returns a `FALSE` filter, so the request still succeeds but sees no
    # rows (Decision 2's "list endpoints filter server-side" shape, not a
    # per-request error).
    suspended: _Actor = authz_context.suspended
    response = suspended.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text
    assert response.json()["incidents"] == []


# --- 1b. Role x action matrix -- connector_accounts -------------------------


def test_connector_create_owner_admin_member_succeed_viewer_denied(
    authz_context: _AuthzContext,
) -> None:
    for role_name in ("owner", "admin", "member"):
        actor: _Actor = authz_context.actor(role_name)
        body = _create_connector(actor.client, actor.token, credential=f"token-{role_name}")
        assert body["provider"] == "sandbox"

    viewer: _Actor = authz_context.viewer
    response = viewer.client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-viewer-denied"},
        headers=_headers(viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_connector_sync_and_disable_viewer_denied_owner_succeeds(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer
    account = _create_connector(owner.client, owner.token, credential="token-sync-disable")

    response = viewer.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    response = viewer.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/disable",
        headers=_headers(viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text

    response = owner.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(owner.token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text

    response = owner.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/disable",
        headers=_headers(owner.token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disconnected"


def test_connector_list_visible_to_every_active_role(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    account = _create_connector(owner.client, owner.token, credential="token-list-visible")

    for role_name in ("owner", "admin", "member", "viewer"):
        actor: _Actor = authz_context.actor(role_name)
        response = actor.client.get("/api/v1/engineering/connectors")
        assert response.status_code == 200, response.text
        ids = {row["id"] for row in response.json()["connectors"]}
        assert account["id"] in ids


# --- 1c. Role x action matrix -- engineering_decisions ----------------------
#
# `decisions_incidents.py`'s decisions endpoints had zero authz-specific
# test coverage before this round -- these mirror the incidents tests
# above, and specifically exercise the two-phase read-then-write
# authorize() fix applied to decide_decision_endpoint (see that function's
# own comment for why a single write-only authorize() call would let a
# suspended member distinguish 404 from 403).


def test_decision_create_owner_admin_member_succeed_viewer_and_suspended_denied(
    authz_context: _AuthzContext,
) -> None:
    for role_name in ("owner", "admin", "member"):
        actor: _Actor = authz_context.actor(role_name)
        body = _create_decision(actor.client, actor.token, title=f"Created by {role_name}")
        assert body["title"] == f"Created by {role_name}"

    for role_name in ("viewer", "suspended"):
        actor = authz_context.actor(role_name)
        response = actor.client.post(
            "/api/v1/engineering/decisions",
            json={"title": f"Denied for {role_name}"},
            headers=_headers(actor.token, key=str(uuid4())),
        )
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_decision_decide_owner_succeeds_viewer_and_suspended_denied(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer
    suspended: _Actor = authz_context.suspended

    # viewer: workspace-visibility read is allowed (existence confirmed via
    # a prior 200 create), write is not -- 403, not 404.
    decision = _create_decision(owner.client, owner.token, title="For viewer denial")
    response = viewer.client.post(
        f"/api/v1/engineering/decisions/{decision['id']}/decide",
        json={"decided_at": datetime.now(UTC).isoformat()},
        headers=_headers(viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"

    # suspended: not an active member at all -- the read-phase authorize()
    # call itself returns False, so this must be 404, not 403 (the exact
    # existence-leak the two-phase fix on decide_decision_endpoint exists
    # to prevent -- a suspended member must not be able to tell "this
    # decision doesn't exist" apart from "it exists but I can't see it").
    decision2 = _create_decision(owner.client, owner.token, title="For suspended denial")
    response = suspended.client.post(
        f"/api/v1/engineering/decisions/{decision2['id']}/decide",
        json={"decided_at": datetime.now(UTC).isoformat()},
        headers=_headers(suspended.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "DECISION_NOT_FOUND"

    for role_name in ("owner", "admin", "member"):
        actor: _Actor = authz_context.actor(role_name)
        decision = _create_decision(owner.client, owner.token, title=f"Decided by {role_name}")
        response = actor.client.post(
            f"/api/v1/engineering/decisions/{decision['id']}/decide",
            json={"decided_at": datetime.now(UTC).isoformat()},
            headers=_headers(actor.token, key=str(uuid4())),
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "decided"


def test_decision_list_visible_to_every_active_role_not_suspended(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    decision = _create_decision(owner.client, owner.token, title="Listed for everyone")

    for role_name in ("owner", "admin", "member", "viewer"):
        actor: _Actor = authz_context.actor(role_name)
        response = actor.client.get("/api/v1/engineering/decisions")
        assert response.status_code == 200, response.text
        ids = {row["id"] for row in response.json()["decisions"]}
        assert decision["id"] in ids, f"{role_name} should see workspace-visible decision"

    suspended: _Actor = authz_context.suspended
    response = suspended.client.get("/api/v1/engineering/decisions")
    assert response.status_code == 200, response.text
    assert response.json()["decisions"] == []


def test_idor_decision_id_guessed_from_another_workspace_is_404_not_403(
    authz_context: _AuthzContext, other_workspace_owner: _Actor
) -> None:
    owner: _Actor = authz_context.owner
    decision = _create_decision(owner.client, owner.token, title="Belongs to workspace A")

    response = other_workspace_owner.client.post(
        f"/api/v1/engineering/decisions/{decision['id']}/decide",
        json={"decided_at": datetime.now(UTC).isoformat()},
        headers=_headers(other_workspace_owner.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "DECISION_NOT_FOUND"


def test_incident_resolve_suspended_membership_is_404_not_403(
    authz_context: _AuthzContext,
) -> None:
    """`resolve_incident_endpoint` got the identical two-phase read-then-
    write authorize() fix as decide_decision_endpoint above -- this is its
    own regression test, a suspended member (not merely a lower-privileged
    active role like viewer) must see 404, not 403, for an incident that
    genuinely exists in their own (former) workspace.
    """
    owner: _Actor = authz_context.owner
    suspended: _Actor = authz_context.suspended
    incident = _create_incident(owner.client, owner.token, title="For suspended IDOR-shape check")

    response = suspended.client.post(
        f"/api/v1/engineering/incidents/{incident['id']}/resolve",
        json={"resolved_at": datetime.now(UTC).isoformat()},
        headers=_headers(suspended.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


# --- 2. IDOR / confused-deputy ----------------------------------------------


@pytest.fixture
def other_workspace_owner() -> Iterator[_Actor]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Authz Workspace', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        owner = _make_actor(connection, workspace_id=workspace_id, role="owner")
    try:
        yield owner
    finally:
        owner.client.close()
        _cleanup_workspace(workspace_id)


def test_idor_incident_id_guessed_from_another_workspace_is_404_not_403(
    authz_context: _AuthzContext, other_workspace_owner: _Actor
) -> None:
    owner: _Actor = authz_context.owner
    incident = _create_incident(owner.client, owner.token, title="Belongs to workspace A")

    response = other_workspace_owner.client.post(
        f"/api/v1/engineering/incidents/{incident['id']}/resolve",
        json={"resolved_at": datetime.now(UTC).isoformat()},
        headers=_headers(other_workspace_owner.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


def test_idor_connector_id_guessed_from_another_workspace_is_404_not_403(
    authz_context: _AuthzContext, other_workspace_owner: _Actor
) -> None:
    owner: _Actor = authz_context.owner
    account = _create_connector(owner.client, owner.token, credential="token-idor")

    response = other_workspace_owner.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(other_workspace_owner.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "CONNECTOR_NOT_FOUND"

    response = other_workspace_owner.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/disable",
        headers=_headers(other_workspace_owner.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text


# --- 3. Revocation propagation (Decision 4) ---------------------------------


def test_suspending_membership_denies_the_very_next_request(
    authz_context: _AuthzContext,
) -> None:
    workspace_id: UUID = authz_context.workspace_id
    member: _Actor = authz_context.member

    body = _create_incident(member.client, member.token, title="Before suspension")
    assert body["title"] == "Before suspension"

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET status = 'suspended' "
                "WHERE workspace_id = :workspace_id AND users_id = :users_id"
            ),
            {"workspace_id": workspace_id, "users_id": member.user_id},
        )

    # No cache, no sleep -- the very next request on the same still-valid
    # session must now deny (authz.py's own "reads fresh from Postgres,
    # no cache anywhere" guarantee). Uses a mutate endpoint (`require_role_
    # action`), not the list endpoint -- `visible_resource_filter_sql`
    # never 403s an inactive membership on a list endpoint (see the
    # `test_incident_list_...` test above), so it can't demonstrate denial
    # via status code the way a mutate endpoint can.
    response = member.client.post(
        "/api/v1/engineering/incidents",
        json={
            "title": "After suspension",
            "severity": "low",
            "detected_at": datetime.now(UTC).isoformat(),
        },
        headers=_headers(member.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text


def test_revoking_owner_role_denies_the_very_next_create(
    authz_context: _AuthzContext,
) -> None:
    workspace_id: UUID = authz_context.workspace_id
    admin: _Actor = authz_context.admin

    body = _create_incident(admin.client, admin.token, title="Before demotion")
    assert body["title"] == "Before demotion"

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET role = 'viewer' "
                "WHERE workspace_id = :workspace_id AND users_id = :users_id"
            ),
            {"workspace_id": workspace_id, "users_id": admin.user_id},
        )

    response = admin.client.post(
        "/api/v1/engineering/incidents",
        json={
            "title": "After demotion",
            "severity": "low",
            "detected_at": datetime.now(UTC).isoformat(),
        },
        headers=_headers(admin.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


# --- 4. Background-job re-check ---------------------------------------------


def test_background_job_must_recheck_authorize_between_steps(
    authz_context: _AuthzContext,
) -> None:
    """Simulates a background/sync job that authorizes once at job start
    and then performs two side-effecting steps -- the second step must
    re-check `authorize()` itself rather than trusting the first check,
    since a revocation between the two steps is exactly the race Decision
    2's "background jobs re-check, never cache authority" line exists to
    prevent. Calls `ecc.platform.authz.authorize` directly (not through
    an HTTP endpoint) since no endpoint in this task's own scope spans
    two side-effecting steps with a gap between them -- this is the
    module-level contract the design doc names, tested at that level.
    """
    from ecc.auth import AuthContext
    from ecc.database import SessionFactory
    from ecc.platform import authz

    workspace_id: UUID = authz_context.workspace_id
    admin: _Actor = authz_context.admin
    owner: _Actor = authz_context.owner

    incident = _create_incident(owner.client, owner.token, title="Background job target")
    incident_id = UUID(incident["id"])
    auth = AuthContext(workspace_id=workspace_id, user_id=admin.user_id, timezone="UTC")

    session = SessionFactory()
    try:
        # Step 1 of the simulated job: authorize, then act.
        assert authz.authorize(
            session, auth, resource_type="incidents", resource_id=incident_id, action="write"
        )
        session.rollback()

        # Something revokes the actor's authority between the job's two steps.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace_memberships SET status = 'suspended' "
                    "WHERE workspace_id = :workspace_id AND users_id = :users_id"
                ),
                {"workspace_id": workspace_id, "users_id": admin.user_id},
            )

        # Step 2 must re-check and now deny -- a job that only checked once
        # at start would incorrectly proceed.
        assert not authz.authorize(
            session, auth, resource_type="incidents", resource_id=incident_id, action="write"
        )
        session.rollback()
    finally:
        session.close()


# --- 5. resource_grants: shared_explicitly, revocation, UNGRANTABLE --------


def test_shared_explicitly_visibility_requires_matching_active_grant(
    authz_context: _AuthzContext,
) -> None:
    workspace_id: UUID = authz_context.workspace_id
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer

    incident = _create_incident(owner.client, owner.token, title="Shared explicitly")
    incident_id = UUID(incident["id"])

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE incidents SET visibility = 'shared_explicitly' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": incident_id, "workspace_id": workspace_id},
        )

    # No grant yet: workspace-visibility read no longer applies, so even
    # a plain GET list must exclude it for viewer.
    response = viewer.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text
    assert incident_id not in {UUID(row["id"]) for row in response.json()["incidents"]}

    grant_response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": str(incident_id),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
        },
        headers=_headers(owner.token),
    )
    assert grant_response.status_code == 201, grant_response.text
    grant_id = grant_response.json()["id"]

    response = viewer.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text
    assert incident_id in {UUID(row["id"]) for row in response.json()["incidents"]}

    # The grant is read-only -- viewer still cannot resolve it.
    response = viewer.client.post(
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        json={"resolved_at": datetime.now(UTC).isoformat()},
        headers=_headers(viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text

    # Revoking the grant immediately removes read access too.
    revoke_response = owner.client.delete(
        f"/api/v1/sharing/grants/{grant_id}", headers=_headers(owner.token)
    )
    assert revoke_response.status_code == 200, revoke_response.text

    response = viewer.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text
    assert incident_id not in {UUID(row["id"]) for row in response.json()["incidents"]}


@pytest.mark.parametrize(
    "resource_type",
    sorted(
        [
            "personal_domains",
            "domain_consents",
            "domain_sources",
            "domain_records",
            "goals",
            "routines",
            "check_ins",
            "personal_insights",
            "cross_domain_grants",
            "personal_insight_feedback",
        ]
    ),
)
def test_grant_creation_rejected_for_every_ungrantable_resource_type(
    authz_context: _AuthzContext, resource_type: str
) -> None:
    """`authz.UNGRANTABLE_RESOURCE_TYPES`'s full ten-entry frozenset, quoted
    verbatim here (not imported) so a future accidental removal from that
    set is still caught by name -- previously only `personal_domains` (1 of
    10) was exercised.
    """
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer

    response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": resource_type,
            "resource_id": str(uuid4()),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
        },
        headers=_headers(owner.token),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "RESOURCE_TYPE_NOT_GRANTABLE"

    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM resource_grants WHERE resource_type = :resource_type"  # noqa: S608
            ),
            {"resource_type": resource_type},
        ).scalar_one()
    assert count == 0


def test_grant_creation_requires_owner_admin_or_resource_owner(
    authz_context: _AuthzContext,
) -> None:
    member: _Actor = authz_context.member
    viewer: _Actor = authz_context.viewer
    owner: _Actor = authz_context.owner

    # member creates an incident it owns -- member itself may grant on it...
    # A freshly-created incident defaults to `workspace` visibility, so
    # `narrow_visibility: true` is required for the grant to take effect
    # (Task 5's own gate against silently narrowing everyone else's
    # default access) -- this test's own subject is the owner/admin/
    # resource-owner authorization gate, not the visibility-narrowing
    # confirmation, so it opts in rather than exercising that gate here.
    incident = _create_incident(member.client, member.token, title="Owned by member")
    response = member.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
        headers=_headers(member.token),
    )
    assert response.status_code == 201, response.text

    # ...but viewer (neither owner/admin nor the resource's own owner)
    # cannot grant on an incident it does not own, even one owner created.
    owner_incident = _create_incident(owner.client, owner.token, title="Owned by owner")
    response = viewer.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": owner_incident["id"],
            "grantee_account_id": str(member.account_id),
            "actions": ["read"],
        },
        headers=_headers(viewer.token),
    )
    assert response.status_code == 403, response.text


def test_grant_does_not_leak_across_resource_type(authz_context: _AuthzContext) -> None:
    """A grant is scoped to one specific `(workspace_id, resource_type,
    resource_id)` triple -- `authz._active_grant_exists`/
    `visible_resource_filter_sql`'s grant subquery both filter on
    `resource_type`, not merely `resource_id`. This proves that predicate
    is actually load-bearing: a grant naming `resource_type="incidents"`
    for one resource must not incidentally widen visibility of an
    unrelated `shared_explicitly` decision the same grantee has no grant
    for, even in the same workspace with the same grantee account.
    """
    workspace_id: UUID = authz_context.workspace_id
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer

    incident = _create_incident(owner.client, owner.token, title="Grant target")
    decision = _create_decision(owner.client, owner.token, title="Not granted")
    decision_id = UUID(decision["id"])

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE engineering_decisions SET visibility = 'shared_explicitly' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": decision_id, "workspace_id": workspace_id},
        )

    grant_response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
            # incident is still default `workspace` visibility (never
            # pre-flipped, unlike decision above) -- opt into narrowing so
            # the grant actually takes effect, per Task 5's own gate.
            "narrow_visibility": True,
        },
        headers=_headers(owner.token),
    )
    assert grant_response.status_code == 201, grant_response.text

    # viewer can now see the granted incident...
    response = viewer.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text
    assert UUID(incident["id"]) in {UUID(row["id"]) for row in response.json()["incidents"]}

    # ...but the incidents grant must not leak into decisions visibility.
    response = viewer.client.get("/api/v1/engineering/decisions")
    assert response.status_code == 200, response.text
    assert decision_id not in {UUID(row["id"]) for row in response.json()["decisions"]}


def test_expired_grant_denies_access(authz_context: _AuthzContext) -> None:
    workspace_id: UUID = authz_context.workspace_id
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer

    incident = _create_incident(owner.client, owner.token, title="Expired grant target")
    incident_id = UUID(incident["id"])
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE incidents SET visibility = 'shared_explicitly' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": incident_id, "workspace_id": workspace_id},
        )

    grant_response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": str(incident_id),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
        headers=_headers(owner.token),
    )
    assert grant_response.status_code == 201, grant_response.text

    response = viewer.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text
    assert incident_id not in {UUID(row["id"]) for row in response.json()["incidents"]}


def test_private_visibility_denies_every_non_owner_workspace_member(
    authz_context: _AuthzContext,
) -> None:
    """`authorize()`'s step 3 (`visibility == "private"` -> `False`, no
    exception even for `owner`/`admin` roles) had no end-to-end regression
    test through any engineering endpoint before this round -- every other
    visibility-branch test in this module exercises `workspace` or
    `shared_explicitly`.
    """
    workspace_id: UUID = authz_context.workspace_id
    member: _Actor = authz_context.member

    incident = _create_incident(member.client, member.token, title="Private incident")
    incident_id = UUID(incident["id"])
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE incidents SET visibility = 'private' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": incident_id, "workspace_id": workspace_id},
        )

    # The owning member can still see and act on its own private resource
    # (authorize()'s step 2 -- owner_id match -- runs before step 3).
    response = member.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text
    assert incident_id in {UUID(row["id"]) for row in response.json()["incidents"]}

    # Every other role, including owner/admin, is denied -- private means
    # private, not "workspace roles that can normally see everything."
    for role_name in ("owner", "admin", "viewer"):
        actor: _Actor = authz_context.actor(role_name)
        response = actor.client.get("/api/v1/engineering/incidents")
        assert response.status_code == 200, response.text
        assert incident_id not in {UUID(row["id"]) for row in response.json()["incidents"]}, (
            f"{role_name} must not see another member's private incident"
        )

        resolve_response = actor.client.post(
            f"/api/v1/engineering/incidents/{incident_id}/resolve",
            json={"resolved_at": datetime.now(UTC).isoformat()},
            headers=_headers(actor.token, key=str(uuid4())),
        )
        assert resolve_response.status_code == 404, resolve_response.text


# --- Task 5: grant creation's visibility-flip gate --------------------------


def test_grant_on_workspace_visible_resource_requires_narrow_visibility_confirmation(
    authz_context: _AuthzContext,
) -> None:
    """A freshly-created incident defaults to `workspace` visibility, where
    a grant would otherwise be silently inert (`authorize()` never
    consults `resource_grants` for a `workspace`-visibility resource) --
    Task 5 makes `POST /grants` 409 rather than insert a no-op row, until
    the caller opts into narrowing visibility via `narrow_visibility:
    true`, and proves narrowing actually changes who has default access.
    """
    owner: _Actor = authz_context.owner
    admin: _Actor = authz_context.admin
    viewer: _Actor = authz_context.viewer

    incident = _create_incident(owner.client, owner.token, title="Workspace visible")
    incident_id = UUID(incident["id"])

    response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": str(incident_id),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
        },
        headers=_headers(owner.token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "GRANT_REQUIRES_NARROW_VISIBILITY"

    # admin can still see it via ordinary workspace-role access before narrowing.
    response = admin.client.get("/api/v1/engineering/incidents")
    assert incident_id in {UUID(row["id"]) for row in response.json()["incidents"]}

    response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": str(incident_id),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
        headers=_headers(owner.token),
    )
    assert response.status_code == 201, response.text

    # Narrowing took effect: admin (no grant of its own) lost default
    # access, viewer (the grantee) gained it.
    response = admin.client.get("/api/v1/engineering/incidents")
    assert incident_id not in {UUID(row["id"]) for row in response.json()["incidents"]}
    response = viewer.client.get("/api/v1/engineering/incidents")
    assert incident_id in {UUID(row["id"]) for row in response.json()["incidents"]}


def test_grant_on_private_resource_auto_widens_visibility_without_confirmation(
    authz_context: _AuthzContext,
) -> None:
    workspace_id: UUID = authz_context.workspace_id
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer

    incident = _create_incident(owner.client, owner.token, title="Private then shared")
    incident_id = UUID(incident["id"])
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE incidents SET visibility = 'private' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": incident_id, "workspace_id": workspace_id},
        )

    # private -> shared_explicitly is a strict widening: no confirmation
    # flag required, unlike the workspace -> shared_explicitly narrowing.
    response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": str(incident_id),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
        },
        headers=_headers(owner.token),
    )
    assert response.status_code == 201, response.text

    response = viewer.client.get("/api/v1/engineering/incidents")
    assert incident_id in {UUID(row["id"]) for row in response.json()["incidents"]}

    with engine.begin() as connection:
        visibility = connection.execute(
            text("SELECT visibility FROM incidents WHERE id = :id"), {"id": incident_id}
        ).scalar_one()
    assert visibility == "shared_explicitly"


# --- Task 5: sharing-review preview -----------------------------------------


def test_grant_preview_on_workspace_visible_resource_reports_narrowing_and_losers(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    admin: _Actor = authz_context.admin
    member: _Actor = authz_context.member
    viewer: _Actor = authz_context.viewer

    incident = _create_incident(owner.client, owner.token, title="Preview target")
    incident_id = UUID(incident["id"])

    response = owner.client.post(
        "/api/v1/sharing/grants/preview",
        json={
            "resource_type": "incidents",
            "resource_id": str(incident_id),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
        },
        headers=_headers(owner.token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_visibility"] == "workspace"
    assert body["proposed_visibility"] == "shared_explicitly"
    assert body["requires_narrow_visibility_confirmation"] is True
    assert body["grantee_already_has_access"] is True  # viewer already reads via role
    assert body["grantee_gains_actions"] == []  # viewer already has read
    losers = {UUID(a) for a in body["members_losing_default_access"]}
    assert losers == {admin.account_id, member.account_id}
    assert viewer.account_id not in losers
    assert owner.account_id not in losers

    # A preview mutates nothing.
    response = viewer.client.get("/api/v1/engineering/incidents")
    assert incident_id not in {UUID(row["id"]) for row in response.json()["incidents"]}
    with engine.begin() as connection:
        visibility = connection.execute(
            text("SELECT visibility FROM incidents WHERE id = :id"), {"id": incident_id}
        ).scalar_one()
        grant_count = connection.execute(
            text("SELECT count(*) FROM resource_grants WHERE resource_id = :id"),
            {"id": incident_id},
        ).scalar_one()
    assert visibility == "workspace"
    assert grant_count == 0


def test_grant_preview_on_private_resource_reports_no_narrowing(
    authz_context: _AuthzContext,
) -> None:
    workspace_id: UUID = authz_context.workspace_id
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer

    incident = _create_incident(owner.client, owner.token, title="Private preview target")
    incident_id = UUID(incident["id"])
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE incidents SET visibility = 'private' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": incident_id, "workspace_id": workspace_id},
        )

    response = owner.client.post(
        "/api/v1/sharing/grants/preview",
        json={
            "resource_type": "incidents",
            "resource_id": str(incident_id),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read", "write"],
        },
        headers=_headers(owner.token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["current_visibility"] == "private"
    assert body["requires_narrow_visibility_confirmation"] is False
    assert body["members_losing_default_access"] == []
    assert body["grantee_already_has_access"] is False
    assert sorted(body["grantee_gains_actions"]) == ["read", "write"]


def test_grant_preview_requires_owner_admin_or_resource_owner(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer
    member: _Actor = authz_context.member

    incident = _create_incident(owner.client, owner.token, title="Not viewer's to preview")
    response = viewer.client.post(
        "/api/v1/sharing/grants/preview",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "grantee_account_id": str(member.account_id),
            "actions": ["read"],
        },
        headers=_headers(viewer.token),
    )
    assert response.status_code == 403, response.text


# --- Task 5: effective-permissions endpoint ---------------------------------


def test_effective_permissions_endpoint_owner_workspace_role_and_grant(
    authz_context: _AuthzContext,
) -> None:
    workspace_id: UUID = authz_context.workspace_id
    owner: _Actor = authz_context.owner
    member: _Actor = authz_context.member
    viewer: _Actor = authz_context.viewer

    incident = _create_incident(owner.client, owner.token, title="Effective perms target")
    incident_id = UUID(incident["id"])

    # Owner: via="owner", full actions, regardless of visibility.
    response = owner.client.get(f"/api/v1/sharing/resources/incidents/{incident_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_owner"] is True
    assert body["via"] == "owner"
    assert sorted(body["granted_actions"]) == ["read", "write"]
    assert body["visibility"] == "workspace"

    # Another active member: via="workspace_role", baseline actions for
    # their own role.
    response = member.client.get(f"/api/v1/sharing/resources/incidents/{incident_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_owner"] is False
    assert body["via"] == "workspace_role"
    assert body["role"] == "member"
    assert sorted(body["granted_actions"]) == ["read", "write"]

    # Narrow to shared_explicitly and grant viewer read-only: via="resource_grant".
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE incidents SET visibility = 'shared_explicitly' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": incident_id, "workspace_id": workspace_id},
        )
    grant_response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": str(incident_id),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
        },
        headers=_headers(owner.token),
    )
    assert grant_response.status_code == 201, grant_response.text

    response = viewer.client.get(f"/api/v1/sharing/resources/incidents/{incident_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["is_owner"] is False
    assert body["via"] == "resource_grant"
    assert body["granted_actions"] == ["read"]

    # member no longer has default access now that it's narrowed.
    response = member.client.get(f"/api/v1/sharing/resources/incidents/{incident_id}")
    assert response.status_code == 404, response.text


def test_effective_permissions_endpoint_404_for_private_non_owner_and_unknown_resource(
    authz_context: _AuthzContext,
) -> None:
    workspace_id: UUID = authz_context.workspace_id
    owner: _Actor = authz_context.owner
    member: _Actor = authz_context.member

    incident = _create_incident(owner.client, owner.token, title="Private effective perms")
    incident_id = UUID(incident["id"])
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE incidents SET visibility = 'private' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": incident_id, "workspace_id": workspace_id},
        )

    response = member.client.get(f"/api/v1/sharing/resources/incidents/{incident_id}")
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    response = owner.client.get(f"/api/v1/sharing/resources/incidents/{uuid4()}")
    assert response.status_code == 404, response.text


# --- Task 5: `sharing` field embedded in engineering domain responses ------


def test_incident_response_embeds_sharing_field(authz_context: _AuthzContext) -> None:
    owner: _Actor = authz_context.owner
    member: _Actor = authz_context.member

    incident = _create_incident(owner.client, owner.token, title="Embedded sharing field")
    assert incident["sharing"]["via"] == "owner"
    assert incident["sharing"]["is_owner"] is True
    assert sorted(incident["sharing"]["granted_actions"]) == ["read", "write"]

    response = member.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text
    [row] = [r for r in response.json()["incidents"] if r["id"] == incident["id"]]
    assert row["sharing"]["via"] == "workspace_role"
    assert row["sharing"]["is_owner"] is False
    assert row["sharing"]["role"] == "member"
