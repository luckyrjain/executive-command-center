"""Phase 6 Engineering Workspace Task 1: connector framework and source
projections (`docs/superpowers/specs/2026-07-27-phase-6-engineering-
workspace-design.md`, `docs/phases/phase-006/CONNECTOR-CONTRACT.md`).

Covers, per this task's own scope ("connector framework and source
projections -- the platform layer only"):

1. `ConnectorRegistry`/`ConnectorAdapter` contract shape, exercised
   directly against `sandbox_adapter.SandboxGithubAdapter` (unit-level, no
   database).
2. Credential encryption round-trip (`ecc.domains.engineering.crypto`) --
   ciphertext never equals plaintext, decrypt recovers the original.
3. `POST /api/v1/engineering/connectors`: authorization success/failure,
   unsupported-provider rejection, duplicate-connection rejection, and
   that no response ever carries the credential or the encrypted column.
4. `GET /api/v1/engineering/connectors` cross-workspace isolation.
5. `POST .../{id}/sync` backfill then incremental (cursor progression),
   `GET /api/v1/engineering/sync-runs` ordering, and the disconnected-
   connector rejection.
6. `POST .../{id}/disable`: revokes (best-effort, sandbox no-ops),
   transitions to `disconnected`, and is idempotent when called again.
7. `Idempotency-Key` replay on create, and `IDEMPOTENCY_CONFLICT` when the
   same key is reused with a different payload.
8. Cross-workspace 404s on sync/disable (workspace B cannot act on
   workspace A's connector).
"""

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
from ecc.domains.engineering.connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorRegistry,
)
from ecc.domains.engineering.crypto import decrypt_credential, encrypt_credential
from ecc.domains.engineering.sandbox_adapter import SandboxGithubAdapter
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# --- unit-level: sandbox adapter / registry / crypto (no database) ---------


def test_sandbox_adapter_authorize_rejects_invalid_credential() -> None:
    adapter = SandboxGithubAdapter()
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("this-is-invalid")
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("")


def test_sandbox_adapter_authorize_is_deterministic() -> None:
    adapter = SandboxGithubAdapter()
    first = adapter.authorize("token-a")
    second = adapter.authorize("token-a")
    third = adapter.authorize("token-b")
    assert first.external_account_id == second.external_account_id
    assert first.external_account_id != third.external_account_id
    assert first.granted_scopes == adapter.required_scopes


def test_sandbox_adapter_incremental_sync_advances_cursor() -> None:
    adapter = SandboxGithubAdapter()
    context = ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="sandbox-x",
        credential="token",
    )
    backfill = adapter.backfill(context, "repository")
    assert backfill.next_cursor == "1"
    assert backfill.items_processed == 3

    step_two = adapter.incremental_sync(context, "repository", backfill.next_cursor)
    assert step_two.next_cursor == "2"
    assert step_two.items_processed == 1

    fresh = adapter.incremental_sync(context, "repository", None)
    assert fresh.next_cursor == "1"


def test_sandbox_adapter_refresh_permissions() -> None:
    adapter = SandboxGithubAdapter()
    active_context = ConnectorAccountContext(
        workspace_id=uuid4(), connector_account_id=uuid4(), external_account_id="x", credential="ok"
    )
    lost_context = ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="x",
        credential="please lose-access now",
    )
    assert adapter.refresh_permissions(active_context) == "active"
    assert adapter.refresh_permissions(lost_context) == "permission_lost"


def test_connector_registry_rejects_duplicate_provider() -> None:
    from ecc.domains.engineering.connectors import AdapterAlreadyRegistered

    registry = ConnectorRegistry()
    registry.register(SandboxGithubAdapter())
    with pytest.raises(AdapterAlreadyRegistered):
        registry.register(SandboxGithubAdapter())


def test_credential_encryption_round_trip() -> None:
    plaintext = "ghp_super_secret_token_value"
    ciphertext = encrypt_credential(plaintext)
    assert ciphertext != plaintext.encode()
    assert plaintext.encode() not in ciphertext
    assert decrypt_credential(ciphertext) == plaintext


