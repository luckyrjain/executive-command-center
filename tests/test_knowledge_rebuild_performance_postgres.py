from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.knowledge.timeline import rebuild_timeline

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

# TEST-PLAN.md's Performance section names projection rebuild as something
# that must be "measured at acceptance dataset size" with p50/p95/p99
# recorded for regressions, but -- like candidate generation -- the phase
# spec never quantifies a numeric budget for it (unlike entity lookup,
# lexical/hybrid retrieval and the 10,000-entry timeline, which all have an
# explicit p95 target). This records the real percentiles as the contract
# asks, gated only by a generous sanity ceiling rather than an invented
# official number.
#
# rebuild_timeline does one INSERT per audit_events row (see its own
# docstring: append-only history replay, not a bulk statement), so its cost
# is genuinely linear in event count. A smaller-than-10,000 event count is
# used here (unlike the timeline-read test's 10,000 rows) purely to keep a
# 5-sample-per-call test's total runtime reasonable -- each sample re-runs
# the full O(N) rebuild, not one lightweight read.
SANITY_CEILING_SECONDS = 5.0
_EVENT_COUNT = 2_000
SAMPLE_SIZE = 5


def _p50(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value at or above 95% of samples."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    return ordered[index]


def _p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(99 * len(ordered)) // 100) - 1)
    return ordered[index]


@pytest.fixture
def rebuild_performance_context() -> Iterator[UUID]:
    workspace_id = uuid4()
    user_id = uuid4()
    entity_id = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Rebuild Performance", "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, :password_hash, :created_at)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "password_hash": "test-password-hash",
                "created_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    status, confidence, version, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'person', 'Rebuild Subject',
                    '{}'::jsonb, 'active', 1.00, 1, :now, :now
                )
                """
            ),
            {"id": entity_id, "workspace_id": workspace_id, "now": now},
        )
        # A representative-scale audit_events history for one entity --
        # rebuild_timeline replays every knowledge_entity-aggregate row it
        # finds in this table.
        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                )
                SELECT
                    gen_random_uuid(), :workspace_id, 'knowledge_entity.claim_recorded',
                    'knowledge_entity', :entity_id, 1, :user_id, gen_random_uuid(),
                    gen_random_uuid(), ARRAY['*'], 'allowed', 'user', '{}'::jsonb,
                    :now - (series * interval '1 minute')
                FROM generate_series(1, :count) AS series
                """
            ),
            {
                "workspace_id": workspace_id,
                "entity_id": entity_id,
                "user_id": user_id,
                "now": now,
                "count": _EVENT_COUNT,
            },
        )

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM (ANALYZE) audit_events, pkos_nodes"))

    try:
        yield workspace_id
    finally:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '60s'"))
            for table in ("timeline_entries", "audit_events", "pkos_nodes", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_rebuild_timeline_2000_event_workspace_records_percentiles(
    rebuild_performance_context: UUID,
) -> None:
    workspace_id = rebuild_performance_context

    with SessionFactory() as session:
        warmup_report = rebuild_timeline(session, workspace_id)
        session.commit()
    assert warmup_report.entries_written == _EVENT_COUNT

    samples: list[float] = []
    for _ in range(SAMPLE_SIZE):
        with SessionFactory() as session:
            started = perf_counter()
            report = rebuild_timeline(session, workspace_id)
            session.commit()
            samples.append(perf_counter() - started)
        assert report.entries_written == _EVENT_COUNT

    p50, p95, p99 = _p50(samples), _p95(samples), _p99(samples)
    print(
        f"\n[projection rebuild] p50={p50 * 1000:.1f}ms p95={p95 * 1000:.1f}ms "
        f"p99={p99 * 1000:.1f}ms samples(ms)={[round(s * 1000, 1) for s in samples]}"
    )
    assert p99 < SANITY_CEILING_SECONDS, (
        f"projection rebuild p99 {p99 * 1000:.1f} ms exceeded the "
        f"{SANITY_CEILING_SECONDS * 1000:.0f} ms sanity ceiling; this indicates a genuine "
        f"regression, not a documented-budget miss (TEST-PLAN.md names this as measured "
        f"but never sets a numeric target)"
    )
