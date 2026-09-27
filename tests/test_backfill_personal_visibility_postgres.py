"""Tests for scripts/backfill_personal_visibility.py (Spec A S1.8(b), plan T15).

The data is written by the production Gmail pipeline with
`ECC_PERSONAL_DATA_ISOLATION` OFF (`tests/gmail_sync_fixtures.py`, with a
bystander `owner` who joined first, so every row the default-owner trigger
assigns -- `pkos_evidence`, `entity_aliases`, `ai_run_steps` -- is owned by
someone who never connected Gmail), then the backfill runs with the flag ON.

A few flag-off-era states the fixture cannot produce through production calls
today are simulated by one direct UPDATE/INSERT each, named where they occur:
a sync cursor owned by another member (T14a note: the flag-off cursor upsert
kept the syncing actor), an active `resource_grants` row (created before S1.3
refused personal-data grants), a message purged into
`email_message_id_purge_log` while its evidence survived, and an email
`ai_runs` row whose owner is no longer the mailbox owner.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import re
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from gmail_sync_fixtures import GmailSyncWorld, gmail_sync_world_factory  # noqa: F401
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.platform.connector_security import PERSONAL_ROW_PREDICATES, personal_sql_params

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _load_module() -> ModuleType:
    path = Path("scripts/backfill_personal_visibility.py")
    spec = importlib.util.spec_from_file_location("backfill_personal_visibility", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


backfill = _load_module()

_FLAG = "ECC_PERSONAL_DATA_ISOLATION"
_ALIAS_PREDICATE = (
    "EXISTS (SELECT 1 FROM pkos_evidence ev WHERE ev.workspace_id = entity_aliases.workspace_id "
    "AND ev.id = entity_aliases.source_id AND ev.source_type = 'gmail_sync')"
)
_RUN_ID = re.compile(r"run_id=([0-9a-f-]{36})")


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _drop_cached_settings() -> Iterator[None]:
    # Torn down after `monkeypatch` has restored the environment: never leak
    # a `Settings` cached with the flag on into a later test.
    yield
    get_settings.cache_clear()


@pytest.fixture
def run_ids() -> Iterator[list[UUID]]:
    """Every run id a test produced; their log rows are deleted afterwards
    (`personal_visibility_backfill_log` has no `workspace_id`, so the
    fixture's workspace cleanup cannot reach it)."""
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
    monkeypatch.setenv("ECC_DATABASE_URL", settings.database_url)
    monkeypatch.delenv(_FLAG, raising=False)
    get_settings.cache_clear()
    built: GmailSyncWorld = gmail_sync_world_factory(
        bystander=True, extra_cleanup_tables=("resource_grants",)
    )
    return built


def _flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_FLAG, "true")
    get_settings.cache_clear()


def _run(
    capsys: pytest.CaptureFixture[str], run_ids: list[UUID], *argv: str
) -> tuple[int, list[dict[str, str]], str, UUID | None]:
    code = backfill.main(list(argv))
    captured = capsys.readouterr()
    match = _RUN_ID.search(captured.err)
    run_id = UUID(match.group(1)) if match else None
    if run_id is not None:
        run_ids.append(run_id)
    rows = list(csv.DictReader(io.StringIO(captured.out)))
    return code, rows, captured.err, run_id


def _counts(rows: list[dict[str, str]]) -> dict[str, tuple[int, int, int]]:
    return {
        r["table_name"]: (
            int(r["rows_changed"]),
            int(r["grants_revoked"]),
            int(r["rows_unresolved"]),
        )
        for r in rows
        if r["record"] == "count"
    }


def _unresolved(rows: list[dict[str, str]]) -> dict[tuple[str, UUID], str]:
    return {
        (r["table_name"], UUID(r["row_id"])): r["reason"]
        for r in rows
        if r["record"] == "unresolved"
    }


def _snapshot(workspace_id: UUID) -> dict[tuple[str, UUID], tuple[UUID, str]]:
    """`(table, id) -> (owner_id, visibility)` for every personal-data row
    and every Gmail-derived alias of the workspace."""
    predicates = {**PERSONAL_ROW_PREDICATES, "entity_aliases": _ALIAS_PREDICATE}
    out: dict[tuple[str, UUID], tuple[UUID, str]] = {}
    with engine.begin() as conn:
        for table, predicate in predicates.items():
            for row_id, owner, visibility in conn.execute(
                text(
                    f"SELECT {table}.id, {table}.owner_id, {table}.visibility FROM {table} "  # noqa: S608
                    f"WHERE {table}.workspace_id = :ws AND ({predicate})"
                ),
                {"ws": workspace_id, **personal_sql_params()},
            ).all():
                out[(table, row_id)] = (owner, visibility)
    return out


def _owner_vis(table: str, row_id: UUID) -> tuple[UUID, str]:
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT owner_id, visibility FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        ).one()
    return row[0], row[1]


def _log_rows(run_id: UUID) -> dict[tuple[str, UUID], tuple[str, UUID, int]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name, row_id, previous_visibility, previous_owner_id, "
                "grants_revoked FROM personal_visibility_backfill_log WHERE run_id = :r"
            ),
            {"r": run_id},
        ).all()
    return {(r[0], r[1]): (r[2], r[3], r[4]) for r in rows}


def _grant(world: GmailSyncWorld, resource_type: str, resource_id: UUID) -> UUID:
    """A flag-off-era share (S1.3 now refuses these): A granted read."""
    grant_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at) VALUES "
                "(:id, :ws, :grantee, :rt, :rid, ARRAY['read'], :by, :now)"
            ),
            {
                "id": grant_id,
                "ws": world.workspace_id,
                "grantee": world.a.account_id,
                "rt": resource_type,
                "rid": resource_id,
                "by": world.b.user_id,
                "now": datetime.now(UTC),
            },
        )
    return grant_id


def _grant_revoked(grant_id: UUID) -> bool:
    with engine.begin() as conn:
        return (
            conn.execute(
                text("SELECT revoked_at FROM resource_grants WHERE id = :id"), {"id": grant_id}
            ).scalar_one()
            is not None
        )


def _evidence_by_ref(world: GmailSyncWorld, source_ref: str) -> list[UUID]:
    with engine.begin() as conn:
        return [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT id FROM pkos_evidence WHERE workspace_id = :ws "
                    "AND source_type = 'gmail_sync' AND source_ref = :ref"
                ),
                {"ws": world.workspace_id, "ref": source_ref},
            ).all()
        ]


def _expected_evidence_owner(world: GmailSyncWorld, source_ref: str) -> set[UUID]:
    """Independent statement of the F1 rule for the assertions."""
    message_id = source_ref.removeprefix("gmail:").removeprefix("detect_action:")
    with engine.begin() as conn:
        return {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT owner_id FROM email_messages WHERE workspace_id = :ws "
                    "AND external_message_id = :m UNION SELECT owner_id "
                    "FROM email_message_id_purge_log WHERE workspace_id = :ws "
                    "AND external_message_id = :m"
                ),
                {"ws": world.workspace_id, "m": message_id},
            ).all()
        }


