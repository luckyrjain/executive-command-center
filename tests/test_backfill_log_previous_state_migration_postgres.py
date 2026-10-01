"""Migration 0084 (plan task FX2, deep review round 3): the nullable JSONB
`personal_visibility_backfill_log.previous_state` snapshot column.

Covers: the column exists at head (jsonb, nullable, no default); downgrading
to 0083 drops only it (existing log rows survive); upgrading adds it back,
NULL for the old rows (restore falls back to the owner/visibility check).

The downgrade test runs real DDL against the suite's database, so it needs a
dedicated test database (never a shared or developer one). Its Alembic runs
use a `lock_timeout`, so a lock held elsewhere fails the test instead of
hanging.
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

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_REVISION = "0084_backfill_log_previous_state"
_PREVIOUS_REVISION = "0083_email_derived_target_idx"
_MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "backend" / "migrations"
_ALEMBIC_PGOPTIONS = "-c lock_timeout=10s"


def _alembic_config() -> Config:
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


def _column() -> tuple[str, str, str | None] | None:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                "WHERE table_name = 'personal_visibility_backfill_log' "
                "AND column_name = 'previous_state'"
            )
        ).one_or_none()
    return None if row is None else (row[0], row[1], row[2])


@pytest.fixture
def restores_head() -> Iterator[None]:
    """Leave the suite's database back at head even if the test body fails."""
    try:
        yield
    finally:
        _run_alembic(command.upgrade, "head")


@pytest.fixture
def old_log_row() -> Iterator[str]:
    """A log row written before 0084 (no snapshot)."""
    run_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO personal_visibility_backfill_log (id, run_id, table_name, row_id, "
                "previous_visibility, previous_owner_id, grants_revoked) "
                "VALUES (:id, :run, 'tasks', :row, 'workspace', NULL, 0)"
            ),
            {"id": uuid4(), "run": run_id, "row": uuid4()},
        )
    yield run_id
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM personal_visibility_backfill_log WHERE run_id = :r"), {"r": run_id}
        )


def test_previous_state_exists_at_head() -> None:
    assert _column() == ("jsonb", "YES", None)


def test_downgrade_to_0083_drops_only_the_column_and_upgrade_adds_it_back(
    old_log_row: str, restores_head: None
) -> None:
    _run_alembic(command.downgrade, _PREVIOUS_REVISION)
    assert _current_revision() == _PREVIOUS_REVISION
    assert _column() is None
    with engine.connect() as connection:
        survived = connection.execute(
            text("SELECT count(*) FROM personal_visibility_backfill_log WHERE run_id = :r"),
            {"r": old_log_row},
        ).scalar_one()
    assert survived == 1

    _run_alembic(command.upgrade, _REVISION)
    assert _current_revision() == _REVISION
    assert _column() == ("jsonb", "YES", None)
    with engine.connect() as connection:
        snapshot = connection.execute(
            text("SELECT previous_state FROM personal_visibility_backfill_log WHERE run_id = :r"),
            {"r": old_log_row},
        ).scalar_one()
    assert snapshot is None
