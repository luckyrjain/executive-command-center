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
from ecc.domains.knowledge.retrieval import rebuild_retrieval_documents
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def retrieval_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Knowledge Retrieval Test", "created_at": now},
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
                "embedding_projections",
                "retrieval_documents",
                "entity_operations",
                "resolution_candidates",
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


def _create_entity(
    client: TestClient, token: str, key: str, kind: str, name: str, summary: str | None = None
) -> UUID:
    payload = {"kind": kind, "canonical_name": name}
    if summary is not None:
        payload["summary"] = summary
    response = client.post("/api/v1/knowledge/entities", headers=_headers(token, key), json=payload)
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _retrieve(client: TestClient, token: str, key: str, **params: object) -> object:
    return client.get("/api/v1/knowledge/retrieve", headers=_headers(token, key), params=params)


def test_entity_creation_is_immediately_retrievable(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = retrieval_test_context
    entity_id = _create_entity(client, token, "create", "person", "Ada Lovelace")

    response = _retrieve(client, token, "retrieve", q="Ada Lovelace")
    assert response.status_code == 200, response.text
    body = response.json()
    assert any(item["entity_id"] == str(entity_id) for item in body["items"])
    assert body["mode"] == "lexical"
    assert body["degraded"] is False


def test_exact_name_match_ranks_above_lexical_relevance(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = retrieval_test_context
    _create_entity(client, token, "exact", "person", "Ada Lovelace")
    _create_entity(
        client,
        token,
        "loose",
        "person",
        "Grace Hopper",
        summary="Worked alongside Ada Lovelace on early computing history",
    )

    response = _retrieve(client, token, "search-exact", q="Ada Lovelace")
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) >= 1
    assert items[0]["title"] == "Ada Lovelace"
    assert items[0]["matching_mode"] == "exact_name"
    assert items[0]["score"] > (items[1]["score"] if len(items) > 1 else 0)


def test_claim_content_becomes_searchable(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = retrieval_test_context
    entity_id = _create_entity(client, token, "claim-entity", "person", "Ada Lovelace")

    now = datetime.now(UTC)
    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
                ) VALUES (
                    :id, :workspace_id, :node_id, 'seed_fixture', :source_ref, :sha256, :captured_at
                )
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": entity_id,
                "source_ref": "test://retrieval/evidence",
                "sha256": sha256(b"retrieval-evidence").hexdigest(),
                "captured_at": now,
            },
        )
    claim = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/claims",
        headers=_headers(token, "claim-create"),
        json={
            "predicate": "known_for",
            "value": {"text": "Analytical Engine programming notes"},
            "source_id": str(evidence_id),
        },
    )
    assert claim.status_code == 201, claim.text

    response = _retrieve(client, token, "claim-search", q="Analytical Engine")
    assert response.status_code == 200
    items = response.json()["items"]
    assert any(item["entity_id"] == str(entity_id) for item in items)


def test_archived_entities_are_excluded_from_retrieval(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = retrieval_test_context
    entity_id = _create_entity(client, token, "archive-target", "person", "Ada Lovelace")
    archived = client.post(
        f"/api/v1/knowledge/entities/{entity_id}/archive",
        headers=_headers(token, "archive-it"),
        json={"expected_version": 1},
    )
    assert archived.status_code == 200, archived.text

    response = _retrieve(client, token, "archived-search", q="Ada Lovelace")
    assert response.status_code == 200
    items = response.json()["items"]
    assert not any(item["entity_id"] == str(entity_id) for item in items)


def test_kind_filter_excludes_other_entity_types(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = retrieval_test_context
    person_id = _create_entity(client, token, "kind-person", "person", "Meridian Project Lead")
    project_id = _create_entity(client, token, "kind-project", "project", "Meridian Project Lead")

    response = _retrieve(client, token, "kind-search", q="Meridian Project Lead", kind="project")
    assert response.status_code == 200
    items = response.json()["items"]
    entity_ids = {item["entity_id"] for item in items}
    assert str(project_id) in entity_ids
    assert str(person_id) not in entity_ids


def test_updated_time_range_filters_results(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = retrieval_test_context
    _create_entity(client, token, "time-entity", "person", "Ada Lovelace")
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    response = _retrieve(client, token, "time-search", q="Ada Lovelace", updated_from=future)
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_hybrid_mode_falls_back_to_degraded_lexical_when_embeddings_disabled(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Settings.embeddings_enabled defaults to False (see config.py), which
    is exactly the state this whole test suite runs under -- so this test
    needs no fixture/monkeypatch setup at all to exercise the degraded path.
    Task 7's real hybrid behavior (embeddings enabled, real ranking, real
    fallback-on-failure) is covered by test_knowledge_embeddings_postgres.py,
    which deliberately tests that degraded path FIRST, before any happy-path
    test, per the implementation plan's ordering requirement."""
    client, _workspace_id, _user_id, token = retrieval_test_context
    _create_entity(client, token, "hybrid-entity", "person", "Ada Lovelace")

    response = _retrieve(client, token, "hybrid-search", q="Ada Lovelace", mode="hybrid")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "lexical"
    assert body["degraded"] is True
    assert body["degraded_reason"] == "embeddings_disabled"


def test_signed_cursor_pagination_and_tamper_rejection(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = retrieval_test_context
    for index in range(3):
        _create_entity(client, token, f"page-entity-{index}", "person", f"Ada Lovelace {index}")

    first_page = _retrieve(client, token, "page-1", q="Ada Lovelace", limit=1)
    assert first_page.status_code == 200
    cursor = first_page.json()["next_cursor"]
    assert cursor is not None

    second_page = _retrieve(client, token, "page-2", q="Ada Lovelace", limit=1, cursor=cursor)
    assert second_page.status_code == 200
    assert second_page.json()["items"][0]["entity_id"] != first_page.json()["items"][0]["entity_id"]

    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    tampered_response = _retrieve(client, token, "page-tamper", q="Ada Lovelace", cursor=tampered)
    assert tampered_response.status_code == 400
    assert tampered_response.json()["error"]["code"] == "MALFORMED_CURSOR"


def test_workspace_isolation(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = retrieval_test_context
    _create_entity(client, token, "iso-mine", "person", "Ada Lovelace")

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": other_workspace_id, "name": "Other Workspace", "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, :password_hash, :created_at)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
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
                "id": uuid4(),
                "workspace_id": other_workspace_id,
                "user_id": other_user_id,
                "token_hash": sha256(other_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        response = _retrieve(other_client, other_token, "iso-other", q="Ada Lovelace")
        assert response.status_code == 200
        assert response.json()["items"] == []
    finally:
        other_client.close()
        with engine.begin() as connection:
            for table in ("sessions", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": other_workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )


def test_rebuild_retrieval_documents_reconstructs_after_manual_deletion(
    retrieval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = retrieval_test_context
    entity_id = _create_entity(client, token, "rebuild-entity", "person", "Ada Lovelace")

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM retrieval_documents WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )

    with SessionFactory() as session:
        report = rebuild_retrieval_documents(session, workspace_id)
        session.commit()
    assert report.documents_written >= 1

    response = _retrieve(client, token, "rebuild-search", q="Ada Lovelace")
    assert response.status_code == 200
    assert any(item["entity_id"] == str(entity_id) for item in response.json()["items"])
