"""Bulk-seeding helpers for Phase 3 Task 7's meeting-prep performance test.

Unlike ``phase3_attention_scenarios.py``/``phase3_planning_scenarios.py``,
meeting-prep composition has no pure scoring/scheduling algorithm to freeze
golden values against -- it is mostly fetch-and-format. The one thing worth
a shared fixture is generating a *large* history dataset cheaply and
consistently for the <2s p95 performance test (TEST-PLAN.md's Performance
section), so that's all this module provides.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import Engine


def seed_large_meeting_history(
    engine: Engine,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    meeting_id: UUID,
    participant_entity_id: UUID,
    now: datetime,
    timeline_count: int = 200,
    commitment_count: int = 50,
    note_count: int = 50,
) -> None:
    """Seed enough timeline/commitment/note history behind one participant
    entity for a realistic large-meeting-history performance measurement.
    Timeline/commitment counts exceed the module's own display caps
    (``_MAX_TIMELINE_ENTRIES``/``_MAX_COMMITMENTS``) so the query -- not
    just the response -- is exercised at scale.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO timeline_entries (
                    id, workspace_id, entity_id, effective_at, recorded_at,
                    event_type, source_id, summary
                ) VALUES (
                    :id, :workspace_id, :entity_id, :effective_at, :recorded_at,
                    'note_created', NULL, :summary
                )
                """
            ),
            [
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "entity_id": participant_entity_id,
                    "effective_at": now - timedelta(hours=i),
                    "recorded_at": now - timedelta(hours=i),
                    "summary": f"Timeline entry {i}",
                }
                for i in range(timeline_count)
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO commitments (
                    id, workspace_id, owner_id, summary, direction, status,
                    counterparty_person_id, due_at, importance, pinned,
                    created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :summary, 'made_to_me', 'active',
                    :counterparty_id, :due_at, 'medium', false,
                    :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            [
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "summary": f"Commitment {i}",
                    "counterparty_id": participant_entity_id,
                    "due_at": now + timedelta(days=i),
                    "now": now,
                }
                for i in range(commitment_count)
            ],
        )
        connection.execute(
            text(
                """
                INSERT INTO notes (
                    id, workspace_id, owner_id, title, body, note_type,
                    meeting_id, source_type, restricted, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :title, :body, 'general',
                    :meeting_id, 'local', false, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            [
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "owner_id": owner_id,
                    "meeting_id": meeting_id,
                    "title": f"Note {i}",
                    "body": f"Body of note {i}",
                    "now": now,
                }
                for i in range(note_count)
            ],
        )
