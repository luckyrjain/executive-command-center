"""scripts/rebuild_knowledge_projections.py refuses to run with
`ECC_PERSONAL_DATA_ISOLATION` off while `private` evidence exists in scope
(deep review B3 / FX2 deep review A: a flag-off rebuild copies claims backed
by it into shared search text), unless `--allow-without-isolation`. Nothing
else triggers it -- in particular not the backfill log (a full rollback
releases the guard) and not narrowed (`shared_explicitly`) evidence.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG = "ECC_PERSONAL_DATA_ISOLATION"


def _load_module() -> ModuleType:
    path = Path("scripts/rebuild_knowledge_projections.py")
    spec = importlib.util.spec_from_file_location("rebuild_knowledge_projections", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


rebuild = _load_module()


@pytest.fixture(autouse=True)
def _drop_cached_settings() -> Iterator[None]:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _new_workspace(name: str) -> UUID:
    ws = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :n, :now)"),
            {"id": ws, "n": name, "now": datetime.now(UTC)},
        )
    return ws


def _drop_workspace(ws: UUID) -> None:
    with engine.begin() as conn:
        for table in (
            "timeline_entries",
            "retrieval_documents",
            "embedding_projections",
            "pkos_evidence",
            "pkos_nodes",
            "users",
        ):
            conn.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                {"ws": ws},
            )
        conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": ws})


@pytest.fixture
def workspace_id(accounts: list[UUID]) -> Iterator[UUID]:
    """An empty workspace: the rebuild of it is cheap and writes nothing."""
    del accounts  # teardown ordering only
    ws = _new_workspace("Rebuild guard")
    yield ws
    _drop_workspace(ws)


@pytest.fixture
def other_workspace_id(accounts: list[UUID]) -> Iterator[UUID]:
    del accounts  # teardown ordering only
    ws = _new_workspace("Rebuild guard (other)")
    yield ws
    _drop_workspace(ws)


@pytest.fixture
def accounts() -> Iterator[list[UUID]]:
    """Accounts `_seed_evidence` creates; the workspace fixtures depend on
    this one, so it is torn down after them (their users reference it)."""
    created: list[UUID] = []
    yield created
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": created})


def _seed_evidence(ws: UUID, accounts: list[UUID], *, source_type: str, visibility: str) -> None:
    """One `pkos_evidence` row (and the user and node it needs) in `ws`;
    removed with the workspace (`_drop_workspace`)."""
    account, user, node = uuid4(), uuid4(), uuid4()
    accounts.append(account)
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO accounts (id, email, password_hash, display_name, created_at) "
                "VALUES (:id, :email, 'x', 'Guard', :now)"
            ),
            {"id": account, "email": f"rebuild-guard-{account}@example.test", "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO users (id, workspace_id, account_id, created_at) "
                "VALUES (:id, :ws, :account, :now)"
            ),
            {"id": user, "ws": ws, "account": account, "now": now},
        )
        conn.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "created_at, updated_at, owner_id) "
                "VALUES (:id, :ws, 'person', 'Guard', :now, :now, :u)"
            ),
            {"id": node, "ws": ws, "now": now, "u": user},
        )
        conn.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, source_ref, "
                "sha256, captured_at, owner_id, visibility) "
                "VALUES (:id, :ws, :node, :st, 'ref:guard', :sha, :now, :u, :vis)"
            ),
            {
                "id": uuid4(),
                "ws": ws,
                "node": node,
                "st": source_type,
                "sha": "0" * 64,
                "now": now,
                "u": user,
                "vis": visibility,
            },
        )


@pytest.fixture
def backfill_log_row() -> Iterator[None]:
    """One `personal_visibility_backfill_log` row: a backfill has run."""
    run_id = uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO personal_visibility_backfill_log (id, run_id, table_name, row_id, "
                "previous_visibility, previous_owner_id, grants_revoked) "
                "VALUES (:id, :run, 'recommendations', :row, 'workspace', NULL, 0)"
            ),
            {"id": uuid4(), "run": run_id, "row": uuid4()},
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM personal_visibility_backfill_log WHERE run_id = :r"), {"r": run_id}
        )


def _flag(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv(_FLAG, raising=False)
    else:
        monkeypatch.setenv(_FLAG, value)
    get_settings.cache_clear()


def _rebuild(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = rebuild.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_runs_without_the_flag_when_no_private_evidence_exists(
    workspace_id: UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A default flag-off deployment. Scoped to an empty workspace, so it
    holds whatever other tests left in this database."""
    _flag(monkeypatch, None)
    code, out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert code == 0, err
    assert "refusing" not in err
    assert f"{workspace_id}\ttimeline_entries\t" in out


