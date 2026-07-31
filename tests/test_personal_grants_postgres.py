"""Phase 7 Personal Intelligence Task 5, part 1: `cross_domain_grants`
schema and grant management (`ecc.domains.personal.grants`,
`docs/phases/phase-007/DATA-MODEL.md`'s "Task 5 status" section).

No AI-generated insight consumes a grant yet -- that is a separate,
follow-up PR (Task 5 part 2). This file covers only the grant lifecycle
itself: create (requiring the source domain be enabled first), list
(optionally filtered by `source_domain_key`/`active_only`), and revoke
(idempotent, a no-op on an already-revoked grant), plus cross-workspace
isolation.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
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
def grants_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Grants Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "cross_domain_grants",
            "personal_insights",
            "check_ins",
            "routines",
            "goals",
            "domain_records",
            "domain_sources",
            "deletion_jobs",
            "domain_consents",
            "personal_domains",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _enable(client: TestClient, token: str, domain_key: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/personal/domains",
        json={"domain_key": domain_key},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _create_grant(
    client: TestClient,
    token: str,
    *,
    source_domain_key: str,
    categories: list[str],
    expires_at: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_domain_key": source_domain_key,
        "purpose": "insight_generation",
        "granted_categories": categories,
    }
    if expires_at is not None:
        body["expires_at"] = expires_at
    resp = client.post(
        "/api/v1/personal/grants", json=body, headers=_headers(token, str(uuid4()))
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def test_create_grant_requires_enabled_source_domain(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = grants_test_context
    resp = client.post(
        "/api/v1/personal/grants",
        json={
            "source_domain_key": "habits",
            "purpose": "insight_generation",
            "granted_categories": ["habit_check_in"],
        },
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DOMAIN_NOT_ENABLED"


def test_create_and_list_grant(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = grants_test_context
    _enable(client, token, "habits")
    created = _create_grant(
        client, token, source_domain_key="habits", categories=["habit_check_in"]
    )
    assert created["source_domain_key"] == "habits"
    assert created["purpose"] == "insight_generation"
    assert created["granted_categories"] == ["habit_check_in"]
    assert created["revoked_at"] is None

    listed = client.get("/api/v1/personal/grants", headers=_headers(token)).json()["grants"]
    assert [g["id"] for g in listed] == [created["id"]]


def test_rejects_unrecognized_purpose(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = grants_test_context
    _enable(client, token, "habits")
    resp = client.post(
        "/api/v1/personal/grants",
        json={
            "source_domain_key": "habits",
            "purpose": "not_a_real_purpose",
            "granted_categories": ["habit_check_in"],
        },
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 422


def test_rejects_empty_granted_categories(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = grants_test_context
    _enable(client, token, "habits")
    resp = client.post(
        "/api/v1/personal/grants",
        json={
            "source_domain_key": "habits",
            "purpose": "insight_generation",
            "granted_categories": [],
        },
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 422


def test_list_filters_by_source_domain_key(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = grants_test_context
    _enable(client, token, "habits")
    _enable(client, token, "learning")
    habits_grant = _create_grant(
        client, token, source_domain_key="habits", categories=["habit_check_in"]
    )
    _create_grant(client, token, source_domain_key="learning", categories=["course"])

    filtered = client.get(
        "/api/v1/personal/grants?source_domain_key=habits", headers=_headers(token)
    ).json()["grants"]
    assert [g["id"] for g in filtered] == [habits_grant["id"]]


def test_active_only_excludes_revoked_and_expired_grants(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = grants_test_context
    _enable(client, token, "habits")
    active = _create_grant(
        client, token, source_domain_key="habits", categories=["habit_check_in"]
    )
    to_revoke = _create_grant(
        client, token, source_domain_key="habits", categories=["habit_check_in"]
    )
    expired = _create_grant(
        client,
        token,
        source_domain_key="habits",
        categories=["habit_check_in"],
        expires_at="2020-01-01T00:00:00Z",
    )

    client.post(
        f"/api/v1/personal/grants/{to_revoke['id']}/revoke",
        headers=_headers(token, str(uuid4())),
    )

    active_only = client.get(
        "/api/v1/personal/grants?active_only=true", headers=_headers(token)
    ).json()["grants"]
    assert [g["id"] for g in active_only] == [active["id"]]

    all_grants = client.get("/api/v1/personal/grants", headers=_headers(token)).json()["grants"]
    assert {g["id"] for g in all_grants} == {active["id"], to_revoke["id"], expired["id"]}


def test_revoke_grant_is_idempotent(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = grants_test_context
    _enable(client, token, "habits")
    grant = _create_grant(client, token, source_domain_key="habits", categories=["habit_check_in"])

    first = client.post(
        f"/api/v1/personal/grants/{grant['id']}/revoke", headers=_headers(token, str(uuid4()))
    )
    assert first.status_code == 200
    assert first.json()["revoked_at"] is not None
    first_revoked_at = first.json()["revoked_at"]

    second = client.post(
        f"/api/v1/personal/grants/{grant['id']}/revoke", headers=_headers(token, str(uuid4()))
    )
    assert second.status_code == 200
    assert second.json()["revoked_at"] == first_revoked_at


def test_revoke_unknown_grant_is_404(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = grants_test_context
    _enable(client, token, "habits")
    resp = client.post(
        f"/api/v1/personal/grants/{uuid4()}/revoke", headers=_headers(token, str(uuid4()))
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "GRANT_NOT_FOUND"


def test_grants_are_workspace_isolated(
    grants_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = grants_test_context
    _enable(client, token, "habits")
    _create_grant(client, token, source_domain_key="habits", categories=["habit_check_in"])

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Grants Test', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": other_workspace_id,
                "user_id": other_user_id,
                "token_hash": sha256(other_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    try:
        other_client = TestClient(app)
        other_client.cookies.set("ecc_session", other_token)
        other_grants = other_client.get(
            "/api/v1/personal/grants", headers=_headers(other_token)
        ).json()["grants"]
        assert other_grants == []
    finally:
        _cleanup_workspace(other_workspace_id)
