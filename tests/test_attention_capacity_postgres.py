from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.attention.planning_constraints import (
    PlanningConstraintCreate,
    archive_constraint,
    create_constraint,
    list_active_constraints,
)
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def capacity_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Capacity Test', 'Asia/Kolkata', :created_at)"
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
                "planning_constraints",
                "capacity_profiles",
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
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}


def _full_week(available: int = 480, focus: int = 240) -> list[dict]:
    return [
        {"weekday": weekday, "available_minutes": available, "focus_minutes": focus}
        for weekday in range(7)
    ]


def test_get_capacity_profile_defaults_to_empty(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, _ = capacity_test_context
    response = client.get("/api/v1/planning/capacity")
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "UTC"
    assert body["version"] == 0
    assert body["days"] == []


def test_put_capacity_profile_creates_and_updates_with_versioning(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = capacity_test_context

    created = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token),
        json={"expected_version": 0, "timezone": "Asia/Kolkata", "days": _full_week()},
    )
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["version"] == 1
    assert len(body["days"]) == 7
    assert {day["weekday"] for day in body["days"]} == set(range(7))

    fetched = client.get("/api/v1/planning/capacity")
    assert fetched.json()["version"] == 1

    updated = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token),
        json={
            "expected_version": 1,
            "timezone": "Asia/Kolkata",
            "days": _full_week(available=600, focus=300),
        },
    )
    assert updated.status_code == 200
    assert updated.json()["version"] == 2
    assert updated.json()["days"][0]["available_minutes"] == 600

    stale = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token),
        json={"expected_version": 1, "timezone": "Asia/Kolkata", "days": _full_week()},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_put_capacity_profile_rejects_incomplete_week(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = capacity_test_context
    response = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token),
        json={"expected_version": 0, "timezone": "UTC", "days": _full_week()[:6]},
    )
    assert response.status_code == 422


def test_put_capacity_profile_rejects_focus_exceeding_available(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = capacity_test_context
    days = _full_week(available=100, focus=200)
    response = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token),
        json={"expected_version": 0, "timezone": "UTC", "days": days},
    )
    assert response.status_code == 422


def test_put_capacity_profile_rejects_unknown_timezone(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = capacity_test_context
    response = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token),
        json={"expected_version": 0, "timezone": "Not/A_Zone", "days": _full_week()},
    )
    assert response.status_code == 422


def test_capacity_profile_hidden_across_workspaces(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """A different, real workspace's session must not see the fixture
    workspace's capacity profile -- not just a bare ``uuid4()`` 404 probe
    (capacity profiles are a singleton per workspace, fetched by
    ``workspace_id`` alone, with no by-id GET), which would prove nothing
    about workspace scoping.
    """
    client, _, _, token = capacity_test_context
    created = client.put(
        "/api/v1/planning/capacity",
        headers=_headers(token),
        json={"expected_version": 0, "timezone": "Asia/Kolkata", "days": _full_week()},
    )
    assert created.status_code == 200, created.text

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
        other_fetched = other_client.get("/api/v1/planning/capacity")
        assert other_fetched.status_code == 200
        other_body = other_fetched.json()
        assert other_body["version"] == 0
        assert other_body["days"] == []
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


def test_planning_constraint_kinds_validate_hardness_and_priority(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _, workspace_id, user_id, _ = capacity_test_context
    auth = AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="Asia/Kolkata")
    now = datetime.now(UTC)

    with SessionFactory() as session, session.begin():
        fixed = create_constraint(
            session,
            auth,
            PlanningConstraintCreate(
                kind="fixed_time",
                label="Board meeting",
                starts_at=now + timedelta(days=1),
                ends_at=now + timedelta(days=1, hours=1),
                hardness="hard",
                priority=90,
            ),
        )
        deadline = create_constraint(
            session,
            auth,
            PlanningConstraintCreate(
                kind="deadline",
                label="Board deck due",
                ends_at=now + timedelta(days=2),
                hardness="hard",
                priority=80,
            ),
        )
        preference = create_constraint(
            session,
            auth,
            PlanningConstraintCreate(
                kind="preference",
                label="No meetings before 10am",
                hardness="soft",
                priority=10,
            ),
        )

        active = list_active_constraints(session, auth)
        assert {c.id for c in active} == {fixed.id, deadline.id, preference.id}
        assert fixed.kind == "fixed_time"
        assert fixed.hardness == "hard"
        assert deadline.priority == 80
        assert preference.hardness == "soft"

        assert archive_constraint(session, auth, preference.id) is True
        remaining = list_active_constraints(session, auth)
        assert preference.id not in {c.id for c in remaining}
        assert archive_constraint(session, auth, preference.id) is False


# ---------------------------------------------------------------------------
# Finding #10: planning_constraints.py had no route wired to it -- there was
# no way for a user to actually create a hard constraint through the API.
# ---------------------------------------------------------------------------


def test_create_and_list_and_archive_constraint_via_http(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = capacity_test_context
    now = datetime.now(UTC)

    created = client.post(
        "/api/v1/planning/constraints",
        headers={**_headers(token), "Idempotency-Key": "create-constraint"},
        json={
            "kind": "fixed_time",
            "label": "Board meeting",
            "starts_at": (now + timedelta(days=1)).isoformat(),
            "ends_at": (now + timedelta(days=1, hours=1)).isoformat(),
            "hardness": "hard",
            "priority": 90,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "fixed_time"
    assert body["hardness"] == "hard"
    assert body["archived_at"] is None

    listed = client.get("/api/v1/planning/constraints")
    assert listed.status_code == 200
    assert [c["id"] for c in listed.json()["items"]] == [body["id"]]

    archived = client.post(
        f"/api/v1/planning/constraints/{body['id']}/archive", headers=_headers(token)
    )
    assert archived.status_code == 200
    assert archived.json()["archived_at"] is not None

    listed_after = client.get("/api/v1/planning/constraints")
    assert listed_after.json()["items"] == []


def test_create_constraint_idempotent_on_replay(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = capacity_test_context
    headers = {**_headers(token), "Idempotency-Key": "same-constraint-key"}
    payload = {
        "kind": "preference",
        "label": "No meetings before 10am",
        "hardness": "soft",
        "priority": 10,
    }
    first = client.post("/api/v1/planning/constraints", headers=headers, json=payload)
    assert first.status_code == 201
    second = client.post("/api/v1/planning/constraints", headers=headers, json=payload)
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]


def test_archive_constraint_rejects_unknown_id(
    capacity_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = capacity_test_context
    response = client.post(
        f"/api/v1/planning/constraints/{uuid4()}/archive", headers=_headers(token)
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PLANNING_CONSTRAINT_NOT_FOUND"


def test_fixed_time_constraint_requires_start_and_end() -> None:
    with pytest.raises(ValueError, match="require starts_at and ends_at"):
        PlanningConstraintCreate(kind="fixed_time", label="Missing range")


def test_deadline_constraint_requires_end() -> None:
    with pytest.raises(ValueError, match="require ends_at"):
        PlanningConstraintCreate(kind="deadline", label="Missing deadline")


def test_constraint_rejects_inverted_time_range() -> None:
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="starts_at must be before ends_at"):
        PlanningConstraintCreate(
            kind="fixed_time",
            label="Inverted",
            starts_at=now + timedelta(hours=2),
            ends_at=now,
        )
