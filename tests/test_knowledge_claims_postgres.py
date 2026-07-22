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
def claims_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Knowledge Claims Test", "created_at": now},
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
            "Idempotency-Key": "create-subject-entity",
            "X-CSRF-Token": csrf,
            "X-Correlation-ID": str(uuid4()),
        },
        json={"kind": "person", "canonical_name": "Ada Lovelace"},
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


def _create_evidence(workspace_id: UUID, entity_id: UUID) -> UUID:
    evidence_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :workspace_id, :node_id, "
                "'manual', 'test-source-ref', :sha256, :captured_at)"
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": entity_id,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": now,
            },
        )
    return evidence_id


def test_claim_record_requires_at_least_one_source_reference(
    claims_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, _workspace_id, _user_id, token, entity_id = claims_test_context
    missing_source = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "missing-source"),
        json={"predicate": "employed_at", "value": {"organization": "Analytical Engines Ltd"}},
    )
    assert missing_source.status_code == 422


def test_claim_record_and_list(
    claims_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, workspace_id, _user_id, token, entity_id = claims_test_context
    evidence_id = _create_evidence(workspace_id, entity_id)

    create = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "create-claim"),
        json={
            "predicate": "employed_at",
            "value": {"organization": "Analytical Engines Ltd"},
            "source_id": str(evidence_id),
        },
    )
    assert create.status_code == 201, create.text
    claim = create.json()
    assert claim["predicate"] == "employed_at"
    assert claim["value"] == {"organization": "Analytical Engines Ltd"}
    assert claim["confidence"] == 1.0
    assert claim["superseded_by"] is None

    listed = client.get(
        f"/api/v1/knowledge/entities/{entity_id}/claims", headers=_headers(token, "list-claims")
    )
    assert listed.status_code == 200
    assert any(item["id"] == claim["id"] for item in listed.json()["items"])


def test_claim_supersede_never_destructively_overwrites(
    claims_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, workspace_id, _user_id, token, entity_id = claims_test_context
    evidence_id = _create_evidence(workspace_id, entity_id)

    create = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "create-claim-2"),
        json={
            "predicate": "employed_at",
            "value": {"organization": "Analytical Engines Ltd"},
            "source_id": str(evidence_id),
        },
    )
    original_id = create.json()["id"]

    supersede = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims/{original_id}/supersede",
        headers=_headers(token, "supersede-claim"),
        json={
            "predicate": "employed_at",
            "value": {"organization": "Cambridge University"},
            "source_id": str(evidence_id),
        },
    )
    assert supersede.status_code == 201, supersede.text
    new_claim = supersede.json()
    assert new_claim["id"] != original_id
    assert new_claim["value"] == {"organization": "Cambridge University"}

    with engine.connect() as connection:
        original_row = (
            connection.execute(
                text("SELECT superseded_by, valid_to FROM knowledge_claims WHERE id = :id"),
                {"id": original_id},
            )
            .mappings()
            .one()
        )
    # The original claim row still exists (not destructively overwritten) and
    # now points at its replacement.
    assert str(original_row["superseded_by"]) == new_claim["id"]
    assert original_row["valid_to"] is not None


def test_claim_record_rejects_unknown_evidence(
    claims_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, _workspace_id, _user_id, token, entity_id = claims_test_context
    response = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "unknown-evidence"),
        json={
            "predicate": "employed_at",
            "value": {"organization": "Analytical Engines Ltd"},
            "source_id": str(uuid4()),
        },
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"


def test_claim_record_rejects_unavailable_evidence(
    claims_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, workspace_id, _user_id, token, entity_id = claims_test_context
    evidence_id = _create_evidence(workspace_id, entity_id)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE pkos_evidence SET evidence_state = 'missing' WHERE id = :id"),
            {"id": evidence_id},
        )
    response = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "unavailable-evidence"),
        json={
            "predicate": "employed_at",
            "value": {"organization": "Analytical Engines Ltd"},
            "source_id": str(evidence_id),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "EVIDENCE_UNAVAILABLE"


def test_claim_supersede_rejects_unavailable_evidence(
    claims_test_context: tuple[TestClient, UUID, UUID, str, UUID],
) -> None:
    client, workspace_id, _user_id, token, entity_id = claims_test_context
    evidence_id = _create_evidence(workspace_id, entity_id)
    create = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "create-claim-for-supersede"),
        json={
            "predicate": "employed_at",
            "value": {"organization": "Analytical Engines Ltd"},
            "source_id": str(evidence_id),
        },
    )
    original_id = create.json()["id"]

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE pkos_evidence SET evidence_state = 'deleted' WHERE id = :id"),
            {"id": evidence_id},
        )

    supersede = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims/{original_id}/supersede",
        headers=_headers(token, "supersede-with-deleted-evidence"),
        json={
            "predicate": "employed_at",
            "value": {"organization": "Cambridge University"},
            "source_id": str(evidence_id),
        },
    )
    assert supersede.status_code == 422, supersede.text
    assert supersede.json()["error"]["code"] == "EVIDENCE_UNAVAILABLE"
