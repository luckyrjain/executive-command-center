from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
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
def resolution_count_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Resolution Count Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "resolution_candidates",
                "pkos_nodes",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "sessions",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str, key: str) -> dict[str, str]:
    # Both entity creation (`entities.py`) and candidate creation
    # (`resolution.py::create_candidate`) require CSRF + Idempotency-Key,
    # same as every other mutating endpoint in this app.
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"Idempotency-Key": key, "X-CSRF-Token": csrf}


def _create_entity(client: TestClient, token: str, key: str, kind: str, name: str) -> str:
    # EntityCreate (backend/ecc/domains/knowledge/entities.py) takes exactly
    # kind/canonical_name/summary -- confirmed against the real model, not guessed.
    response = client.post(
        "/api/v1/knowledge/entities",
        json={"kind": kind, "canonical_name": name},
        headers=_headers(token, key),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_resolution_count_matches_open_candidates(
    resolution_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_count_context
    left_id = _create_entity(client, token, "count-left", "person", "Grace Hopper")
    right_id = _create_entity(client, token, "count-right", "person", "Grace Hoper")
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "count-candidate"),
        json={"left_entity_id": left_id, "right_entity_id": right_id},
    )
    assert created.status_code == 201, created.text

    counted = client.get("/api/v1/knowledge/resolution/candidates/count")
    assert counted.status_code == 200
    assert counted.json()["count"] == 1

    listed = client.get("/api/v1/knowledge/resolution/candidates", params={"status": "open"})
    assert len(listed.json()["items"]) == counted.json()["count"]
