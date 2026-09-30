"""scripts/backfill_personal_visibility.py: the derived-row batches (tasks /
commitments / risks created from email recommendations) at a moderate size
-- review round 1, Lens B: the round-1 rule resolved each row's source
recommendation with a correlated subquery, 123-144 s at 20k derived rows.

Everything is seeded inside one transaction that is rolled back; the batches
run on that same connection (`_process_batch`, as a real run does per
batch, minus the per-batch commit). Structural plan assertions only (no
per-row SubPlan over `recommendations`; the batch's source lookup uses
migration 0083's index), and a generous wall-clock bound. Production-scale
timings (200k recommendations) are in the FX2 round-2 builder report.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from gmail_sync_fixtures import GmailSyncWorld, gmail_sync_world_factory  # noqa: F401
from sqlalchemy import event, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.platform.connector_security import (
    EMAIL_RECOMMENDATION_TYPE,
    email_derived_sources_sql,
    personal_sql_params,
)

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _load_module() -> ModuleType:
    path = Path("scripts/backfill_personal_visibility.py")
    spec = importlib.util.spec_from_file_location("backfill_personal_visibility_perf", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backfill = _load_module()

_TARGET_TYPES = {"tasks": "task", "commitments": "commitment", "risks": "risk"}
_DERIVED_PER_TABLE = 6_700  # ~20k derived rows in all
_OTHER_ROWS_PER_TABLE = 5_000  # not derived from anything
_DANGLING_EMAIL_CREATES = 40_000  # executed email creates whose target is gone
_OTHER_RECOMMENDATIONS = 60_000  # other types / not executed
_INDEX = "ix_recommendations_email_derived_target"
# Unlocked keyset read, grant lock, locked row read, source ids, source
# recommendations, active members, grant count, grant revoke, log insert
# (one executemany), update, attention mirror -- plus the optional history
# lookups. A per-row lookup would be ~500.
_MAX_STATEMENTS_PER_BATCH = 14

_SEED_ROWS = {
    "tasks": (
        "INSERT INTO tasks (id, workspace_id, owner_id, title, status, manual_priority, "
        "pinned, source_type, created_by, updated_by, created_at, updated_at, version, "
        "visibility) "
        "SELECT t, :ws, :a, 'perf', 'captured', 'medium', false, 'local', :a, :a, "
        "now(), now(), 1, 'workspace' FROM unnest(CAST(:ids AS uuid[])) AS t"
    ),
    "commitments": (
        "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
        "created_by, updated_by, created_at, updated_at, visibility) "
        "SELECT t, :ws, :a, 'perf', 'made_by_me', :a, :a, now(), now(), 'workspace' "
        "FROM unnest(CAST(:ids AS uuid[])) AS t"
    ),
    "risks": (
        "INSERT INTO risks (id, workspace_id, owner_id, description, probability, impact, "
        "created_by, updated_by, created_at, updated_at, visibility) "
        "SELECT t, :ws, :a, 'perf', 3, 3, :a, :a, now(), now(), 'workspace' "
        "FROM unnest(CAST(:ids AS uuid[])) AS t"
    ),
}

# `:targets` (text[]) become the `target_id`s of executed email `create`
# recommendations of type `:target_type`; `:n` more get a random
# (nonexistent) target.
_SEED_EMAIL_CREATES = (
    "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
    "proposed_action, rationale, confidence, status, source, execution_result, created_by, "
    "updated_by, created_at, updated_at, version, owner_id, visibility) "
    "SELECT gen_random_uuid(), :ws, :type, CAST(:target_type AS text), "
    "'{\"operation\": \"create\", \"value\": null}'::jsonb, 'perf', 0.5, 'executed', 'ai', "
    "jsonb_build_object('operation', 'create', 'target_type', CAST(:target_type AS text), "
    "'target_id', t, 'resulting_version', 1), :a, :a, now(), now(), 1, :a, 'workspace' "
    "FROM (SELECT unnest(CAST(:targets AS text[])) AS t "
    "UNION ALL SELECT gen_random_uuid()::text FROM generate_series(1, :n)) AS targets"
)
_SEED_OTHER_RECOMMENDATIONS = (
    "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
    "proposed_action, rationale, confidence, status, source, execution_result, created_by, "
    "updated_by, created_at, updated_at, version, owner_id, visibility) "
    "SELECT gen_random_uuid(), :ws, "
    "CASE WHEN g % 2 = 0 THEN 'task_priority' ELSE :type END, 'task', "
    '\'{"operation": "create", "value": null}\'::jsonb, \'perf\', 0.5, '
    "CASE WHEN g % 2 = 0 THEN 'executed' ELSE 'proposed' END, 'ai', "
    "CASE WHEN g % 2 = 0 THEN jsonb_build_object('operation', 'create', 'target_type', "
    "'task', 'target_id', gen_random_uuid()::text) END, "
    ":a, :a, now(), now(), 1, :a, 'workspace' FROM generate_series(1, :n) AS g"
)


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch,
    gmail_sync_world_factory: Any,  # noqa: F811
) -> GmailSyncWorld:
    monkeypatch.setenv("ECC_DATABASE_URL", settings.database_url)
    get_settings.cache_clear()
    built: GmailSyncWorld = gmail_sync_world_factory(extra_cleanup_tables=("resource_grants",))
    return built


@contextmanager
def _restores_table_statistics() -> Iterator[None]:
    """pg_statistic rolls back with the seed, but `pg_class.reltuples` /
    `relpages` are updated in place by ANALYZE and persist; re-analyzing
    the now seed-free tables restores them for later tests (the pattern of
    tests/test_personal_derived_rows_postgres.py)."""
    try:
        yield
    finally:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL statement_timeout = '60s'"))
            for table in ("recommendations", *_TARGET_TYPES):
                connection.execute(text(f"ANALYZE {table}"))  # noqa: S608 -- fixed names


def _seed(connection: Any, world: GmailSyncWorld) -> dict[str, list[UUID]]:
    ws, a = world.workspace_id, world.a.user_id
    derived: dict[str, list[UUID]] = {}
    for table, target_type in _TARGET_TYPES.items():
        derived[table] = [uuid4() for _ in range(_DERIVED_PER_TABLE)]
        others = [uuid4() for _ in range(_OTHER_ROWS_PER_TABLE)]
        connection.execute(
            text(_SEED_ROWS[table]), {"ws": ws, "a": a, "ids": derived[table] + others}
        )
        connection.execute(
            text(_SEED_EMAIL_CREATES),
            {
                "ws": ws,
                "a": a,
                "type": EMAIL_RECOMMENDATION_TYPE,
                "target_type": target_type,
                "targets": [str(row_id) for row_id in derived[table]],
                "n": _DANGLING_EMAIL_CREATES // len(_TARGET_TYPES),
            },
        )
    connection.execute(
        text(_SEED_OTHER_RECOMMENDATIONS),
        {"ws": ws, "a": a, "type": EMAIL_RECOMMENDATION_TYPE, "n": _OTHER_RECOMMENDATIONS},
    )
    # The seed is invisible to autoanalyze (uncommitted); in production the
    # same volume is committed and analyzed.
    for table in ("recommendations", *_TARGET_TYPES):
        connection.execute(text(f"ANALYZE {table}"))  # noqa: S608 -- fixed names
    return derived


def _assert_derived_index_serves_the_sources_query(connection: Any) -> None:
    """Catalog check: migration 0083's index keys, in order, and its partial
    predicate -- the conditions `email_derived_sources_sql` binds with
    equality / `= ANY`."""
    keys, predicate = connection.execute(
        text(
            "SELECT array(SELECT pg_get_indexdef(i.indexrelid, k, true) "
            "FROM generate_series(1, i.indnkeyatts) AS k), pg_get_expr(i.indpred, i.indrelid) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name"
        ),
        {"name": _INDEX},
    ).one()
    normalized = [key.replace("::text", "").replace("(", "").replace(")", "") for key in keys]
    assert normalized == [
        "workspace_id",
        "execution_result ->> 'operation'",
        "execution_result ->> 'target_type'",
        "execution_result ->> 'target_id'",
    ], keys
    assert "email_action_detected" in predicate and "execution_result IS NOT NULL" in predicate
    sql = email_derived_sources_sql("tasks")
    assert "derived_rec.workspace_id = :workspace_id" in sql
    assert "(derived_rec.execution_result ->> 'target_id') = ANY(:target_ids)" in sql
    assert "derived_rec.recommendation_type = 'email_action_detected'" in sql
    assert "derived_rec.execution_result IS NOT NULL" in sql


def _plan(connection: Any, sql: str, params: dict[str, Any]) -> str:
    return "\n".join(row[0] for row in connection.execute(text(f"EXPLAIN {sql}"), params))


def _assert_plans(connection: Any, ws: UUID, derived: dict[str, list[UUID]]) -> None:
    """No per-row SubPlan over `recommendations` in the batch reads or the
    source query; the source query can use migration 0083's index
    (planner-independent, QA-8: catalog check plus seq/bitmap scans off)."""
    params = {
        **personal_sql_params(),
        "workspace_id": ws,
        "after": UUID(int=0),
        "limit": backfill.DEFAULT_BATCH_SIZE,
        "ids": derived["tasks"][:500],
    }
    for table in _TARGET_TYPES:
        for sql in (backfill._batch_sql(table, lock=False), backfill._locked_rows_sql(table)):
            plan = _plan(connection, sql, params)
            assert "SubPlan" not in plan, plan
        sources_plan = _plan(
            connection,
            email_derived_sources_sql(table),
            {"workspace_id": ws, "target_ids": [str(i) for i in derived[table][:500]]},
        )
        assert "SubPlan" not in sources_plan, sources_plan
    _assert_derived_index_serves_the_sources_query(connection)
    connection.execute(text("SET LOCAL enable_seqscan = off"))
    connection.execute(text("SET LOCAL enable_bitmapscan = off"))
    forced = _plan(
        connection,
        email_derived_sources_sql("tasks"),
        {"workspace_id": ws, "target_ids": [str(i) for i in derived["tasks"][:500]]},
    )
    assert _INDEX in forced, forced
    connection.execute(text("RESET enable_seqscan"))
    connection.execute(text("RESET enable_bitmapscan"))


def _run_all_batches(connection: Any, ws: UUID) -> tuple[Any, list[int], float]:
    """Every derived-table batch on `connection` (write mode); returns the
    report, the statement count of each batch, and the elapsed time."""
    report = backfill.Report()
    run_id = uuid4()
    # Statements per batch: a constant, whatever the batch size -- a
    # per-row lookup (N+1 in Python) would issue ~500 per batch.
    statements: list[int] = []

    def _count(*_args: Any) -> None:
        statements[-1] += 1

    started = time.perf_counter()
    event.listen(connection, "before_cursor_execute", _count)
    for table in _TARGET_TYPES:
        after: UUID | None = UUID(int=0)
        while after is not None:
            statements.append(0)
            after = backfill._process_batch(
                connection,
                table,
                ws,
                after,
                backfill.DEFAULT_BATCH_SIZE,
                write=True,
                run_id=run_id,
                report=report,
            )
    event.remove(connection, "before_cursor_execute", _count)
    return report, statements, time.perf_counter() - started


def _assert_all_private(
    connection: Any, ws: UUID, report: Any, derived: dict[str, list[UUID]]
) -> None:
    for table, row_ids in derived.items():
        assert report.stats[table].changed == len(row_ids), table
        assert report.stats[table].unresolved == 0, table
        private = connection.execute(
            text(
                f"SELECT count(*) FROM {table} WHERE workspace_id = :ws "  # noqa: S608
                "AND visibility = 'private' AND id = ANY(:ids)"
            ),
            {"ws": ws, "ids": row_ids},
        ).scalar_one()
        assert private == len(row_ids), table


def test_derived_row_batches_scale_without_a_per_row_scan(world: GmailSyncWorld) -> None:
    ws = world.workspace_id
    with _restores_table_statistics():
        with engine.connect() as connection, connection.begin() as transaction:
            connection.execute(text("SET LOCAL statement_timeout = '120s'"))
            derived = _seed(connection, world)
            recommendations = connection.execute(
                text("SELECT count(*) FROM recommendations WHERE workspace_id = :ws"),
                {"ws": ws},
            ).scalar_one()
            assert recommendations >= 100_000
            _assert_plans(connection, ws, derived)
            report, statements, elapsed = _run_all_batches(connection, ws)
            print(
                f"derived backfill: {sum(len(v) for v in derived.values())} rows, "
                f"{recommendations} recommendations, {len(statements)} batches, "
                f"{elapsed:.2f} s, max {max(statements)} statements per batch"
            )
            _assert_all_private(connection, ws, report, derived)
            assert max(statements) <= _MAX_STATEMENTS_PER_BATCH, statements
            # Round 1 took 123-144 s here; measured well under 10 s now.
            assert elapsed < 60.0, elapsed
            transaction.rollback()
