"""`list_relationships` filters BOTH ends of every edge (`src` and `tgt`)
through `visible_resource_filter_sql`, each with its own `param_prefix`.

An edge whose far end was narrowed away from `workspace` visibility must be
hidden from a member with no grant on it, and still shown to a member who
holds an explicit `pkos_nodes` grant on it -- whichever side of the edge
the narrowed entity is on. The grant cases are the ones that exercise each
fragment's own bound parameters (the `resource_grants` subquery).
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
    account_id: UUID
    token: str
    client: TestClient


def _make_actor(connection: Connection, *, workspace_id: UUID, role: str) -> _Actor:
    user_id = uuid4()
    token = f"session-{uuid4()}"
    create_identity(
        connection,
        workspace_id=workspace_id,
        user_id=user_id,
        email=f"{user_id}@example.test",
        role=role,
    )
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
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    account_id = connection.execute(
        text("SELECT account_id FROM users WHERE id = :id"), {"id": user_id}
    ).scalar_one()
    return _Actor(account_id=account_id, token=token, client=client)


@dataclass(frozen=True)
class _Context:
    workspace_id: UUID
    owner: _Actor
    grantee: _Actor
    outsider: _Actor


@pytest.fixture
def context() -> Iterator[_Context]:
    workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Relationship Visibility Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": datetime.now(UTC)},
        )
        owner = _make_actor(connection, workspace_id=workspace_id, role="owner")
        grantee = _make_actor(connection, workspace_id=workspace_id, role="member")
        outsider = _make_actor(connection, workspace_id=workspace_id, role="member")
    try:
        yield _Context(workspace_id=workspace_id, owner=owner, grantee=grantee, outsider=outsider)
    finally:
        for actor in (owner, grantee, outsider):
            actor.client.close()
        with engine.begin() as connection:
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "resource_grants",
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
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608 -- literal table list
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _create_entity(ctx: _Context, key: str, kind: str, name: str) -> UUID:
    response = ctx.owner.client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(ctx.owner.token, key),
        json={"kind": kind, "canonical_name": name},
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _create_edge(ctx: _Context, source_id: UUID, target_id: UUID) -> UUID:
    # No HTTP endpoint writes pkos_evidence (see test_knowledge_relationships_postgres).
    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :workspace_id, :node_id, "
                "'manual', 'relationships-visibility-ref', :sha256, :captured_at)"
            ),
            {
                "id": evidence_id,
                "workspace_id": ctx.workspace_id,
                "node_id": source_id,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": datetime.now(UTC),
            },
        )
    response = ctx.owner.client.post(
        f"/api/v1/knowledge/entities/{source_id}/relationships",
        headers=_headers(ctx.owner.token, f"edge-{source_id}"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(target_id),
            "evidence_id": str(evidence_id),
        },
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _narrow_to_grantee(ctx: _Context, entity_id: UUID) -> None:
    response = ctx.owner.client.post(
        "/api/v1/sharing/grants",
        headers=_headers(ctx.owner.token, f"narrow-{entity_id}"),
        json={
            "resource_type": "pkos_nodes",
            "resource_id": str(entity_id),
            "grantee_account_id": str(ctx.grantee.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
    )
    assert response.status_code == 201, response.text


def _listed_edge_ids(actor: _Actor, entity_id: UUID) -> set[UUID]:
    response = actor.client.get(f"/api/v1/knowledge/entities/{entity_id}/relationships")
    assert response.status_code == 200, response.text
    return {UUID(item["id"]) for item in response.json()["items"]}


def test_edge_to_narrowed_target_hidden_without_grant_shown_with_grant(
    context: _Context,
) -> None:
    person_id = _create_entity(context, "person-1", "person", "Ada Lovelace")
    project_id = _create_entity(context, "project-1", "project", "Analytical Engine")
    edge_id = _create_edge(context, person_id, project_id)
    _narrow_to_grantee(context, project_id)

    assert edge_id not in _listed_edge_ids(context.outsider, person_id)
    assert edge_id in _listed_edge_ids(context.grantee, person_id)


def test_edge_from_narrowed_source_hidden_without_grant_shown_with_grant(
    context: _Context,
) -> None:
    person_id = _create_entity(context, "person-2", "person", "Grace Hopper")
    project_id = _create_entity(context, "project-2", "project", "COBOL")
    edge_id = _create_edge(context, person_id, project_id)
    _narrow_to_grantee(context, person_id)

    assert edge_id not in _listed_edge_ids(context.outsider, project_id)
    assert edge_id in _listed_edge_ids(context.grantee, project_id)
