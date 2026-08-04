"""Regression tests for the fourth whole-phase Phase 8 review's
`resolution_candidates` fix (`ecc.domains.knowledge.resolution.
list_candidates`): the endpoint used to trust `resolution_candidates`'
own visibility alone and never re-checked the two `pkos_nodes` entities a
candidate actually names, so an entity narrowed to `private`/
`shared_explicitly` *after* a candidate naming it was created kept
leaking through the list to a caller who could no longer read it
directly.

Hand-verification against a real Postgres database while building this
fix (not merely reading the code) found two further, more subtle bugs in
the fix itself, both covered here:

1. Joining `pkos_nodes` twice (as `left_entity`/`right_entity`) makes the
   candidate's own bare, unqualified `id`/`status`/`created_at` column
   references ambiguous -- `list_candidates` raised
   `column reference "id" is ambiguous` before the SELECT list was
   qualified with the `resolution_candidates.` table prefix.
2. `visible_resource_filter_sql` binds a fixed `:__authz_resource_type`
   parameter name regardless of the `resource_type` argument passed to
   it. The candidate's own filter call passes
   `resource_type="resolution_candidates"`; the two entity filter calls
   pass `resource_type="pkos_nodes"`. Reusing only the candidate's own
   params dict for all three fragments (as `relationships.py`'s
   `list_relationships` correctly does when every call shares one
   `resource_type`) silently bound `'resolution_candidates'` into the
   entity fragments' `resource_grants` subquery too -- an entity visible
   to the caller only via an explicit `pkos_nodes` grant (not ownership)
   was incorrectly hidden, because the grant subquery checked the wrong
   `resource_type` and never matched.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
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
class _VisibilityContext:
    workspace_id: UUID
    owner: _Actor
    grantee: _Actor
    outsider: _Actor


@pytest.fixture
def visibility_context() -> Iterator[_VisibilityContext]:
    """One workspace, three members: `owner` (creates the entities and the
    candidate), `grantee` (a plain `member` who will receive an explicit
    `pkos_nodes` grant on one entity), `outsider` (a plain `member` with no
    grant at all).
    """
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Resolution Visibility Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        owner = _make_actor(connection, workspace_id=workspace_id, role="owner")
        grantee = _make_actor(connection, workspace_id=workspace_id, role="member")
        outsider = _make_actor(connection, workspace_id=workspace_id, role="member")

    try:
        yield _VisibilityContext(
            workspace_id=workspace_id, owner=owner, grantee=grantee, outsider=outsider
        )
    finally:
        for actor in (owner, grantee, outsider):
            actor.client.close()
        with engine.begin() as connection:
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "resource_grants",
                "resolution_candidates",
                "entity_operations",
                "timeline_entries",
                "knowledge_claims",
                "entity_aliases",
                "pkos_edges",
                "pkos_evidence",
                "pkos_nodes",
                "sessions",
                "workspace_memberships",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _create_entity(client: TestClient, token: str, key: str, name: str) -> UUID:
    response = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, key),
        json={"kind": "person", "canonical_name": name},
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _create_candidate(client: TestClient, token: str, key: str, left: UUID, right: UUID) -> UUID:
    response = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, key),
        json={"left_entity_id": str(left), "right_entity_id": str(right)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["candidate"] is not None, (
        "expected a reviewable candidate, not a deterministic merge"
    )
    return UUID(body["candidate"]["id"])


def test_list_candidates_hides_candidate_after_entity_narrowed_with_no_grant(
    visibility_context: _VisibilityContext,
) -> None:
    """The core fourth-review fix: once an entity a candidate names is
    narrowed away from `workspace` visibility, a caller with no ownership
    and no grant on that entity must no longer see the candidate at all,
    even though the candidate row itself is still `workspace`-visible.
    """
    ctx = visibility_context
    left_id = _create_entity(ctx.owner.client, ctx.owner.token, "left-1", "Ada Lovelace")
    right_id = _create_entity(ctx.owner.client, ctx.owner.token, "right-1", "Ada Lovelase")
    candidate_id = _create_candidate(
        ctx.owner.client, ctx.owner.token, "candidate-1", left_id, right_id
    )

    grant_response = ctx.owner.client.post(
        "/api/v1/sharing/grants",
        headers=_headers(ctx.owner.token, "narrow-1"),
        json={
            "resource_type": "pkos_nodes",
            "resource_id": str(right_id),
            "grantee_account_id": str(ctx.grantee.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
    )
    assert grant_response.status_code == 201, grant_response.text

    response = ctx.outsider.client.get("/api/v1/knowledge/resolution/candidates")
    assert response.status_code == 200, response.text
    assert candidate_id not in {UUID(item["id"]) for item in response.json()["items"]}


def test_list_candidates_shows_candidate_via_explicit_entity_grant(
    visibility_context: _VisibilityContext,
) -> None:
    """Regression test for the `__authz_resource_type` bind-parameter
    collision found while hand-verifying the fix above: a caller who has
    no ownership of either entity, but does hold an explicit `read` grant
    on the narrowed `right_entity` (`resource_type="pkos_nodes"`), must
    still see the candidate. Before the fix, this query silently checked
    `resource_type = 'resolution_candidates'` instead of `'pkos_nodes'`
    in the entity grant subquery, so the grant never matched and the
    candidate stayed hidden even from its own grantee.
    """
    ctx = visibility_context
    left_id = _create_entity(ctx.owner.client, ctx.owner.token, "left-2", "Grace Hopper")
    right_id = _create_entity(ctx.owner.client, ctx.owner.token, "right-2", "Grace Hoper")
    candidate_id = _create_candidate(
        ctx.owner.client, ctx.owner.token, "candidate-2", left_id, right_id
    )

    grant_response = ctx.owner.client.post(
        "/api/v1/sharing/grants",
        headers=_headers(ctx.owner.token, "narrow-2"),
        json={
            "resource_type": "pkos_nodes",
            "resource_id": str(right_id),
            "grantee_account_id": str(ctx.grantee.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
    )
    assert grant_response.status_code == 201, grant_response.text

    response = ctx.grantee.client.get("/api/v1/knowledge/resolution/candidates")
    assert response.status_code == 200, response.text
    assert candidate_id in {UUID(item["id"]) for item in response.json()["items"]}

    outsider_response = ctx.outsider.client.get("/api/v1/knowledge/resolution/candidates")
    assert outsider_response.status_code == 200, outsider_response.text
    assert candidate_id not in {UUID(item["id"]) for item in outsider_response.json()["items"]}
