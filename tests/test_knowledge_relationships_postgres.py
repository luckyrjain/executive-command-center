from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def relationships_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str, UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Knowledge Relationships Test", "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, :password_hash, :created_at)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "password_hash": "test-password-hash",
                "created_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) VALUES (:id, :workspace_id, :user_id, "
                ":token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": session_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)

    def _create_entity(kind: str, name: str, key: str) -> UUID:
        csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
        response = client.post(
            "/api/v1/knowledge/entities",
            headers={
                "Idempotency-Key": key,
                "X-CSRF-Token": csrf,
                "X-Correlation-ID": str(uuid4()),
            },
            json={"kind": kind, "canonical_name": name},
        )
        return UUID(response.json()["id"])

    person_id = _create_entity("person", "Ada Lovelace", "create-person-fixture")
    project_id = _create_entity("project", "Analytical Engine", "create-project-fixture")

    try:
        yield client, workspace_id, user_id, token, person_id, project_id
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "timeline_entries",
                "knowledge_claims",
                "entity_aliases",
                "pkos_edges",
                "pkos_evidence",
                "pkos_nodes",
                "sessions",
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


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


def test_relationship_create_and_list_from_either_direction(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id, token, person_id, project_id = relationships_test_context

    create = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "create-relationship"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
        },
    )
    assert create.status_code == 201, create.text
    relationship = create.json()
    assert relationship["from_entity_id"] == str(person_id)
    assert relationship["to_entity_id"] == str(project_id)
    assert relationship["relationship_type"] == "WORKS_ON"
    assert relationship["status"] == "active"
    assert relationship["confidence"] == 1.0

    from_person = client.get(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "list-from"),
    )
    assert from_person.status_code == 200
    assert any(item["id"] == relationship["id"] for item in from_person.json()["items"])

    from_project = client.get(
        f"/api/v1/knowledge/entities/{project_id}/relationships",
        headers=_headers(token, "list-to"),
    )
    assert from_project.status_code == 200
    assert any(item["id"] == relationship["id"] for item in from_project.json()["items"])


def test_relationship_rejects_self_relationship_by_default(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id, token, person_id, _project_id = relationships_test_context
    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "self-relationship"),
        json={"relationship_type": "WORKS_ON", "to_entity_id": str(person_id)},
    )
    assert response.status_code == 422


def test_relationship_invalidate_supersedes_not_deletes(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    create = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "create-relationship-2"),
        json={"relationship_type": "WORKS_ON", "to_entity_id": str(project_id)},
    )
    relationship_id = create.json()["id"]

    invalidate = client.post(
        f"/api/v1/knowledge/relationships/{relationship_id}/invalidate",
        headers=_headers(token, "invalidate-relationship"),
        json={},
    )
    assert invalidate.status_code == 200, invalidate.text
    assert invalidate.json()["status"] == "invalidated"

    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT status FROM pkos_edges WHERE id = :id"),
                {"id": relationship_id},
            )
            .mappings()
            .one()
        )
    # The row still exists (supersede semantics, not delete).
    assert row["status"] == "invalidated"

    conflict = client.post(
        f"/api/v1/knowledge/relationships/{relationship_id}/invalidate",
        headers=_headers(token, "invalidate-relationship-again"),
        json={},
    )
    assert conflict.status_code == 409


def test_relationship_cross_workspace_404(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id, token, person_id, _project_id = relationships_test_context
    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "create-cross-ws"),
        json={"relationship_type": "WORKS_ON", "to_entity_id": str(uuid4())},
    )
    assert response.status_code == 404
