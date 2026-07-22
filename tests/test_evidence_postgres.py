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
def evidence_test_context() -> Iterator[tuple[TestClient, UUID, str]]:
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        for wid, name in ((workspace_id, "Evidence Test"), (other_workspace_id, "Other Workspace")):
            connection.execute(
                text(
                    """
                    INSERT INTO workspaces (id, name, timezone, created_at)
                    VALUES (:id, :name, :timezone, :created_at)
                    """
                ),
                {"id": wid, "name": name, "timezone": "UTC", "created_at": now},
            )
        connection.execute(
            text(
                """
                INSERT INTO users (id, workspace_id, email, password_hash, created_at)
                VALUES (:id, :workspace_id, :email, :password_hash, :created_at)
                """
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
                """
                INSERT INTO sessions (
                    id, workspace_id, user_id, token_hash, expires_at, last_seen_at
                ) VALUES (
                    :id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at
                )
                """
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
        yield client, workspace_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "embedding_projections",
                "retrieval_documents",
                "timeline_entries",
                "knowledge_claims",
                "pkos_evidence",
                "pkos_nodes",
                "sessions",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": other_workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id IN (:workspace_id, :other_workspace_id)"),
                {"workspace_id": workspace_id, "other_workspace_id": other_workspace_id},
            )


