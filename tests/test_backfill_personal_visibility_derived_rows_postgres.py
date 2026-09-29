"""scripts/backfill_personal_visibility.py: tasks / commitments / risks created
by confirming an `email_action_detected` recommendation while
`ECC_PERSONAL_DATA_ISOLATION` was off (deep review F1, probe P1).

With the flag on, the confirm writes the derived row with the
recommendation's owner and `private` (plan note N23); rows confirmed before
that stayed `workspace`-visible after the backfill until this rule. The rows
are written through the production confirm route with the flag OFF; the
commitment/risk recommendations are built through `create_recommendation`
(the Gmail detector only proposes tasks in the fixture). A flag-off-era
share, a later ownership transfer and a deleted target are simulated by one
direct INSERT/UPDATE/DELETE each, named where they occur.
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
from gmail_sync_fixtures import GmailSyncWorld, csrf_headers, gmail_sync_world_factory  # noqa: F401
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.governance.recommendation_models import RecommendationCreate
from ecc.domains.governance.recommendation_mutations import (
    create_recommendation,
    synthetic_request,
)
from ecc.platform.connector_security import (
    PERSONAL_DERIVED_PREDICATES,
    email_derived_sources_sql,
)

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
_RUN_ID = re.compile(r"run_id=([0-9a-f-]{36})")
_DERIVED_TABLES = ("tasks", "commitments", "risks")
_ROUTE = {"tasks": "tasks", "commitments": "commitments", "risks": "risks"}
_FIELDS: dict[str, dict[str, Any]] = {
    "commitment": {"summary": "Send the confidential figures", "direction": "made_by_me"},
    "risk": {"description": "Confidential deal may slip", "probability": 3, "impact": 4},
}


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


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
    monkeypatch.setenv("ECC_DATABASE_URL", settings.database_url)
    monkeypatch.delenv(_FLAG, raising=False)
    get_settings.cache_clear()
    built: GmailSyncWorld = gmail_sync_world_factory(
        bystander=True,
        extra_cleanup_tables=("resource_grants", "recommendation_feedback", "attention_feedback"),
    )
    return built


def _flag(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    if on:
        monkeypatch.setenv(_FLAG, "true")
    else:
        monkeypatch.delenv(_FLAG, raising=False)
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
    return code, list(csv.DictReader(io.StringIO(captured.out))), captured.err, run_id


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


def _owner_vis(table: str, row_id: UUID) -> tuple[UUID, str]:
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT owner_id, visibility FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        ).one()
    return row[0], row[1]


def _version(table: str, row_id: UUID) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(f"SELECT version FROM {table} WHERE id = :id"),  # noqa: S608
                {"id": row_id},
            ).scalar_one()
        )


def _snapshot(world: GmailSyncWorld) -> dict[tuple[str, UUID], tuple[UUID, str]]:
    out: dict[tuple[str, UUID], tuple[UUID, str]] = {}
    with engine.begin() as conn:
        for table in _DERIVED_TABLES:
            for row_id, owner, visibility in conn.execute(
                text(
                    f"SELECT id, owner_id, visibility FROM {table} WHERE workspace_id = :ws"  # noqa: S608
                ),
                {"ws": world.workspace_id},
            ).all():
                out[(table, row_id)] = (owner, visibility)
    return out


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
    """A flag-off-era share: the row's owner granted B read."""
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
                "grantee": world.b.account_id,
                "rt": resource_type,
                "rid": resource_id,
                "by": world.a.user_id,
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


def _rec_version(rec_id: UUID) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT version FROM recommendations WHERE id = :id"), {"id": rec_id}
            ).scalar_one()
        )


