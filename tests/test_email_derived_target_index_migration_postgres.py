"""Migration 0083 (Spec A deep review F2, plan task FX3): the partial
expression index `ix_recommendations_email_derived_target` and the extended
statistics `stx_recommendations_execution_result_target` that serve
`connector_security.PERSONAL_DERIVED_PREDICATES`.

Covers: both exist at head with the expected keys / expressions and partial
condition; downgrading to 0082 removes only them; upgrading restores them,
and the upgrade's own `ANALYZE` has populated the statistics.

The downgrade test runs real DDL against the suite's database, so it needs a
dedicated test database (never a shared or developer one). Its Alembic runs
use a `lock_timeout`, so a lock held elsewhere fails the test instead of
hanging.
"""

import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_REVISION = "0083_email_derived_target_idx"
_PREVIOUS_REVISION = "0082_revoke_idx_backfill_log"
_INDEX = "ix_recommendations_email_derived_target"
_STATISTICS = "stx_recommendations_execution_result_target"
_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "backend" / "migrations"
_ALEMBIC_PGOPTIONS = "-c lock_timeout=10s"


def _alembic_config() -> Config:
    # No ini file on purpose: `migrations/env.py` only calls
    # `logging.config.fileConfig` when a config file is set.
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return config


def _run_alembic(operation: Callable[[Config, str], None], revision: str) -> None:
    engine.dispose()
    previous = os.environ.get("PGOPTIONS")
    os.environ["PGOPTIONS"] = _ALEMBIC_PGOPTIONS
    try:
        operation(_alembic_config(), revision)
    finally:
        if previous is None:
            del os.environ["PGOPTIONS"]
        else:
            os.environ["PGOPTIONS"] = previous


def _current_revision() -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one())


def _statistics_expressions() -> str | None:
    """The extended-statistics object's expressions, or None if absent."""
    with engine.connect() as connection:
        value = connection.execute(
            text(
                "SELECT pg_get_statisticsobjdef_expressions(oid)::text "
                "FROM pg_statistic_ext WHERE stxname = :name"
            ),
            {"name": _STATISTICS},
        ).scalar_one_or_none()
    return None if value is None else str(value)


def _index_definition(name: str) -> str | None:
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"), {"name": name}
        ).scalar_one_or_none()
    return None if value is None else str(value)


def _statistics_have_data() -> bool:
    """`ANALYZE` has populated the extended-statistics object's
    per-expression statistics (`CREATE STATISTICS` alone leaves none)."""
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text(
                    "SELECT EXISTS (SELECT 1 FROM pg_statistic_ext s "
                    "JOIN pg_statistic_ext_data d ON d.stxoid = s.oid "
                    "WHERE s.stxname = :name AND d.stxdexpr IS NOT NULL)"
                ),
                {"name": _STATISTICS},
            ).scalar_one()
        )


def _index_expression_stats() -> int:
    """pg_stats rows ANALYZE wrote for the index's three expression keys."""
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM pg_stats WHERE tablename = :index"),
                {"index": _INDEX},
            ).scalar_one()
        )


@pytest.fixture
def seeded_recommendations() -> Iterator[None]:
    """Committed executed email recommendations, so the migration's
    `ANALYZE` has rows to sample (an empty table gets no statistics).

    ANALYZE reads a random sample of at most 300 x `default_statistics_
    target` pages (30,000 by default). If `recommendations` is larger than
    that -- e.g. a shared test database bloated by earlier bulk seeds that
    were rolled back or deleted -- the sample can miss the few pages holding
    these rows, ANALYZE then sees 0 live rows and writes no statistics, and
    the data assertion below fails at random. That is a property of the
    database, not of the migration, so it is checked up front and fails
    with the fix (VACUUM FULL recommendations) instead of flaking."""
    workspace_id, user_id = uuid4(), uuid4()
    account_id: UUID | None = None
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, '0083 migration test', 'UTC', now())"
            ),
            {"id": workspace_id},
        )
        account_id = create_identity(
            connection, workspace_id=workspace_id, user_id=user_id, now=datetime.now(UTC)
        )
        connection.execute(
            text(
                "INSERT INTO recommendations (id, workspace_id, recommendation_type, "
                "target_type, proposed_action, rationale, confidence, status, source, "
                "execution_result, created_by, updated_by, created_at, updated_at, version, "
                "owner_id, visibility) "
                "SELECT gen_random_uuid(), :ws, 'email_action_detected', 'task', "
                '\'{"operation": "create", "value": null}\'::jsonb, \'m\', 0.5, '
                "'executed', 'ai', jsonb_build_object('operation', 'create', "
                "'target_type', 'task', 'target_id', gen_random_uuid()::text), "
                ":u, :u, now(), now(), 1, :u, 'private' FROM generate_series(1, 50)"
            ),
            {"ws": workspace_id, "u": user_id},
        )
        pages, sample_pages = connection.execute(
            text(
                "SELECT pg_relation_size('recommendations') / "
                "current_setting('block_size')::int, "
                "300 * current_setting('default_statistics_target')::int"
            )
        ).one()
    try:
        assert pages <= sample_pages, (
            f"recommendations has {pages} pages, more than ANALYZE's {sample_pages}-page "
            "sample: the migration's ANALYZE may miss this fixture's rows. The test "
            "database is bloated; run VACUUM FULL recommendations on it."
        )
        yield
    finally:
        with engine.begin() as connection:
            for table in ("recommendations", "audit_events", "workspace_memberships", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": workspace_id},
                )
            connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
            connection.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": account_id})


@pytest.fixture
def restores_head() -> Iterator[None]:
    try:
        yield
    finally:
        _run_alembic(command.upgrade, "head")


def test_index_exists_at_head() -> None:
    definition = _index_definition(_INDEX)
    assert definition is not None
    assert (
        "ON public.recommendations USING btree (workspace_id, "
        "((execution_result ->> 'operation'::text)), "
        "((execution_result ->> 'target_type'::text)), "
        "((execution_result ->> 'target_id'::text)))"
    ) in definition
    assert "recommendation_type)::text = 'email_action_detected'::text" in definition
    assert "execution_result IS NOT NULL" in definition
    expressions = _statistics_expressions()
    assert expressions is not None
    for key in ("operation", "target_type", "target_id"):
        assert f"(execution_result ->> '{key}'::text)" in expressions


def test_downgrade_removes_index_and_upgrade_restores(
    seeded_recommendations: None, restores_head: None
) -> None:
    _run_alembic(command.downgrade, _PREVIOUS_REVISION)
    assert _current_revision() == _PREVIOUS_REVISION
    assert _index_definition(_INDEX) is None
    assert _statistics_expressions() is None
    # 0082's index is untouched.
    assert _index_definition("ix_connector_accounts_provider_external_id") is not None

    _run_alembic(command.upgrade, _REVISION)
    assert _current_revision() == _REVISION
    assert _index_definition(_INDEX) is not None
    assert _statistics_expressions() is not None
    # Round 2 (blocking): the migration itself ANALYZEs, so the planner has
    # statistics for the new expressions immediately -- not days later.
    assert _statistics_have_data()
    assert _index_expression_stats() == 3
