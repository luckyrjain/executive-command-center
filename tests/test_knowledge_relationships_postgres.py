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


def _create_evidence(workspace_id: UUID, node_id: UUID) -> UUID:
    # No HTTP endpoint writes pkos_evidence (evidence.py only exposes GET),
    # matching the pattern established by claims/entity-operations tests.
    evidence_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :workspace_id, :node_id, "
                "'manual', 'relationships-test-ref', :sha256, :captured_at)"
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": node_id,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": now,
            },
        )
    return evidence_id


def test_relationship_create_and_list_from_either_direction(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)

    create = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "create-relationship"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
    )
    assert create.status_code == 201, create.text
    relationship = create.json()
    assert relationship["from_entity_id"] == str(person_id)
    assert relationship["to_entity_id"] == str(project_id)
    assert relationship["relationship_type"] == "WORKS_ON"
    assert relationship["status"] == "active"
    assert relationship["confidence"] == 1.0
    assert relationship["evidence_id"] == str(evidence_id)
    # Resolved from `pkos_nodes` -- see relationships.py's own docstring for
    # why raw IDs alone made this response unusable for a human-facing list.
    assert relationship["from_entity_name"] == "Ada Lovelace"
    assert relationship["from_entity_kind"] == "person"
    assert relationship["to_entity_name"] == "Analytical Engine"
    assert relationship["to_entity_kind"] == "project"

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


def test_relationship_list_paginates_and_terminates(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    """Regression test: `list_relationships` had no `LIMIT`/cursor at all.
    `pkos_edges` has no `created_at`/`updated_at` column, so the cursor
    here keys purely on `id` (the endpoint's own pre-existing `ORDER BY
    e.id` sort key), unlike every other paginated list in this codebase
    which keys on a `(timestamp, id)` pair.
    """
    client, workspace_id, _user_id, token, person_id, _project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)

    created_ids: list[str] = []
    for i in range(3):
        csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
        target = client.post(
            "/api/v1/knowledge/entities",
            headers={
                "Idempotency-Key": f"create-target-{i}",
                "X-CSRF-Token": csrf,
                "X-Correlation-ID": str(uuid4()),
            },
            json={"kind": "project", "canonical_name": f"Target Project {i}"},
        )
        target_id = target.json()["id"]
        create = client.post(
            f"/api/v1/knowledge/entities/{person_id}/relationships",
            headers=_headers(token, f"create-relationship-{i}"),
            json={
                "relationship_type": "WORKS_ON",
                "to_entity_id": target_id,
                "evidence_id": str(evidence_id),
            },
        )
        assert create.status_code == 201, create.text
        created_ids.append(create.json()["id"])

    first_page = client.get(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        params={"limit": 2},
        headers=_headers(token, "list-page-1"),
    )
    assert first_page.status_code == 200, first_page.text
    first_body = first_page.json()
    assert len(first_body["items"]) == 2
    assert first_body["next_cursor"] is not None

    second_page = client.get(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        params={"limit": 2, "cursor": first_body["next_cursor"]},
        headers=_headers(token, "list-page-2"),
    )
    assert second_page.status_code == 200, second_page.text
    second_body = second_page.json()
    assert len(second_body["items"]) == 1
    assert second_body["next_cursor"] is None

    seen_ids = {item["id"] for item in first_body["items"]} | {
        item["id"] for item in second_body["items"]
    }
    assert seen_ids == set(created_ids)


def test_relationship_rejects_self_relationship_by_default(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, _project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "self-relationship"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(person_id),
            "evidence_id": str(evidence_id),
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "SELF_RELATIONSHIP_NOT_PERMITTED"


def test_relationship_invalidate_supersedes_not_deletes(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    create = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "create-relationship-2"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
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
    client, workspace_id, _user_id, token, person_id, _project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "create-cross-ws"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(uuid4()),
            "evidence_id": str(evidence_id),
        },
    )
    assert response.status_code == 404


