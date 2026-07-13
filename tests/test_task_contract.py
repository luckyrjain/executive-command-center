from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from ecc.domains.planning.tasks import (
    TaskAction,
    TaskCreate,
    TaskPatch,
    _decode_cursor,
    _encode_cursor,
    router,
)


def test_task_create_defaults_to_captured_medium() -> None:
    task = TaskCreate(title="Prepare weekly operating review")

    assert task.status == "captured"
    assert task.manual_priority == "medium"
    assert task.due_date is None
    assert task.due_at is None


def test_task_create_rejects_both_due_precisions() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(
            title="Prepare weekly operating review",
            due_date=date(2026, 7, 14),
            due_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        )


def test_task_create_rejects_terminal_initial_status() -> None:
    with pytest.raises(ValidationError):
        TaskCreate(title="Already done", status="completed")  # type: ignore[arg-type]


def test_task_create_rejects_client_owned_fields() -> None:
    with pytest.raises(ValidationError):
        TaskCreate.model_validate(
            {
                "title": "Attempt ownership override",
                "owner_id": "00000000-0000-0000-0000-000000000001",
            }
        )


def test_task_patch_requires_expected_version() -> None:
    with pytest.raises(ValidationError):
        TaskPatch.model_validate({"title": "Updated title"})


def test_task_patch_rejects_both_due_precisions() -> None:
    with pytest.raises(ValidationError):
        TaskPatch(
            expected_version=1,
            due_date=date(2026, 7, 14),
            due_at=datetime(2026, 7, 14, 9, 0, tzinfo=UTC),
        )


def test_task_action_requires_positive_version() -> None:
    with pytest.raises(ValidationError):
        TaskAction(expected_version=0)


def test_task_router_exposes_frozen_phase_one_routes() -> None:
    routes = {(route.path, method) for route in router.routes for method in route.methods or set()}

    expected = {
        ("/api/v1/tasks", "POST"),
        ("/api/v1/tasks", "GET"),
        ("/api/v1/tasks/{task_id}", "GET"),
        ("/api/v1/tasks/{task_id}", "PATCH"),
        ("/api/v1/tasks/{task_id}/complete", "POST"),
        ("/api/v1/tasks/{task_id}/cancel", "POST"),
        ("/api/v1/tasks/{task_id}/archive", "POST"),
        ("/api/v1/tasks/{task_id}/restore", "POST"),
    }
    assert expected <= routes


def test_task_cursor_is_signed_and_round_trips() -> None:
    created_at = datetime(2026, 7, 14, 9, 30, tzinfo=UTC)
    task_id = uuid4()

    cursor = _encode_cursor(created_at, task_id)

    assert _decode_cursor(cursor) == (created_at, task_id)
