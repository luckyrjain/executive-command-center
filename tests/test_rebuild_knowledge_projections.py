from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.knowledge.timeline import rebuild_timeline
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def rebuild_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Rebuild Test", "created_at": now},
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
            "Idempotency-Key": "create-rebuild-subject",
            "X-CSRF-Token": csrf,
            "X-Correlation-ID": str(uuid4()),
        },
        json={"kind": "person", "canonical_name": "Rebuild Subject"},
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


def _row_checksum(workspace_id: UUID) -> str:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                """
                SELECT coalesce(md5(string_agg(h, ',')), 'empty') FROM (
                    SELECT md5((entity_id, effective_at, recorded_at, event_type, source_id,
                                summary)::text) AS h
                    FROM timeline_entries
                    WHERE workspace_id = :workspace_id
                    ORDER BY h
                ) s
                """
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
    return str(row)


def test_rebuild_timeline_is_deterministic_across_repeated_calls(
    rebuild_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, workspace_id, _user_id, token, entity_id = rebuild_test_context
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    client.patch(
        f"/api/v1/knowledge/entities/{entity_id}",
        headers={
            "Idempotency-Key": "rebuild-update",
            "X-CSRF-Token": csrf,
            "X-Correlation-ID": str(uuid4()),
        },
        json={"expected_version": 1, "summary": "rebuild test"},
    )

    with SessionFactory() as session:
        first_report = rebuild_timeline(session, workspace_id)
        session.commit()
    first_checksum = _row_checksum(workspace_id)
    assert first_report.entries_written > 0

    with SessionFactory() as session:
        second_report = rebuild_timeline(session, workspace_id)
        session.commit()
    second_checksum = _row_checksum(workspace_id)

    assert first_report.entries_written == second_report.entries_written
    assert first_checksum == second_checksum


def test_rebuild_timeline_reconstructs_content_after_manual_deletion(
    rebuild_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, workspace_id, _user_id, token, entity_id = rebuild_test_context

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM timeline_entries WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )
        remaining = connection.execute(
            text("SELECT count(*) FROM timeline_entries WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert remaining == 0

    with SessionFactory() as session:
        report = rebuild_timeline(session, workspace_id)
        session.commit()
    assert report.entries_written >= 1

    response = client.get(
        f"/api/v1/knowledge/entities/{entity_id}/timeline",
        headers={
            "Idempotency-Key": "rebuild-check",
            "X-CSRF-Token": new(
                settings.session_secret.encode(), token.encode(), "sha256"
            ).hexdigest(),
            "X-Correlation-ID": str(uuid4()),
        },
    )
    assert response.status_code == 200
    assert any(
        item["event_type"] == "knowledge_entity.created" for item in response.json()["items"]
    )
