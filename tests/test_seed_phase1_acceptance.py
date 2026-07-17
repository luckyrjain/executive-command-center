"""Tests for scripts/seed_phase1_acceptance.py.

These require a real PostgreSQL database (the seed script and the fixtures
it writes only make sense there -- see the module skip below) and are
therefore skipped unless ECC_DATABASE_URL points at PostgreSQL, matching the
convention used throughout tests/test_*_postgres.py.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import psycopg
import pytest

from ecc.config import get_settings

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _load_module() -> ModuleType:
    path = Path("scripts/seed_phase1_acceptance.py")
    spec = importlib.util.spec_from_file_location("seed_phase1_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


seed_module = _load_module()


def _pg_url() -> str:
    value = settings.database_url
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    return value


@pytest.fixture
def seeded_connection() -> Iterator[psycopg.Connection]:
    with psycopg.connect(_pg_url()) as conn:
        seed_module.seed(conn)
        conn.commit()
        yield conn


def _all_seeded_tables() -> tuple[str, ...]:
    return seed_module.ALL_PHASE1_TABLES


def test_seed_populates_every_phase1_table(seeded_connection: psycopg.Connection) -> None:
    workspace_ids = list(seed_module.WORKSPACE_IDS.values())
    event_ids = [
        seed_module.FIXTURE_IDS[label]["outbox_event"] for label in seed_module.WORKSPACE_LABELS
    ]
    with seeded_connection.cursor() as cur:
        for table in _all_seeded_tables():
            if table == "workspaces":
                cur.execute(
                    f"SELECT count(*) FROM {table} WHERE id = ANY(%s)",  # noqa: S608
                    (workspace_ids,),
                )
            elif table in ("event_inbox", "event_dead_letters"):
                cur.execute(
                    f"SELECT count(*) FROM {table} WHERE event_id = ANY(%s)",  # noqa: S608
                    (event_ids,),
                )
            else:
                cur.execute(
                    f"SELECT count(*) FROM {table} WHERE workspace_id = ANY(%s)",  # noqa: S608
                    (workspace_ids,),
                )
            count = cur.fetchone()[0]
            assert count > 0, f"expected seeded rows in {table}, found none"


def test_seed_creates_two_genuinely_isolated_workspaces(
    seeded_connection: psycopg.Connection,
) -> None:
    alpha = seed_module.WORKSPACE_IDS["alpha"]
    bravo = seed_module.WORKSPACE_IDS["bravo"]
    assert alpha != bravo

    with seeded_connection.cursor() as cur:
        for table in (
            "tasks",
            "commitments",
            "notes",
            "calendar_events",
            "meetings",
            "risks",
            "pkos_nodes",
        ):
            cur.execute(
                f"SELECT count(*) FROM {table} WHERE workspace_id = %s",  # noqa: S608
                (alpha,),
            )
            alpha_count = cur.fetchone()[0]
            cur.execute(
                f"SELECT count(*) FROM {table} WHERE workspace_id = %s",  # noqa: S608
                (bravo,),
            )
            bravo_count = cur.fetchone()[0]
            assert alpha_count > 0, f"{table} missing alpha-workspace rows"
            assert bravo_count > 0, f"{table} missing bravo-workspace rows"

        # No row belonging to one workspace should ever reference the
        # other workspace's owner/user id -- true isolation, not just two
        # workspace rows sharing underlying data.
        cur.execute(
            "SELECT count(*) FROM tasks WHERE workspace_id = %s AND owner_id NOT IN "
            "(SELECT id FROM users WHERE workspace_id = %s)",
            (alpha, alpha),
        )
        assert cur.fetchone()[0] == 0


def test_seed_is_idempotent_and_produces_deterministic_checksums(
    seeded_connection: psycopg.Connection,
) -> None:
    first_checksums = seed_module.fixture_row_checksums(seeded_connection)

    # Re-run the seed script's seed() against the same, already-seeded
    # database. It must not raise (no duplicate-key errors) and must not
    # change a single row.
    seed_module.seed(seeded_connection)
    seeded_connection.commit()

    second_checksums = seed_module.fixture_row_checksums(seeded_connection)
    assert first_checksums == second_checksums

    with seeded_connection.cursor() as cur:
        workspace_ids = list(seed_module.WORKSPACE_IDS.values())
        cur.execute(
            "SELECT count(*) FROM tasks WHERE workspace_id = ANY(%s)",
            (workspace_ids,),
        )
        # Exactly two tasks (one active, one archived) per workspace.
        assert cur.fetchone()[0] == 4


def test_seed_populates_lifecycle_fields_for_archived_rows(
    seeded_connection: psycopg.Connection,
) -> None:
    workspace_ids = list(seed_module.WORKSPACE_IDS.values())
    with seeded_connection.cursor() as cur:
        for table in ("tasks", "commitments", "notes", "calendar_events", "meetings", "risks"):
            cur.execute(
                f"SELECT count(*) FROM {table} "  # noqa: S608
                "WHERE workspace_id = ANY(%s) AND archived_at IS NOT NULL "
                "AND pre_archive_status IS NOT NULL",
                (workspace_ids,),
            )
            assert cur.fetchone()[0] > 0, f"{table} missing an archived lifecycle fixture row"


def test_seed_embeds_search_marker_across_searchable_tables(
    seeded_connection: psycopg.Connection,
) -> None:
    workspace_ids = list(seed_module.WORKSPACE_IDS.values())
    marker = seed_module.SEED_MARKER
    with seeded_connection.cursor() as cur:
        cur.execute(
            """
            SELECT count(*) FROM (
                SELECT id FROM tasks
                WHERE workspace_id = ANY(%(ws)s) AND title LIKE %(pattern)s
                UNION ALL
                SELECT id FROM commitments
                WHERE workspace_id = ANY(%(ws)s) AND summary LIKE %(pattern)s
                UNION ALL
                SELECT id FROM notes
                WHERE workspace_id = ANY(%(ws)s) AND title LIKE %(pattern)s
                UNION ALL
                SELECT id FROM meetings
                WHERE workspace_id = ANY(%(ws)s) AND title LIKE %(pattern)s
                UNION ALL
                SELECT id FROM calendar_events
                WHERE workspace_id = ANY(%(ws)s) AND title LIKE %(pattern)s
                UNION ALL
                SELECT id FROM risks
                WHERE workspace_id = ANY(%(ws)s) AND description LIKE %(pattern)s
            ) marker_hits
            """,
            {"ws": workspace_ids, "pattern": f"%{marker}%"},
        )
        assert cur.fetchone()[0] == 12  # 2 workspaces x 6 searchable tables
