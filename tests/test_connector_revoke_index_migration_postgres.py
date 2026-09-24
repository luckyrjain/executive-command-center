"""Migration 0082 (security remediation Spec A, S1.12): the cross-workspace
revoke-safety index on `connector_accounts (provider, external_account_id)`
and the ops-only `personal_visibility_backfill_log` table.

Covers: both objects exist at head with the expected shape; downgrading to
0081 removes both; upgrading again restores them; and
`connector_security._LIVE_ROW_EXISTS_SQL` (the boolean-only tenancy
exception) can be answered from the new index.

The downgrade test runs real DDL against the suite's database, so it needs a
dedicated test database (never a shared or developer one): DROP INDEX takes
an ACCESS EXCLUSIVE lock on `connector_accounts`. Its Alembic runs use a
`lock_timeout`, so a lock held elsewhere fails the test instead of hanging.
"""

import os
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.platform import connector_security

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_REVISION = "0082_revoke_idx_backfill_log"
_PREVIOUS_REVISION = "0081_backfill_resume_cursor"
_INDEX = "ix_connector_accounts_provider_external_id"
_LOG_TABLE = "personal_visibility_backfill_log"
_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "backend" / "migrations"
# `migrations/env.py` builds its own engine from settings, so the timeout is
# passed through libpq's PGOPTIONS rather than engine connect args.
_ALEMBIC_PGOPTIONS = "-c lock_timeout=10s"


def _alembic_config() -> Config:
    # Built without an ini file on purpose: `migrations/env.py` only calls
    # `logging.config.fileConfig` when a config file is set, and doing that
    # mid-test-run would disable every logger other tests capture.
    config = Config()
    config.set_main_option("script_location", str(_MIGRATIONS_DIR))
    return config


def _run_alembic(operation: Callable[[Config, str], None], revision: str) -> None:
    """Run one Alembic command with a lock timeout. The suite's own pooled
    connections are dropped first so none holds a lock the DDL waits on."""
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


def _index_columns(index_name: str) -> list[str] | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT array_agg(a.attname ORDER BY k.ord) "
                "FROM pg_class i "
                "JOIN pg_index x ON x.indexrelid = i.oid "
                "JOIN LATERAL unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord) ON true "
                "JOIN pg_attribute a ON a.attrelid = x.indrelid AND a.attnum = k.attnum "
                "WHERE i.relname = :name AND i.relkind = 'i' "
                "GROUP BY i.oid"
            ),
            {"name": index_name},
        ).scalar_one_or_none()
    return list(row) if row is not None else None


def _table_exists(table_name: str) -> bool:
    with engine.connect() as connection:
        return bool(
            connection.execute(
                text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{table_name}"}
            ).scalar_one()
        )


@pytest.fixture
def restores_head() -> Iterator[None]:
    """Leave the suite's database back at head even if the test body fails
    partway through a downgrade, so later tests see the full schema."""
    try:
        yield
    finally:
        _run_alembic(command.upgrade, "head")


def test_revoke_index_and_backfill_log_exist_at_head() -> None:
    assert _index_columns(_INDEX) == ["provider", "external_account_id"]
    assert _table_exists(_LOG_TABLE)

    with engine.connect() as connection:
        columns = {
            row.column_name: (row.data_type, row.is_nullable, row.column_default)
            for row in connection.execute(
                text(
                    "SELECT column_name, data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": _LOG_TABLE},
            )
        }
        constraints = {
            row.conname: (row.contype, row.definition)
            for row in connection.execute(
                text(
                    "SELECT conname, contype, pg_get_constraintdef(oid) AS definition "
                    "FROM pg_constraint WHERE conrelid = CAST(:table AS regclass) "
                    # PostgreSQL 18+ also lists NOT NULL constraints ('n').
                    "AND contype <> 'n'"
                ),
                {"table": _LOG_TABLE},
            )
        }

    assert {name: (dtype, nullable) for name, (dtype, nullable, _) in columns.items()} == {
        "id": ("uuid", "NO"),
        "run_id": ("uuid", "NO"),
        "table_name": ("text", "NO"),
        "row_id": ("uuid", "NO"),
        "previous_visibility": ("text", "NO"),
        "previous_owner_id": ("uuid", "YES"),
        "grants_revoked": ("integer", "NO"),
        "at": ("timestamp with time zone", "NO"),
    }
    assert columns["grants_revoked"][2] == "0"
    assert columns["at"][2] == "now()"

    # Primary key on id, one unique key on (run_id, table_name, row_id), and
    # no foreign keys: logged rows may be deleted later and the log must
    # outlive them.
    kinds = sorted(contype for contype, _ in constraints.values())
    assert kinds == ["p", "u"]
    assert constraints["uq_personal_visibility_backfill_log_run_row"][1] == (
        "UNIQUE (run_id, table_name, row_id)"
    )
    # The unique key's index leads with run_id, so `--restore <run_id>` is indexed.
    assert _index_columns("uq_personal_visibility_backfill_log_run_row") == [
        "run_id",
        "table_name",
        "row_id",
    ]


def test_backfill_log_defaults_and_uniqueness() -> None:
    run_id, row_id = uuid4(), uuid4()
    insert = text(
        f"INSERT INTO {_LOG_TABLE} (id, run_id, table_name, row_id, previous_visibility) "  # noqa: S608
        "VALUES (:id, :run_id, 'connector_accounts', :row_id, 'workspace') "
        "ON CONFLICT (run_id, table_name, row_id) DO NOTHING "
        "RETURNING grants_revoked, previous_owner_id, at IS NOT NULL"
    )
    with engine.connect() as connection, connection.begin() as transaction:
        first = connection.execute(
            insert, {"id": uuid4(), "run_id": run_id, "row_id": row_id}
        ).one()
        assert tuple(first) == (0, None, True)
        # A retried run logging the same row again is a no-op, not a second entry.
        retried = connection.execute(
            insert, {"id": uuid4(), "run_id": run_id, "row_id": row_id}
        ).one_or_none()
        assert retried is None
        transaction.rollback()


def test_downgrade_to_0081_removes_both_and_upgrade_restores(restores_head: None) -> None:
    _run_alembic(command.downgrade, _PREVIOUS_REVISION)
    assert _current_revision() == _PREVIOUS_REVISION
    assert _index_columns(_INDEX) is None
    assert not _table_exists(_LOG_TABLE)
    # The pre-existing workspace-scoped unique key is untouched by the downgrade.
    assert _index_columns("uq_connector_accounts_workspace_provider_external_id") == [
        "workspace_id",
        "provider",
        "external_account_id",
    ]

    _run_alembic(command.upgrade, _REVISION)
    assert _current_revision() == _REVISION
    assert _index_columns(_INDEX) == ["provider", "external_account_id"]
    assert _table_exists(_LOG_TABLE)


def test_live_row_exists_query_can_use_revoke_index() -> None:
    """`revoke_is_safe(scope="global")`'s cross-workspace existence check is
    answerable from the new index. Seq scans are disabled for this one
    transaction so the planner's choice on a tiny test table does not mask
    whether the index is usable at all."""
    params = {"provider": "gmail", "external_account_id": "someone@example.com"}
    with engine.connect() as connection, connection.begin() as transaction:
        connection.execute(text("SET LOCAL enable_seqscan = off"))
        for exclude_row_id in (None, uuid4()):
            plan = "\n".join(
                row[0]
                for row in connection.execute(
                    text("EXPLAIN " + connector_security._LIVE_ROW_EXISTS_SQL),
                    {**params, "exclude_row_id": exclude_row_id},
                )
            )
            assert _INDEX in plan, plan
        transaction.rollback()
