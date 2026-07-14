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
def commitment_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, timezone, created_at)
                VALUES (:id, :name, :timezone, :created_at)
                """
            ),
            {
                "id": workspace_id,
                "name": "Commitment Test",
                "timezone": "Asia/Kolkata",
                "created_at": now,
            },
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
                    id, workspace_id, user_id, token_hash,
                    expires_at, last_seen_at
                ) VALUES (
                    :id, :workspace_id, :user_id, :token_hash,
                    :expires_at, :last_seen_at
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
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "commitments",
                "pkos_evidence",
                "pkos_edges",
                "pkos_nodes",
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
    csrf = new(
        settings.session_secret.encode(),
        token.encode(),
        "sha256",
    ).hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


def test_commitment_lifecycle_is_transactional_and_workspace_scoped(
    commitment_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = commitment_test_context
    evidence_id = uuid4()
    evidence_node_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'evidence', :name, '{}'::jsonb, :now, :now
                )
                """
            ),
            {
                "id": evidence_node_id,
                "workspace_id": workspace_id,
                "name": "Commitment evidence",
                "now": datetime.now(UTC),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
                ) VALUES (
                    :id, :workspace_id, :node_id, 'test', :source_ref, :sha256, :captured_at
                )
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": evidence_node_id,
                "source_ref": str(evidence_id),
                "sha256": "0" * 64,
                "captured_at": datetime.now(UTC),
            },
        )

    create = client.post(
        "/api/v1/commitments",
        headers=_headers(token, "create-commitment"),
        json={
            "summary": "Share revised operating plan",
            "direction": "made_by_me",
            "status": "detected",
            "evidence_id": str(evidence_id),
            "confidence": 0.8,
            "importance": "high",
        },
    )
    assert create.status_code == 201
    created = create.json()
    commitment_id = created["id"]
    assert created["owner_id"] == str(user_id)
    assert created["status"] == "detected"
    assert created["version"] == 1

    replay = client.post(
        "/api/v1/commitments",
        headers=_headers(token, "create-commitment"),
        json={
            "summary": "Share revised operating plan",
            "direction": "made_by_me",
            "status": "detected",
            "evidence_id": str(evidence_id),
            "confidence": 0.8,
            "importance": "high",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == commitment_id

    confirm = client.post(
        f"/api/v1/commitments/{commitment_id}/confirm",
        headers=_headers(token, "confirm-commitment"),
        json={"expected_version": 1},
    )
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "active"
    assert confirm.json()["version"] == 2

    update = client.patch(
        f"/api/v1/commitments/{commitment_id}",
        headers=_headers(token, "update-commitment"),
        json={"expected_version": 2, "pinned": True},
    )
    assert update.status_code == 200
    assert update.json()["pinned"] is True
    assert update.json()["version"] == 3

    conflict = client.patch(
        f"/api/v1/commitments/{commitment_id}",
        headers=_headers(token, "stale-update"),
        json={"expected_version": 2, "summary": "Stale"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"

    fulfil = client.post(
        f"/api/v1/commitments/{commitment_id}/fulfil",
        headers=_headers(token, "fulfil-commitment"),
        json={"expected_version": 3},
    )
    assert fulfil.status_code == 200
    assert fulfil.json()["status"] == "fulfilled"
    assert fulfil.json()["fulfilled_at"] is not None

    archive = client.post(
        f"/api/v1/commitments/{commitment_id}/archive",
        headers=_headers(token, "archive-commitment"),
        json={"expected_version": 4},
    )
    assert archive.status_code == 200
    assert archive.json()["archived_at"] is not None

    restore = client.post(
        f"/api/v1/commitments/{commitment_id}/restore",
        headers=_headers(token, "restore-commitment"),
        json={"expected_version": 5},
    )
    assert restore.status_code == 200
    assert restore.json()["status"] == "fulfilled"
    assert restore.json()["archived_at"] is None

    with engine.connect() as connection:
        audit_types = (
            connection.execute(
                text(
                    """
                    SELECT event_type
                    FROM audit_events
                    WHERE workspace_id = :workspace_id
                      AND aggregate_id = :commitment_id
                    ORDER BY occurred_at
                    """
                ),
                {
                    "workspace_id": workspace_id,
                    "commitment_id": commitment_id,
                },
            )
            .scalars()
            .all()
        )
        outbox_types = (
            connection.execute(
                text(
                    """
                    SELECT event_type
                    FROM event_outbox
                    WHERE workspace_id = :workspace_id
                    ORDER BY occurred_at
                    """
                ),
                {"workspace_id": workspace_id},
            )
            .scalars()
            .all()
        )

    assert "commitment.created" in audit_types
    assert "commitment.confirmed" in audit_types
    assert "commitment.updated" in audit_types
    assert "commitment.fulfilled" in audit_types
    assert "commitment.archived" in audit_types
    assert "commitment.restored" in audit_types
    assert "commitment.detected.v1" in outbox_types
    assert "commitment.confirmed.v1" in outbox_types
    assert "commitment.updated.v1" in outbox_types
    assert "commitment.fulfilled.v1" in outbox_types
    assert "commitment.archived.v1" in outbox_types
    assert "commitment.restored.v1" in outbox_types


def test_restore_requires_archived_state(
    commitment_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = commitment_test_context
    created = client.post(
        "/api/v1/commitments",
        headers=_headers(token, "restore-create"),
        json={"summary": "Never archived", "direction": "made_by_me"},
    ).json()
    response = client.post(
        f"/api/v1/commitments/{created['id']}/restore",
        headers=_headers(token, "restore-invalid"),
        json={"expected_version": created["version"]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COMMITMENT_NOT_ARCHIVED"


def test_commitment_list_uses_signed_cursor_pagination(
    commitment_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = commitment_test_context
    for index in range(3):
        response = client.post(
            "/api/v1/commitments",
            headers=_headers(token, f"page-create-{index}"),
            json={"summary": f"Commitment {index}", "direction": "made_by_me"},
        )
        assert response.status_code == 201
    first = client.get("/api/v1/commitments?limit=2")
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["next_cursor"] is not None
    second = client.get(
        "/api/v1/commitments",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) >= 1
    malformed = client.get("/api/v1/commitments?cursor=not-signed")
    assert malformed.status_code == 400


def test_cross_workspace_references_are_not_disclosed(
    commitment_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = commitment_test_context
    response = client.post(
        "/api/v1/commitments",
        headers=_headers(token, "cross-workspace-reference"),
        json={
            "summary": "Invalid evidence",
            "direction": "made_by_me",
            "status": "detected",
            "evidence_id": str(uuid4()),
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"
