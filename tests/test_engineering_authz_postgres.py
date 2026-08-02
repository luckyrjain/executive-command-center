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
        assert response.json()["detail"] == "INSUFFICIENT_ROLE"


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
    assert response.json()["detail"] == "INSUFFICIENT_ROLE"

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
    assert response.json()["detail"] == "INSUFFICIENT_ROLE"


def test_connector_sync_and_disable_viewer_denied_owner_succeeds(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer
    account = _create_connector(owner.client, owner.token, credential="token-sync-disable")

    response = viewer.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/sync",
        json={"run_type": "backfill", "resource_type": "repositories"},
        headers=_headers(viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "INSUFFICIENT_ROLE"

    response = viewer.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/disable",
        headers=_headers(viewer.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text

    response = owner.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/sync",
        json={"run_type": "backfill", "resource_type": "repositories"},
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
    assert response.json()["detail"] == "INCIDENT_NOT_FOUND"


def test_idor_connector_id_guessed_from_another_workspace_is_404_not_403(
    authz_context: _AuthzContext, other_workspace_owner: _Actor
) -> None:
    owner: _Actor = authz_context.owner
    account = _create_connector(owner.client, owner.token, credential="token-idor")

    response = other_workspace_owner.client.post(
        f"/api/v1/engineering/connectors/{account['id']}/sync",
        json={"run_type": "backfill", "resource_type": "repositories"},
        headers=_headers(other_workspace_owner.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "CONNECTOR_NOT_FOUND"

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

    response = member.client.get("/api/v1/engineering/incidents")
    assert response.status_code == 200, response.text

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
    # no cache anywhere" guarantee).
    response = member.client.get("/api/v1/engineering/incidents")
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
    assert response.json()["detail"] == "INSUFFICIENT_ROLE"


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


def test_grant_creation_rejected_for_ungrantable_personal_domain_resource_type(
    authz_context: _AuthzContext,
) -> None:
    owner: _Actor = authz_context.owner
    viewer: _Actor = authz_context.viewer

    response = owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "personal_domains",
            "resource_id": str(uuid4()),
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
        },
        headers=_headers(owner.token),
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "RESOURCE_TYPE_NOT_GRANTABLE"

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM resource_grants WHERE resource_type = 'personal_domains'")
        ).scalar_one()
    assert count == 0


def test_grant_creation_requires_owner_admin_or_resource_owner(
    authz_context: _AuthzContext,
) -> None:
    member: _Actor = authz_context.member
    viewer: _Actor = authz_context.viewer
    owner: _Actor = authz_context.owner

    # member creates an incident it owns -- member itself may grant on it...
    incident = _create_incident(member.client, member.token, title="Owned by member")
    response = member.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "grantee_account_id": str(viewer.account_id),
            "actions": ["read"],
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