def _seed_foreign_workspace_relationship() -> tuple[UUID, UUID, UUID]:
    """A second, fully independent workspace with two nodes and an active
    relationship between them, for proving create/invalidate/list never
    act on or expose a resource that belongs to a different workspace."""
    other_workspace_id = uuid4()
    node_a, node_b = uuid4(), uuid4()
    relationship_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": other_workspace_id, "name": "Foreign Workspace", "created_at": now},
        )
        create_identity(connection, workspace_id=other_workspace_id, user_id=uuid4(), now=now)
        for node_id, name in ((node_a, "Foreign A"), (node_b, "Foreign B")):
            connection.execute(
                text(
                    "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                    "attributes, status, confidence, version, created_at, updated_at) "
                    "VALUES (:id, :workspace_id, 'person', :name, '{}'::jsonb, 'active', "
                    "1.0, 1, :now, :now)"
                ),
                {"id": node_id, "workspace_id": other_workspace_id, "name": name, "now": now},
            )
        evidence_id = uuid4()
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :workspace_id, :node_id, "
                "'manual', 'relationships-isolation-test-ref', :sha256, :captured_at)"
            ),
            {
                "id": evidence_id,
                "workspace_id": other_workspace_id,
                "node_id": node_a,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO pkos_edges (
                    id, workspace_id, source_node_id, target_node_id, edge_type,
                    attributes, confidence, evidence_id, status
                ) VALUES (
                    :id, :workspace_id, :source, :target, 'WORKS_ON',
                    '{}'::jsonb, 1.0, :evidence_id, 'active'
                )
                """
            ),
            {
                "id": relationship_id,
                "workspace_id": other_workspace_id,
                "source": node_a,
                "target": node_b,
                "evidence_id": evidence_id,
            },
        )
    return other_workspace_id, node_a, relationship_id


def _teardown_foreign_workspace_relationship(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "pkos_edges",
            "pkos_evidence",
            "pkos_nodes",
            "workspace_memberships",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM users WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def test_relationship_create_rejects_to_entity_from_another_workspace(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, _project_id = relationships_test_context
    other_workspace_id, foreign_node_id, _relationship_id = _seed_foreign_workspace_relationship()
    try:
        evidence_id = _create_evidence(workspace_id, person_id)
        response = client.post(
            f"/api/v1/knowledge/entities/{person_id}/relationships",
            headers=_headers(token, "isolation-create-foreign-target"),
            json={
                "relationship_type": "WORKS_ON",
                "to_entity_id": str(foreign_node_id),
                "evidence_id": str(evidence_id),
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "ENTITY_NOT_FOUND"
    finally:
        _teardown_foreign_workspace_relationship(other_workspace_id)


def test_relationship_invalidate_rejects_a_foreign_workspace_relationship(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id, token, _person_id, _project_id = relationships_test_context
    other_workspace_id, _foreign_node_id, foreign_relationship_id = (
        _seed_foreign_workspace_relationship()
    )
    try:
        response = client.post(
            f"/api/v1/knowledge/relationships/{foreign_relationship_id}/invalidate",
            headers=_headers(token, "isolation-invalidate-foreign"),
            json={},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RELATIONSHIP_NOT_FOUND"

        with engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM pkos_edges WHERE id = :id"),
                {"id": foreign_relationship_id},
            ).scalar_one()
        assert status == "active"
    finally:
        _teardown_foreign_workspace_relationship(other_workspace_id)


def test_relationship_list_excludes_other_workspaces(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id, token, _person_id, _project_id = relationships_test_context
    other_workspace_id, foreign_node_id, foreign_relationship_id = (
        _seed_foreign_workspace_relationship()
    )
    try:
        response = client.get(
            f"/api/v1/knowledge/entities/{foreign_node_id}/relationships",
            headers=_headers(token, "isolation-list-foreign"),
        )
        assert response.status_code == 200
        assert all(item["id"] != str(foreign_relationship_id) for item in response.json()["items"])
    finally:
        _teardown_foreign_workspace_relationship(other_workspace_id)


def test_relationship_create_requires_evidence_id(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "missing-evidence"),
        json={"relationship_type": "WORKS_ON", "to_entity_id": str(project_id)},
    )
    assert response.status_code == 422


def test_relationship_create_rejects_evidence_from_another_workspace(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    other_workspace_id = uuid4()
    other_node_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": other_workspace_id, "name": "Other Workspace", "created_at": now},
        )
        create_identity(connection, workspace_id=other_workspace_id, user_id=uuid4(), now=now)
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "attributes, created_at, updated_at) VALUES (:id, :workspace_id, 'person', "
                "'Foreign Node', '{}'::jsonb, :now, :now)"
            ),
            {"id": other_node_id, "workspace_id": other_workspace_id, "now": now},
        )
    try:
        foreign_evidence_id = _create_evidence(other_workspace_id, other_node_id)
        response = client.post(
            f"/api/v1/knowledge/entities/{person_id}/relationships",
            headers=_headers(token, "foreign-evidence"),
            json={
                "relationship_type": "WORKS_ON",
                "to_entity_id": str(project_id),
                "evidence_id": str(foreign_evidence_id),
            },
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "EVIDENCE_NOT_FOUND"
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM pkos_evidence WHERE workspace_id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )
            connection.execute(
                text("DELETE FROM pkos_nodes WHERE workspace_id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspace_memberships WHERE workspace_id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )
            connection.execute(
                text("DELETE FROM users WHERE workspace_id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )


def test_relationship_create_rejects_unavailable_evidence(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE pkos_evidence SET evidence_state = 'deleted' WHERE id = :id"),
            {"id": evidence_id},
        )
    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "unavailable-evidence"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "EVIDENCE_UNAVAILABLE"


def test_relationship_create_rejects_archived_target_entity(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    archive = client.post(
        f"/api/v1/knowledge/entities/{project_id}/archive",
        headers=_headers(token, "archive-target"),
        json={"expected_version": 1},
    )
    assert archive.status_code == 200, archive.text

    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "relate-to-archived"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_RELATIONSHIP"


def test_relationship_create_rejects_archived_source_entity(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    archive = client.post(
        f"/api/v1/knowledge/entities/{person_id}/archive",
        headers=_headers(token, "archive-source"),
        json={"expected_version": 1},
    )
    assert archive.status_code == 200, archive.text

    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "relate-from-archived"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "INVALID_RELATIONSHIP"


def test_relationship_handles_a_three_node_cycle(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    # Adversarial regression test: only a *self* relationship is rejected
    # (SELF_RELATIONSHIP_NOT_PERMITTED, see relationships.py) -- there is no
    # cycle-detection across 3+ distinct entities, and there shouldn't be:
    # real directed relationship types (DEPENDS_ON, MANAGES, WORKS_ON) can
    # legitimately form a cycle (A depends on B, B depends on C, C depends
    # on A is a real, if awkward, state of the world). This proves the
    # system stores and serves such a cycle correctly rather than looping
    # or corrupting state -- not that it should reject one.
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)

    third = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "cycle-create-third"),
        json={"kind": "project", "canonical_name": "Third Node"},
    )
    assert third.status_code == 201, third.text
    third_id = UUID(third.json()["id"])

    edges = [
        (person_id, project_id, "cycle-edge-1"),
        (project_id, third_id, "cycle-edge-2"),
        (third_id, person_id, "cycle-edge-3"),
    ]
    created_ids = []
    for from_id, to_id, key in edges:
        response = client.post(
            f"/api/v1/knowledge/entities/{from_id}/relationships",
            headers=_headers(token, key),
            json={
                "relationship_type": "DEPENDS_ON",
                "to_entity_id": str(to_id),
                "evidence_id": str(evidence_id),
            },
        )
        assert response.status_code == 201, response.text
        created_ids.append(response.json()["id"])

    for entity_id in (person_id, project_id, third_id):
        listed = client.get(
            f"/api/v1/knowledge/entities/{entity_id}/relationships",
            headers=_headers(token, f"cycle-list-{entity_id}"),
        )
        assert listed.status_code == 200
        listed_ids = {item["id"] for item in listed.json()["items"]}
        # Every node in a 3-cycle is both a source (one outgoing edge) and a
        # target (one incoming edge) of the cycle, so exactly two of the
        # three created edges must be visible from each entity.
        assert len(listed_ids & set(created_ids)) == 2


def test_relationship_create_rejects_valid_to_at_or_before_valid_from(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    # Adversarial regression test: RelationshipCreate.validate_valid_interval
    # has been enforced since the evidence-required fix, but had zero test
    # coverage until now.
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    now = datetime.now(UTC)

    response = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "invalid-interval"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
            "valid_from": now.isoformat(),
            "valid_to": now.isoformat(),
        },
    )
    assert response.status_code == 422, response.text


# --- relationship_type/direction filters (team-concept gap analysis) -------
#
# `MEMBER_OF` was a real, working relationship type with no way to query it
# as a roster -- these prove a team's membership list is exactly
# `relationship_type=MEMBER_OF&direction=incoming` composed against this
# same generic endpoint, per relationships.py's own docstring.


def test_relationship_direction_incoming_excludes_outgoing_edges(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    create = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "direction-create"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
    )
    relationship_id = create.json()["id"]

    # From the person's own perspective, this is an outgoing edge -- an
    # "incoming"-only filter must exclude it.
    incoming = client.get(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        params={"direction": "incoming"},
        headers=_headers(token, "direction-incoming-person"),
    )
    assert incoming.status_code == 200, incoming.text
    assert all(item["id"] != relationship_id for item in incoming.json()["items"])

    outgoing = client.get(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        params={"direction": "outgoing"},
        headers=_headers(token, "direction-outgoing-person"),
    )
    assert outgoing.status_code == 200, outgoing.text
    assert any(item["id"] == relationship_id for item in outgoing.json()["items"])

    # From the project's own perspective, the identical edge is incoming.
    incoming_from_project = client.get(
        f"/api/v1/knowledge/entities/{project_id}/relationships",
        params={"direction": "incoming"},
        headers=_headers(token, "direction-incoming-project"),
    )
    assert incoming_from_project.status_code == 200, incoming_from_project.text
    assert any(item["id"] == relationship_id for item in incoming_from_project.json()["items"])


def test_relationship_type_filter_excludes_other_types(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    evidence_id = _create_evidence(workspace_id, person_id)
    works_on = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "type-filter-works-on"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
    )
    depends_on = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "type-filter-depends-on"),
        json={
            "relationship_type": "DEPENDS_ON",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
    )

    filtered = client.get(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        params={"relationship_type": "WORKS_ON"},
        headers=_headers(token, "type-filter-list"),
    )
    assert filtered.status_code == 200, filtered.text
    listed_ids = {item["id"] for item in filtered.json()["items"]}
    assert works_on.json()["id"] in listed_ids
    assert depends_on.json()["id"] not in listed_ids


def test_team_roster_is_relationship_type_and_direction_composed(
    relationships_test_context: tuple[TestClient, UUID, UUID, str, UUID, UUID],
) -> None:
    """The actual "team roster" query this filter pair exists for: a team
    entity's membership list, resolved to real names -- not bare UUIDs a
    human would have to look up one at a time.
    """
    client, workspace_id, _user_id, token, person_id, project_id = relationships_test_context
    team = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, "roster-create-team"),
        json={"kind": "team", "canonical_name": "Platform Engineering"},
    )
    assert team.status_code == 201, team.text
    team_id = UUID(team.json()["id"])
    evidence_id = _create_evidence(workspace_id, person_id)

    member_of = client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "roster-member-of"),
        json={
            "relationship_type": "MEMBER_OF",
            "to_entity_id": str(team_id),
            "evidence_id": str(evidence_id),
        },
    )
    assert member_of.status_code == 201, member_of.text

    # A second relationship of a different type, same two entities, to
    # prove the roster query is genuinely type-filtered, not just showing
    # every incoming edge.
    client.post(
        f"/api/v1/knowledge/entities/{person_id}/relationships",
        headers=_headers(token, "roster-relates-to"),
        json={
            "relationship_type": "RELATES_TO",
            "to_entity_id": str(team_id),
            "evidence_id": str(evidence_id),
        },
    )

    # A third relationship, same MEMBER_OF type but the OUTGOING direction
    # from the team's own perspective (team -> project). Without this edge,
    # `direction=incoming` below would pass even if the direction filter
    # were deleted or inverted entirely -- there would be nothing for it to
    # wrongly include.
    outgoing_member_of = client.post(
        f"/api/v1/knowledge/entities/{team_id}/relationships",
        headers=_headers(token, "roster-outgoing-member-of"),
        json={
            "relationship_type": "MEMBER_OF",
            "to_entity_id": str(project_id),
            "evidence_id": str(evidence_id),
        },
    )
    assert outgoing_member_of.status_code == 201, outgoing_member_of.text

    roster = client.get(
        f"/api/v1/knowledge/entities/{team_id}/relationships",
        params={"relationship_type": "MEMBER_OF", "direction": "incoming"},
        headers=_headers(token, "roster-list"),
    )
    assert roster.status_code == 200, roster.text
    items = roster.json()["items"]
    assert len(items) == 1
    assert items[0]["id"] == member_of.json()["id"]
    assert items[0]["from_entity_name"] == "Ada Lovelace"
    assert items[0]["from_entity_kind"] == "person"
    assert items[0]["to_entity_name"] == "Platform Engineering"
    assert items[0]["to_entity_kind"] == "team"

    outgoing_roster_ids = {
        item["id"]
        for item in client.get(
            f"/api/v1/knowledge/entities/{team_id}/relationships",
            params={"relationship_type": "MEMBER_OF", "direction": "outgoing"},
            headers=_headers(token, "roster-outgoing-list"),
        ).json()["items"]
    }
    assert outgoing_member_of.json()["id"] in outgoing_roster_ids
    assert member_of.json()["id"] not in outgoing_roster_ids
