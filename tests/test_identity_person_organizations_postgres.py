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
def identity_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Identity Test", "created_at": now},
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


def test_create_person_forces_kind_regardless_of_caller_input(
    identity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = identity_test_context
    response = client.post(
        "/api/v1/identity/people",
        headers=_headers(token, "create-person"),
        json={"canonical_name": "Grace Hopper"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "person"


def test_create_organization_forces_kind_regardless_of_caller_input(
    identity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = identity_test_context
    response = client.post(
        "/api/v1/identity/organizations",
        headers=_headers(token, "create-org"),
        json={"canonical_name": "Analytical Engines Ltd"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "organization"


def test_person_create_rejects_kind_field_in_payload(
    identity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = identity_test_context
    response = client.post(
        "/api/v1/identity/people",
        headers=_headers(token, "create-person-with-kind"),
        json={"canonical_name": "Someone", "kind": "organization"},
    )
    assert response.status_code == 422


def _seed_second_session() -> tuple[UUID, str]:
    """A fully independent workspace + user + session, for proving an
    identity-created entity is invisible to a *different* authenticated
    session -- not just to an unauthenticated request."""
    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": other_workspace_id, "name": "Second Identity Workspace", "created_at": now},
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
    return other_workspace_id, other_token


def _teardown_second_session(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in ("pkos_nodes", "sessions", "users"):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def test_person_created_via_identity_is_invisible_from_another_workspace(
    identity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = identity_test_context
    created = client.post(
        "/api/v1/identity/people",
        headers=_headers(token, "isolation-create-person"),
        json={"canonical_name": "Isolated Person"},
    )
    assert created.status_code == 201, created.text
    person_id = created.json()["id"]

    other_workspace_id, other_token = _seed_second_session()
    try:
        other_client = TestClient(app)
        other_client.cookies.set("ecc_session", other_token)
        response = other_client.get(
            f"/api/v1/knowledge/entities/{person_id}",
            headers=_headers(other_token, "isolation-read-foreign-person"),
        )
        assert response.status_code == 404, response.text
    finally:
        _teardown_second_session(other_workspace_id)


def test_organization_created_via_identity_is_invisible_from_another_workspace(
    identity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = identity_test_context
    created = client.post(
        "/api/v1/identity/organizations",
        headers=_headers(token, "isolation-create-org"),
        json={"canonical_name": "Isolated Org"},
    )
    assert created.status_code == 201, created.text
    org_id = created.json()["id"]

    other_workspace_id, other_token = _seed_second_session()
    try:
        other_client = TestClient(app)
        other_client.cookies.set("ecc_session", other_token)
        response = other_client.get(
            f"/api/v1/knowledge/entities/{org_id}",
            headers=_headers(other_token, "isolation-read-foreign-org"),
        )
        assert response.status_code == 404, response.text
    finally:
        _teardown_second_session(other_workspace_id)
