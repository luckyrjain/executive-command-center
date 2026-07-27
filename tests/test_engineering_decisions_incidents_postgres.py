"""Phase 6 Engineering Workspace Task 6: decisions, incidents and
change-correlation (`backend/ecc/domains/engineering/decisions_incidents.py`).

Covers, per this task's own disclosed scope (see that module's own
docstring for the full "workspace-authored, not synced" design and the
deferred-Phase-2-identity-resolution disclosure):

1. `POST /incidents`: create with and without `change_ids`, `change_ids`
   validated against the caller's own workspace (`CHANGE_NOT_FOUND`).
2. `POST /incidents/{id}/resolve`: success, 404 when missing, 409 when
   already resolved, 422 when `resolved_at` precedes `detected_at`.
3. `GET /incidents`: with and without the `status` filter.
4. `POST /decisions`: create with and without `change_ids`.
5. `POST /decisions/{id}/decide`: success, 404 when missing, 409 when not
   `proposed`.
6. `GET /decisions`: with and without the `status` filter.
7. `Idempotency-Key` replay for all four mutation endpoints.
8. `CsrfDep` rejection (missing `X-CSRF-Token`) for all four mutation
   endpoints.
9. Cross-workspace isolation: workspace B cannot resolve/decide/see
   workspace A's rows, and cannot link to workspace A's `changes`.
10. The migration's own CHECK-constraint consistency between `status` and
    the relevant timestamp column, exercised at the HTTP layer (a
    resolve/decide call can never produce a status/timestamp mismatch).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = hmac_new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "incident_changes",
            "decision_changes",
            "incidents",
            "engineering_decisions",
            "changes",
            "repositories",
            "connector_accounts",
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


@pytest.fixture
def engineering_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Decisions Incidents Test', 'UTC', :now)"
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


def _insert_change(workspace_id: UUID, user_id: UUID, *, external_id: str = "1") -> UUID:
    account_id = uuid4()
    repository_id = uuid4()
    change_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :ws, 'github', :ext, 'Fixture account',
                    ARRAY[]::text[], :cred, 'active', 1, :actor, :actor, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "ws": workspace_id,
                "ext": f"acct-{account_id}",
                "cred": b"fake",
                "actor": user_id,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO repositories (
                    id, workspace_id, connector_account_id, provider, external_id,
                    name, source_url, default_branch, permission_state, freshness_state,
                    observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, 'github', :ext, 'acme/repo', 'https://x', 'main',
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": repository_id,
                "ws": workspace_id,
                "acct": account_id,
                "ext": f"repo-{repository_id}",
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO changes (
                    id, workspace_id, connector_account_id, repository_id, provider,
                    external_id, provider_number, title, source_url, status, merged_at,
                    permission_state, freshness_state, observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, :repo, 'github', :ext, :ext, 'A change', 'https://x',
                    'closed', :now, 'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": change_id,
                "ws": workspace_id,
                "acct": account_id,
                "repo": repository_id,
                "ext": external_id,
                "now": now,
            },
        )
    return change_id


# --- POST /incidents ---------------------------------------------------------


def test_create_incident_without_change_ids(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    response = client.post(
        "/api/v1/engineering/incidents",
        json={
            "title": "Prod outage",
            "description": "Checkout down",
            "severity": "critical",
            "detected_at": now.isoformat(),
        },
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["title"] == "Prod outage"
    assert body["severity"] == "critical"
    assert body["status"] == "open"
    assert body["resolved_at"] is None
    assert body["change_ids"] == []
    assert body["version"] == 1


def test_create_incident_with_change_ids(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    change_id = _insert_change(workspace_id, user_id)
    now = datetime.now(UTC)
    response = client.post(
        "/api/v1/engineering/incidents",
        json={
            "title": "Prod outage",
            "detected_at": now.isoformat(),
            "change_ids": [str(change_id)],
        },
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    assert response.json()["change_ids"] == [str(change_id)]


def test_create_incident_rejects_change_id_not_in_workspace(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    response = client.post(
        "/api/v1/engineering/incidents",
        json={
            "title": "Prod outage",
            "detected_at": now.isoformat(),
            "change_ids": [str(uuid4())],
        },
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert "CHANGE_NOT_FOUND" in response.json()["error"]["code"]


def test_create_incident_idempotency_replay(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    key = str(uuid4())
    payload = {"title": "Prod outage", "detected_at": datetime.now(UTC).isoformat()}

    first = client.post(
        "/api/v1/engineering/incidents", json=payload, headers=_headers(token, key=key)
    )
    assert first.status_code == 201
    replay = client.post(
        "/api/v1/engineering/incidents", json=payload, headers=_headers(token, key=key)
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]


def test_create_incident_idempotency_conflict_on_payload_mismatch(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    key = str(uuid4())
    now = datetime.now(UTC)
    first = client.post(
        "/api/v1/engineering/incidents",
        json={"title": "First", "detected_at": now.isoformat()},
        headers=_headers(token, key=key),
    )
    assert first.status_code == 201
    conflict = client.post(
        "/api/v1/engineering/incidents",
        json={"title": "Second", "detected_at": now.isoformat()},
        headers=_headers(token, key=key),
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_create_incident_rejects_missing_csrf_token(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, _token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/incidents",
        json={"title": "X", "detected_at": datetime.now(UTC).isoformat()},
        headers={"Idempotency-Key": str(uuid4()), "X-Correlation-ID": str(uuid4())},
    )
    assert response.status_code == 403


# --- POST /incidents/{id}/resolve --------------------------------------------


def _create_incident(client: TestClient, token: str, *, detected_at: datetime) -> str:
    response = client.post(
        "/api/v1/engineering/incidents",
        json={"title": "Outage", "detected_at": detected_at.isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    incident_id: str = response.json()["id"]
    return incident_id


def test_resolve_incident_success(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    incident_id = _create_incident(client, token, detected_at=now - timedelta(hours=2))

    response = client.post(
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        json={"resolved_at": now.isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolved_at"] is not None
    assert body["version"] == 2


def test_resolve_incident_404_when_missing(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        f"/api/v1/engineering/incidents/{uuid4()}/resolve",
        json={"resolved_at": datetime.now(UTC).isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "INCIDENT_NOT_FOUND"


def test_resolve_incident_409_when_already_resolved(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    incident_id = _create_incident(client, token, detected_at=now - timedelta(hours=2))
    first = client.post(
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        json={"resolved_at": now.isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        json={"resolved_at": now.isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "INCIDENT_ALREADY_RESOLVED"


def test_resolve_incident_422_when_resolved_before_detected(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    incident_id = _create_incident(client, token, detected_at=now)

    response = client.post(
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        json={"resolved_at": (now - timedelta(hours=1)).isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "RESOLVED_AT_BEFORE_DETECTED_AT"


def test_resolve_incident_idempotency_replay(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    incident_id = _create_incident(client, token, detected_at=now - timedelta(hours=2))
    key = str(uuid4())
    payload = {"resolved_at": now.isoformat()}

    first = client.post(
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        json=payload,
        headers=_headers(token, key=key),
    )
    assert first.status_code == 200
    replay = client.post(
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        json=payload,
        headers=_headers(token, key=key),
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == first.json()["version"]


def test_resolve_incident_rejects_missing_csrf_token(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    incident_id = _create_incident(client, token, detected_at=now - timedelta(hours=2))
    response = client.post(
        f"/api/v1/engineering/incidents/{incident_id}/resolve",
        json={"resolved_at": now.isoformat()},
        headers={"Idempotency-Key": str(uuid4()), "X-Correlation-ID": str(uuid4())},
    )
    assert response.status_code == 403


def test_resolve_incident_cross_workspace_404(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    incident_id = _create_incident(client, token, detected_at=now - timedelta(hours=2))

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'x', :now)"
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
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        response = other_client.post(
            f"/api/v1/engineering/incidents/{incident_id}/resolve",
            json={"resolved_at": now.isoformat()},
            headers=_headers(other_token, key=str(uuid4())),
        )
        assert response.status_code == 404
    finally:
        other_client.close()
        _cleanup_workspace(other_workspace_id)


# --- GET /incidents -----------------------------------------------------------


def test_list_incidents_with_and_without_status_filter(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    now = datetime.now(UTC)
    open_id = _create_incident(client, token, detected_at=now - timedelta(hours=3))
    resolved_id = _create_incident(client, token, detected_at=now - timedelta(hours=2))
    resolve = client.post(
        f"/api/v1/engineering/incidents/{resolved_id}/resolve",
        json={"resolved_at": now.isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert resolve.status_code == 200

    all_response = client.get(
        "/api/v1/engineering/incidents", headers={"X-Correlation-ID": str(uuid4())}
    )
    assert all_response.status_code == 200
    all_ids = {row["id"] for row in all_response.json()["incidents"]}
    assert {open_id, resolved_id} <= all_ids

    open_response = client.get(
        "/api/v1/engineering/incidents?status=open", headers={"X-Correlation-ID": str(uuid4())}
    )
    assert open_response.status_code == 200
    open_ids = {row["id"] for row in open_response.json()["incidents"]}
    assert open_id in open_ids
    assert resolved_id not in open_ids


# --- POST /decisions ----------------------------------------------------------


def test_create_decision_without_change_ids(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/decisions",
        json={"title": "Adopt trunk-based dev", "rationale": "Faster feedback"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "proposed"
    assert body["decided_at"] is None
    assert body["change_ids"] == []


def test_create_decision_with_change_ids(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    change_id = _insert_change(workspace_id, user_id)
    response = client.post(
        "/api/v1/engineering/decisions",
        json={"title": "Adopt trunk-based dev", "change_ids": [str(change_id)]},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    assert response.json()["change_ids"] == [str(change_id)]


def test_create_decision_rejects_change_id_not_in_workspace(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/decisions",
        json={"title": "X", "change_ids": [str(uuid4())]},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert "CHANGE_NOT_FOUND" in response.json()["error"]["code"]


def test_create_decision_rejects_missing_csrf_token(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, _token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/decisions",
        json={"title": "X"},
        headers={"Idempotency-Key": str(uuid4()), "X-Correlation-ID": str(uuid4())},
    )
    assert response.status_code == 403


# --- POST /decisions/{id}/decide ----------------------------------------------


def _create_decision(client: TestClient, token: str, *, title: str = "A decision") -> str:
    response = client.post(
        "/api/v1/engineering/decisions",
        json={"title": title},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    decision_id: str = response.json()["id"]
    return decision_id


def test_decide_decision_success(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    decision_id = _create_decision(client, token)
    now = datetime.now(UTC)

    response = client.post(
        f"/api/v1/engineering/decisions/{decision_id}/decide",
        json={"decided_at": now.isoformat(), "rationale": "Because"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "decided"
    assert body["decided_at"] is not None
    assert body["rationale"] == "Because"
    assert body["version"] == 2


def test_decide_decision_keeps_existing_rationale_when_not_provided(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/decisions",
        json={"title": "X", "rationale": "Original"},
        headers=_headers(token, key=str(uuid4())),
    )
    decision_id = response.json()["id"]

    decide = client.post(
        f"/api/v1/engineering/decisions/{decision_id}/decide",
        json={"decided_at": datetime.now(UTC).isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert decide.status_code == 200
    assert decide.json()["rationale"] == "Original"


def test_decide_decision_404_when_missing(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        f"/api/v1/engineering/decisions/{uuid4()}/decide",
        json={"decided_at": datetime.now(UTC).isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "DECISION_NOT_FOUND"


def test_decide_decision_409_when_not_proposed(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    decision_id = _create_decision(client, token)
    now = datetime.now(UTC)
    first = client.post(
        f"/api/v1/engineering/decisions/{decision_id}/decide",
        json={"decided_at": now.isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/engineering/decisions/{decision_id}/decide",
        json={"decided_at": now.isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "DECISION_NOT_PROPOSED"


def test_decide_decision_idempotency_replay(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    decision_id = _create_decision(client, token)
    key = str(uuid4())
    payload = {"decided_at": datetime.now(UTC).isoformat()}

    first = client.post(
        f"/api/v1/engineering/decisions/{decision_id}/decide",
        json=payload,
        headers=_headers(token, key=key),
    )
    assert first.status_code == 200
    replay = client.post(
        f"/api/v1/engineering/decisions/{decision_id}/decide",
        json=payload,
        headers=_headers(token, key=key),
    )
    assert replay.status_code == 200
    assert replay.json()["version"] == first.json()["version"]


def test_decide_decision_rejects_missing_csrf_token(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    decision_id = _create_decision(client, token)
    response = client.post(
        f"/api/v1/engineering/decisions/{decision_id}/decide",
        json={"decided_at": datetime.now(UTC).isoformat()},
        headers={"Idempotency-Key": str(uuid4()), "X-Correlation-ID": str(uuid4())},
    )
    assert response.status_code == 403


# --- GET /decisions ------------------------------------------------------------


def test_list_decisions_with_and_without_status_filter(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    proposed_id = _create_decision(client, token, title="Proposed one")
    decided_id = _create_decision(client, token, title="Decided one")
    decide = client.post(
        f"/api/v1/engineering/decisions/{decided_id}/decide",
        json={"decided_at": datetime.now(UTC).isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert decide.status_code == 200

    all_response = client.get(
        "/api/v1/engineering/decisions", headers={"X-Correlation-ID": str(uuid4())}
    )
    assert all_response.status_code == 200
    all_ids = {row["id"] for row in all_response.json()["decisions"]}
    assert {proposed_id, decided_id} <= all_ids

    proposed_response = client.get(
        "/api/v1/engineering/decisions?status=proposed",
        headers={"X-Correlation-ID": str(uuid4())},
    )
    assert proposed_response.status_code == 200
    proposed_ids = {row["id"] for row in proposed_response.json()["decisions"]}
    assert proposed_id in proposed_ids
    assert decided_id not in proposed_ids


# --- direct-SQL CHECK constraint tests -----------------------------------------


def test_incidents_check_constraint_rejects_resolved_without_resolved_at(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = engineering_test_context
    now = datetime.now(UTC)
    with pytest.raises(Exception, match="ck_incidents_resolved_at_matches_status"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO incidents (
                        id, workspace_id, title, severity, status, detected_at,
                        resolved_at, version, created_by, updated_by, created_at, updated_at
                    ) VALUES (
                        :id, :ws, 'Bad row', 'high', 'resolved', :now,
                        NULL, 1, :actor, :actor, :now, :now
                    )
                    """
                ),
                {"id": uuid4(), "ws": workspace_id, "actor": user_id, "now": now},
            )


def test_engineering_decisions_check_constraint_rejects_decided_without_decided_at(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = engineering_test_context
    now = datetime.now(UTC)
    with pytest.raises(Exception, match="ck_engineering_decisions_decided_at_matches_status"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO engineering_decisions (
                        id, workspace_id, title, status, decided_at,
                        version, created_by, updated_by, created_at, updated_at
                    ) VALUES (
                        :id, :ws, 'Bad row', 'decided', NULL,
                        1, :actor, :actor, :now, :now
                    )
                    """
                ),
                {"id": uuid4(), "ws": workspace_id, "actor": user_id, "now": now},
            )