# --- HTTP-level: connector lifecycle ---------------------------------------


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
                "VALUES (:id, 'Engineering Connector Test', 'Asia/Kolkata', :now)"
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
            "sync_runs",
            "sync_cursors",
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def test_create_connector_success_never_returns_credential(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-abc"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "credential" not in body
    assert "encrypted_credentials" not in body
    assert body["provider"] == "sandbox"
    assert body["status"] == "active"
    assert sorted(body["granted_scopes"]) == ["contents:read", "metadata:read"]

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT encrypted_credentials FROM connector_accounts "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        ).one()
        stored = bytes(row[0])
        assert b"token-abc" not in stored
        assert decrypt_credential(stored) == "token-abc"


def test_create_connector_rejects_invalid_credential(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "this-is-invalid"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "CONNECTOR_AUTHORIZATION_FAILED"


def test_create_connector_rejects_unsupported_provider(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    response = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "bitbucket", "credential": "token-abc"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "CONNECTOR_PROVIDER_NOT_SUPPORTED"


def test_create_connector_rejects_duplicate_connection(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    first = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-dup"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert first.status_code == 201

    second = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-dup"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "CONNECTOR_ALREADY_CONNECTED"


def test_create_connector_idempotency_replay_and_conflict(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    key = str(uuid4())
    payload = {"provider": "sandbox", "credential": "token-idem"}

    first = client.post(
        "/api/v1/engineering/connectors", json=payload, headers=_headers(token, key=key)
    )
    assert first.status_code == 201

    replay = client.post(
        "/api/v1/engineering/connectors", json=payload, headers=_headers(token, key=key)
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]

    conflict = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "different-token"},
        headers=_headers(token, key=key),
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "IDEMPOTENCY_CONFLICT"

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM connector_accounts WHERE workspace_id = :workspace_id"),
            {"workspace_id": _workspace_id},
        ).scalar_one()
        assert count == 1


def test_list_connectors_cross_workspace_isolation(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-list"},
        headers=_headers(token, key=str(uuid4())),
    )

    workspace_b_id = uuid4()
    user_b_id = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_b_id,
                "workspace_id": workspace_b_id,
                "email": f"{user_b_id}@example.test",
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
                "workspace_id": workspace_b_id,
                "user_id": user_b_id,
                "token_hash": sha256(token_b.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    try:
        client_b = TestClient(app)
        client_b.cookies.set("ecc_session", token_b)
        response = client_b.get("/api/v1/engineering/connectors")
        assert response.status_code == 200
        assert response.json()["connectors"] == []
        client_b.close()
    finally:
        _cleanup_workspace(workspace_b_id)


def test_sync_backfill_then_incremental_advances_cursor_and_records_runs(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-sync"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = created["id"]

    backfill = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert backfill.status_code == 200, backfill.text
    assert backfill.json()["status"] == "succeeded"
    assert backfill.json()["items_processed"] == 3

    incremental = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "incremental", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert incremental.status_code == 200, incremental.text
    assert incremental.json()["items_processed"] == 1

    with engine.begin() as connection:
        cursor = connection.execute(
            text(
                "SELECT cursor_value FROM sync_cursors WHERE workspace_id = :workspace_id "
                "AND connector_account_id = :account_id AND resource_type = 'repository'"
            ),
            {"workspace_id": workspace_id, "account_id": UUID(account_id)},
        ).scalar_one()
        assert cursor == "2"

        last_synced = connection.execute(
            text(
                "SELECT last_synced_at FROM connector_accounts WHERE id = :account_id"
            ),
            {"account_id": UUID(account_id)},
        ).scalar_one()
        assert last_synced is not None

    runs = client.get(
        "/api/v1/engineering/sync-runs", params={"connector_account_id": account_id}
    )
    assert runs.status_code == 200
    run_bodies = runs.json()["sync_runs"]
    assert len(run_bodies) == 2
    assert run_bodies[0]["started_at"] >= run_bodies[1]["started_at"]


def test_sync_rejected_after_disconnect(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-disconnect"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = created["id"]

    disable = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert disable.status_code == 200
    assert disable.json()["status"] == "disconnected"
    assert disable.json()["disconnected_at"] is not None

    # Idempotent: disabling an already-disconnected connector again is a no-op 200.
    disable_again = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert disable_again.status_code == 200
    assert disable_again.json()["status"] == "disconnected"

    sync_after_disconnect = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert sync_after_disconnect.status_code == 409
    assert sync_after_disconnect.json()["detail"] == "CONNECTOR_DISCONNECTED"


def test_sync_and_disable_cross_workspace_404(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = engineering_test_context
    created = client.post(
        "/api/v1/engineering/connectors",
        json={"provider": "sandbox", "credential": "token-cross"},
        headers=_headers(token, key=str(uuid4())),
    ).json()
    account_id = created["id"]

    workspace_b_id = uuid4()
    user_b_id = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace B', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_b_id,
                "workspace_id": workspace_b_id,
                "email": f"{user_b_id}@example.test",
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
                "workspace_id": workspace_b_id,
                "user_id": user_b_id,
                "token_hash": sha256(token_b.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    try:
        client_b = TestClient(app)
        client_b.cookies.set("ecc_session", token_b)

        sync_response = client_b.post(
            f"/api/v1/engineering/connectors/{account_id}/sync",
            json={"run_type": "backfill", "resource_type": "repository"},
            headers=_headers(token_b, key=str(uuid4())),
        )
        assert sync_response.status_code == 404
        assert sync_response.json()["detail"] == "CONNECTOR_NOT_FOUND"

        disable_response = client_b.post(
            f"/api/v1/engineering/connectors/{account_id}/disable",
            headers=_headers(token_b, key=str(uuid4())),
        )
        assert disable_response.status_code == 404
        assert disable_response.json()["detail"] == "CONNECTOR_NOT_FOUND"

        client_b.close()
    finally:
        _cleanup_workspace(workspace_b_id)
