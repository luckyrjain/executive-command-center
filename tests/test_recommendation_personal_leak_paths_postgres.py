"""Two leak paths for Gmail-derived recommendations (Security Remediation
Spec A S1.8, plan notes N19(2) and N23).

1. `POST /api/v1/recommendations` must not create an
   `email_action_detected` row: that type is reserved for the Gmail
   action-detection hook (the personal-data predicate matches it, so a
   member-created one would be share-refused and non-removal-blocking
   without being private or Gmail-derived). Rejected regardless of
   `ECC_PERSONAL_DATA_ISOLATION`; every other type is unaffected.
2. Confirming a personal-data recommendation creates its target row
   (task/commitment/risk) owned by the recommendation's owner and
   `private` when the flag is on -- the email-derived title/summary never
   lands in a workspace-visible row. Non-personal recommendations and the
   flag-off path create exactly today's workspace-visible row.

Every Gmail-derived recommendation comes from the real sync
(`gmail_sync_fixtures`); the worlds add a `bystander` workspace `owner`.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import GmailSyncWorld, build_gmail_sync_world, csrf_headers
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.governance.recommendation_models import RecommendationCreate
from ecc.domains.governance.recommendation_mutations import (
    create_recommendation,
    synthetic_request,
)

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG_ON = {"ECC_PERSONAL_DATA_ISOLATION": "true"}
_FLAG_OFF = {"ECC_PERSONAL_DATA_ISOLATION": "false"}
_EXTRA_CLEANUP_TABLES = ("recommendation_feedback",)
_TABLE_BY_TARGET = {"task": "tasks", "commitment": "commitments", "risk": "risks"}
_ROUTE_BY_TARGET = {"task": "tasks", "commitment": "commitments", "risk": "risks"}
_NOT_FOUND_BY_TARGET = {
    "task": "TASK_NOT_FOUND",
    "commitment": "COMMITMENT_NOT_FOUND",
    "risk": "RISK_NOT_FOUND",
}
_FIELDS_BY_TARGET: dict[str, dict[str, Any]] = {
    "task": {"title": "Reply to the confidential request"},
    "commitment": {"summary": "Send the confidential figures", "direction": "made_by_me"},
    "risk": {"description": "Confidential deal may slip", "probability": 3, "impact": 4},
}


@pytest.fixture
def world_on() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(
        env=_FLAG_ON, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES, bystander=True
    ) as world:
        yield world


@pytest.fixture
def world_off() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(
        env=_FLAG_OFF, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES, bystander=True
    ) as world:
        yield world


# --- helpers ----------------------------------------------------------------------


def _bystander(world: GmailSyncWorld) -> UUID:
    assert world.bystander_user_id is not None
    return world.bystander_user_id


def _client(world: GmailSyncWorld, user_id: UUID) -> tuple[TestClient, str]:
    return world.harness.client_for(world.workspace_id, user_id)


def _row(table: str, row_id: UUID) -> tuple[UUID, str]:
    with engine.begin() as connection:
        row = connection.execute(
            text(f"SELECT owner_id, visibility FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        ).one()
    return row[0], row[1]


def _rec_version(recommendation_id: UUID) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text("SELECT version FROM recommendations WHERE id = :id"),
                {"id": recommendation_id},
            ).scalar_one()
        )


def _publish_and_confirm(
    world: GmailSyncWorld,
    user_id: UUID,
    rec_id: UUID,
    *,
    target_expected_version: int | None = None,
) -> dict[str, Any]:
    client, token = _client(world, user_id)
    published = client.post(
        f"/api/v1/recommendations/{rec_id}/publish",
        json={"expected_version": _rec_version(rec_id)},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert published.status_code == 200, published.text
    confirmed = client.post(
        f"/api/v1/recommendations/{rec_id}/confirm",
        json={
            "expected_version": _rec_version(rec_id),
            "target_expected_version": target_expected_version,
        },
        headers=csrf_headers(token, str(uuid4())),
    )
    assert confirmed.status_code == 200, confirmed.text
    body: dict[str, Any] = confirmed.json()
    assert body["status"] == "executed"
    return body


def _create_payload(recommendation_type: str, target_type: str = "task") -> dict[str, Any]:
    return {
        "recommendation_type": recommendation_type,
        "target_type": target_type,
        "target_id": None,
        "proposed_action": {"operation": "create", "value": None},
        "proposed_fields": _FIELDS_BY_TARGET[target_type],
        "rationale": "Detected action item.",
        "confidence": 0.8,
        "evidence_ids": [],
        "source": "ai",
    }


def _post_recommendation(world: GmailSyncWorld, user_id: UUID, recommendation_type: str) -> Any:
    client, token = _client(world, user_id)
    return client.post(
        "/api/v1/recommendations",
        headers=csrf_headers(token, str(uuid4())),
        json=_create_payload(recommendation_type),
    )


def _email_rec_count(world: GmailSyncWorld) -> int:
    with engine.begin() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM recommendations WHERE workspace_id = :ws "
                    "AND recommendation_type = 'email_action_detected'"
                ),
                {"ws": world.workspace_id},
            ).scalar_one()
        )


def _assert_hidden(world: GmailSyncWorld, viewer: UUID, target_type: str, row_id: UUID) -> None:
    client, _token = _client(world, viewer)
    route = _ROUTE_BY_TARGET[target_type]
    detail = client.get(f"/api/v1/{route}/{row_id}")
    assert detail.status_code == 404, (viewer, detail.text)
    assert detail.json()["error"]["code"] == _NOT_FOUND_BY_TARGET[target_type]
    listed = client.get(f"/api/v1/{route}", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    assert str(row_id) not in {item["id"] for item in listed.json()["items"]}


def _assert_visible(world: GmailSyncWorld, viewer: UUID, target_type: str, row_id: UUID) -> None:
    client, _token = _client(world, viewer)
    route = _ROUTE_BY_TARGET[target_type]
    assert client.get(f"/api/v1/{route}/{row_id}").status_code == 200
    listed = client.get(f"/api/v1/{route}", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    assert str(row_id) in {item["id"] for item in listed.json()["items"]}


# --- Issue 1 (N19(2)): the public create route ------------------------------------


@pytest.mark.parametrize("world_name", ["world_on", "world_off"])
def test_public_create_rejects_reserved_email_type(
    world_name: str, request: pytest.FixtureRequest
) -> None:
    world: GmailSyncWorld = request.getfixturevalue(world_name)
    before = _email_rec_count(world)
    for actor in (world.b.user_id, world.a.user_id, _bystander(world)):
        response = _post_recommendation(world, actor, "email_action_detected")
        assert response.status_code == 422, response.text
        error = response.json()["error"]
        assert error["code"] == "VALIDATION_ERROR"
        assert error["details"]["violations"][0]["loc"] == ["body", "recommendation_type"]
    assert _email_rec_count(world) == before


def test_public_create_other_types_unaffected(world_on: GmailSyncWorld) -> None:
    response = _post_recommendation(world_on, world_on.b.user_id, "task_detected")
    assert response.status_code == 201, response.text
    rec_id = UUID(response.json()["id"])
    assert _row("recommendations", rec_id) == (world_on.b.user_id, "workspace")


# --- Issue 2 (N23): confirming a personal recommendation ---------------------------


def test_flag_on_confirm_email_recommendation_creates_private_task(
    world_on: GmailSyncWorld,
) -> None:
    a = world_on.a
    rec_id = a.recommendation_ids[0]
    assert _row("recommendations", rec_id) == (a.user_id, "private")

    executed = _publish_and_confirm(world_on, a.user_id, rec_id)
    result = executed["execution_result"]
    assert result["target_type"] == "task"
    task_id = UUID(result["target_id"])

    assert _row("tasks", task_id) == (a.user_id, "private")
    for viewer in (world_on.b.user_id, _bystander(world_on)):
        _assert_hidden(world_on, viewer, "task", task_id)
    _assert_visible(world_on, a.user_id, "task", task_id)
    # The confirmed recommendation itself stays private to A.
    assert _row("recommendations", rec_id) == (a.user_id, "private")


@pytest.mark.parametrize("target_type", ["task", "commitment", "risk"])
def test_flag_on_confirm_private_email_recommendation_every_target_type(
    world_on: GmailSyncWorld, target_type: str
) -> None:
    """Every `operation="create"` target the detector may propose
    (`validator.py`: task/commitment/risk) inherits owner + `private`."""
    a = world_on.a
    with SessionFactory() as session:
        created = create_recommendation(
            session,
            AuthContext(workspace_id=world_on.workspace_id, user_id=a.user_id, timezone="UTC"),
            RecommendationCreate(**_create_payload("email_action_detected", target_type)),
            synthetic_request(uuid4(), uuid4()),
            f"leak-path-test:{uuid4()}",
            visibility="private",
        )
    executed = _publish_and_confirm(world_on, a.user_id, created.id)
    row_id = UUID(executed["execution_result"]["target_id"])
    assert _row(_TABLE_BY_TARGET[target_type], row_id) == (a.user_id, "private")
    for viewer in (world_on.b.user_id, _bystander(world_on)):
        _assert_hidden(world_on, viewer, target_type, row_id)
    _assert_visible(world_on, a.user_id, target_type, row_id)


def test_flag_on_non_owner_confirms_workspace_era_email_recommendation(
    world_on: GmailSyncWorld,
) -> None:
    """A flag-off-era (still `workspace`-visible, not yet backfilled) email
    recommendation of A's, confirmed by B: the created task is still the
    mailbox owner's and `private` -- B confirms but cannot read it."""
    a, b = world_on.a, world_on.b
    rec_id = a.recommendation_ids[0]
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE recommendations SET visibility = 'workspace' WHERE id = :id"),
            {"id": rec_id},
        )
    executed = _publish_and_confirm(world_on, b.user_id, rec_id)
    task_id = UUID(executed["execution_result"]["target_id"])
    assert _row("tasks", task_id) == (a.user_id, "private")
    _assert_hidden(world_on, b.user_id, "task", task_id)
    _assert_visible(world_on, a.user_id, "task", task_id)


