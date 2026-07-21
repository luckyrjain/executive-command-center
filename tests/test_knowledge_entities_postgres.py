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


def test_pkos_nodes_has_phase2_reconciliation_columns() -> None:
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pkos_nodes'"
                )
            )
        }
    assert {"entity_id", "status", "confidence", "version"} <= columns


def test_pkos_edges_has_phase2_reconciliation_columns() -> None:
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pkos_edges'"
                )
            )
        }
    assert {"confidence", "evidence_id", "valid_from", "valid_to", "status"} <= columns


def test_pkos_evidence_has_phase2_reconciliation_columns() -> None:
    with engine.connect() as connection:
        columns = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'pkos_evidence'"
                )
            )
        }
    assert {"evidence_state", "observed_at"} <= columns


def test_existing_seeded_pkos_rows_backfill_to_valid_defaults() -> None:
    """Existing pkos_nodes/pkos_edges rows created before this migration (e.g.
    by scripts/seed_phase1_acceptance.py) must round-trip with valid
    reconciliation-column defaults, not NULLs that would violate the new
    check constraints -- this is what Task 1's migration backfill step
    proves, not merely that the columns exist."""
    workspace_id = uuid4()
    node_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Reconciliation Backfill Test", "created_at": now},
        )
        # Deliberately omit the new columns, mirroring a pre-migration insert
        # shape, to prove the server defaults (not application code) supply
        # valid values.
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "created_at, updated_at) VALUES (:id, :workspace_id, 'person', "
                "'Backfill Person', :now, :now)"
            ),
            {"id": node_id, "workspace_id": workspace_id, "now": now},
        )
    try:
        with engine.connect() as connection:
            row = (
                connection.execute(
                    text("SELECT status, confidence, version FROM pkos_nodes WHERE id = :id"),
                    {"id": node_id},
                )
                .mappings()
                .one()
            )
        assert row["status"] == "active"
        assert row["version"] == 1
        assert 0 <= float(row["confidence"]) <= 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM pkos_nodes WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


@pytest.fixture
def knowledge_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Knowledge Entities Test", "created_at": now},
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
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "entity_aliases",
                "knowledge_claims",
                "pkos_edges",
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


def test_entity_lifecycle_is_transactional_and_workspace_scoped(
    knowledge_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = knowledge_test_context

    create = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "create-entity"),
        json={"kind": "project", "canonical_name": "Project Atlas"},
    )
    assert create.status_code == 201, create.text
    created = create.json()
    entity_id = created["id"]
    assert created["kind"] == "project"
    assert created["canonical_name"] == "Project Atlas"
    assert created["status"] == "active"
    assert created["version"] == 1
    assert created["confidence"] == 1.0

    replay = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "create-entity"),
        json={"kind": "project", "canonical_name": "Project Atlas"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == entity_id

    listed = client.get("/api/v1/knowledge/entities", headers=_headers(token, "list"))
    assert listed.status_code == 200
    assert any(item["id"] == entity_id for item in listed.json()["items"])

    filtered = client.get(
        "/api/v1/knowledge/entities",
        params={"kind": "person"},
        headers=_headers(token, "list-filtered"),
    )
    assert filtered.status_code == 200
    assert all(item["kind"] == "person" for item in filtered.json()["items"])

    update = client.patch(
        f"/api/v1/knowledge/entities/{entity_id}",
        headers=_headers(token, "update-entity"),
        json={"expected_version": 1, "summary": "Flagship platform migration"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["version"] == 2
    assert update.json()["summary"] == "Flagship platform migration"

    conflict = client.patch(
        f"/api/v1/knowledge/entities/{entity_id}",
        headers=_headers(token, "stale-update"),
        json={"expected_version": 1, "summary": "Stale"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"

    immutable = client.patch(
        f"/api/v1/knowledge/entities/{entity_id}",
        headers=_headers(token, "change-kind"),
        json={"expected_version": 2, "kind": "person"},
    )
    assert immutable.status_code == 422

    archive = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/archive",
        headers=_headers(token, "archive-entity"),
        json={"expected_version": 2},
    )
    assert archive.status_code == 200
    assert archive.json()["status"] == "archived"

    restore = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/restore",
        headers=_headers(token, "restore-entity"),
        json={"expected_version": 3},
    )
    assert restore.status_code == 200
    assert restore.json()["status"] == "active"

    with engine.connect() as connection:
        audit_types = (
            connection.execute(
                text(
                    "SELECT event_type FROM audit_events WHERE workspace_id = :workspace_id "
                    "AND aggregate_id = :entity_id ORDER BY occurred_at"
                ),
                {"workspace_id": workspace_id, "entity_id": entity_id},
            )
            .scalars()
            .all()
        )
        outbox_types = (
            connection.execute(
                text(
                    "SELECT event_type FROM event_outbox WHERE workspace_id = :workspace_id "
                    "ORDER BY occurred_at"
                ),
                {"workspace_id": workspace_id},
            )
            .scalars()
            .all()
        )
    assert "knowledge_entity.created" in audit_types
    assert "knowledge_entity.updated" in audit_types
    assert "knowledge_entity.archived" in audit_types
    assert "knowledge_entity.restored" in audit_types
    assert "knowledge_entity.created.v1" in outbox_types
    assert "knowledge_entity.archived.v1" in outbox_types
    assert "knowledge_entity.restored.v1" in outbox_types


def test_entity_create_requires_kind_and_canonical_name(
    knowledge_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = knowledge_test_context
    missing_name = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "missing-name"),
        json={"kind": "project"},
    )
    assert missing_name.status_code == 422

    missing_kind = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "missing-kind"),
        json={"canonical_name": "No Kind"},
    )
    assert missing_kind.status_code == 422


def test_entity_get_is_workspace_scoped_and_404s_across_workspaces(
    knowledge_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = knowledge_test_context
    create = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "create-for-get"),
        json={"kind": "decision", "canonical_name": "Adopt PKOS reconciliation"},
    )
    entity_id = create.json()["id"]

    found = client.get(f"/api/v1/knowledge/entities/{entity_id}", headers=_headers(token, "get"))
    assert found.status_code == 200

    missing = client.get(
        f"/api/v1/knowledge/entities/{uuid4()}", headers=_headers(token, "get-missing")
    )
    assert missing.status_code == 404
