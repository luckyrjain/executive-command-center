"""Phase 5 Automation Task 1: triggers (design doc Decision 7).

`triggers.py` has no HTTP surface in this task (`API-SCHEMAS.md` names no
trigger endpoint yet) -- covers its data-layer functions directly, plus the
DB-level `CHECK` constraints that enforce "a `schedule` trigger always
names its own timezone explicitly, never inferred from server-local time"
(migration `0038_phase5_workflow_schema.py`), and cross-workspace
isolation of the underlying table.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import triggers as automation_triggers

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def trigger_test_context() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Trigger Test', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
    try:
        yield workspace_id, user_id
    finally:
        with engine.begin() as connection:
            for table in (
                "triggers",
                "automation_policies",
                "workflow_versions",
                "workflow_definitions",
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


def test_create_manual_trigger(trigger_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = trigger_test_context
    workflow_id = f"test.manual.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session:
        with session.begin():
            created = automation_triggers.create_trigger(
                session, workspace_id, user_id, workflow_id=workflow_id, trigger_type="manual"
            )
        assert created.trigger_type == "manual"
        assert created.schedule_expression is None
        assert created.skip_missed is False


def test_create_schedule_trigger_requires_expression_and_timezone(
    trigger_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = trigger_test_context
    workflow_id = f"test.schedule-missing.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with pytest.raises(IntegrityError):
        with SessionFactory() as session:
            with session.begin():
                automation_triggers.create_trigger(
                    session,
                    workspace_id,
                    user_id,
                    workflow_id=workflow_id,
                    trigger_type="schedule",
                )


def test_create_schedule_trigger_with_expression_and_timezone_succeeds(
    trigger_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = trigger_test_context
    workflow_id = f"test.schedule-ok.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session:
        with session.begin():
            created = automation_triggers.create_trigger(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                trigger_type="schedule",
                schedule_expression="0 9 * * MON-FRI",
                timezone="America/New_York",
                skip_missed=True,
            )
        assert created.schedule_expression == "0 9 * * MON-FRI"
        assert created.timezone == "America/New_York"
        assert created.skip_missed is True


def test_create_event_trigger_requires_event_type_filter(
    trigger_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = trigger_test_context
    workflow_id = f"test.event-missing.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with pytest.raises(IntegrityError):
        with SessionFactory() as session:
            with session.begin():
                automation_triggers.create_trigger(
                    session, workspace_id, user_id, workflow_id=workflow_id, trigger_type="event"
                )


def test_create_event_trigger_with_filter_succeeds(
    trigger_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = trigger_test_context
    workflow_id = f"test.event-ok.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session:
        with session.begin():
            created = automation_triggers.create_trigger(
                session,
                workspace_id,
                user_id,
                workflow_id=workflow_id,
                trigger_type="event",
                event_type_filter="waiting_link.aged",
            )
        assert created.event_type_filter == "waiting_link.aged"


def test_list_triggers_filters_by_workflow_id(trigger_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = trigger_test_context
    workflow_a = f"test.list-a.{uuid4().hex}"
    workflow_b = f"test.list-b.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_a)
    _insert_workflow_family(workspace_id, user_id, workflow_b)

    with SessionFactory() as session:
        with session.begin():
            automation_triggers.create_trigger(
                session, workspace_id, user_id, workflow_id=workflow_a, trigger_type="manual"
            )
            automation_triggers.create_trigger(
                session, workspace_id, user_id, workflow_id=workflow_b, trigger_type="manual"
            )

    with SessionFactory() as session:
        results = automation_triggers.list_triggers(session, workspace_id, workflow_id=workflow_a)
    assert [t.workflow_id for t in results] == [workflow_a]


def test_trigger_referencing_unknown_workflow_is_rejected_by_fk(
    trigger_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = trigger_test_context
    with pytest.raises(IntegrityError):
        with SessionFactory() as session:
            with session.begin():
                automation_triggers.create_trigger(
                    session,
                    workspace_id,
                    user_id,
                    workflow_id="nonexistent.workflow",
                    trigger_type="manual",
                )


def test_trigger_is_hidden_across_workspaces(trigger_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = trigger_test_context
    workflow_id = f"test.cross-workspace.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session:
        with session.begin():
            created = automation_triggers.create_trigger(
                session, workspace_id, user_id, workflow_id=workflow_id, trigger_type="manual"
            )

    other_workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
    try:
        with SessionFactory() as session:
            assert automation_triggers.get_trigger(session, other_workspace_id, created.id) is None
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )
