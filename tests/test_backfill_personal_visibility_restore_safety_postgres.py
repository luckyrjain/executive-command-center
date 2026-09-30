"""scripts/backfill_personal_visibility.py -- FX2 deep review round 1:
restore safety (SEC-1, SRE-1, SRE-4, QA-3/QA-4), the derived-row
owner_inactive / manual-fix semantics (C), attention items of derived rows
(SEC-2), a cascade-redacted source (SEC-3) and the single-source / tie-break
edges (QA-1, QA-5, QA-6).

Post-backfill changes are simulated by one direct UPDATE each, named where
they occur, unless an API path exists (task PATCH, the email-disable
cascade).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import test_backfill_personal_visibility_derived_rows_postgres as base
from gmail_sync_fixtures import GmailSyncWorld, csrf_headers, gmail_sync_world_factory  # noqa: F401
from sqlalchemy import text
from test_backfill_personal_visibility_derived_rows_postgres import (
    _FIELDS,
    _confirm,
    _counts,
    _email_rec,
    _flag,
    _grant,
    _grant_revoked,
    _log_rows,
    _owner_vis,
    _run,
    _unresolved,
)

from ecc.config import get_settings, setting_source
from ecc.database import engine
from ecc.platform import connector_security

backfill = base.backfill
pytestmark = base.pytestmark


@pytest.fixture(autouse=True)
def _drop_cached_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


@pytest.fixture
def run_ids() -> Iterator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    if ids:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM personal_visibility_backfill_log WHERE run_id = ANY(:ids)"),
                {"ids": ids},
            )


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch,
    gmail_sync_world_factory: Any,  # noqa: F811
) -> GmailSyncWorld:
    monkeypatch.setenv("ECC_DATABASE_URL", base.settings.database_url)
    monkeypatch.delenv("ECC_PERSONAL_DATA_ISOLATION", raising=False)
    get_settings.cache_clear()
    built: GmailSyncWorld = gmail_sync_world_factory(
        bystander=True,
        extra_cleanup_tables=("resource_grants", "recommendation_feedback", "attention_feedback"),
    )
    return built


def _remove_member(world: GmailSyncWorld, user_id: UUID) -> None:
    """Removed while the flag was off (simulated membership update)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed', removed_at = now() "
                "WHERE workspace_id = :ws AND users_id = :u"
            ),
            {"ws": world.workspace_id, "u": user_id},
        )


def _commitment_confirmed_by_b(world: GmailSyncWorld) -> UUID:
    rec = _email_rec(world, world.a.user_id, "commitment", fields=_FIELDS["commitment"])
    return _confirm(world, world.b.user_id, rec)


def _updated_at(table: str, row_id: UUID) -> Any:
    with engine.begin() as conn:
        return conn.execute(
            text(f"SELECT updated_at FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        ).scalar_one()


# ---------------------------------------------------------------------------
# B. Restore safety
# ---------------------------------------------------------------------------


def _log_state(run_id: UUID, table: str, row_id: UUID) -> Any:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT previous_state FROM personal_visibility_backfill_log WHERE run_id = :r "
                "AND table_name = :t AND row_id = :id"
            ),
            {"r": run_id, "t": table, "id": row_id},
        ).scalar_one()


def test_restore_skips_a_derived_row_edited_after_the_backfill(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SEC-1 (snapshot, migration 0084): A writes into the (now private)
    task; a restore must not republish it. The log snapshot holds the
    task's pre-backfill `version`/`updated_at` and the backfill's own bump;
    the PATCH moves both, so the restore reports `content`."""
    a, ws = world.a, world.workspace_id
    task = _confirm(world, world.b.user_id, a.recommendation_ids[0])
    untouched = _commitment_confirmed_by_b(world)
    _flag(monkeypatch, on=True)
    _code, _rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None, err
    state = _log_state(run_id, "tasks", task)
    assert state["version"] == 1 and state["version_bump"] == 1 and state["attention"] == []

    client, token = world.harness.client_for(ws, a.user_id)
    patched = client.patch(
        f"/api/v1/tasks/{task}",
        json={"expected_version": base._version("tasks", task), "description": "private note"},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert patched.status_code == 200, patched.text

    _flag(monkeypatch, on=False)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert _unresolved(rows)[("tasks", task)] == "changed_since_backfill:content"
    assert _owner_vis("tasks", task) == (a.user_id, "private")
    assert ("commitments", untouched) not in _unresolved(rows)
    assert _owner_vis("commitments", untouched) == (world.b.user_id, "workspace")


def test_backfill_and_restore_leave_updated_at_alone(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """R3-3: the backfill is not a content edit -- `updated_at` (list order,
    staleness) is unchanged by it, and still the pre-backfill value after
    the restore."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    before = _updated_at("tasks", task)
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert run_id is not None
    assert _owner_vis("tasks", task) == (world.a.user_id, "private")
    assert _updated_at("tasks", task) == before
    _flag(monkeypatch, on=False)
    _run(capsys, run_ids, "--restore", str(run_id))
    assert _owner_vis("tasks", task) == (world.b.user_id, "workspace")
    assert _updated_at("tasks", task) == before


def test_restore_refuses_while_the_flag_is_on_unless_overridden(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SRE-1 scenario 3: a restore while the application still writes rows
    private leaves a half-restored data set -- refused unless
    `--allow-with-isolation`, which warns loudly."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert run_id is not None
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_ERROR
    assert "refusing to restore" in err and rows == []
    assert _owner_vis("tasks", task) == (world.a.user_id, "private")

    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id), "--allow-with-isolation")
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert "WARNING: restoring with ECC_PERSONAL_DATA_ISOLATION enabled" in err
    assert _owner_vis("tasks", task) == (world.b.user_id, "workspace")

    code, _rows, err, _ = _run(capsys, run_ids, "--dry-run", "--allow-with-isolation")
    assert code == backfill.EXIT_ERROR and "--restore only" in err


# The SQL docs/SETUP.md gives for "which grants did run <run_id> revoke"
# (restore does not re-grant). Keep the two in sync.
REVOKED_GRANTS_SQL = (
    "SELECT g.id, g.resource_type, g.resource_id, g.grantee_account_id, g.actions "
    "FROM personal_visibility_backfill_log l "
    "JOIN resource_grants g ON g.resource_type = l.table_name AND g.resource_id = l.row_id "
    "AND g.revoked_at = l.at "
    "WHERE l.run_id = :run_id AND l.grants_revoked > 0"
)


def test_documented_sql_lists_exactly_the_grants_a_run_revoked(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SRE-4: grants are not restored; the runbook's SQL lists them. The
    backfill revokes (`revoked_at = now()`) in the transaction that writes
    the log row (`at` = now()), so the timestamps are equal."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    commitment = _commitment_confirmed_by_b(world)
    revoked_by_run = {_grant(world, "tasks", task), _grant(world, "commitments", commitment)}
    revoked_earlier = _grant(world, "tasks", task)
    with engine.begin() as conn:  # revoked before the run: must not be listed
        conn.execute(
            text("UPDATE resource_grants SET revoked_at = now() - interval '1 day' WHERE id = :id"),
            {"id": revoked_earlier},
        )
    docs = (Path(__file__).parents[1] / "docs" / "SETUP.md").read_text()
    assert "g.revoked_at = l.at" in docs and "l.grants_revoked > 0" in docs

    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert run_id is not None
    with engine.begin() as conn:
        listed = {r[0] for r in conn.execute(text(REVOKED_GRANTS_SQL), {"run_id": run_id})}
    assert listed == revoked_by_run
    assert all(_grant_revoked(g) for g in revoked_by_run)


def _threadless_workspace_item(world: GmailSyncWorld) -> UUID:
    """A's first attention item, its thread deleted (the decision keeps the
    item's owner) and written workspace-visible (simulated) so a run logs it."""
    item = world.a.attention_item_ids[0]
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM email_threads WHERE id = (SELECT entity_id FROM attention_items "
                "WHERE id = :id)"
            ),
            {"id": item},
        )
        conn.execute(
            text("UPDATE attention_items SET visibility = 'workspace' WHERE id = :id"),
            {"id": item},
        )
    return item


def _granted_feedback(world: GmailSyncWorld) -> tuple[UUID, UUID]:
    """Two feedback rows on A's email rows (a recommendation's `accept`, an
    attention item's `useful`), each with a flag-off-era grant, so the run
    logs them (grants revoked, owner/visibility kept)."""
    a = world.a
    _confirm(world, a.user_id, a.recommendation_ids[0])
    with engine.begin() as conn:
        rec_feedback = conn.execute(
            text("SELECT id FROM recommendation_feedback WHERE recommendation_id = :r"),
            {"r": a.recommendation_ids[0]},
        ).scalar_one()
    item_feedback = base._attention_feedback(world, a.user_id, a.attention_item_ids[0])
    _grant(world, "recommendation_feedback", rec_feedback)
    _grant(world, "attention_feedback", item_feedback)
    return rec_feedback, item_feedback


def _reowned_alias(log: dict[tuple[str, UUID], tuple[str, UUID, int]]) -> UUID:
    aliases = [
        row_id
        for (table, row_id), (_vis, prev_owner, _g) in log.items()
        if table == "entity_aliases" and _owner_vis(table, row_id)[0] != prev_owner
    ]
    assert aliases, "the world has aliases the run re-owned"
    return aliases[0]


def test_restore_cas_skips_post_backfill_changes_on_kept_owner_tables(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA-3/QA-4, QA2-1: after the backfill (simulated, one UPDATE each) a
    connector account and a thread-less attention item are transferred, one
    feedback row is re-owned and another narrowed to `private`, and an alias
    the run re-owned is narrowed to `private`. None may be restored; each
    is reported."""
    item = _threadless_workspace_item(world)
    rec_feedback, item_feedback = _granted_feedback(world)
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert run_id is not None
    log = _log_rows(run_id)
    changed = {
        ("connector_accounts", world.b.connector_account_id): "owner_id = :by",
        ("attention_items", item): "owner_id = :by",
        ("recommendation_feedback", rec_feedback): "owner_id = :by",
        ("attention_feedback", item_feedback): "visibility = 'private'",
        ("entity_aliases", _reowned_alias(log)): "visibility = 'private'",
    }
    cause = {
        key: assignment.split(" ")[0].removesuffix("_id") for key, assignment in changed.items()
    }
    with engine.begin() as conn:
        for (table, row_id), assignment in changed.items():
            assert (table, row_id) in log, (table, row_id)
            conn.execute(
                text(f"UPDATE {table} SET {assignment} WHERE id = :id"),  # noqa: S608
                {"by": world.bystander_user_id, "id": row_id},
            )
    before = {key: _owner_vis(*key) for key in changed}

    _flag(monkeypatch, on=False)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_UNRESOLVED, err
    unresolved = _unresolved(rows)
    for key in changed:
        assert unresolved[key] == f"changed_since_backfill:{cause[key]}", key
        assert _owner_vis(*key) == before[key], key


# ---------------------------------------------------------------------------
# C. owner_inactive and manual fixes
# ---------------------------------------------------------------------------

# docs/SETUP.md's owner_inactive remedy (per row), with bind parameters.
REMEDY_RECORD_SQL = (
    "INSERT INTO ownership_transfers (id, workspace_id, resource_type, resource_id, "
    "from_account_id, to_account_id, status, initiated_by, created_at, completed_at) "
    "SELECT gen_random_uuid(), r.workspace_id, 'commitments', r.id, fu.account_id, "
    "tu.account_id, 'completed', :operator, now(), now() "
    "FROM commitments r JOIN users fu ON fu.id = r.owner_id JOIN users tu ON tu.id = r.created_by "
    "WHERE r.workspace_id = :ws AND r.id = :id"
)
REMEDY_SQL = (
    "UPDATE commitments SET owner_id = created_by, version = version + 1 "
    "WHERE workspace_id = :ws AND id = :id"
)
# docs/SETUP.md's attention statement (both templates), with bind
# parameters: the row's attention items follow it, and a dismissal made at
# the pre-bump version is carried to the new one, as the tool does.
_CARRY = (
    "(attention_items.dismissed_entity_version = attention_items.source_entity_version "
    "AND attention_items.source_entity_version = r.version - 1)"
)


def _template_attention_sql(table: str, entity_type: str) -> str:
    return (
        "UPDATE attention_items SET owner_id = r.owner_id, visibility = r.visibility, "  # noqa: S608
        f"source_entity_version = CASE WHEN {_CARRY} "
        "THEN r.version ELSE attention_items.source_entity_version END, "
        f"dismissed_entity_version = CASE WHEN {_CARRY} "
        "THEN r.version ELSE attention_items.dismissed_entity_version END "
        f"FROM {table} r WHERE r.workspace_id = :ws AND r.id = :id "
        "AND attention_items.workspace_id = r.workspace_id "
        f"AND attention_items.entity_type = '{entity_type}' AND attention_items.entity_id = r.id"
    )


def _template_locks(conn: Any, ws: UUID) -> None:
    """The templates' locks, in the tool's order."""
    conn.execute(
        text("SELECT pg_advisory_xact_lock_shared(hashtextextended(:key, 0))"),
        {"key": f"membership-mutation:{ws}"},
    )
    conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"attention-regenerate:{ws}"},
    )


def _assert_template_text_in_docs() -> None:
    docs = (Path(__file__).parents[1] / "docs" / "SETUP.md").read_text()
    for needle in (
        "pg_advisory_xact_lock_shared(hashtextextended('membership-mutation:<workspace_id>', 0));"
        "\n  SELECT pg_advisory_xact_lock("
        "hashtextextended('attention-regenerate:<workspace_id>', 0));",
        "AND attention_items.source_entity_version = r.version - 1)",
        "THEN r.version ELSE attention_items.dismissed_entity_version END",
    ):
        assert docs.count(needle) >= 2, needle  # both templates


def _dismiss_task_item(world: GmailSyncWorld, task: UUID) -> UUID:
    """B regenerates attention and dismisses the task's item (flag off)."""
    world.harness.regenerate_attention(workspace_id=world.workspace_id, user_id=world.b.user_id)
    with engine.begin() as conn:
        item: UUID = conn.execute(
            text("SELECT id FROM attention_items WHERE entity_type = :et AND entity_id = :t"),
            {"et": "task", "t": task},
        ).scalar_one()
    client, token = world.harness.client_for(world.workspace_id, world.b.user_id)
    response = client.post(
        f"/api/v1/attention/{item}/dismiss", json={}, headers=csrf_headers(token, str(uuid4()))
    )
    assert response.status_code == 200, response.text
    return item


def _insert_attention_item(world: GmailSyncWorld, entity_type: str, entity_id: UUID) -> UUID:
    """What a flag-off regenerate writes for the entity (B's, workspace)."""
    item = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO attention_items (id, workspace_id, entity_type, entity_id, "
                "source_entity_version, score, confidence, factors, explanation, generated_at, "
                "expires_at, pinned, policy_version, owner_id, visibility) VALUES "
                "(:id, :ws, :et, :eid, 1, 50, 0.5, '[]'::jsonb, 'Confidential figures', "
                "now(), now() + interval '1 day', false, 1, :b, 'workspace')"
            ),
            {
                "id": item,
                "ws": world.workspace_id,
                "et": entity_type,
                "eid": entity_id,
                "b": world.b.user_id,
            },
        )
    return item


def _apply_documented_remedy(world: GmailSyncWorld, commitment: UUID) -> None:
    docs = (Path(__file__).parents[1] / "docs" / "SETUP.md").read_text()
    assert "SET owner_id = created_by, version = version + 1" in docs
    assert "INSERT INTO ownership_transfers" in docs
    assert "UPDATE attention_items SET owner_id = r.owner_id, visibility = r.visibility" in docs
    _assert_template_text_in_docs()
    params = {"ws": world.workspace_id, "id": commitment}
    with engine.begin() as conn:  # one transaction, as documented
        _template_locks(conn, world.workspace_id)
        recorded = conn.execute(
            text(REMEDY_RECORD_SQL), {**params, "operator": world.bystander_user_id}
        ).rowcount
        conn.execute(text(REMEDY_SQL), params)
        conn.execute(text(_template_attention_sql("commitments", "commitment")), params)
    assert recorded == 1


def test_a_manual_owner_fix_sticks_and_is_reported(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """C.1, R2-3: run 1 re-owns B's commitment (and its attention item) to A
    (removed) -- `owner_inactive`. The operator hands both back to B with
    the documented SQL. Later runs keep B, reporting `derived_owner_changed`
    every run (exit 1); a restore of run 1 leaves the fix alone."""
    a, b, ws = world.a, world.b, world.workspace_id
    commitment = _commitment_confirmed_by_b(world)
    item = _insert_attention_item(world, "commitment", commitment)
    _remove_member(world, a.user_id)
    _flag(monkeypatch, on=True)
    _code, rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    assert _unresolved(rows)[("commitments", commitment)] == "owner_inactive"
    assert _owner_vis("attention_items", item) == (a.user_id, "private")

    _apply_documented_remedy(world, commitment)
    assert _owner_vis("attention_items", item) == (b.user_id, "private")
    for _ in range(2):
        code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
        assert code == backfill.EXIT_UNRESOLVED, err
        assert _unresolved(rows)[("commitments", commitment)] == "derived_owner_changed"
        assert _counts(rows)["commitments"] == (0, 0, 1)
        assert _owner_vis("commitments", commitment) == (b.user_id, "private")

    _flag(monkeypatch, on=False)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert _unresolved(rows)[("commitments", commitment)] == "changed_since_backfill:owner", err
    assert _owner_vis("commitments", commitment) == (b.user_id, "private")


def test_a_restored_row_is_reowned_by_the_next_backfill(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA2-2 (1): backfill, restore (flag off), re-enable, backfill again.
    The row has earlier log entries but is `workspace` again, so the rule
    applies fresh: re-owned to the recommendation's owner A, not reported."""
    a, b, ws = world.a, world.b, world.workspace_id
    commitment = _commitment_confirmed_by_b(world)
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    _flag(monkeypatch, on=False)
    _run(capsys, run_ids, "--restore", str(run_id))
    assert _owner_vis("commitments", commitment) == (b.user_id, "workspace")

    _flag(monkeypatch, on=True)
    _code, rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert ("commitments", commitment) not in _unresolved(rows)
    assert _owner_vis("commitments", commitment) == (a.user_id, "private")


def test_a_private_row_never_backfilled_is_reowned(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA2-2 (2): the confirmer B made the flag-off row private (simulated)
    before any backfill: no earlier log entry, so B still counts as the
    confirm's owner and the first run re-owns it to A."""
    a, b, ws = world.a, world.b, world.workspace_id
    commitment = _commitment_confirmed_by_b(world)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE commitments SET visibility = 'private' WHERE id = :id"),
            {"id": commitment},
        )
    assert _owner_vis("commitments", commitment) == (b.user_id, "private")
    _flag(monkeypatch, on=True)
    _code, rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert ("commitments", commitment) not in _unresolved(rows)
    assert _owner_vis("commitments", commitment) == (a.user_id, "private")


def test_owner_inactive_is_reported_again_after_a_failed_run(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """C.2 (integrity probe): the run crashes after the batch that re-owned
    the commitment committed; the re-run still reports `owner_inactive`
    (rebuilt from the log + the row, not from the crashed run's memory)."""
    a, ws = world.a, world.workspace_id
    commitment = _commitment_confirmed_by_b(world)
    _remove_member(world, a.user_id)
    _flag(monkeypatch, on=True)
    original = backfill._process_batch

    def crash_on_risks(conn: Any, table: str, *args: Any, **kwargs: Any) -> Any:
        if table == "risks":
            raise RuntimeError("simulated crash after the commitments batch committed")
        return original(conn, table, *args, **kwargs)

    monkeypatch.setattr(backfill, "_process_batch", crash_on_risks)
    code, rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_ERROR and rows == []
    assert run_id is not None and ("commitments", commitment) in _log_rows(run_id)
    assert _owner_vis("commitments", commitment) == (a.user_id, "private")

    monkeypatch.setattr(backfill, "_process_batch", original)
    code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert _unresolved(rows)[("commitments", commitment)] == "owner_inactive"


# ---------------------------------------------------------------------------
# SEC-2 / SEC-3
# ---------------------------------------------------------------------------


def test_attention_items_of_derived_rows_follow_backfill_and_restore(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SEC-2: an attention item copies its task's owner/visibility (and
    title) at regenerate time; it follows the task in the same batch, and
    back on restore."""
    a, b, ws = world.a, world.b, world.workspace_id
    task = _confirm(world, b.user_id, a.recommendation_ids[0])
    item = _insert_attention_item(world, "task", task)
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    assert _owner_vis("tasks", task) == (a.user_id, "private")
    assert _owner_vis("attention_items", item) == (a.user_id, "private")

    _flag(monkeypatch, on=False)
    _run(capsys, run_ids, "--restore", str(run_id))
    assert _owner_vis("tasks", task) == (b.user_id, "workspace")
    assert _owner_vis("attention_items", item) == (b.user_id, "workspace")


def test_derived_row_of_a_cascade_redacted_recommendation_is_made_private(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SEC-3: A withdraws email consent (flag off): the Gmail cascade
    redacts A's executed recommendation in place (rationale, proposed
    action, evidence ids) but keeps `execution_result`, so the task B
    confirmed from it is still recognised and made private to A."""
    a, b, ws = world.a, world.b, world.workspace_id
    rec = a.recommendation_ids[0]
    task = _confirm(world, b.user_id, rec)
    client, token = world.harness.client_for(ws, a.user_id)
    disabled = client.post(
        "/api/v1/personal/domains/email/disable", headers=csrf_headers(token, str(uuid4()))
    )
    assert disabled.status_code == 200, disabled.text
    with engine.begin() as conn:
        evidence_ids = conn.execute(
            text("SELECT evidence_ids FROM recommendations WHERE id = :id"), {"id": rec}
        ).scalar_one()
    assert evidence_ids == []  # redacted by the cascade's UPDATE
    _flag(monkeypatch, on=True)
    _code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert ("tasks", task) not in _unresolved(rows), err
    assert _owner_vis("tasks", task) == (a.user_id, "private")


# ---------------------------------------------------------------------------
# QA-1, QA-5, QA-6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source", ["derived_predicates", "target_types"])
def test_uncovered_table_from_either_input_refuses_and_is_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], source: str
) -> None:
    """QA-1: the real `_uncovered_tables`, fed an extra table through each
    of its inputs."""
    monkeypatch.setenv("ECC_DATABASE_URL", base.settings.database_url)
    if source == "derived_predicates":
        monkeypatch.setattr(
            connector_security,
            "PERSONAL_DERIVED_PREDICATES",
            {**connector_security.PERSONAL_DERIVED_PREDICATES, "new_derived_table": "true"},
        )
    else:
        monkeypatch.setattr(
            connector_security,
            "EMAIL_DERIVED_TARGET_TYPES",
            {**connector_security.EMAIL_DERIVED_TARGET_TYPES, "new_derived_table": "x"},
        )
    assert backfill.main(["--dry-run"]) == backfill.EXIT_ERROR
    err = capsys.readouterr().err
    assert "refusing to run" in err and "new_derived_table" in err


def test_sources_query_is_scoped_to_the_workspace(world: GmailSyncWorld) -> None:
    """QA-5: a recommendation in ANOTHER workspace whose `target_id` text is
    this workspace's task id is not a source."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    other_ws, copy_id = uuid4(), uuid4()
    with engine.connect() as conn, conn.begin() as transaction:
        # Rolled back; FKs/owner triggers off so the copy needs no users there.
        conn.execute(text("SET LOCAL session_replication_role = replica"))
        conn.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, 'QA-5', now())"),
            {"id": other_ws},
        )
        conn.execute(
            text(
                "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
                "proposed_action, rationale, confidence, status, source, execution_result, "
                "created_by, updated_by, created_at, updated_at, version, owner_id, visibility) "
                "SELECT :copy, :other, recommendation_type, target_type, "
                "proposed_action, rationale, confidence, status, source, execution_result, "
                "created_by, updated_by, now(), now(), 1, owner_id, visibility "
                "FROM recommendations WHERE workspace_id = :ws "
                "AND execution_result ->> 'target_id' = :task"
            ),
            {"copy": copy_id, "other": other_ws, "ws": world.workspace_id, "task": str(task)},
        )
        found = {
            ws: {
                rec_id
                for rec_id, _target in conn.execute(
                    text(connector_security.email_derived_sources_sql("tasks")),
                    {"workspace_id": ws, "target_ids": [str(task)]},
                ).all()
            }
            for ws in (world.workspace_id, other_ws)
        }
        transaction.rollback()
    assert found[other_ws] == {copy_id}
    assert found[world.workspace_id] and copy_id not in found[world.workspace_id]


def test_lowest_source_wins_and_a_missing_source_is_reported(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA-6: two sources for one target (not possible through the app;
    simulated) -> the lowest recommendation id decides, deterministically.
    A row whose source the batch cannot resolve is reported
    `derived_source_missing` (grants still revoked, nothing else changed)."""
    a, b, ws = world.a, world.b, world.workspace_id
    task = _confirm(world, a.user_id, a.recommendation_ids[0])
    b_rec = UUID(int=uuid4().int >> 24)  # three zero leading bytes: sorts first
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
                "proposed_action, rationale, confidence, status, source, execution_result, "
                "created_by, updated_by, created_at, updated_at, version, owner_id, visibility) "
                "SELECT :id, workspace_id, recommendation_type, target_type, proposed_action, "
                "rationale, confidence, status, source, execution_result, :b, :b, now(), now(), "
                "1, :b, 'workspace' FROM recommendations WHERE id = :rec"
            ),
            {"id": b_rec, "b": b.user_id, "rec": a.recommendation_ids[0]},
        )
    _flag(monkeypatch, on=True)
    _code, _rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert _owner_vis("tasks", task) == (b.user_id, "private")  # B's rec has the lowest id

    _flag(monkeypatch, on=False)  # a flag-off-era confirm
    other = _commitment_confirmed_by_b(world)
    grant = _grant(world, "commitments", other)
    _flag(monkeypatch, on=True)
    monkeypatch.setattr(
        backfill,
        "_derived_sources_sql",
        lambda table: connector_security.email_derived_sources_sql(table) + " AND false",
    )
    _code, rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert _unresolved(rows)[("commitments", other)] == "derived_source_missing"
    assert _owner_vis("commitments", other) == (b.user_id, "workspace")
    assert _grant_revoked(grant)


# ---------------------------------------------------------------------------
# SEC-4: member-authored attention text is never republished by a restore
# ---------------------------------------------------------------------------


def _defer(world: GmailSyncWorld, user_id: UUID, item: UUID) -> None:
    client, token = world.harness.client_for(world.workspace_id, user_id)
    deferred = client.post(
        f"/api/v1/attention/{item}/defer",
        json={
            "deferred_until": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
            "reason": "private note about the confidential deal",
        },
        headers=csrf_headers(token, str(uuid4())),
    )
    assert deferred.status_code == 200, deferred.text


def _transferred_email_item(world: GmailSyncWorld) -> UUID:
    """A's email attention item, transferred to the bystander while the flag
    was off (simulated): the backfill gives it back to A, private."""
    item = world.a.attention_item_ids[0]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE attention_items SET owner_id = :by, visibility = 'workspace' WHERE id = :id"
            ),
            {"by": world.bystander_user_id, "id": item},
        )
    return item


def test_restore_skips_an_attention_item_deferred_after_the_backfill(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """A's email attention item had been transferred to the bystander
    (simulated, flag off); the backfill gives it back to A, private; A then
    defers it with a reason. Restoring it would hand A's text to the
    bystander: not moved, reported."""
    a, ws = world.a, world.workspace_id
    item = a.attention_item_ids[0]
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE attention_items SET owner_id = :by, visibility = 'workspace' WHERE id = :id"
            ),
            {"by": world.bystander_user_id, "id": item},
        )
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None and ("attention_items", item) in _log_rows(run_id)
    _defer(world, a.user_id, item)

    _flag(monkeypatch, on=False)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert _unresolved(rows)[("attention_items", item)] == "changed_since_backfill:attention"
    assert _owner_vis("attention_items", item) == (a.user_id, "private")