def _purge_message(world: GmailSyncWorld, owner: UUID, external_message_id: str) -> None:
    """Simulates a purged message whose evidence survived: the message row
    is gone and only the purge log names its owner."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM email_messages WHERE workspace_id = :ws AND owner_id = :o "
                "AND external_message_id = :m"
            ),
            {"ws": world.workspace_id, "o": owner, "m": external_message_id},
        )
        conn.execute(
            text(
                "INSERT INTO email_message_id_purge_log (workspace_id, external_message_id, "
                "owner_id, purged_at) VALUES (:ws, :m, :o, now())"
            ),
            {"ws": world.workspace_id, "o": owner, "m": external_message_id},
        )


def _messages(world: GmailSyncWorld, key: str) -> tuple[str, str]:
    """(exclusive message id, colliding message id) of member `key`."""
    ids = [m.external_message_id for m in world.mailboxes[key].messages]
    collide = world.colliding_external_message_id
    return next(i for i in ids if i != collide), collide


# ---------------------------------------------------------------------------
# The backfill
# ---------------------------------------------------------------------------


def test_backfill_assigns_mailbox_owners_privacy_and_revokes_grants(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    a, b, ws = world.a, world.b, world.workspace_id
    bystander = world.bystander_user_id
    assert bystander is not None
    b_only, collide = _messages(world, "b")

    # Flag-off baseline: everything workspace-visible (email_thread
    # attention items have always been written private); trigger-owned rows
    # belong to the bystander (the workspace's original user).
    before = _snapshot(ws)
    assert {vis for (t, _), (_o, vis) in before.items() if t != "attention_items"} == {"workspace"}
    b_step_owners = set()
    with engine.begin() as conn:
        b_step_owners = {
            r[0]
            for r in conn.execute(
                text("SELECT owner_id FROM ai_run_steps WHERE run_id = ANY(:ids)"),
                {"ids": list(b.ai_run_ids)},
            ).all()
        }
    assert b_step_owners == {bystander}

    # Simulated flag-off states (see module docstring).
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE sync_cursors SET owner_id = :a WHERE id = ANY(:ids)"),
            {"a": a.user_id, "ids": list(b.sync_cursor_ids)},
        )
    purged_evidence = _evidence_by_ref(world, f"gmail:{b_only}")
    assert purged_evidence
    _purge_message(world, b.user_id, b_only)
    rec_grant = _grant(world, "recommendations", b.recommendation_ids[0])
    ambiguous_evidence = _evidence_by_ref(world, f"gmail:{collide}")
    assert len(ambiguous_evidence) == 2  # one per mailbox's resolution
    ambiguous_grant = _grant(world, "pkos_evidence", ambiguous_evidence[0])
    alias_grant = _grant(
        world, "entity_aliases", b.entity_alias_ids[world.mailboxes["b"].google_email.casefold()]
    )
    connector_grant = _grant(world, "connector_accounts", b.connector_account_id)
    before = _snapshot(ws)
    assert before[("sync_cursors", b.sync_cursor_ids[0])][0] == a.user_id

    _flag_on(monkeypatch)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert run_id is not None
    after = _snapshot(ws)

    # Connectors: owner unchanged, private.
    for member in (a, b):
        assert _owner_vis("connector_accounts", member.connector_account_id) == (
            member.user_id,
            "private",
        )
        # Runs and cursors: the CONNECTOR owner, private.
        for run in member.sync_run_ids:
            assert _owner_vis("sync_runs", run) == (member.user_id, "private")
        for cursor in member.sync_cursor_ids:
            assert _owner_vis("sync_cursors", cursor) == (member.user_id, "private")
        for item in member.attention_item_ids:
            assert _owner_vis("attention_items", item) == (member.user_id, "private")
        for rec in member.recommendation_ids:
            assert _owner_vis("recommendations", rec) == (member.user_id, "private")
        for run in member.ai_run_ids:
            assert _owner_vis("ai_runs", run) == (member.user_id, "private")
    # Steps follow their parent run.
    with engine.begin() as conn:
        steps = conn.execute(
            text(
                "SELECT s.owner_id, s.visibility, r.owner_id, r.visibility FROM ai_run_steps s "
                "JOIN ai_runs r ON r.id = s.run_id WHERE s.workspace_id = :ws "
                "AND r.task_type = 'email.detect_action'"
            ),
            {"ws": ws},
        ).all()
    assert steps
    assert all((s[0], s[1]) == (s[2], s[3]) for s in steps)
    assert {s[0] for s in steps} == {a.user_id, b.user_id}

    # Evidence: exactly one owner -> that owner, private (incl. via the
    # purge log); zero/several -> unchanged and reported.
    unresolved = _unresolved(rows)
    for (table, row_id), (owner, visibility) in after.items():
        if table != "pkos_evidence":
            continue
        with engine.begin() as conn:
            ref = conn.execute(
                text("SELECT source_ref FROM pkos_evidence WHERE id = :id"), {"id": row_id}
            ).scalar_one()
        owners = _expected_evidence_owner(world, ref)
        if len(owners) == 1:
            assert (owner, visibility) == (next(iter(owners)), "private"), ref
            assert (table, row_id) not in unresolved
        else:
            assert (owner, visibility) == before[(table, row_id)], ref
            assert unresolved[(table, row_id)] == "evidence_owner_ambiguous"
    for evidence_id in purged_evidence:
        assert _owner_vis("pkos_evidence", evidence_id) == (b.user_id, "private")
    colliding = set(ambiguous_evidence) | set(
        _evidence_by_ref(world, f"gmail:detect_action:{collide}")
    )
    assert len(colliding) == 4
    assert {k for k in unresolved if k[0] == "pkos_evidence"} == {
        ("pkos_evidence", e) for e in colliding
    }
    for evidence_id in colliding:
        assert after[("pkos_evidence", evidence_id)] == (bystander, "workspace")

    # Aliases: owner = the mailbox owner via their evidence, visibility
    # stays workspace; aliases over colliding evidence are reported.
    alias_owners = {
        row_id: owner for (table, row_id), (owner, _v) in after.items() if table == "entity_aliases"
    }
    assert {vis for (t, _), (_o, vis) in after.items() if t == "entity_aliases"} == {"workspace"}
    assert b.entity_alias_ids[world.mailboxes["b"].google_email.casefold()] in alias_owners
    moved = [i for i, o in alias_owners.items() if o in (a.user_id, b.user_id)]
    assert moved and all(before[("entity_aliases", i)][0] == bystander for i in moved)
    for (table, row_id), reason in unresolved.items():
        if table == "entity_aliases":
            assert reason == "evidence_owner_ambiguous"
            assert after[(table, row_id)] == before[(table, row_id)]
    assert {t for t, _ in unresolved} == {"pkos_evidence", "entity_aliases"}

    # Grants on personal rows are revoked and counted -- also on the
    # unresolved evidence row (otherwise left unchanged, still reported) --
    # but never on aliases (workspace knowledge).
    assert _grant_revoked(rec_grant)
    assert _grant_revoked(connector_grant)
    assert _grant_revoked(ambiguous_grant)
    assert not _grant_revoked(alias_grant)
    counts = _counts(rows)
    assert counts["recommendations"][1] == 1
    assert counts["connector_accounts"][1] == 1
    assert counts["pkos_evidence"][1] == 1
    assert counts["entity_aliases"][1] == 0
    ambiguous_record = next(
        r for r in rows if r["record"] == "unresolved" and r["row_id"] == str(ambiguous_evidence[0])
    )
    assert ambiguous_record["grants_revoked"] == "1"

    # Log: one row per changed row, previous values as they were.
    log = _log_rows(run_id)
    changed = {k for k in after if after[k] != before[k]}
    assert changed <= set(log)
    for key, (prev_vis, prev_owner, _grants) in log.items():
        assert (prev_owner, prev_vis) == before[key]
    assert log[("recommendations", b.recommendation_ids[0])][2] == 1
    assert sum(c[0] for c in counts.values()) == len(log)

    # Summary: target db without credentials + the rebuild reminder.
    assert "scripts/rebuild_knowledge_projections.py" in err
    assert "@" not in err
    # CSV carries ids and code-defined values only.
    for row in rows:
        assert set(row) == set(backfill.CSV_HEADER)
        assert "@" not in ",".join(row.values())

    # Re-run: a no-op (idempotent), same unresolved set.
    code2, rows2, _err2, run_id2 = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code2 == backfill.EXIT_UNRESOLVED
    assert all(c[0] == 0 and c[1] == 0 for c in _counts(rows2).values())
    assert _unresolved(rows2) == unresolved
    assert run_id2 is not None and _log_rows(run_id2) == {}
    assert _snapshot(ws) == after


def test_sync_run_written_by_another_member_is_reowned_to_connector_owner(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    # Flag off: A (owner role) syncs B's workspace-visible Gmail connector
    # through the real route; the new run is owned by the syncing actor.
    client = world.client("a")
    response = client.post(
        f"/api/v1/engineering/connectors/{world.b.connector_account_id}/sync",
        headers=world.headers("a", idempotency_key=str(uuid4())),
        json={"run_type": "backfill", "resource_type": "message"},
    )
    assert response.status_code == 201, response.text
    run_id_by_a = UUID(response.json()["id"])
    assert _owner_vis("sync_runs", run_id_by_a) == (world.a.user_id, "workspace")

    _flag_on(monkeypatch)
    code, _rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert _owner_vis("sync_runs", run_id_by_a) == (world.b.user_id, "private")


def test_email_run_not_owned_by_its_mailbox_owner_is_reported_and_left_unchanged(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    run = world.b.ai_run_ids[0]
    with engine.begin() as conn:  # simulated: owner is no longer the actor
        conn.execute(
            text("UPDATE ai_runs SET owner_id = :a WHERE id = :id"),
            {"a": world.a.user_id, "id": run},
        )
    _flag_on(monkeypatch)
    code, rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert code == backfill.EXIT_UNRESOLVED
    assert _unresolved(rows)[("ai_runs", run)] == "owner_not_mailbox_owner"
    assert _owner_vis("ai_runs", run) == (world.a.user_id, "workspace")
    # Its steps are not re-owned to the unverified parent owner: they stay
    # as written (the original user, workspace) and are reported too.
    with engine.begin() as conn:
        steps = conn.execute(
            text("SELECT id, owner_id, visibility FROM ai_run_steps WHERE run_id = :id"),
            {"id": run},
        ).all()
    assert steps
    assert {(s[1], s[2]) for s in steps} == {(world.bystander_user_id, "workspace")}
    for step in steps:
        assert _unresolved(rows)[("ai_run_steps", step[0])] == "owner_not_mailbox_owner"


def test_grants_on_unresolved_rows_are_revoked_and_the_rows_otherwise_left_alone(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    ws = world.workspace_id
    _b_only, collide = _messages(world, "b")
    evidence = _evidence_by_ref(world, f"gmail:{collide}")[0]
    rec = world.b.recommendation_ids[0]
    with engine.begin() as conn:  # simulated: owner is no longer the mailbox owner
        conn.execute(
            text("UPDATE recommendations SET owner_id = :a WHERE id = :id"),
            {"a": world.a.user_id, "id": rec},
        )
    grants = [_grant(world, "pkos_evidence", evidence), _grant(world, "recommendations", rec)]
    before = {("pkos_evidence", evidence): _owner_vis("pkos_evidence", evidence)}
    before[("recommendations", rec)] = _owner_vis("recommendations", rec)

    _flag_on(monkeypatch)
    code, dry_rows, _err, _ = _run(capsys, run_ids, "--dry-run", "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED
    assert _counts(dry_rows)["pkos_evidence"][1] == 1
    assert _counts(dry_rows)["recommendations"][1] == 1
    assert not any(_grant_revoked(g) for g in grants)

    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert run_id is not None
    assert all(_grant_revoked(g) for g in grants)
    unresolved = _unresolved(rows)
    assert unresolved[("pkos_evidence", evidence)] == "evidence_owner_ambiguous"
    assert unresolved[("recommendations", rec)] == "owner_not_mailbox_owner"
    log = _log_rows(run_id)
    for key, current in before.items():
        assert _owner_vis(*key) == current  # only the grant went
        assert log[key] == (current[1], current[0], 1)
    assert _counts(rows) == _counts(dry_rows)

    code, rows2, _err, run_id2 = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED
    assert all(c[1] == 0 for c in _counts(rows2).values())
    assert run_id2 is not None and _log_rows(run_id2) == {}

    # Restore treats grant-only entries as already restored.
    monkeypatch.delenv(_FLAG)
    get_settings.cache_clear()
    code, restore_rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_CLEAN, err
    assert not (set(before) & set(_unresolved(restore_rows)))
    for key, current in before.items():
        assert _owner_vis(*key) == current


def test_dry_run_writes_nothing_and_reports_the_same_counts(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    ws = world.workspace_id
    grant = _grant(world, "recommendations", world.b.recommendation_ids[0])
    before = _snapshot(ws)

    # Allowed without the flag (planning before R5).
    code, rows, err, dry_run_id = _run(capsys, run_ids, "--dry-run", "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert "would_change=" in err
    assert "rebuild_knowledge_projections" not in err
    assert _snapshot(ws) == before
    assert not _grant_revoked(grant)
    assert dry_run_id is not None and _log_rows(dry_run_id) == {}
    dry_counts = _counts(rows)
    assert dry_counts["recommendations"][1] == 1
    assert dry_counts["ai_run_steps"][0] > 0  # computed from the parents' targets

    _flag_on(monkeypatch)
    _code, real_rows, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert _counts(real_rows) == dry_counts
    assert _unresolved(real_rows) == _unresolved(rows)


def test_restore_round_trip_restores_visibility_and_owner_but_not_grants(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    ws = world.workspace_id
    grant = _grant(world, "ai_runs", world.b.ai_run_ids[0])
    before = _snapshot(ws)
    _flag_on(monkeypatch)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    assert _snapshot(ws) != before
    assert _grant_revoked(grant)

    # Rollback path: flag off, then restore.
    monkeypatch.delenv(_FLAG)
    get_settings.cache_clear()
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_CLEAN, err
    assert "NOT restored" in err
    assert _snapshot(ws) == before
    assert _grant_revoked(grant)
    assert sum(c[0] for c in _counts(rows).values()) == len(_log_rows(run_id))

    # Idempotent.
    code2, rows2, _err2, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code2 == backfill.EXIT_CLEAN
    assert all(c[0] == 0 for c in _counts(rows2).values())
    assert _snapshot(ws) == before


def test_restore_is_compare_and_set_and_requires_an_active_previous_owner(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    ws, bystander = world.workspace_id, world.bystander_user_id
    assert bystander is not None
    before = _snapshot(ws)
    _flag_on(monkeypatch)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    log = _log_rows(run_id)

    # A row changed after the backfill (made workspace-visible again) is
    # left alone and reported.
    changed_conn = world.b.connector_account_id
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE connector_accounts SET visibility = 'shared_explicitly' WHERE id = :id"),
            {"id": changed_conn},
        )
        # The original user (previous owner of every trigger-owned row) is
        # no longer an active member.
        conn.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed', removed_at = now() "
                "WHERE workspace_id = :ws AND users_id = :u"
            ),
            {"ws": ws, "u": bystander},
        )
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_UNRESOLVED, err
    unresolved = _unresolved(rows)
    assert unresolved[("connector_accounts", changed_conn)] == "changed_since_backfill"
    assert _owner_vis("connector_accounts", changed_conn) == (
        world.b.user_id,
        "shared_explicitly",
    )
    from_bystander = {k for k, (_vis, owner, _g) in log.items() if owner == bystander}
    assert from_bystander
    for key in from_bystander:
        assert unresolved[key] == "previous_owner_inactive"
        assert _owner_vis(*key) != before[key]  # left as the backfill set it
    # Everything else went back.
    for key in set(log) - from_bystander - {("connector_accounts", changed_conn)}:
        assert _owner_vis(*key) == before[key]


def test_restore_of_unknown_run_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], run_ids: list[UUID]
) -> None:
    monkeypatch.setenv("ECC_DATABASE_URL", settings.database_url)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(uuid4()))
    assert code == backfill.EXIT_ERROR
    assert rows == []
    assert "no log rows" in err


def test_engineering_rows_and_other_workspaces_are_untouched(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    now = datetime.now(UTC)
    ws = world.workspace_id
    github_id, github_run = uuid4(), uuid4()
    other_ws, other_user, other_gmail = uuid4(), uuid4(), uuid4()
    other_account: UUID | None = None
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO connector_accounts (id, workspace_id, provider, "
                    "external_account_id, display_name, granted_scopes, encrypted_credentials, "
                    "status, version, created_by, updated_by, created_at, updated_at, owner_id, "
                    "visibility) VALUES (:id, :ws, :p, :ext, 'x', ARRAY[]::text[], :cred, "
                    "'active', 1, :o, :o, :now, :now, :o, 'workspace')"
                ),
                [
                    {
                        "id": github_id,
                        "ws": ws,
                        "p": "github",
                        "ext": f"gh-{github_id}",
                        "cred": b"x",
                        "o": world.b.user_id,
                        "now": now,
                    }
                ],
            )
            conn.execute(
                text(
                    "INSERT INTO sync_runs (id, workspace_id, connector_account_id, run_type, "
                    "status, started_at, created_at, owner_id, visibility) VALUES "
                    "(:id, :ws, :ca, 'backfill', 'succeeded', :now, :now, :o, 'workspace')"
                ),
                {"id": github_run, "ws": ws, "ca": github_id, "now": now, "o": world.a.user_id},
            )
            # A second workspace with a flag-off (workspace-visible) Gmail row.
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, timezone, created_at) "
                    "VALUES (:id, 'Backfill other', 'UTC', :now)"
                ),
                {"id": other_ws, "now": now},
            )
            other_account = create_identity(
                conn,
                workspace_id=other_ws,
                user_id=other_user,
                email=f"t15-other-{other_user}@example.test",
                now=now,
                role="owner",
            )
            conn.execute(
                text(
                    "INSERT INTO connector_accounts (id, workspace_id, provider, "
                    "external_account_id, display_name, granted_scopes, encrypted_credentials, "
                    "status, version, created_by, updated_by, created_at, updated_at, owner_id, "
                    "visibility) VALUES (:id, :ws, 'gmail', :ext, 'x', ARRAY[]::text[], :cred, "
                    "'active', 1, :o, :o, :now, :now, :o, 'workspace')"
                ),
                {
                    "id": other_gmail,
                    "ws": other_ws,
                    "ext": f"other-{other_gmail}@example.test",
                    "cred": b"x",
                    "o": other_user,
                    "now": now,
                },
            )

        _flag_on(monkeypatch)
        code, _rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
        assert code == backfill.EXIT_UNRESOLVED, err
        assert _owner_vis("connector_accounts", github_id) == (world.b.user_id, "workspace")
        assert _owner_vis("sync_runs", github_run) == (world.a.user_id, "workspace")
        assert _owner_vis("connector_accounts", other_gmail) == (other_user, "workspace")
        assert _owner_vis("connector_accounts", world.b.connector_account_id)[1] == "private"

        # Scoped to the other workspace: only its row changes.
        code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(other_ws))
        assert code == backfill.EXIT_CLEAN, err
        assert _counts(rows)["connector_accounts"] == (1, 0, 0)
        assert _owner_vis("connector_accounts", other_gmail) == (other_user, "private")
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM sync_runs WHERE id = :id"), {"id": github_run})
            conn.execute(
                text("DELETE FROM connector_accounts WHERE id = ANY(:ids)"),
                {"ids": [github_id, other_gmail]},
            )
            for table in ("sessions", "workspace_memberships", "users"):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": other_ws},
                )
            conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": other_ws})
            if other_account is not None:
                conn.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": other_account})


def test_single_row_batches_page_through_everything_and_rerun_is_a_no_op(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    ws = world.workspace_id
    _flag_on(monkeypatch)
    code, rows, err, _ = _run(capsys, run_ids, "--dry-run", "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    expected = _counts(rows)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws), "--batch-size", "1")
    assert code == backfill.EXIT_UNRESOLVED, err
    assert _counts(rows) == expected
    assert run_id is not None
    assert len(_log_rows(run_id)) == sum(c[0] for c in expected.values())
    after = _snapshot(ws)
    code, rows2, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws), "--batch-size", "1")
    assert code == backfill.EXIT_UNRESOLVED
    assert all(c[0] == 0 and c[1] == 0 for c in _counts(rows2).values())
    assert _unresolved(rows2) == _unresolved(rows)
    assert _snapshot(ws) == after


def test_concurrent_row_lock_times_out_with_exit_2_and_the_run_id(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    _flag_on(monkeypatch)
    with engine.connect() as holder:
        holder.execute(
            text("SELECT 1 FROM connector_accounts WHERE id = :id FOR UPDATE"),
            {"id": world.a.connector_account_id},
        )
        code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
        holder.rollback()
    assert code == backfill.EXIT_ERROR
    assert rows == []
    assert run_id is not None
    assert "sqlstate=55P03" in err  # lock_not_available (lock_timeout)
    assert f"--restore {run_id}" in err


def test_unknown_workspace_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], run_ids: list[UUID]
) -> None:
    monkeypatch.setenv("ECC_DATABASE_URL", settings.database_url)
    _flag_on(monkeypatch)
    code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(uuid4()))
    assert code == backfill.EXIT_ERROR
    assert rows == []
    assert "workspace not found" in err


def test_refuses_without_the_isolation_flag(
    world: GmailSyncWorld, capsys: pytest.CaptureFixture[str], run_ids: list[UUID]
) -> None:
    before = _snapshot(world.workspace_id)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert code == backfill.EXIT_ERROR
    assert rows == []
    assert run_id is None
    assert "ECC_PERSONAL_DATA_ISOLATION must be enabled" in err
    assert _snapshot(world.workspace_id) == before


@pytest.mark.parametrize("value", ["false", "0", "", "not-a-bool"])
def test_flag_parsing_is_strict(value: str) -> None:
    assert backfill.isolation_flag_enabled({_FLAG: value}) is False


@pytest.mark.parametrize("value", ["true", "1", "yes", "ON"])
def test_flag_parsing_accepts_the_apps_true_values(value: str) -> None:
    assert backfill.isolation_flag_enabled({_FLAG: value}) is True


def test_missing_database_url_refuses_to_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], run_ids: list[UUID]
) -> None:
    monkeypatch.delenv("ECC_DATABASE_URL", raising=False)
    code, rows, err, _ = _run(capsys, run_ids, "--dry-run")
    assert code == backfill.EXIT_ERROR
    assert rows == []
    assert "ECC_DATABASE_URL must be set explicitly" in err


def test_every_personal_data_table_is_backfilled() -> None:
    assert set(PERSONAL_ROW_PREDICATES) <= set(backfill.TABLES)
    assert backfill.TABLES.index("ai_runs") < backfill.TABLES.index("ai_run_steps")


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("gmail:abc", "abc"),
        ("gmail:detect_action:abc", "abc"),
        ("gmail:", None),
        ("gmail:detect_action:", None),
        ("calendar:abc", None),
        (None, None),
    ],
)
def test_message_id_from_source_ref(ref: str | None, expected: str | None) -> None:
    assert backfill.message_id_from_source_ref(ref) == expected


def test_rerun_after_member_removal_never_reowns_rows_to_the_removed_member(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    ws, b = world.workspace_id, world.b
    _flag_on(monkeypatch)
    code, _rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err

    # Removal (flag on) re-owns B's Gmail-derived aliases/nodes but keeps
    # B's messages (DS2), so the F1 rule still resolves them to B.
    response = world.client("a").delete(
        f"/api/v1/identity/workspaces/{ws}/members/{b.user_id}", headers=world.headers("a")
    )
    assert response.status_code == 200, response.text
    before = _snapshot(ws)
    b_owned_before = {k for k, (owner, _v) in before.items() if owner == b.user_id}

    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    after = _snapshot(ws)
    assert {k for k, (owner, _v) in after.items() if owner == b.user_id} == b_owned_before
    assert after == before
    assert run_id is not None and _log_rows(run_id) == {}
    inactive = {k for k, reason in _unresolved(rows).items() if reason == "alias_owner_inactive"}
    assert inactive and {t for t, _ in inactive} == {"entity_aliases"}


def test_first_backfill_after_removal_assigns_personal_rows_to_the_removed_member(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """DS2 steady state: a removed member's personal rows stay theirs,
    private -- only aliases (workspace knowledge) are not handed to them."""
    ws, b = world.workspace_id, world.b
    b_only, _collide = _messages(world, "b")
    b_evidence = _evidence_by_ref(world, f"gmail:{b_only}") + _evidence_by_ref(
        world, f"gmail:detect_action:{b_only}"
    )
    assert b_evidence
    assert all(_owner_vis("pkos_evidence", e)[0] != b.user_id for e in b_evidence)

    _flag_on(monkeypatch)
    response = world.client("a").delete(
        f"/api/v1/identity/workspaces/{ws}/members/{b.user_id}", headers=world.headers("a")
    )
    assert response.status_code == 200, response.text
    before = _snapshot(ws)

    code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    for evidence_id in b_evidence:
        assert _owner_vis("pkos_evidence", evidence_id) == (b.user_id, "private")
    with engine.begin() as conn:
        steps = conn.execute(
            text(
                "SELECT s.owner_id, s.visibility, r.owner_id, r.visibility FROM ai_run_steps s "
                "JOIN ai_runs r ON r.id = s.run_id WHERE r.id = ANY(:ids)"
            ),
            {"ids": list(b.ai_run_ids)},
        ).all()
    assert steps
    assert all(tuple(s) == (b.user_id, "private", b.user_id, "private") for s in steps)
    unresolved = _unresolved(rows)
    assert "owner_inactive" not in unresolved.values()
    inactive_aliases = {k for k, r in unresolved.items() if r == "alias_owner_inactive"}
    assert inactive_aliases and {t for t, _ in inactive_aliases} == {"entity_aliases"}
    for key in inactive_aliases:
        assert _owner_vis(*key) == before[key]  # current owner kept
    with engine.begin() as conn:
        b_aliases = conn.execute(
            text(
                f"SELECT count(*) FROM entity_aliases WHERE workspace_id = :ws "  # noqa: S608
                f"AND owner_id = :u AND {_ALIAS_PREDICATE}"
            ),
            {"ws": ws, "u": b.user_id},
        ).scalar_one()
    assert b_aliases == 0


def test_restore_itemizes_deleted_and_no_longer_personal_rows(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    ws = world.workspace_id
    _flag_on(monkeypatch)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    reclassified = world.b.recommendation_ids[0]
    with engine.begin() as conn:
        deleted = conn.execute(
            text(
                "SELECT row_id FROM personal_visibility_backfill_log WHERE run_id = :r "
                "AND table_name = 'sync_runs' LIMIT 1"
            ),
            {"r": run_id},
        ).scalar_one()
        conn.execute(text("DELETE FROM sync_runs WHERE id = :id"), {"id": deleted})
        conn.execute(
            text("UPDATE recommendations SET recommendation_type = 'reclassified' WHERE id = :id"),
            {"id": reclassified},
        )
    assert ("recommendations", reclassified) in _log_rows(run_id)

    monkeypatch.delenv(_FLAG)
    get_settings.cache_clear()
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id), "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    unresolved = _unresolved(rows)
    assert unresolved[("sync_runs", deleted)] == "deleted"
    assert unresolved[("recommendations", reclassified)] == "no_longer_personal"
    assert _owner_vis("recommendations", reclassified)[1] == "private"  # left alone


def test_restoring_two_runs_newest_first_returns_to_the_original_state(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    ws = world.workspace_id
    original = _snapshot(ws)
    _flag_on(monkeypatch)
    _c, _r, _e, first = _run(capsys, run_ids, "--workspace-id", str(ws), "--batch-size", "1")
    # A flag-off-era write between runs (flag disabled and re-enabled): the
    # connector is workspace-visible again, then a second run fixes it.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE connector_accounts SET visibility = 'workspace' WHERE id = :id"),
            {"id": world.b.connector_account_id},
        )
    _c, _r, _e, second = _run(capsys, run_ids, "--workspace-id", str(ws), "--batch-size", "1")
    assert first is not None and second is not None
    assert list(_log_rows(second)) == [("connector_accounts", world.b.connector_account_id)]

    monkeypatch.delenv(_FLAG)
    get_settings.cache_clear()
    for run_id in (second, first):
        code, _rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id), "--batch-size", "1")
        assert code == backfill.EXIT_CLEAN, err
    assert _snapshot(ws) == original


# ---------------------------------------------------------------------------
# Plan note N21: removing the original user after the backfill
# ---------------------------------------------------------------------------


def test_removing_the_original_user_with_flag_off_gmail_aliases_succeeds_after_backfill(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    bystander = world.bystander_user_id
    assert bystander is not None
    ws = world.workspace_id
    with engine.begin() as conn:
        owned_aliases = conn.execute(
            text(
                f"SELECT count(*) FROM entity_aliases WHERE workspace_id = :ws "  # noqa: S608
                f"AND owner_id = :u AND {_ALIAS_PREDICATE}"
            ),
            {"ws": ws, "u": bystander},
        ).scalar_one()
    assert owned_aliases > 0  # flag-off Gmail aliases belong to the original user

    _flag_on(monkeypatch)
    code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    # Only the aliases over colliding evidence are still the original
    # user's; removal itself re-owns those to their node's owner.
    still_owned = _bystander_gmail_aliases(ws, bystander)
    assert still_owned == {i for (t, i) in _unresolved(rows) if t == "entity_aliases"}
    assert 0 < len(still_owned) < owned_aliases

    client = world.client("a")
    response = client.delete(
        f"/api/v1/identity/workspaces/{ws}/members/{bystander}", headers=world.headers("a")
    )
    assert response.status_code == 200, response.text
    with engine.begin() as conn:
        status = conn.execute(
            text(
                "SELECT wm.status FROM workspace_memberships wm JOIN users u "
                "ON u.account_id = wm.account_id AND u.workspace_id = wm.workspace_id "
                "WHERE u.id = :u"
            ),
            {"u": bystander},
        ).scalar_one()
    assert status == "removed"
    assert _bystander_gmail_aliases(ws, bystander) == set()
    with engine.begin() as conn:
        reassigned = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT aggregate_id FROM audit_events WHERE workspace_id = :ws "
                    "AND event_type = 'entity_alias.ownership_reassigned'"
                ),
                {"ws": ws},
            ).all()
        }
    assert reassigned == still_owned


def _bystander_gmail_aliases(ws: UUID, user_id: UUID) -> set[UUID]:
    with engine.begin() as conn:
        return {
            r[0]
            for r in conn.execute(
                text(
                    f"SELECT id FROM entity_aliases WHERE workspace_id = :ws "  # noqa: S608
                    f"AND owner_id = :u AND {_ALIAS_PREDICATE}"
                ),
                {"ws": ws, "u": user_id},
            ).all()
        }
