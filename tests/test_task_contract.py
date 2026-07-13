from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from ecc.domains.planning.tasks import TaskCreate


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