def _confirm(
    world: GmailSyncWorld,
    user_id: UUID,
    rec_id: UUID,
    *,
    target_expected_version: int | None = None,
) -> UUID:
    """Publish + confirm through the production routes; the target id."""
    client, token = world.harness.client_for(world.workspace_id, user_id)
    published = client.post(
        f"/api/v1/recommendations/{rec_id}/publish",
        json={"expected_version": _rec_version(rec_id)},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert published.status_code == 200, published.text
    confirmed = client.post(
        f"/api/v1/recommendations/{rec_id}/confirm",
        json={
            "expected_version": _rec_version(rec_id),
            "target_expected_version": target_expected_version,
        },
        headers=csrf_headers(token, str(uuid4())),
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "executed"
    return UUID(confirmed.json()["execution_result"]["target_id"])


def _email_rec(
    world: GmailSyncWorld,
    owner: UUID,
    target_type: str,
    *,
    fields: dict[str, Any] | None = None,
    target_id: UUID | None = None,
    action: dict[str, Any] | None = None,
) -> UUID:
    """A flag-off-era email recommendation of `owner`'s (workspace-visible),
    built the way the Gmail hook builds it."""
    with SessionFactory() as session:
        created = create_recommendation(
            session,
            AuthContext(workspace_id=world.workspace_id, user_id=owner, timezone="UTC"),
            RecommendationCreate(
                recommendation_type="email_action_detected",
                target_type=target_type,
                target_id=target_id,
                proposed_action=action or {"operation": "create", "value": None},
                proposed_fields=fields,
                expected_version=1 if target_id is not None else None,
                rationale="Detected action item.",
                confidence=0.8,
                evidence_ids=[],
                source="ai",
            ),
            synthetic_request(uuid4(), uuid4()),
            f"derived-backfill-test:{uuid4()}",
            visibility="workspace",
        )
    return created.id


def _readable(world: GmailSyncWorld, viewer: UUID, table: str, row_id: UUID) -> bool:
    client, _token = world.harness.client_for(world.workspace_id, viewer)
    detail = client.get(f"/api/v1/{_ROUTE[table]}/{row_id}")
    listed = client.get(f"/api/v1/{_ROUTE[table]}", params={"limit": 100})
    assert listed.status_code == 200, listed.text
    in_list = str(row_id) in {item["id"] for item in listed.json()["items"]}
    assert (detail.status_code == 200) == in_list, (table, detail.status_code, in_list)
    return in_list


def _search_hits(world: GmailSyncWorld, viewer: UUID, query: str, row_id: UUID) -> int:
    client, _token = world.harness.client_for(world.workspace_id, viewer)
    found = client.get("/api/v1/search", params={"q": query})
    assert found.status_code == 200, found.text
    return sum(1 for i in found.json().get("items", []) if i["entity_id"] == str(row_id))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_flag_off_confirmed_email_targets_become_private_to_the_recommendation_owner(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    a, b, ws = world.a, world.b, world.workspace_id
    bystander = world.bystander_user_id
    assert bystander is not None

    # Flag off: A confirms their own detected task (probe P1); B confirms
    # A's commitment (the flag-off confirm writes the CONFIRMING member as
    # owner); A confirms a risk.
    task = _confirm(world, a.user_id, a.recommendation_ids[0])
    commitment = _confirm(
        world, b.user_id, _email_rec(world, a.user_id, "commitment", fields=_FIELDS["commitment"])
    )
    risk = _confirm(world, a.user_id, _email_rec(world, a.user_id, "risk", fields=_FIELDS["risk"]))
    derived = {("tasks", task): a, ("commitments", commitment): b, ("risks", risk): a}
    for key, confirmer in derived.items():
        assert _owner_vis(*key) == (confirmer.user_id, "workspace")
    task_grant = _grant(world, "tasks", task)

    # Untouched: a task from a non-email recommendation, and a task an
    # email recommendation only changed (operation set_status, not create).
    client, token = world.harness.client_for(ws, a.user_id)
    generic = client.post(
        "/api/v1/recommendations",
        headers=csrf_headers(token, str(uuid4())),
        json={
            "recommendation_type": "task_detected",
            "target_type": "task",
            "target_id": None,
            "proposed_action": {"operation": "create", "value": None},
            "proposed_fields": {"title": "Generic task"},
            "rationale": "Detected.",
            "confidence": 0.8,
            "evidence_ids": [],
            "source": "ai",
        },
    )
    assert generic.status_code == 201, generic.text
    generic_task = _confirm(world, a.user_id, UUID(generic.json()["id"]))
    manual = client.post(
        "/api/v1/tasks", json={"title": "Manual task"}, headers=csrf_headers(token, str(uuid4()))
    )
    assert manual.status_code == 201, manual.text
    manual_task = UUID(manual.json()["id"])
    status_rec = _email_rec(
        world,
        a.user_id,
        "task",
        target_id=manual_task,
        action={"operation": "set_status", "value": "in_progress"},
    )
    assert _confirm(world, a.user_id, status_rec, target_expected_version=1) == manual_task
    untouched_grant = _grant(world, "tasks", generic_task)

    before = _snapshot(world)
    for key in derived:
        assert _readable(world, bystander, *key)
    assert _search_hits(world, b.user_id, "Reply to the request", task) == 1

    # Dry run (flag off is fine): counts, nothing written.
    code, dry_rows, err, dry_run_id = _run(capsys, run_ids, "--dry-run", "--workspace-id", str(ws))
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert _snapshot(world) == before
    assert not _grant_revoked(task_grant)
    assert dry_run_id is not None and _log_rows(dry_run_id) == {}
    dry_counts = _counts(dry_rows)
    assert dry_counts["tasks"] == (1, 1, 0)
    assert dry_counts["commitments"] == (1, 0, 0)
    assert dry_counts["risks"] == (1, 0, 0)

    _flag(monkeypatch, on=True)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert run_id is not None
    assert _counts(rows) == dry_counts
    assert not {k for k in _unresolved(rows) if k[0] in _DERIVED_TABLES}

    # Owner = the recommendation's owner (A), private, grant revoked, version bumped.
    for key in derived:
        assert _owner_vis(*key) == (a.user_id, "private"), key
        assert _version(*key) == 2, key
        for viewer in (b.user_id, bystander):
            assert not _readable(world, viewer, *key), (key, viewer)
        assert _readable(world, a.user_id, *key)
    assert _search_hits(world, b.user_id, "Reply to the request", task) == 0
    assert _search_hits(world, bystander, "Reply to the request", task) == 0
    assert _grant_revoked(task_grant)
    assert not _grant_revoked(untouched_grant)
    after = _snapshot(world)
    for key in set(before) - set(derived):
        assert after[key] == before[key], key
    assert after[("tasks", generic_task)] == (a.user_id, "workspace")
    assert after[("tasks", manual_task)] == (a.user_id, "workspace")

    # Logged in the same run: previous values, target table as table_name.
    log = _log_rows(run_id)
    for key, confirmer in derived.items():
        assert log[key] == ("workspace", confirmer.user_id, 1 if key[0] == "tasks" else 0)
    assert not {k for k in log if k[0] in _DERIVED_TABLES} - set(derived)

    # Re-run: no-op.
    code, rows2, _err, run_id2 = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert all(_counts(rows2)[t] == (0, 0, 0) for t in _DERIVED_TABLES)
    assert run_id2 is not None and _log_rows(run_id2) == {}
    assert _snapshot(world) == after

    # Restore (rollback: flag off): previous owner and visibility back,
    # the revoked grant is not restored.
    _flag(monkeypatch, on=False)
    code, restore_rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_CLEAN, err
    assert all(_counts(restore_rows)[t][0] == 1 for t in _DERIVED_TABLES)
    assert _snapshot(world) == before
    assert _grant_revoked(task_grant)
    code, restore_rows2, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_CLEAN, err
    assert all(_counts(restore_rows2)[t][0] == 0 for t in _DERIVED_TABLES)


def test_reowned_derived_row_is_made_private_with_its_current_owner_and_reported(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """Re-owned since the confirm (an ownership transfer; simulated): the
    new owner may have been chosen by the mailbox owner, or the previous
    one removed -- so no re-own, but private + reported."""
    a, ws = world.a, world.workspace_id
    bystander = world.bystander_user_id
    assert bystander is not None
    task = _confirm(world, a.user_id, a.recommendation_ids[0])
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET owner_id = :o WHERE id = :id"), {"o": bystander, "id": task}
        )

    _flag(monkeypatch, on=True)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert run_id is not None
    assert _unresolved(rows)[("tasks", task)] == "derived_owner_changed"
    assert _counts(rows)["tasks"] == (1, 0, 1)  # made private AND reported
    assert _owner_vis("tasks", task) == (bystander, "private")
    assert not _readable(world, world.b.user_id, "tasks", task)
    assert _log_rows(run_id)[("tasks", task)] == ("workspace", bystander, 0)

    # Reported again on the next run until the owner is fixed; nothing changes.
    code, rows2, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED
    assert _unresolved(rows2)[("tasks", task)] == "derived_owner_changed"
    assert _counts(rows2)["tasks"] == (0, 0, 1)

    # The restore's compare-and-set rebuilds the same target.
    _flag(monkeypatch, on=False)
    code, restore_rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_CLEAN, err
    assert _owner_vis("tasks", task) == (bystander, "workspace")


def test_derived_row_of_an_unverified_recommendation_keeps_its_owner_and_is_reported(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """The recommendation is reported `owner_not_mailbox_owner` (its owner
    is no longer its creator; simulated): the task is never re-owned to that
    unverified owner -- private with its current owner, same reason."""
    a, b, ws = world.a, world.b, world.workspace_id
    rec = a.recommendation_ids[0]
    task = _confirm(world, a.user_id, rec)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE recommendations SET owner_id = :b WHERE id = :id"),
            {"b": b.user_id, "id": rec},
        )

    _flag(monkeypatch, on=True)
    code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    unresolved = _unresolved(rows)
    assert unresolved[("recommendations", rec)] == "owner_not_mailbox_owner"
    assert unresolved[("tasks", task)] == "owner_not_mailbox_owner"
    assert _owner_vis("recommendations", rec) == (b.user_id, "private")
    assert _owner_vis("tasks", task) == (a.user_id, "private")


def test_restore_leaves_rows_transferred_after_the_backfill_and_reports_them(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """Review round 1 (Lens A): after the backfill, (1) a derived task is
    transferred on its own, (2) a risk is transferred together with its
    recommendation (both simulated). The rules would now keep each new
    owner, so a compare-and-set against "the rules' owner" matched
    trivially and the restore handed the rows back to their pre-backfill
    owners. Rows whose owner the backfill kept compare against the LOGGED
    previous owner instead: both are left alone and reported. An untouched
    derived row is still restored."""
    a, b, ws = world.a, world.b, world.workspace_id
    bystander = world.bystander_user_id
    assert bystander is not None
    task = _confirm(world, b.user_id, a.recommendation_ids[0])  # flag-off owner: B
    risk_rec = _email_rec(world, a.user_id, "risk", fields=_FIELDS["risk"])
    risk = _confirm(world, a.user_id, risk_rec)
    commitment = _confirm(
        world, b.user_id, _email_rec(world, a.user_id, "commitment", fields=_FIELDS["commitment"])
    )

    _flag(monkeypatch, on=True)
    code, _rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None, err
    for key in (("tasks", task), ("risks", risk), ("commitments", commitment)):
        assert _owner_vis(*key) == (a.user_id, "private"), key
    assert _owner_vis("recommendations", risk_rec) == (a.user_id, "private")

    with engine.begin() as conn:  # post-backfill transfers (simulated)
        for table, row_id in (("tasks", task), ("risks", risk), ("recommendations", risk_rec)):
            conn.execute(
                text(f"UPDATE {table} SET owner_id = :o WHERE id = :id"),  # noqa: S608
                {"o": bystander, "id": row_id},
            )

    _flag(monkeypatch, on=False)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_UNRESOLVED, err
    unresolved = _unresolved(rows)
    for key in (("tasks", task), ("risks", risk), ("recommendations", risk_rec)):
        assert unresolved[key] == "changed_since_backfill", key
        assert _owner_vis(*key) == (bystander, "private"), key
    assert ("commitments", commitment) not in unresolved
    assert _owner_vis("commitments", commitment) == (b.user_id, "workspace")


def test_derived_row_of_a_removed_member_stays_private_to_them_and_is_reported(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """Review round 1 (Lens A): the recommendation's owner (A) is no longer
    an active member (removed while the flag was off; simulated). The
    commitment B confirmed from A's recommendation becomes A's and private
    -- like the rest of a removed member's personal data (DS2, as FX3
    leaves it on removal), never an admin's -- and the run reports it
    `owner_inactive`. The next run has nothing to report for it; the
    restore gives it back to B (still active)."""
    a, b, ws = world.a, world.b, world.workspace_id
    bystander = world.bystander_user_id
    assert bystander is not None
    commitment = _confirm(
        world, b.user_id, _email_rec(world, a.user_id, "commitment", fields=_FIELDS["commitment"])
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed', removed_at = now() "
                "WHERE workspace_id = :ws AND users_id = :u"
            ),
            {"ws": ws, "u": a.user_id},
        )

    _flag(monkeypatch, on=True)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code == backfill.EXIT_UNRESOLVED, err
    assert run_id is not None
    assert _unresolved(rows)[("commitments", commitment)] == "owner_inactive"
    assert _counts(rows)["commitments"] == (1, 0, 1)  # re-owned AND reported
    assert _owner_vis("commitments", commitment) == (a.user_id, "private")
    for viewer in (b.user_id, bystander):
        assert not _readable(world, viewer, "commitments", commitment), viewer
    assert _log_rows(run_id)[("commitments", commitment)] == ("workspace", b.user_id, 0)

    _code, rows2, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert ("commitments", commitment) not in _unresolved(rows2)
    assert _counts(rows2)["commitments"] == (0, 0, 0)

    _flag(monkeypatch, on=False)
    _code, restore_rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert ("commitments", commitment) not in _unresolved(restore_rows), err
    assert _owner_vis("commitments", commitment) == (b.user_id, "workspace")


def _attention_feedback(world: GmailSyncWorld, actor: UUID, item_id: UUID) -> UUID:
    client, token = world.harness.client_for(world.workspace_id, actor)
    response = client.post(
        f"/api/v1/attention/{item_id}/feedback",
        json={"label": "useful"},
        headers=csrf_headers(token, str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def test_feedback_on_email_rows_loses_its_grants_and_keeps_owner_and_visibility(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """`recommendation_feedback` / `attention_feedback` on email rows are
    in `PERSONAL_DERIVED_PREDICATES` (share-refused with the flag on). A
    flag-off-era grant on them is revoked and logged; owner and visibility
    stay as the flag-on writers write them (actor, `workspace`). Feedback
    on a non-email target keeps its grant."""
    a, ws = world.a, world.workspace_id
    rec = a.recommendation_ids[0]
    _confirm(world, a.user_id, rec)  # writes A's `accept` feedback
    with engine.begin() as conn:
        rec_feedback = conn.execute(
            text("SELECT id FROM recommendation_feedback WHERE recommendation_id = :r"),
            {"r": rec},
        ).scalar_one()
    item_feedback = _attention_feedback(world, a.user_id, a.attention_item_ids[0])
    other_feedback = uuid4()
    with engine.begin() as conn:  # same shape, target not an email attention item
        conn.execute(
            text(
                "INSERT INTO attention_feedback (id, workspace_id, target_type, target_id, "
                "label, actor_id, policy_version, created_at, owner_id, visibility) "
                "VALUES (:id, :ws, 'attention_item', :target, 'useful', :a, 1, now(), :a, "
                "'workspace')"
            ),
            {"id": other_feedback, "ws": ws, "target": uuid4(), "a": a.user_id},
        )
    grants = {
        ("recommendation_feedback", rec_feedback): _grant(
            world, "recommendation_feedback", rec_feedback
        ),
        ("attention_feedback", item_feedback): _grant(world, "attention_feedback", item_feedback),
    }
    other_grant = _grant(world, "attention_feedback", other_feedback)

    _flag(monkeypatch, on=True)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None, err
    counts = _counts(rows)
    assert counts["recommendation_feedback"] == (1, 1, 0)
    assert counts["attention_feedback"] == (1, 1, 0)
    log = _log_rows(run_id)
    for key, grant_id in grants.items():
        assert _grant_revoked(grant_id), key
        assert _owner_vis(*key) == (a.user_id, "workspace"), key
        assert log[key] == ("workspace", a.user_id, 1), key
    assert not _grant_revoked(other_grant)
    assert ("attention_feedback", other_feedback) not in log

    _code, rows2, _err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert _counts(rows2)["recommendation_feedback"] == (0, 0, 0)
    assert _counts(rows2)["attention_feedback"] == (0, 0, 0)

    _flag(monkeypatch, on=False)
    code, restore_rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    assert code == backfill.EXIT_CLEAN, err
    for key in grants:
        assert key not in _unresolved(restore_rows)
        assert _owner_vis(*key) == (a.user_id, "workspace")


def test_deleted_target_is_skipped(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """The recommendation's target no longer exists (simulated delete):
    nothing to protect, nothing reported, no error."""
    a, ws = world.a, world.workspace_id
    rec = a.recommendation_ids[0]
    task = _confirm(world, a.user_id, rec)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM tasks WHERE id = :id"), {"id": task})

    _flag(monkeypatch, on=True)
    code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert _counts(rows)["tasks"] == (0, 0, 0)
    assert not {k for k in _unresolved(rows) if k[0] in _DERIVED_TABLES}
    assert _owner_vis("recommendations", rec) == (a.user_id, "private")


def test_flag_on_confirmed_targets_need_no_change(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """With the flag on the confirm already writes (owner, private) -- also
    when another member confirms -- so the backfill leaves it alone."""
    a, b, ws = world.a, world.b, world.workspace_id
    _flag(monkeypatch, on=True)
    task = _confirm(world, b.user_id, a.recommendation_ids[0])
    assert _owner_vis("tasks", task) == (a.user_id, "private")
    code, rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert _counts(rows)["tasks"] == (0, 0, 0)


def test_derived_tables_are_processed_and_restorable() -> None:
    for table in _DERIVED_TABLES:
        assert table in backfill.TABLES
        assert backfill.TABLES.index("recommendations") < backfill.TABLES.index(table)
    assert backfill._uncovered_tables() == set()


def test_refuses_to_run_and_names_tables_without_a_rule(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ECC_DATABASE_URL", settings.database_url)
    monkeypatch.setattr(backfill, "_uncovered_tables", lambda: {"new_personal_table"})
    assert backfill.main(["--dry-run"]) == backfill.EXIT_ERROR
    err = capsys.readouterr().err
    assert "refusing to run" in err
    assert "new_personal_table" in err


@pytest.mark.parametrize(
    "table", [*_DERIVED_TABLES, "recommendation_feedback", "attention_feedback"]
)
def test_derived_rows_come_from_the_single_source_without_a_per_row_subquery(table: str) -> None:
    """Review round 1 (Lens B): the batch walks the rows with FX3's own
    `PERSONAL_DERIVED_PREDICATES` fragment (one top-level EXISTS, a
    semi-join) -- no copied predicate text, no correlated scalar subquery
    in the select list."""
    sql = backfill._batch_sql(table, lock=True)
    assert f"AND ({PERSONAL_DERIVED_PREDICATES[table]}) AND {table}.id > :after" in sql
    select_list = sql.split(f" FROM {table} WHERE ", 1)[0]
    assert "SELECT" not in select_list.removeprefix("SELECT ")
    assert "recommendations" not in select_list
    if table in _DERIVED_TABLES:
        # The per-batch source query shares the fragment's
        # recommendation-side conditions verbatim.
        conditions = (
            PERSONAL_DERIVED_PREDICATES[table]
            .split(f"= {table}.id::text AND ", 1)[1]
            .removesuffix(")")
        )
        assert email_derived_sources_sql(table).endswith(conditions)


# ---------------------------------------------------------------------------
# The rebuild reminder (deep review B3)
# ---------------------------------------------------------------------------


def test_backfill_prints_the_rebuild_command_with_the_isolation_flag(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    _flag(monkeypatch, on=True)
    _code, _rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert run_id is not None
    reminder = next(line for line in err.splitlines() if line.startswith("REMINDER"))
    assert (
        "ECC_PERSONAL_DATA_ISOLATION=true uv run python scripts/rebuild_knowledge_projections.py "
        "--workspace-id <UUID>" in reminder
    )
    assert "--allow-without-isolation" not in reminder

    _flag(monkeypatch, on=False)
    _code, _rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    reminder = next(line for line in err.splitlines() if line.startswith("REMINDER"))
    assert "rebuild_knowledge_projections.py --workspace-id <UUID> --allow-without-isolation" in (
        reminder
    )
    assert "ECC_PERSONAL_DATA_ISOLATION=true" not in reminder