@pytest.mark.usefixtures("backfill_log_row")
def test_backfill_log_rows_alone_do_not_block(
    workspace_id: UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """After a full rollback the log keeps its rows (restore needs them)
    while the evidence is back to `workspace`: the guard must release."""
    _flag(monkeypatch, None)
    code, out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert code == 0, err
    assert "WARNING" not in err


@pytest.mark.parametrize("value", [None, "false"])
def test_refuses_without_the_flag_when_private_evidence_exists(
    workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    for _ in range(2):
        _seed_evidence(workspace_id, accounts, source_type="gmail_sync", visibility="private")
    _flag(monkeypatch, value)
    code, out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert code == rebuild.EXIT_REFUSED == 2
    assert "refusing to run" in err
    assert f"total=2 in 1 workspace(s): {workspace_id}=2" in err
    assert "ECC_PERSONAL_DATA_ISOLATION=true" in err
    assert out == ""  # nothing rebuilt


def test_override_and_flag_on_both_run(
    workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_evidence(workspace_id, accounts, source_type="gmail_sync", visibility="private")
    _flag(monkeypatch, None)
    code, out, err = _rebuild(
        "--workspace-id", str(workspace_id), "--allow-without-isolation", capsys=capsys
    )
    assert code == 0, err
    assert "WARNING" in err and f"{workspace_id}=1" in err
    assert f"{workspace_id}\tretrieval_documents\t" in out

    _flag(monkeypatch, "true")
    code, out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert code == 0, err
    assert "WARNING" not in err
    assert "isolation=on (from environment)" in err


@pytest.mark.parametrize("source_type", ["gmail_sync", "manual"])
def test_narrowed_evidence_does_not_block(
    workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_type: str,
) -> None:
    """`shared_explicitly` evidence -- Gmail-sourced too: a flag-off
    application creates it with an ordinary narrowing grant and already puts
    it in shared search text, so refusing would only block deployments that
    never enable isolation."""
    _seed_evidence(workspace_id, accounts, source_type=source_type, visibility="shared_explicitly")
    _flag(monkeypatch, None)
    code, out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert code == 0, err
    assert f"{workspace_id}\tretrieval_documents\t" in out


def test_evidence_back_to_workspace_after_a_rollback_releases_the_guard(
    workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_evidence(workspace_id, accounts, source_type="gmail_sync", visibility="private")
    _flag(monkeypatch, None)
    assert _rebuild("--workspace-id", str(workspace_id), capsys=capsys)[0] == 2
    with engine.begin() as conn:  # what `--restore` does to backfilled evidence
        conn.execute(
            text("UPDATE pkos_evidence SET visibility = 'workspace' WHERE workspace_id = :ws"),
            {"ws": workspace_id},
        )
    code, _out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert code == 0, err


def test_private_evidence_in_another_workspace_does_not_block_a_scoped_rebuild(
    workspace_id: UUID,
    other_workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_evidence(other_workspace_id, accounts, source_type="gmail_sync", visibility="private")
    _flag(monkeypatch, None)
    code, _out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert code == 0, err
    assert _rebuild("--workspace-id", str(other_workspace_id), capsys=capsys)[0] == 2


def test_unscoped_rebuild_refuses_and_lists_the_workspace(
    workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """QA-2: no `--workspace-id` -- every workspace is in scope."""
    _seed_evidence(workspace_id, accounts, source_type="gmail_sync", visibility="private")
    _flag(monkeypatch, None)
    code, out, err = _rebuild(capsys=capsys)
    assert code == rebuild.EXIT_REFUSED
    assert out == ""
    assert "private pkos_evidence: total=" in err


def test_counts_list_the_top_workspaces_and_the_total() -> None:
    counts = [(uuid4(), 20 - i) for i in range(12)]
    described = rebuild._describe_counts(counts)
    assert f"total={sum(n for _, n in counts)} in 12 workspace(s)" in described
    assert f"{counts[9][0]}=" in described and f"{counts[10][0]}=" not in described
    assert "(+2 more workspaces)" in described


def test_unknown_workspace_and_invalid_uuid_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _flag(monkeypatch, None)
    code, out, err = _rebuild("--workspace-id", str(uuid4()), capsys=capsys)
    assert code == 2
    assert "workspace not found" in err and out == ""
    with pytest.raises(SystemExit) as raised:
        rebuild.main(["--workspace-id", "not-a-uuid"])
    assert raised.value.code == 2


def test_announces_the_database_and_the_flag_source(
    workspace_id: UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _flag(monkeypatch, "false")
    _code, _out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert "isolation=off (from environment)" in err
    assert "password" not in err and "ecc:ecc@" not in err
    assert "name=" in err and "host=" in err
    _flag(monkeypatch, None)
    monkeypatch.chdir(Path(__file__).parent)  # no .env here: the default
    _code, _out, err = _rebuild("--workspace-id", str(workspace_id), capsys=capsys)
    assert "isolation=off (from default)" in err


def test_private_evidence_counts_are_largest_first_under_the_guard_timeout(
    workspace_id: UUID,
    other_workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """QA2-3: the refusal lists the workspace with more private evidence
    first, and the guard's read runs under a 120 s statement timeout
    (checked inside the transaction), which is put back afterwards: the
    guard runs inside the rebuild's own transaction."""
    _seed_evidence(workspace_id, accounts, source_type="gmail_sync", visibility="private")
    for _ in range(2):
        _seed_evidence(other_workspace_id, accounts, source_type="gmail_sync", visibility="private")
    seen: list[str] = []
    with SessionFactory() as session:
        original = session.execute

        def spy(statement: Any, *args: Any, **kwargs: Any) -> Any:
            result = original(statement, *args, **kwargs)
            if "pkos_evidence" in str(statement):
                seen.append(original(text("SHOW statement_timeout")).scalar_one())
            return result

        monkeypatch.setattr(session, "execute", spy)
        before = original(text("SHOW statement_timeout")).scalar_one()
        counts = rebuild.private_evidence_counts(session, None)
        # Same transaction as the rebuild: the session's own timeout is
        # back for what follows.
        assert original(text("SHOW statement_timeout")).scalar_one() == before
        session.rollback()
    ours = [ws for ws, _n in counts if ws in (workspace_id, other_workspace_id)]
    assert ours == [other_workspace_id, workspace_id]
    assert dict(counts)[other_workspace_id] == 2 and dict(counts)[workspace_id] == 1
    assert seen == ["2min"]