def test_flag_on_non_create_email_confirm_leaves_target_scope(world_on: GmailSyncWorld) -> None:
    """No production path writes a non-create email recommendation (the
    Gmail hook only proposes `create`; the public route refuses the type),
    so it is built through `create_recommendation` itself. Confirming it
    changes the existing target in place: owner/visibility untouched."""
    a = world_on.a
    client, token = _client(world_on, a.user_id)
    task = client.post(
        "/api/v1/tasks",
        json={"title": "Existing task"},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert task.status_code == 201, task.text
    task_id = UUID(task.json()["id"])
    assert _row("tasks", task_id) == (a.user_id, "workspace")
    with SessionFactory() as session:
        created = create_recommendation(
            session,
            AuthContext(workspace_id=world_on.workspace_id, user_id=a.user_id, timezone="UTC"),
            RecommendationCreate(
                recommendation_type="email_action_detected",
                target_type="task",
                target_id=task_id,
                proposed_action={"operation": "set_status", "value": "in_progress"},
                expected_version=1,
                rationale="Detected status change.",
                confidence=0.8,
                source="ai",
            ),
            synthetic_request(uuid4(), uuid4()),
            f"leak-path-test:{uuid4()}",
            visibility="private",
        )
    executed = _publish_and_confirm(world_on, a.user_id, created.id, target_expected_version=1)
    assert executed["execution_result"]["target_id"] == str(task_id)
    assert _row("tasks", task_id) == (a.user_id, "workspace")
    _assert_visible(world_on, world_on.b.user_id, "task", task_id)


def test_flag_on_non_email_confirm_unchanged(world_on: GmailSyncWorld) -> None:
    a = world_on.a
    created = _post_recommendation(world_on, a.user_id, "task_detected")
    assert created.status_code == 201, created.text
    executed = _publish_and_confirm(world_on, a.user_id, UUID(created.json()["id"]))
    task_id = UUID(executed["execution_result"]["target_id"])
    assert _row("tasks", task_id) == (a.user_id, "workspace")
    for viewer in (world_on.b.user_id, _bystander(world_on)):
        _assert_visible(world_on, viewer, "task", task_id)


def test_flag_off_confirm_email_recommendation_unchanged(world_off: GmailSyncWorld) -> None:
    a = world_off.a
    rec_id = a.recommendation_ids[0]
    assert _row("recommendations", rec_id) == (a.user_id, "workspace")
    executed = _publish_and_confirm(world_off, a.user_id, rec_id)
    task_id = UUID(executed["execution_result"]["target_id"])
    assert _row("tasks", task_id) == (a.user_id, "workspace")
    for viewer in (world_off.b.user_id, _bystander(world_off)):
        _assert_visible(world_off, viewer, "task", task_id)
