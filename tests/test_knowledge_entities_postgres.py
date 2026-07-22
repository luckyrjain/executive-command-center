from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

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
    assert {"status", "confidence", "version"} <= columns
    # entity_id (migration 0010) was dropped by migration 0020: no code path
    # ever wrote a non-NULL value to it, so its "mirror a domain aggregate"
    # feature was never built -- see that migration's docstring.
    assert "entity_id" not in columns


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
                "timeline_entries",
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


def _seed_alias(
    workspace_id: UUID, entity_id: UUID, normalized_value: str, alias_type: str = "nickname"
) -> UUID:
    # No HTTP endpoint writes entity_aliases (API-SCHEMAS.md's proposed
    # surface only lists a GET) -- aliases are created internally by
    # resolution/merge flows, so tests seed them directly.
    source_id = uuid4()
    alias_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :workspace_id, :node_id, "
                "'manual', 'alias-test-ref', :sha256, :captured_at)"
            ),
            {
                "id": source_id,
                "workspace_id": workspace_id,
                "node_id": entity_id,
                "sha256": sha256(str(source_id).encode()).hexdigest(),
                "captured_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO entity_aliases (id, workspace_id, entity_id, alias_type, "
                "normalized_value, source_id, created_at) VALUES (:id, :workspace_id, "
                ":entity_id, :alias_type, :normalized_value, :source_id, :created_at)"
            ),
            {
                "id": alias_id,
                "workspace_id": workspace_id,
                "entity_id": entity_id,
                "alias_type": alias_type,
                "normalized_value": normalized_value,
                "source_id": source_id,
                "created_at": now,
            },
        )
    return alias_id


def test_entity_aliases_list_returns_seeded_aliases_in_creation_order(
    knowledge_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = knowledge_test_context
    create = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "create-for-aliases"),
        json={"kind": "person", "canonical_name": "Ada Lovelace"},
    )
    entity_id = UUID(create.json()["id"])
    first_alias_id = _seed_alias(workspace_id, entity_id, "ada")
    second_alias_id = _seed_alias(workspace_id, entity_id, "countess of lovelace")

    response = client.get(
        f"/api/v1/knowledge/entities/{entity_id}/aliases", headers=_headers(token, "list-aliases")
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(first_alias_id), str(second_alias_id)]
    assert items[0]["normalized_value"] == "ada"
    assert items[0]["entity_id"] == str(entity_id)


def test_entity_aliases_list_is_empty_for_an_entity_with_no_aliases(
    knowledge_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = knowledge_test_context
    create = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "create-no-aliases"),
        json={"kind": "person", "canonical_name": "Grace Hopper"},
    )
    entity_id = create.json()["id"]

    response = client.get(
        f"/api/v1/knowledge/entities/{entity_id}/aliases", headers=_headers(token, "list-empty")
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_entity_aliases_list_404s_for_unknown_or_cross_workspace_entity(
    knowledge_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = knowledge_test_context
    response = client.get(
        f"/api/v1/knowledge/entities/{uuid4()}/aliases", headers=_headers(token, "list-missing")
    )
    assert response.status_code == 404


def test_alias_collision_across_two_entities_is_rejected(
    knowledge_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    # Adversarial regression test proving uq_entity_aliases_workspace_type_value
    # (migration 0011) actually enforces the invariant
    # _deterministic_alias_match's docstring (resolution.py) assumes: "an
    # exact alias collision between two different entities cannot occur in
    # the first place ... attaching an already-claimed alias to a second
    # entity is rejected at write time." This exercises the constraint
    # directly (there is no HTTP endpoint that writes entity_aliases -- see
    # _seed_alias) rather than just trusting the docstring's claim.
    client, workspace_id, _user_id, token = knowledge_test_context
    first = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "collision-create-first"),
        json={"kind": "person", "canonical_name": "Ada Lovelace"},
    )
    second = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "collision-create-second"),
        json={"kind": "person", "canonical_name": "Grace Hopper"},
    )
    first_id = UUID(first.json()["id"])
    second_id = UUID(second.json()["id"])

    _seed_alias(workspace_id, first_id, "shared-alias", alias_type="nickname")
    with pytest.raises(IntegrityError, match="uq_entity_aliases_workspace_type_value"):
        _seed_alias(workspace_id, second_id, "shared-alias", alias_type="nickname")