def _insert_node_and_evidence(
    workspace_id: UUID,
    node_name: str,
    source_type: str,
    captured_at: datetime,
) -> UUID:
    node_id = uuid4()
    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, created_at, updated_at
                )
                VALUES (:id, :workspace_id, 'entity', :canonical_name, :now, :now)
                """
            ),
            {
                "id": node_id,
                "workspace_id": workspace_id,
                "canonical_name": node_name,
                "now": captured_at,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
                ) VALUES (
                    :id, :workspace_id, :node_id, :source_type, 'ref', :sha256, :captured_at
                )
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": node_id,
                "source_type": source_type,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": captured_at,
            },
        )
    return evidence_id


def test_evidence_resolves_available_id(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, workspace_id, _ = evidence_test_context
    captured_at = datetime.now(UTC) - timedelta(hours=1)
    evidence_id = _insert_node_and_evidence(workspace_id, "Board Deck", "document", captured_at)

    response = client.get(f"/api/v1/evidence?id={evidence_id}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == str(evidence_id)
    assert items[0]["status"] == "available"
    assert items[0]["source_type"] == "document"
    assert items[0]["label"] == "Board Deck"
    assert items[0]["captured_at"] is not None


def test_evidence_resolves_missing_id(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _, _ = evidence_test_context
    missing_id = uuid4()

    response = client.get(f"/api/v1/evidence?id={missing_id}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert items == [
        {
            "id": str(missing_id),
            "status": "missing",
            "source_type": None,
            "label": None,
            "captured_at": None,
        }
    ]


def test_evidence_resolves_mixed_batch_preserving_order(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, workspace_id, _ = evidence_test_context
    captured_at = datetime.now(UTC) - timedelta(hours=2)
    available_id = _insert_node_and_evidence(
        workspace_id, "Vendor Contract", "document", captured_at
    )
    missing_id = uuid4()

    response = client.get(f"/api/v1/evidence?id={missing_id}&id={available_id}")
    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["id"] for item in items] == [str(missing_id), str(available_id)]
    assert items[0]["status"] == "missing"
    assert items[1]["status"] == "available"


def test_cross_workspace_evidence_resolves_as_missing_not_permission_denied(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _, _ = evidence_test_context
    other_workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, timezone, created_at)
                VALUES (:id, 'Foreign Workspace', 'UTC', :now)
                """
            ),
            {"id": other_workspace_id, "now": datetime.now(UTC)},
        )
    try:
        foreign_evidence_id = _insert_node_and_evidence(
            other_workspace_id, "Foreign Secret", "document", datetime.now(UTC)
        )
        response = client.get(f"/api/v1/evidence?id={foreign_evidence_id}")
        assert response.status_code == 200
        items = response.json()["items"]
        assert items == [
            {
                "id": str(foreign_evidence_id),
                "status": "missing",
                "source_type": None,
                "label": None,
                "captured_at": None,
            }
        ]
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM pkos_evidence WHERE workspace_id = :w"),
                {"w": other_workspace_id},
            )
            connection.execute(
                text("DELETE FROM pkos_nodes WHERE workspace_id = :w"),
                {"w": other_workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :w"),
                {"w": other_workspace_id},
            )


def test_evidence_with_no_ids_returns_empty_list(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _, _ = evidence_test_context
    response = client.get("/api/v1/evidence")
    assert response.status_code == 200
    assert response.json()["items"] == []


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


def test_delete_evidence_marks_deleted_and_redacts_source_ref(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, workspace_id, token = evidence_test_context
    evidence_id = _insert_node_and_evidence(
        workspace_id, "Board Deck", "document", datetime.now(UTC)
    )

    response = client.post(
        f"/api/v1/evidence/{evidence_id}/delete",
        headers=_headers(token, "delete-once"),
        json={"reason": "source revoked access"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["id"] == str(evidence_id)
    assert response.json()["evidence_state"] == "deleted"

    with engine.connect() as connection:
        row = (
            connection.execute(
                text("SELECT evidence_state, source_ref FROM pkos_evidence WHERE id = :id"),
                {"id": evidence_id},
            )
            .mappings()
            .one()
        )
    assert row["evidence_state"] == "deleted"
    assert row["source_ref"] != "ref"
    assert "redacted" in row["source_ref"]


def test_delete_evidence_is_idempotent(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, workspace_id, token = evidence_test_context
    evidence_id = _insert_node_and_evidence(
        workspace_id, "Board Deck", "document", datetime.now(UTC)
    )

    first = client.post(
        f"/api/v1/evidence/{evidence_id}/delete",
        headers=_headers(token, "delete-idem-1"),
        json={"reason": "source revoked access"},
    )
    second = client.post(
        f"/api/v1/evidence/{evidence_id}/delete",
        headers=_headers(token, "delete-idem-2"),
        json={"reason": "source revoked access"},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["evidence_state"] == first.json()["evidence_state"]


def test_delete_evidence_404_for_unknown_id(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _workspace_id, token = evidence_test_context
    response = client.post(
        f"/api/v1/evidence/{uuid4()}/delete",
        headers=_headers(token, "delete-missing"),
        json={"reason": "attempt"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"


def test_delete_evidence_removes_derived_claim_content_from_retrieval(
    evidence_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _workspace_id, token = evidence_test_context

    create_entity = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "create-entity"),
        json={"kind": "person", "canonical_name": "Ada Lovelace"},
    )
    assert create_entity.status_code == 201, create_entity.text
    entity_id = create_entity.json()["id"]

    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :workspace_id, :node_id, "
                "'manual', 'test-ref', :sha256, :captured_at)"
            ),
            {
                "id": evidence_id,
                "workspace_id": _workspace_id,
                "node_id": entity_id,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": datetime.now(UTC),
            },
        )

    claim = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "create-claim"),
        json={
            "predicate": "codename",
            "value": {"text": "EnchantressOfNumbers"},
            "source_id": str(evidence_id),
        },
    )
    assert claim.status_code == 201, claim.text

    before = client.get(
        "/api/v1/knowledge/retrieve",
        headers=_headers(token, "search-before"),
        params={"q": "EnchantressOfNumbers"},
    )
    assert any(item["entity_id"] == entity_id for item in before.json()["items"])

    delete = client.post(
        f"/api/v1/evidence/{evidence_id}/delete",
        headers=_headers(token, "delete-cascade"),
        json={"reason": "source retracted"},
    )
    assert delete.status_code == 200, delete.text

    after = client.get(
        "/api/v1/knowledge/retrieve",
        headers=_headers(token, "search-after"),
        params={"q": "EnchantressOfNumbers"},
    )
    assert all(item["entity_id"] != entity_id for item in after.json()["items"])