def test_restore_skips_a_derived_row_whose_attention_item_was_deferred(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """Restoring the task would let the next regenerate move its attention
    item -- A's defer reason included -- to B: the task is not restored."""
    a, b, ws = world.a, world.b, world.workspace_id
    task = _confirm(world, b.user_id, a.recommendation_ids[0])
    item = _insert_attention_item(world, "task", task)
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    _defer(world, a.user_id, item)

    _flag(monkeypatch, on=False)
    _code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert _unresolved(rows)[("tasks", task)] == "changed_since_backfill:attention", err
    assert _owner_vis("tasks", task) == (a.user_id, "private")
    assert _owner_vis("attention_items", item) == (a.user_id, "private")


# ---------------------------------------------------------------------------
# SEC-5 / SEC-6: the flag from the application's settings (.env)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line", ["ECC_PERSONAL_DATA_ISOLATION=true", "export ecc_personal_data_isolation=true"]
)
def test_restore_refuses_when_dotenv_enables_the_flag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    line: str,
) -> None:
    """The flag set only in `.env` (read by the application's settings):
    the restore refuses and names the source. `export ` prefixes and
    lower-case keys are what pydantic-settings accepts too."""
    monkeypatch.setenv("ECC_DATABASE_URL", base.settings.database_url)
    monkeypatch.delenv("ECC_PERSONAL_DATA_ISOLATION", raising=False)
    (tmp_path / ".env").write_text(line + "\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    assert get_settings().personal_data_isolation is True
    assert setting_source("ECC_PERSONAL_DATA_ISOLATION") == ".env file"
    assert backfill.main(["--restore", str(uuid4())]) == backfill.EXIT_ERROR
    err = capsys.readouterr().err
    assert "refusing to restore" in err and "(from .env file)" in err


def test_setting_source_environment_and_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env
    monkeypatch.delenv("ECC_PERSONAL_DATA_ISOLATION", raising=False)
    assert setting_source("ECC_PERSONAL_DATA_ISOLATION") == "default"
    monkeypatch.setenv("ecc_personal_data_isolation", "false")  # any case
    assert setting_source("ECC_PERSONAL_DATA_ISOLATION") == "environment"
    (tmp_path / ".env").write_text("OTHER=1\n", encoding="utf-8")
    monkeypatch.delenv("ecc_personal_data_isolation")
    assert setting_source("ECC_PERSONAL_DATA_ISOLATION") == "default"


# ---------------------------------------------------------------------------
# Round 3: QA3-1, QA3-2, QA3-7, SEC-7, R3-2
# ---------------------------------------------------------------------------


def test_a_manual_fix_to_the_confirmer_sticks_with_an_active_owner(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-1: A (the recommendation's owner) is active. Run 1 re-owns B's
    commitment to A; an operator sets it back to B (simulated UPDATE). The
    next run keeps B -- the rule was already applied to this private row --
    and reports `derived_owner_changed`."""
    a, b, ws = world.a, world.b, world.workspace_id
    commitment = _commitment_confirmed_by_b(world)
    _flag(monkeypatch, on=True)
    _run(capsys, run_ids, "--workspace-id", str(ws))
    assert _owner_vis("commitments", commitment) == (a.user_id, "private")
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE commitments SET owner_id = :b WHERE id = :id"),
            {"b": b.user_id, "id": commitment},
        )
    _code, rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert _unresolved(rows)[("commitments", commitment)] == "derived_owner_changed"
    assert _owner_vis("commitments", commitment) == (b.user_id, "private")


def test_owner_inactive_after_a_restore_and_a_new_run(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-2: run 1 re-owns B's commitment to A (removed) -- `owner_inactive`;
    the restore gives it back to B; run 2 re-owns it to A again and reports
    `owner_inactive` again."""
    a, b, ws = world.a, world.b, world.workspace_id
    commitment = _commitment_confirmed_by_b(world)
    _remove_member(world, a.user_id)
    _flag(monkeypatch, on=True)
    _code, rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    assert _unresolved(rows)[("commitments", commitment)] == "owner_inactive"
    _flag(monkeypatch, on=False)
    _run(capsys, run_ids, "--restore", str(run_id))
    assert _owner_vis("commitments", commitment) == (b.user_id, "workspace")
    _flag(monkeypatch, on=True)
    _code, rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert _unresolved(rows)[("commitments", commitment)] == "owner_inactive"
    assert _owner_vis("commitments", commitment) == (a.user_id, "private")


def test_attention_item_of_an_already_private_derived_row_is_made_private(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-7: the task already has the right owner and is private (the
    flag-on confirm wrote it so), but its attention item still says
    `workspace` (written before): the batch mirrors it anyway."""
    a, ws = world.a, world.workspace_id
    _flag(monkeypatch, on=True)
    task = _confirm(world, a.user_id, a.recommendation_ids[0])
    assert _owner_vis("tasks", task) == (a.user_id, "private")
    item = _insert_attention_item(world, "task", task)  # (B, workspace)
    _code, rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert _counts(rows)["tasks"] == (0, 0, 0)  # the task itself needs nothing
    assert _owner_vis("attention_items", item) == (a.user_id, "private")


def test_skewed_audit_clock_does_not_matter(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SEC-7: the defer's audit event is dated 10 minutes before the
    backfill (an app server whose clock runs slow; simulated). The restore
    still refuses -- it compares the snapshot, not times."""
    a, ws = world.a, world.workspace_id
    item = _transferred_email_item(world)
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    _defer(world, a.user_id, item)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE audit_events SET occurred_at = now() - interval '10 minutes' "
                "WHERE workspace_id = :ws AND aggregate_id = :id"
            ),
            {"ws": ws, "id": item},
        )
    _flag(monkeypatch, on=False)
    _code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert _unresolved(rows)[("attention_items", item)] == "changed_since_backfill:attention", err
    assert _owner_vis("attention_items", item) == (a.user_id, "private")


def test_an_attention_action_before_the_backfill_does_not_block_the_restore(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """R3-2: the bystander deferred the (transferred) item BEFORE the
    backfill; the snapshot holds that, so the restore puts it back."""
    ws = world.workspace_id
    item = _transferred_email_item(world)
    assert world.bystander_user_id is not None
    _defer(world, world.bystander_user_id, item)
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    _flag(monkeypatch, on=False)
    _code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert ("attention_items", item) not in _unresolved(rows), err
    assert _owner_vis("attention_items", item) == (world.bystander_user_id, "workspace")


def test_real_run_refusal_mentions_a_dotenv_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """R3-5: the flag only in `.env` -- the real run still refuses (it reads
    only its own environment) and says so."""
    monkeypatch.setenv("ECC_DATABASE_URL", base.settings.database_url)
    monkeypatch.delenv("ECC_PERSONAL_DATA_ISOLATION", raising=False)
    (tmp_path / ".env").write_text("ECC_PERSONAL_DATA_ISOLATION=true\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    assert backfill.main([]) == backfill.EXIT_ERROR
    err = capsys.readouterr().err
    assert "set ECC_PERSONAL_DATA_ISOLATION=true on this command line" in err
    assert "enabled in .env" in err


# ---------------------------------------------------------------------------
# Round 4: time zones, digests, dismissals, snapshots
# ---------------------------------------------------------------------------


def _backfill_then_flag_off(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> UUID:
    _flag(monkeypatch, on=True)
    _code, _rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert run_id is not None, err
    _flag(monkeypatch, on=False)
    return run_id


def _session_time_zone(monkeypatch: pytest.MonkeyPatch, zone: str) -> None:
    """Every backfill/restore transaction also runs `SET LOCAL TimeZone`
    to `zone` after the command's own settings -- an operator session in
    another zone, as before the command pinned UTC."""
    original = backfill._set_local_timeouts

    def with_zone(conn: Any) -> None:
        original(conn)
        conn.execute(text(f"SET LOCAL TimeZone = '{zone}'"))

    monkeypatch.setattr(backfill, "_set_local_timeouts", with_zone)


@pytest.mark.parametrize(
    ("backfill_zone", "restore_zone"), [(None, "Asia/Kolkata"), ("Asia/Kolkata", None)]
)
def test_restore_matches_snapshots_whatever_the_session_time_zone(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
    backfill_zone: str | None,
    restore_zone: str | None,
) -> None:
    """SEC-10: a snapshot written in one session time zone matches a row
    read in another (timestamps compared parsed, in UTC); with `PGTZ` set,
    the command itself pins UTC."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    item = _transferred_email_item(world)
    _defer(world, world.bystander_user_id or world.b.user_id, item)  # timestamps in the snapshot
    with monkeypatch.context() as zoned:
        if backfill_zone:
            _session_time_zone(zoned, backfill_zone)
        run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    if restore_zone:
        monkeypatch.setenv("PGTZ", restore_zone)
        _session_time_zone(monkeypatch, restore_zone)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert ("tasks", task) not in _unresolved(rows), err
    assert ("attention_items", item) not in _unresolved(rows), err
    assert _owner_vis("tasks", task) == (world.b.user_id, "workspace")
    assert _owner_vis("attention_items", item)[1] == "workspace"


def test_the_log_keeps_a_digest_of_member_text_never_the_text(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SEC-8: `override_reason` is logged as its SHA-256 digest."""
    item = _transferred_email_item(world)
    assert world.bystander_user_id is not None
    _defer(world, world.bystander_user_id, item)
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    state = _log_state(run_id, "attention_items", item)
    assert "override_reason" not in state
    expected = hashlib.sha256(b"private note about the confidential deal").hexdigest()
    assert state["override_reason_sha256"] == expected
    assert "confidential" not in json.dumps(state)


def _task_item_after_regenerate(world: GmailSyncWorld) -> tuple[UUID, UUID]:
    """B confirms A's detected task (flag off) and regenerates attention:
    the task's attention item exists (B, workspace)."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    world.harness.regenerate_attention(workspace_id=world.workspace_id, user_id=world.b.user_id)
    with engine.begin() as conn:
        item = conn.execute(
            text("SELECT id FROM attention_items WHERE entity_type = 'task' AND entity_id = :t"),
            {"t": task},
        ).scalar_one()
    return task, item


def _dismissed(item: UUID) -> bool:
    with engine.begin() as conn:
        return (
            conn.execute(
                text("SELECT dismissed_at FROM attention_items WHERE id = :id"), {"id": item}
            ).scalar_one()
            is not None
        )


def test_a_dismissal_survives_regenerate_after_the_backfill_and_the_restore(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """INT-6: B dismissed the task's item (flag off). The backfill bumps the
    task's version; the dismissal is carried across it, so a regenerate
    keeps it and the restore still matches the snapshot (and carries it
    across its own bump)."""
    task, item = _task_item_after_regenerate(world)
    client, token = world.harness.client_for(world.workspace_id, world.b.user_id)
    dismissed = client.post(
        f"/api/v1/attention/{item}/dismiss", json={}, headers=csrf_headers(token, str(uuid4()))
    )
    assert dismissed.status_code == 200, dismissed.text
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    assert _owner_vis("attention_items", item) == (world.a.user_id, "private")
    world.harness.regenerate_attention(workspace_id=world.workspace_id, user_id=world.a.user_id)
    assert _dismissed(item)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert ("tasks", task) not in _unresolved(rows), err
    assert _owner_vis("tasks", task) == (world.b.user_id, "workspace")
    world.harness.regenerate_attention(workspace_id=world.workspace_id, user_id=world.b.user_id)
    assert _dismissed(item)


def _email_item_action(world: GmailSyncWorld, item: UUID, change: str) -> None:
    """One member field changed on the email item (A owns it after the
    backfill): `dismiss` (dismissed_at only), `defer` (deferred_until
    only), `reason` (override_reason only, via SQL)."""
    client, token = world.harness.client_for(world.workspace_id, world.a.user_id)
    if change == "reason":
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE attention_items SET override_reason = 'x' WHERE id = :id"),
                {"id": item},
            )
        return
    body = (
        {}
        if change == "dismiss"
        else {"deferred_until": (datetime.now(UTC) + timedelta(days=1)).isoformat()}
    )
    response = client.post(
        f"/api/v1/attention/{item}/{change}", json=body, headers=csrf_headers(token, str(uuid4()))
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("change", ["dismiss", "defer", "reason"])
def test_each_attention_member_field_alone_blocks_the_restore(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
    change: str,
) -> None:
    """QA4-2: `dismissed_at`, `deferred_until` and `override_reason` are each
    pinned on their own."""
    item = _transferred_email_item(world)
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    _email_item_action(world, item, change)
    _code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert _unresolved(rows)[("attention_items", item)] == "changed_since_backfill:attention", err
    assert _owner_vis("attention_items", item) == (world.a.user_id, "private")


def test_a_replaced_attention_item_blocks_the_restore(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA4-1: after the backfill the task's attention item is replaced by a
    new one (new id) carrying member text: `:attention`."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    old_item = _insert_attention_item(world, "task", task)
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM attention_items WHERE id = :id"), {"id": old_item})
    new_item = _insert_attention_item(world, "task", task)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE attention_items SET override_reason = 'later', "
                "deferred_until = now() + interval '1 day' WHERE id = :id"
            ),
            {"id": new_item},
        )
    _code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert _unresolved(rows)[("tasks", task)] == "changed_since_backfill:attention", err


@pytest.mark.parametrize(
    "assignment", ["version = version + 1", "updated_at = updated_at + interval '1 second'"]
)
def test_a_raw_content_change_blocks_the_restore(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
    assignment: str,
) -> None:
    """QA4-5: a `version`-only bump, or an `updated_at`-only touch (raw
    SQL), after the backfill: `:content`."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    with engine.begin() as conn:
        conn.execute(text(f"UPDATE tasks SET {assignment} WHERE id = :id"), {"id": task})  # noqa: S608
    _code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert _unresolved(rows)[("tasks", task)] == "changed_since_backfill:content", err
    assert _owner_vis("tasks", task) == (world.a.user_id, "private")


def test_a_row_the_rules_no_longer_resolve_is_reported_as_rule(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA4-4: at restore time the task's source can no longer be resolved
    (simulated): `changed_since_backfill:rule`, not restored."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    monkeypatch.setattr(
        backfill,
        "_derived_sources_sql",
        lambda table: connector_security.email_derived_sources_sql(table) + " AND false",
    )
    _code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert _unresolved(rows)[("tasks", task)] == "changed_since_backfill:rule", err
    assert _owner_vis("tasks", task) == (world.a.user_id, "private")


def test_pre_0084_log_rows_refuse_snapshot_tables_and_fall_back_elsewhere(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SEC-9 / QA4-3: log rows without a snapshot (written before 0084;
    simulated by clearing it): the task is refused `:no_snapshot`; the
    connector account (no snapshot table) is restored by the owner/
    visibility check."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE personal_visibility_backfill_log SET previous_state = NULL "
                "WHERE run_id = :r"
            ),
            {"r": run_id},
        )
    _code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert _unresolved(rows)[("tasks", task)] == "changed_since_backfill:no_snapshot", err
    assert _owner_vis("tasks", task) == (world.a.user_id, "private")
    assert ("connector_accounts", world.b.connector_account_id) not in _unresolved(rows)
    assert _owner_vis("connector_accounts", world.b.connector_account_id)[1] == "workspace"


# docs/SETUP.md's manual restore template (versioned-table variant).
MANUAL_RESTORE_SQL = (
    "UPDATE tasks SET owner_id = :owner, visibility = :visibility, version = version + 1 "
    "WHERE workspace_id = :ws AND id = :id"
)


def test_documented_manual_restore_template(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """SEC-11: the template takes the membership then the attention-
    regenerate lock, bumps `version` on versioned tables and moves the
    attention items with the row, carrying B's dismissal across the bump --
    so it survives the next regenerate."""
    docs = (Path(__file__).parents[1] / "docs" / "SETUP.md").read_text()
    for needle in (
        "restoring publishes whatever was written since the backfill",
        "connector_accounts, entity_aliases), bump the version too",
    ):
        assert needle in docs, needle
    _assert_template_text_in_docs()
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    item = _dismiss_task_item(world, task)
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    params = {"owner": world.b.user_id, "visibility": "workspace", "ws": world.workspace_id}
    with engine.begin() as conn:
        prev = conn.execute(
            text(
                "SELECT previous_owner_id, previous_visibility "
                "FROM personal_visibility_backfill_log WHERE run_id = :r AND row_id = :id"
            ),
            {"r": run_id, "id": task},
        ).one()
        assert (prev[0], prev[1]) == (world.b.user_id, "workspace")
        _template_locks(conn, world.workspace_id)
        conn.execute(text(MANUAL_RESTORE_SQL), {**params, "id": task})
        conn.execute(text(_template_attention_sql("tasks", "task")), {**params, "id": task})
    assert _owner_vis("tasks", task) == (world.b.user_id, "workspace")
    assert _owner_vis("attention_items", item) == (world.b.user_id, "workspace")
    world.harness.regenerate_attention(workspace_id=world.workspace_id, user_id=world.b.user_id)
    assert _dismissed(item)


# ---------------------------------------------------------------------------
# Round 5 (QA5-1, QA5-3, QA5-4)
# ---------------------------------------------------------------------------


def _set_item_versions(item: UUID, source: int, dismissed: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE attention_items SET source_entity_version = :s, "
                "dismissed_entity_version = :d, dismissed_at = now() WHERE id = :id"
            ),
            {"s": source, "d": dismissed, "id": item},
        )


def _item_versions(item: UUID) -> tuple[int, int]:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT source_entity_version, dismissed_entity_version "
                "FROM attention_items WHERE id = :id"
            ),
            {"id": item},
        ).one()
    return int(row[0]), int(row[1])


def test_stale_dismissal_of_an_unchanged_row_is_not_carried(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA5-1: the task is already right (flag-on confirm), so the batch does
    not bump it; an item dismissed at `version - 1` must not be carried."""
    a = world.a
    _flag(monkeypatch, on=True)
    task = _confirm(world, a.user_id, a.recommendation_ids[0])
    version = base._version("tasks", task)
    item = _insert_attention_item(world, "task", task)
    _set_item_versions(item, version - 1, version - 1)
    _code, rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert _counts(rows)["tasks"] == (0, 0, 0)
    assert base._version("tasks", task) == version
    assert _item_versions(item) == (version - 1, version - 1)


def test_a_dismissal_of_an_older_version_is_not_carried_across_the_bump(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA5-1: the backfill bumps the task, but the item's dismissal belongs
    to an older version than the item's (`dismissed < source`): no carry."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    version = base._version("tasks", task)
    item = _insert_attention_item(world, "task", task)
    _set_item_versions(item, version, version - 1)
    _flag(monkeypatch, on=True)
    _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert base._version("tasks", task) == version + 1
    assert _item_versions(item) == (version, version - 1)


def test_no_reason_is_logged_as_null_and_a_bare_replacement_item_restores(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA5-3: an item without a reason has a NULL digest (not the digest of
    ''); a task whose item is replaced by one with no member fields still
    restores (new items only need NULL member fields)."""
    item = _transferred_email_item(world)
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    old_item = _insert_attention_item(world, "task", task)
    run_id = _backfill_then_flag_off(world, monkeypatch, capsys, run_ids)
    assert _log_state(run_id, "attention_items", item)["override_reason_sha256"] is None
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM attention_items WHERE id = :id"), {"id": old_item})
    _insert_attention_item(world, "task", task)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert ("tasks", task) not in _unresolved(rows), err
    assert _owner_vis("tasks", task) == (world.b.user_id, "workspace")


def test_help_prints_the_exit_codes_once(capsys: pytest.CaptureFixture[str]) -> None:
    """QA5-4."""
    with pytest.raises(SystemExit) as raised:
        backfill.main(["--help"])
    assert raised.value.code == 0
    assert capsys.readouterr().out.count("Exit codes") == 1


def test_a_row_made_private_while_its_source_was_unverified_is_reowned_after_the_fix(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """pr-review: run 1 finds A's recommendation owned by B (unverified,
    simulated), so B's task is only made private with its current owner
    (`owner_not_mailbox_owner`); that log entry is not the derived rule's
    own decision (`derived_rule: false`). Once the operator fixes the
    recommendation's owner, run 2 applies the rule: re-owned to A, not
    reported."""
    a, b, ws = world.a, world.b, world.workspace_id
    rec = a.recommendation_ids[0]
    task = _confirm(world, b.user_id, rec)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE recommendations SET owner_id = :b WHERE id = :id"),
            {"b": b.user_id, "id": rec},
        )
    _flag(monkeypatch, on=True)
    _code, rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    assert _unresolved(rows)[("tasks", task)] == "owner_not_mailbox_owner"
    assert _owner_vis("tasks", task) == (b.user_id, "private")
    assert _log_state(run_id, "tasks", task)["derived_rule"] is False
    with engine.begin() as conn:  # the operator's fix
        conn.execute(
            text("UPDATE recommendations SET owner_id = :a WHERE id = :id"),
            {"a": a.user_id, "id": rec},
        )
    _code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert ("tasks", task) not in _unresolved(rows), err
    assert _owner_vis("tasks", task) == (a.user_id, "private")
