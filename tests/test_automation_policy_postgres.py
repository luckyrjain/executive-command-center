"""Phase 5 Automation Task 1: automation policy model (design doc Decision
6, `docs/phases/phase-005/APPROVAL-POLICY.md`).

Covers: policy creation (90-day default expiry, required non-nullable
`value_limit`/`count_limit`, `rate_limit` default), `policy_status`/`is_
policy_usable`, `revoke_policy`'s state transitions (application layer),
the DB-level `NOT NULL`/`CHECK` backstops, HTTP endpoints, and
cross-workspace isolation.

Plus, added by the test-coverage audit that found this module had no
`Idempotency-Key` coverage of either kind: same-key replay and same-key-
different-request `IDEMPOTENCY_CONFLICT` on `POST /policies/{id}/revoke` --
the one policy mutation whose effect is immediate and run-affecting. See
that section's own header comment for why `policy.py`'s independently
hand-copied idempotency helpers make sibling-module coverage worthless here.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import policy as automation_policy
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def policy_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Policy Test', 'Asia/Kolkata', :now)"
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
            "triggers",
            "automation_policies",
            "workflow_versions",
            "workflow_definitions",
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


def _insert_workflow_family(workspace_id: UUID, user_id: UUID, workflow_id: str) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
                "created_at, updated_at) VALUES (:id, :workspace_id, :workflow_id, :created_by, "
                ":now, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "workflow_id": workflow_id,
                "created_by": user_id,
                "now": now,
            },
        )


def _create_workflow_via_api(client: TestClient, token: str, workflow_id: str) -> None:
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "condition",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
            }
        ]
    }
    response = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": workflow_id, "graph": graph, "trigger_refs": []},
        headers=_headers(token, key=f"wf-{workflow_id}"),
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# policy_status / is_policy_usable -- pure functions.
# ---------------------------------------------------------------------------


def _make_policy(
    *, expires_at: datetime, revoked_at: datetime | None
) -> automation_policy.AutomationPolicy:
    now = datetime.now(UTC)
    return automation_policy.AutomationPolicy(
        id=uuid4(),
        workspace_id=uuid4(),
        workflow_id="test.workflow",
        action_types=(),
        data_classes=(),
        value_limit=Decimal("0"),
        count_limit=0,
        rate_limit={"runs_per_workflow_per_hour": 10},
        schedule=None,
        approval_mode="per_run",
        expires_at=expires_at,
        revoked_at=revoked_at,
        version=1,
        created_by=uuid4(),
        updated_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


def test_policy_status_active_when_not_expired_and_not_revoked() -> None:
    policy = _make_policy(expires_at=datetime.now(UTC) + timedelta(days=1), revoked_at=None)
    assert automation_policy.policy_status(policy) == "active"
    assert automation_policy.is_policy_usable(policy) is True


def test_policy_status_expired_when_expires_at_passed() -> None:
    policy = _make_policy(expires_at=datetime.now(UTC) - timedelta(seconds=1), revoked_at=None)
    assert automation_policy.policy_status(policy) == "expired"
    assert automation_policy.is_policy_usable(policy) is False


def test_policy_status_revoked_takes_precedence_over_expired() -> None:
    now = datetime.now(UTC)
    policy = _make_policy(expires_at=now - timedelta(days=1), revoked_at=now - timedelta(hours=1))
    assert automation_policy.policy_status(policy) == "revoked"
    assert automation_policy.is_policy_usable(policy) is False


# ---------------------------------------------------------------------------
# create_policy / revoke_policy -- application layer.
# ---------------------------------------------------------------------------


def test_create_policy_defaults_expires_at_to_90_days_and_default_rate_limit(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = policy_test_context
    workflow_id = f"test.create-policy.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session:
        with session.begin():
            created = automation_policy.create_policy(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                action_types=["local.create_note"],
                data_classes=["internal"],
                value_limit=Decimal("100.00"),
                count_limit=5,
                rate_limit=None,
                schedule=None,
                approval_mode="per_run",
            )
        assert created.value_limit == Decimal("100.00")
        assert created.count_limit == 5
        assert created.rate_limit == {"runs_per_workflow_per_hour": 10}
        assert created.revoked_at is None
        delta = created.expires_at - created.created_at
        assert timedelta(days=89) < delta < timedelta(days=91)


def test_revoke_policy_sets_revoked_at_and_bumps_version(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = policy_test_context
    workflow_id = f"test.revoke.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session:
        with session.begin():
            created = automation_policy.create_policy(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                action_types=[],
                data_classes=[],
                value_limit=Decimal("0"),
                count_limit=0,
                rate_limit=None,
                schedule=None,
                approval_mode="preview_only",
            )
    with SessionFactory() as session:
        with session.begin():
            result = automation_policy.revoke_policy(session, workspace_id, user_id, created.id)
        assert isinstance(result, automation_policy.AutomationPolicy)
        assert result.revoked_at is not None
        assert result.version == 2


def test_revoke_policy_already_revoked_returns_already_revoked(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = policy_test_context
    workflow_id = f"test.revoke-twice.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session:
        with session.begin():
            created = automation_policy.create_policy(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                action_types=[],
                data_classes=[],
                value_limit=Decimal("0"),
                count_limit=0,
                rate_limit=None,
                schedule=None,
                approval_mode="preview_only",
            )
    with SessionFactory() as session:
        with session.begin():
            automation_policy.revoke_policy(session, workspace_id, user_id, created.id)
    with SessionFactory() as session:
        with session.begin():
            result = automation_policy.revoke_policy(session, workspace_id, user_id, created.id)
        assert isinstance(result, automation_policy.PolicyAlreadyRevoked)


def test_revoke_policy_already_expired_returns_already_expired(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = policy_test_context
    workflow_id = f"test.revoke-expired.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session:
        with session.begin():
            created = automation_policy.create_policy(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                action_types=[],
                data_classes=[],
                value_limit=Decimal("0"),
                count_limit=0,
                rate_limit=None,
                schedule=None,
                approval_mode="preview_only",
            )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE automation_policies SET expires_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": created.id},
        )
    with SessionFactory() as session:
        with session.begin():
            result = automation_policy.revoke_policy(session, workspace_id, user_id, created.id)
        assert isinstance(result, automation_policy.PolicyAlreadyExpired)


def test_revoke_policy_unknown_id_returns_not_found(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = policy_test_context
    with SessionFactory() as session:
        with session.begin():
            result = automation_policy.revoke_policy(session, workspace_id, user_id, uuid4())
        assert isinstance(result, automation_policy.PolicyNotFound)


# ---------------------------------------------------------------------------
# DB-level NOT NULL / CHECK backstops (direct SQL).
# ---------------------------------------------------------------------------


def test_value_limit_is_not_nullable_at_the_database_layer(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = policy_test_context
    workflow_id = f"test.db-not-null.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO automation_policies (
                        id, workspace_id, workflow_id, count_limit, approval_mode,
                        expires_at, version, created_by, updated_by, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :workflow_id, 1, 'per_run',
                        now() + interval '90 days', 1, :actor_id, :actor_id, now(), now()
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "workflow_id": workflow_id,
                    "actor_id": user_id,
                },
            )


def test_count_limit_is_not_nullable_at_the_database_layer(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = policy_test_context
    workflow_id = f"test.db-not-null-count.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO automation_policies (
                        id, workspace_id, workflow_id, value_limit, approval_mode,
                        expires_at, version, created_by, updated_by, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :workflow_id, 0, 'per_run',
                        now() + interval '90 days', 1, :actor_id, :actor_id, now(), now()
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "workflow_id": workflow_id,
                    "actor_id": user_id,
                },
            )


# ---------------------------------------------------------------------------
# HTTP endpoints.
# ---------------------------------------------------------------------------


def test_create_policy_via_endpoint(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.http-create.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)

    response = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "action_types": ["local.create_note"],
            "data_classes": ["internal"],
            "value_limit": "50.00",
            "count_limit": 3,
            "approval_mode": "bounded_recurring",
        },
        headers=_headers(token, key="policy-create-1"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workflow_id"] == workflow_id
    assert body["status"] == "active"
    assert body["revoked_at"] is None
    assert body["rate_limit"] == {"runs_per_workflow_per_hour": 10}


def test_create_policy_missing_value_limit_and_count_limit_is_422(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.http-missing-limits.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)

    response = client.post(
        "/api/v1/automations/policies",
        json={"workflow_id": workflow_id, "approval_mode": "per_run"},
        headers=_headers(token, key="policy-missing-limits"),
    )
    assert response.status_code == 422


def test_create_policy_for_unknown_workflow_is_404(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    response = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": f"test.no-such-workflow.{uuid4().hex}",
            "value_limit": "1",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
        headers=_headers(token, key="policy-no-workflow"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORKFLOW_NOT_FOUND"


def test_list_policies_filters_by_workflow_id(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_a = f"test.http-list-a.{uuid4().hex}"
    workflow_b = f"test.http-list-b.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_a)
    _create_workflow_via_api(client, token, workflow_b)

    for workflow_id, key in ((workflow_a, "list-a"), (workflow_b, "list-b")):
        client.post(
            "/api/v1/automations/policies",
            json={
                "workflow_id": workflow_id,
                "value_limit": "1",
                "count_limit": 1,
                "approval_mode": "per_run",
            },
            headers=_headers(token, key=key),
        )

    response = client.get("/api/v1/automations/policies", params={"workflow_id": workflow_a})
    assert response.status_code == 200
    ids = {p["workflow_id"] for p in response.json()["policies"]}
    assert ids == {workflow_a}


def test_revoke_policy_via_endpoint(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.http-revoke.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)
    created = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "value_limit": "1",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
        headers=_headers(token, key="revoke-create"),
    ).json()

    response = client.post(
        f"/api/v1/automations/policies/{created['id']}/revoke",
        headers=_headers(token, key="revoke-1"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    assert response.json()["revoked_at"] is not None


def test_revoke_policy_twice_is_policy_revoked(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.http-revoke-twice.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)
    created = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "value_limit": "1",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
        headers=_headers(token, key="revoke-twice-create"),
    ).json()

    client.post(
        f"/api/v1/automations/policies/{created['id']}/revoke",
        headers=_headers(token, key="revoke-twice-1"),
    )
    second = client.post(
        f"/api/v1/automations/policies/{created['id']}/revoke",
        headers=_headers(token, key="revoke-twice-2"),
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "POLICY_REVOKED"


def test_revoke_expired_policy_is_policy_expired(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.http-revoke-expired.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)
    created = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "value_limit": "1",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
        headers=_headers(token, key="revoke-expired-create"),
    ).json()

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE automation_policies SET expires_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": created["id"]},
        )

    response = client.post(
        f"/api/v1/automations/policies/{created['id']}/revoke",
        headers=_headers(token, key="revoke-expired-1"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "POLICY_EXPIRED"


def test_revoke_unknown_policy_is_404(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    response = client.post(
        f"/api/v1/automations/policies/{uuid4()}/revoke",
        headers=_headers(token, key="revoke-404"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "POLICY_NOT_FOUND"


def test_create_policy_requires_csrf(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.no-csrf.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)
    response = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "value_limit": "1",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
        headers={"Idempotency-Key": "no-csrf"},
    )
    assert response.status_code == 403


def test_create_policy_requires_authentication(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.no-auth.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)
    client.cookies.clear()
    response = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "value_limit": "1",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
        headers=_headers(token, key="no-auth"),
    )
    assert response.status_code == 401


def test_policy_is_hidden_across_workspaces(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.cross-workspace.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)
    created = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "value_limit": "1",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
        headers=_headers(token, key="cross-create"),
    ).json()

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
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
                "VALUES (:id, :workspace_id, :email, 'hash', :now)"
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
        list_response = other_client.get("/api/v1/automations/policies")
        assert list_response.status_code == 200
        assert all(p["id"] != created["id"] for p in list_response.json()["policies"])

        revoke_response = other_client.post(
            f"/api/v1/automations/policies/{created['id']}/revoke",
            headers=_headers(other_token, key="cross-revoke"),
        )
        assert revoke_response.status_code == 404
        assert revoke_response.json()["error"]["code"] == "POLICY_NOT_FOUND"
    finally:
        other_client.close()
        _cleanup_workspace(other_workspace_id)


# ---------------------------------------------------------------------------
# Idempotency-Key handling on POST /policies/{id}/revoke.
#
# Added by the test-coverage audit: this module had zero idempotency coverage
# of either kind, despite `policy.py` carrying its own hand-copied
# `_request_hash`/`_load_cached`/`_store_idempotency` trio (every Phase 5
# module has an independent copy, so a passing test in `runs.py`'s or
# `approvals.py`'s module proves nothing here). Revoke is the endpoint worth
# covering first: it is the one policy mutation with an immediate, run-
# affecting side effect -- `APPROVAL-POLICY.md`'s "Revocation takes effect
# immediately for any not-yet-started step", enforced by
# `worker._evaluate_dispatch_gate` and `worker._compensation_policy_usable`.
# ---------------------------------------------------------------------------


def test_revoke_policy_idempotency_key_replays_cached_response(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """A retried revoke under the same `Idempotency-Key` must replay the
    first call's 200 body, not surface the `409 POLICY_REVOKED` that a
    *genuine* second revoke attempt correctly produces
    (`test_revoke_policy_twice_is_policy_revoked` covers that other case,
    with two distinct keys).

    The distinction matters operationally, not just cosmetically: revoking a
    policy is what an operator does to stop a misbehaving automation, and the
    HTTP client retry that follows a dropped response is exactly when this
    path runs. A client that receives 409 for its own retry cannot tell
    "my revoke landed and this is my own retry" apart from "someone else
    already revoked this, so my intent may not be what took effect" -- and
    the safe-looking reaction to the ambiguity (re-check, re-issue) is how a
    real incident acquires extra noise at its worst moment.

    `version` is asserted unchanged across the replay because `revoke_policy`
    bumps it on every real write: a replay that reached the domain function
    instead of the cache would be visible there even if the response body
    happened to look similar.
    """
    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.http-revoke-idempotent.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)
    created = client.post(
        "/api/v1/automations/policies",
        json={
            "workflow_id": workflow_id,
            "value_limit": "1",
            "count_limit": 1,
            "approval_mode": "per_run",
        },
        headers=_headers(token, key="revoke-idempotent-create"),
    ).json()

    headers = _headers(token, key="revoke-retry-key")
    first = client.post(f"/api/v1/automations/policies/{created['id']}/revoke", headers=headers)
    second = client.post(f"/api/v1/automations/policies/{created['id']}/revoke", headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    ignored = {"request_id", "correlation_id"}
    first_body = {k: v for k, v in first.json().items() if k not in ignored}
    second_body = {k: v for k, v in second.json().items() if k not in ignored}
    assert first_body == second_body
    assert first_body["status"] == "revoked"

    # Exactly one real revoke write happened: `revoke_policy` bumps `version`
    # on every write, so a second write would be visible as version 3.
    listed = client.get("/api/v1/automations/policies", params={"workflow_id": workflow_id}).json()
    persisted = next(p for p in listed["policies"] if p["id"] == created["id"])
    assert persisted["version"] == first_body["version"]
    assert persisted["revoked_at"] == first_body["revoked_at"]


def test_revoke_policy_same_key_different_policy_is_idempotency_conflict(
    policy_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """`revoke_policy_endpoint`'s request hash is built from an *empty* body
    plus the action string `f"revoke:{policy_id}"`, so the `policy_id` path
    parameter is the only thing distinguishing two revoke requests. Reusing
    one key across two different policies must therefore 409
    `IDEMPOTENCY_CONFLICT`.

    This is the conflict case for an endpoint whose body cannot vary at all,
    and it is the one that guards a genuinely dangerous regression: if the
    `policy_id` were ever dropped from the hash material (a plausible
    "simplify this" edit, since every other endpoint's hash is dominated by
    its body), revoking policy B under a key already used for policy A would
    silently return A's cached response -- reporting policy B as revoked
    while B stays active and its workflow keeps dispatching. A test asserting
    only same-key/same-request replay would pass throughout that regression.
    """
    from ecc.observability import render_metrics

    client, _workspace_id, _user_id, token = policy_test_context
    workflow_id = f"test.http-revoke-two-policies.{uuid4().hex}"
    _create_workflow_via_api(client, token, workflow_id)

    def _create_policy(key: str) -> dict[str, object]:
        response = client.post(
            "/api/v1/automations/policies",
            json={
                "workflow_id": workflow_id,
                "value_limit": "1",
                "count_limit": 1,
                "approval_mode": "per_run",
            },
            headers=_headers(token, key=key),
        )
        assert response.status_code == 201, response.text
        return response.json()

    policy_a = _create_policy("revoke-conflict-create-a")
    policy_b = _create_policy("revoke-conflict-create-b")
    assert policy_a["id"] != policy_b["id"]

    headers = _headers(token, key="revoke-conflict-shared-key")
    revoked_a = client.post(
        f"/api/v1/automations/policies/{policy_a['id']}/revoke", headers=headers
    )
    assert revoked_a.status_code == 200, revoked_a.text
    assert revoked_a.json()["status"] == "revoked"

    conflicting = client.post(
        f"/api/v1/automations/policies/{policy_b['id']}/revoke", headers=headers
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert 'ecc_idempotency_conflicts_total{domain="automation_policy"}' in render_metrics()

    # The load-bearing half: policy B was never touched. A silent replay of
    # A's response would have reported B as revoked while leaving it usable.
    listed = client.get("/api/v1/automations/policies", params={"workflow_id": workflow_id}).json()
    by_id = {p["id"]: p for p in listed["policies"]}
    assert by_id[policy_a["id"]]["status"] == "revoked"
    assert by_id[policy_b["id"]]["status"] == "active"
    assert by_id[policy_b["id"]]["revoked_at"] is None
