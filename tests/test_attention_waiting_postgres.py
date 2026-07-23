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
def waiting_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Waiting Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :created_at)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "created_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
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
                "attention_items",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "waiting_links",
                "tasks",
                "commitments",
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _seed_node(workspace_id: UUID, node_type: str, name: str) -> UUID:
    node_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    status, confidence, version, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :node_type, :name, '{}'::jsonb,
                    'active', 1.00, 1, :now, :now
                )
                """
            ),
            {
                "id": node_id,
                "workspace_id": workspace_id,
                "node_type": node_type,
                "name": name,
                "now": now,
            },
        )
    return node_id


def _seed_task(workspace_id: UUID, owner_id: UUID) -> UUID:
    task_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, status, manual_priority,
                    pinned, source_type, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'Needs a counterparty', 'planned', 'medium',
                    false, 'local', :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {"id": task_id, "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )
    return task_id


def test_waiting_link_lifecycle_and_direction_history(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = waiting_test_context
    task_id = _seed_task(workspace_id, user_id)
    counterparty = _seed_node(workspace_id, "person", "Counterparty Person")
    now = datetime.now(UTC)

    created = client.post(
        "/api/v1/waiting",
        headers=_headers(token, "create-waiting"),
        json={
            "subject_type": "task",
            "subject_id": str(task_id),
            "counterparty_entity_id": str(counterparty),
            "direction": "waiting_on_them",
            "expected_at": (now + timedelta(days=2)).isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    link = created.json()
    assert link["status"] == "open"
    assert link["superseded_by"] is None

    fetched = client.get(f"/api/v1/waiting/{link['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["direction"] == "waiting_on_them"

    # A direction change supersedes: the old row becomes 'superseded' and
    # points at a brand-new row, rather than mutating in place.
    changed = client.patch(
        f"/api/v1/waiting/{link['id']}",
        headers=_headers(token, "patch-direction"),
        json={"expected_version": 1, "direction": "blocked_by"},
    )
    assert changed.status_code == 200, changed.text
    new_link = changed.json()
    assert new_link["id"] != link["id"]
    assert new_link["direction"] == "blocked_by"
    assert new_link["version"] == 1

    old = client.get(f"/api/v1/waiting/{link['id']}")
    assert old.status_code == 200
    assert old.json()["status"] == "superseded"
    assert old.json()["superseded_by"] == new_link["id"]

    fulfil = client.post(
        f"/api/v1/waiting/{new_link['id']}/fulfil",
        headers=_headers(token),
        json={"expected_version": new_link["version"]},
    )
    assert fulfil.status_code == 200
    assert fulfil.json()["status"] == "fulfilled"

    # Fulfilling again with the same version is idempotent (already terminal).
    replay = client.post(
        f"/api/v1/waiting/{new_link['id']}/fulfil",
        headers=_headers(token),
        json={"expected_version": fulfil.json()["version"]},
    )
    assert replay.status_code == 200
    assert replay.json()["status"] == "fulfilled"


def test_direction_change_carries_over_original_since_at(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Finding #3: a direction change (via PATCH) supersedes the row with a
    brand-new one, but the underlying wait has existed since the original
    ``since_at``, not since the moment of the edit -- the new row must
    carry that original timestamp over, not reset it to `now`.
    """
    client, workspace_id, user_id, token = waiting_test_context
    task_id = _seed_task(workspace_id, user_id)
    counterparty = _seed_node(workspace_id, "person", "Counterparty Person")
    original_since_at = datetime.now(UTC) - timedelta(days=10)

    created = client.post(
        "/api/v1/waiting",
        headers=_headers(token, "create-waiting-since"),
        json={
            "subject_type": "task",
            "subject_id": str(task_id),
            "counterparty_entity_id": str(counterparty),
            "direction": "waiting_on_them",
            "since_at": original_since_at.isoformat(),
        },
    )
    assert created.status_code == 201, created.text
    link = created.json()
    assert datetime.fromisoformat(link["since_at"]) == original_since_at

    changed = client.patch(
        f"/api/v1/waiting/{link['id']}",
        headers=_headers(token, "patch-direction-since"),
        json={"expected_version": 1, "direction": "blocked_by"},
    )
    assert changed.status_code == 200, changed.text
    new_link = changed.json()
    assert new_link["id"] != link["id"]
    assert datetime.fromisoformat(new_link["since_at"]) == original_since_at


