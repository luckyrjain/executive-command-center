"""Deterministic, batched PostgreSQL fixture generation for Phase 1 performance tests.

This module has exactly one responsibility: generate the documented
representative-scale dataset (see
``docs/superpowers/specs/2026-07-16-phase-1-completion-design.md`` and
``docs/phases/phase-001/TEST-PLAN.md``) inside an already-provisioned
workspace, using batched ``INSERT ... SELECT FROM generate_series`` calls --
never per-row round trips.

It is distinct in purpose from ``scripts/seed_phase1_acceptance.py``:

* ``scripts/seed_phase1_acceptance.py`` seeds a small, deterministic,
  idempotent set of rows across two workspaces for backup/restore acceptance
  evidence (Task 9).
* This module seeds the full documented *performance-scale* dataset (10,000
  tasks, commitments, risks, and calendar events; 50,000 notes; and 100,000
  audit rows) into a single caller-provided workspace, for load-shape
  performance testing. It carries no idempotency guarantees -- callers are
  expected to seed into a fresh, disposable per-test workspace and delete it
  in teardown, the same way the existing ``tests/test_*_postgres.py`` fixtures
  already do.

Content is deterministic in *shape* (titles, status/priority distributions,
body lengths) so repeated runs are comparable, but row identifiers are
generated with ``gen_random_uuid()`` since uniqueness -- not identity -- is
all a disposable per-test-run dataset needs.

All seeded text is synthetic placeholder content ("Representative synthetic
..."), never anything that could be mistaken for real note bodies, tokens,
or other sensitive content, per the Phase 1 privacy constraint that logs,
metrics, and test artifacts must never carry real payload content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Connection, text

TASK_COUNT = 10_000
COMMITMENT_COUNT = 10_000
RISK_COUNT = 10_000
CALENDAR_EVENT_COUNT = 10_000
NOTE_COUNT = 50_000
AUDIT_EVENT_COUNT = 100_000


@dataclass(frozen=True)
class Phase1DatasetCounts:
    """Documented row counts produced by :func:`seed_phase1_dataset`."""

    tasks: int = TASK_COUNT
    commitments: int = COMMITMENT_COUNT
    risks: int = RISK_COUNT
    calendar_events: int = CALENDAR_EVENT_COUNT
    notes: int = NOTE_COUNT
    audit_events: int = AUDIT_EVENT_COUNT


def seed_phase1_dataset(
    connection: Connection,
    *,
    workspace_id: UUID,
    owner_id: UUID,
    actor_id: UUID | None = None,
) -> Phase1DatasetCounts:
    """Batch-insert the documented representative-scale Phase 1 dataset.

    ``workspace_id`` and ``owner_id`` (a valid, already-inserted user in that
    workspace) must exist before this is called -- this function only
    populates entity tables, never workspace/user/session scaffolding, so it
    composes with each test file's own auth fixture.

    Returns the counts actually requested (always the documented constants);
    callers that want to *prove* the counts landed should query the tables
    themselves, which is exactly what the dataset-shape tests do.
    """
    actor = actor_id if actor_id is not None else owner_id
    now = datetime.now(UTC)

    connection.execute(
        text(
            """
            INSERT INTO tasks (
                id, workspace_id, owner_id, title, description, status,
                manual_priority, due_date, pinned, source_type,
                created_by, updated_by, created_at, updated_at, version
            )
            SELECT
                gen_random_uuid(), :workspace_id, :owner_id,
                'Representative synthetic task ' || series::text,
                'Representative synthetic task body ' || series::text,
                'planned',
                (ARRAY['low', 'medium', 'high', 'critical'])[1 + (series % 4)],
                NULL,
                (series % 11) = 0,
                'local',
                :actor_id, :actor_id, :created_at, :updated_at, 1
            FROM generate_series(1, :count) AS series
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "actor_id": actor,
            "created_at": now,
            "updated_at": now,
            "count": TASK_COUNT,
        },
    )

    connection.execute(
        text(
            """
            INSERT INTO commitments (
                id, workspace_id, owner_id, summary, description, direction,
                status, due_at, importance, confidence, pinned,
                created_by, updated_by, created_at, updated_at, version
            )
            SELECT
                gen_random_uuid(), :workspace_id, :owner_id,
                'Representative synthetic commitment ' || series::text,
                'Representative synthetic commitment body ' || series::text,
                (ARRAY['made_by_me', 'made_to_me'])[1 + (series % 2)],
                'active',
                :now + make_interval(hours => (series % 240)),
                (ARRAY['low', 'medium', 'high', 'critical'])[1 + (series % 4)],
                0.500,
                (series % 13) = 0,
                :actor_id, :actor_id, :created_at, :updated_at, 1
            FROM generate_series(1, :count) AS series
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "actor_id": actor,
            "now": now,
            "created_at": now,
            "updated_at": now,
            "count": COMMITMENT_COUNT,
        },
    )

    connection.execute(
        text(
            """
            INSERT INTO risks (
                id, workspace_id, description, probability, impact, status,
                owner_id, review_at, pinned,
                created_by, updated_by, created_at, updated_at, version
            )
            SELECT
                gen_random_uuid(), :workspace_id,
                'Representative synthetic risk ' || series::text,
                1 + (series % 5),
                1 + ((series / 5) % 5),
                'identified',
                :owner_id,
                :now + make_interval(hours => (series % 240)),
                (series % 17) = 0,
                :actor_id, :actor_id, :created_at, :updated_at, 1
            FROM generate_series(1, :count) AS series
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "actor_id": actor,
            "now": now,
            "created_at": now,
            "updated_at": now,
            "count": RISK_COUNT,
        },
    )

    connection.execute(
        text(
            """
            INSERT INTO calendar_events (
                id, workspace_id, external_source, title, starts_at, ends_at,
                all_day, timezone, status, source_authoritative,
                created_by, updated_by, created_at, updated_at, version
            )
            SELECT
                gen_random_uuid(), :workspace_id, 'local',
                'Representative synthetic event ' || series::text,
                :now + make_interval(hours => (series % 720)),
                :now + make_interval(hours => (series % 720) + 1),
                false,
                'UTC',
                'confirmed',
                true,
                :actor_id, :actor_id, :created_at, :updated_at, 1
            FROM generate_series(1, :count) AS series
            """
        ),
        {
            "workspace_id": workspace_id,
            "actor_id": actor,
            "now": now,
            "created_at": now,
            "updated_at": now,
            "count": CALENDAR_EVENT_COUNT,
        },
    )

    connection.execute(
        text(
            """
            INSERT INTO notes (
                id, workspace_id, owner_id, title, body, note_type,
                source_type, created_by, updated_by,
                created_at, updated_at, version
            )
            SELECT
                gen_random_uuid(), :workspace_id, :owner_id,
                'Representative synthetic note ' || series::text,
                'Representative synthetic note body content for load testing '
                    || series::text,
                (ARRAY['general', 'meeting', 'decision', 'journal'])[1 + (series % 4)],
                'local',
                :actor_id, :actor_id, :created_at, :updated_at, 1
            FROM generate_series(1, :count) AS series
            """
        ),
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "actor_id": actor,
            "created_at": now,
            "updated_at": now,
            "count": NOTE_COUNT,
        },
    )

    connection.execute(
        text(
            """
            INSERT INTO audit_events (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, actor_id, request_id, correlation_id,
                changed_fields, authorization_result, source, occurred_at
            )
            SELECT
                gen_random_uuid(), :workspace_id,
                'task.updated',
                'task',
                gen_random_uuid(),
                1 + (series % 5),
                :actor_id,
                gen_random_uuid(),
                gen_random_uuid(),
                ARRAY['title'],
                'allowed',
                'user',
                :now - make_interval(secs => series)
            FROM generate_series(1, :count) AS series
            """
        ),
        {
            "workspace_id": workspace_id,
            "actor_id": actor,
            "now": now,
            "count": AUDIT_EVENT_COUNT,
        },
    )

    # Refresh planner statistics for the tables this call just bulk-loaded.
    # Without this, PostgreSQL's query planner has only the stale
    # pre-insert statistics for these tables (autovacuum's periodic
    # ANALYZE has not yet run against rows that landed a moment ago in the
    # same transaction), which can make it badly misestimate join
    # cardinality on the freshly inserted rows and pick pathological plans
    # that a normally-populated production database -- where data
    # accumulates gradually under autovacuum -- would never hit. Explicit
    # ANALYZE here makes this disposable performance fixture behave like a
    # realistically-aged production dataset for query planning purposes,
    # which is the whole point of a *representative*-scale fixture. (This
    # is safe inside the caller's transaction: plain ANALYZE, unlike VACUUM
    # or VACUUM ANALYZE, is allowed inside a transaction block.)
    connection.execute(
        text("ANALYZE tasks, commitments, risks, calendar_events, notes, audit_events")
    )

    return Phase1DatasetCounts()
