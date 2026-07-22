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
def timeline_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Knowledge Timeline Test", "created_at": now},
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
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    entity_response = client.post(
        "/api/v1/knowledge/entities",
        headers={
            "Idempotency-Key": "create-timeline-subject",
            "X-CSRF-Token": csrf,
            "X-Correlation-ID": str(uuid4()),
        },
        json={"kind": "person", "canonical_name": "Timeline Subject"},
    )
    entity_id = UUID(entity_response.json()["id"])

    try:
        yield client, workspace_id, user_id, token, entity_id
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


def test_entity_creation_is_recorded_on_its_own_timeline(
    timeline_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, _workspace_id, _user_id, token, entity_id = timeline_test_context
    response = client.get(
        f"/api/v1/knowledge/entities/{entity_id}/timeline", headers=_headers(token, "get-timeline")
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["event_type"] == "knowledge_entity.created" for item in items)


def test_claim_and_relationship_mutations_appear_on_timeline(
    timeline_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, workspace_id, _user_id, token, entity_id = timeline_test_context

    evidence_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :workspace_id, :node_id, "
                "'manual', 'timeline-test-ref', :sha256, :captured_at)"
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": entity_id,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": now,
            },
        )

    client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "create-timeline-claim"),
        json={
            "predicate": "role",
            "value": {"title": "Engineer"},
            "source_id": str(evidence_id),
        },
    )

    other = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "create-timeline-other-entity"),
        json={"kind": "project", "canonical_name": "Timeline Project"},
    )
    other_id = other.json()["id"]
    client.post(
        f"/api/v1/knowledge/entities/{entity_id}/relationships",
        headers=_headers(token, "create-timeline-relationship"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": other_id,
            "evidence_id": str(evidence_id),
        },
    )

    response = client.get(
        f"/api/v1/knowledge/entities/{entity_id}/timeline", headers=_headers(token, "list-timeline")
    )
    event_types = {item["event_type"] for item in response.json()["items"]}
    assert "knowledge_entity.claim_recorded" in event_types
    assert "relationship.created" in event_types


def test_timeline_is_ordered_deterministically_and_paginates(
    timeline_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, _workspace_id, _user_id, token, entity_id = timeline_test_context
    for index in range(5):
        client.patch(
            f"/api/v1/knowledge/entities/{entity_id}",
            headers=_headers(token, f"update-timeline-{index}"),
            json={"expected_version": index + 1, "summary": f"revision {index}"},
        )

    page_one = client.get(
        f"/api/v1/knowledge/entities/{entity_id}/timeline",
        params={"limit": 3},
        headers=_headers(token, "timeline-page-1"),
    )
    assert page_one.status_code == 200
    first_items = page_one.json()["items"]
    assert len(first_items) == 3
    cursor = page_one.json()["next_cursor"]
    assert cursor is not None

    page_two = client.get(
        f"/api/v1/knowledge/entities/{entity_id}/timeline",
        params={"limit": 3, "cursor": cursor},
        headers=_headers(token, "timeline-page-2"),
    )
    assert page_two.status_code == 200
    second_items = page_two.json()["items"]
    first_ids = {item["id"] for item in first_items}
    second_ids = {item["id"] for item in second_items}
    assert first_ids.isdisjoint(second_ids)