def test_waiting_link_cancel_and_stale_version_conflict(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = waiting_test_context
    task_id = _seed_task(workspace_id, user_id)
    counterparty = _seed_node(workspace_id, "organization", "Vendor Org")

    created = client.post(
        "/api/v1/waiting",
        headers=_headers(token, "create-waiting-cancel"),
        json={
            "subject_type": "task",
            "subject_id": str(task_id),
            "counterparty_entity_id": str(counterparty),
            "direction": "waiting_on_me",
        },
    )
    assert created.status_code == 201
    link = created.json()

    stale = client.post(
        f"/api/v1/waiting/{link['id']}/cancel",
        headers=_headers(token),
        json={"expected_version": 99},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    cancelled = client.post(
        f"/api/v1/waiting/{link['id']}/cancel",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"


def test_create_waiting_link_idempotent_on_replay(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = waiting_test_context
    task_id = _seed_task(workspace_id, user_id)
    counterparty = _seed_node(workspace_id, "organization", "Vendor Org")
    headers = _headers(token, "idempotent-create")
    payload = {
        "subject_type": "task",
        "subject_id": str(task_id),
        "counterparty_entity_id": str(counterparty),
        "direction": "waiting_on_me",
    }

    first = client.post("/api/v1/waiting", headers=headers, json=payload)
    assert first.status_code == 201
    replay = client.post("/api/v1/waiting", headers=headers, json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]


def test_create_waiting_link_conflicting_replay_returns_409_and_records_metric(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Reusing an Idempotency-Key with a materially different payload must
    409 IDEMPOTENCY_CONFLICT, and must record the same
    ``record_idempotency_conflict`` observability signal every other
    idempotency-replay path in the codebase emits on this same conflict.
    """
    from ecc.observability import render_metrics

    client, workspace_id, user_id, token = waiting_test_context
    task_id = _seed_task(workspace_id, user_id)
    counterparty = _seed_node(workspace_id, "organization", "Vendor Org")
    headers = _headers(token, "conflicting-create")

    first = client.post(
        "/api/v1/waiting",
        headers=headers,
        json={
            "subject_type": "task",
            "subject_id": str(task_id),
            "counterparty_entity_id": str(counterparty),
            "direction": "waiting_on_me",
        },
    )
    assert first.status_code == 201, first.text

    conflicting = client.post(
        "/api/v1/waiting",
        headers=headers,
        json={
            "subject_type": "task",
            "subject_id": str(task_id),
            "counterparty_entity_id": str(counterparty),
            "direction": "waiting_on_them",
        },
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert 'ecc_idempotency_conflicts_total{domain="waiting"}' in render_metrics()


def test_waiting_link_rejects_non_person_org_counterparty(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = waiting_test_context
    task_id = _seed_task(workspace_id, user_id)
    project_node = _seed_node(workspace_id, "project", "Not a person")

    response = client.post(
        "/api/v1/waiting",
        headers=_headers(token, "create-invalid-counterparty"),
        json={
            "subject_type": "task",
            "subject_id": str(task_id),
            "counterparty_entity_id": str(project_node),
            "direction": "waiting_on_them",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_WAITING_DIRECTION"


def test_waiting_link_rejects_circular_blocked_by_chain(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """TEST-PLAN.md's determinism/property-test section names circular
    dependencies explicitly: A blocked_by B, then B blocked_by A must be
    rejected at creation, not accepted and left to loop forever in any
    downstream traversal.
    """
    client, workspace_id, _, token = waiting_test_context
    entity_a = _seed_node(workspace_id, "person", "Entity A")
    entity_b = _seed_node(workspace_id, "person", "Entity B")

    first = client.post(
        "/api/v1/waiting",
        headers=_headers(token, "cycle-a-blocked-by-b"),
        json={
            "subject_type": "knowledge_entity",
            "subject_id": str(entity_a),
            "counterparty_entity_id": str(entity_b),
            "direction": "blocked_by",
        },
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/api/v1/waiting",
        headers=_headers(token, "cycle-b-blocked-by-a"),
        json={
            "subject_type": "knowledge_entity",
            "subject_id": str(entity_b),
            "counterparty_entity_id": str(entity_a),
            "direction": "blocked_by",
        },
    )
    assert second.status_code == 422, second.text
    assert second.json()["error"]["code"] == "INVALID_WAITING_DIRECTION"


def test_waiting_link_list_signed_cursor_pagination_and_tamper_rejection(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = waiting_test_context
    counterparty = _seed_node(workspace_id, "person", "Paginated Counterparty")
    for index in range(3):
        task_id = _seed_task(workspace_id, user_id)
        created = client.post(
            "/api/v1/waiting",
            headers=_headers(token, f"paginate-{index}"),
            json={
                "subject_type": "task",
                "subject_id": str(task_id),
                "counterparty_entity_id": str(counterparty),
                "direction": "waiting_on_them",
            },
        )
        assert created.status_code == 201

    first_page = client.get("/api/v1/waiting", params={"limit": 2})
    assert first_page.status_code == 200
    body = first_page.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    second_page = client.get("/api/v1/waiting", params={"limit": 2, "cursor": body["next_cursor"]})
    assert second_page.status_code == 200
    assert len(second_page.json()["items"]) == 1
    seen_ids = {item["id"] for item in body["items"]} | {
        item["id"] for item in second_page.json()["items"]
    }
    assert len(seen_ids) == 3

    tampered = client.get("/api/v1/waiting", params={"cursor": body["next_cursor"][:-1] + "x"})
    assert tampered.status_code == 400
    assert tampered.json()["error"]["code"] == "CURSOR_INVALID"


def test_waiting_link_is_hidden_across_workspaces(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = waiting_test_context
    task_id = _seed_task(workspace_id, user_id)
    counterparty = _seed_node(workspace_id, "person", "Cross-Workspace Counterparty")

    created = client.post(
        "/api/v1/waiting",
        headers=_headers(token, "create-waiting-cross-workspace"),
        json={
            "subject_type": "task",
            "subject_id": str(task_id),
            "counterparty_entity_id": str(counterparty),
            "direction": "waiting_on_them",
        },
    )
    assert created.status_code == 201, created.text
    link_id = created.json()["id"]

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
        response = other_client.get(f"/api/v1/waiting/{link_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "WAITING_LINK_NOT_FOUND"
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


def test_waiting_link_surfaces_in_attention_queue_and_ages(
    waiting_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = waiting_test_context
    task_id = _seed_task(workspace_id, user_id)
    counterparty = _seed_node(workspace_id, "person", "Ageing Counterparty")
    now = datetime.now(UTC)

    created = client.post(
        "/api/v1/waiting",
        headers=_headers(token, "create-waiting-attention"),
        json={
            "subject_type": "task",
            "subject_id": str(task_id),
            "counterparty_entity_id": str(counterparty),
            "direction": "waiting_on_me",
            "since_at": (now - timedelta(days=15)).isoformat(),
        },
    )
    assert created.status_code == 201
    link_id = created.json()["id"]

    regenerate = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate.status_code == 200
    item = next(i for i in regenerate.json()["items"] if i["entity_id"] == link_id)
    assert item["entity_type"] == "waiting_link"
    factor_codes = {f["code"] for f in item["factors"]}
    assert "waiting_direction" in factor_codes
    assert "stale_14d" in factor_codes

    fulfil = client.post(
        f"/api/v1/waiting/{link_id}/fulfil",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    assert fulfil.status_code == 200

    regenerate_again = client.post("/api/v1/attention/regenerate", headers=_headers(token), json={})
    assert regenerate_again.status_code == 200
    assert all(i["entity_id"] != link_id for i in regenerate_again.json()["items"])
