"""scripts/rebuild_knowledge_projections.py refuses to run with
`ECC_PERSONAL_DATA_ISOLATION` off once the personal-data backfill has run
(deep review B3: the flag-off rebuild writes claims backed by private
evidence back into shared search text), unless `--allow-without-isolation`.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.config import get_settings
from ecc.database import engine

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
def without_backfill_log(
    monkeypatch: pytest.MonkeyPatch, workspace_id: UUID, other_workspace_id: UUID
) -> Iterator[None]:
    """The rebuild runs on one connection whose transaction has emptied
    `personal_visibility_backfill_log` and is rolled back afterwards -- so
    "no backfill has run" holds whatever rows other tests or runs left in
    this database, and nothing is deleted for real. Depends on both
    workspace fixtures so it is rolled back before either is dropped (a
    rebuild that wrongly ran against one holds locks there until then)."""
    del workspace_id, other_workspace_id
    with engine.connect() as conn:
        transaction = conn.begin()
        conn.execute(text("DELETE FROM personal_visibility_backfill_log"))
        monkeypatch.setattr(
            rebuild,
            "SessionFactory",
            lambda: Session(bind=conn, join_transaction_mode="create_savepoint"),
        )
        try:
            yield
        finally:
            transaction.rollback()


@pytest.fixture
def backfill_log_row() -> Iterator[None]:
    """One `personal_visibility_backfill_log` row: the backfill has run."""
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


@pytest.mark.usefixtures("backfill_log_row")
@pytest.mark.parametrize("value", [None, "false"])
def test_refuses_after_a_backfill_without_the_isolation_flag(
    workspace_id: UUID,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    _flag(monkeypatch, value)
    code = rebuild.main(["--workspace-id", str(workspace_id)])
    captured = capsys.readouterr()
    assert code == rebuild.EXIT_REFUSED == 2
    assert "refusing to run" in captured.err
    assert "ECC_PERSONAL_DATA_ISOLATION=true" in captured.err
    assert captured.out == ""  # nothing rebuilt


@pytest.mark.usefixtures("backfill_log_row")
def test_runs_after_a_backfill_with_the_isolation_flag(
    workspace_id: UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _flag(monkeypatch, "true")
    code = rebuild.main(["--workspace-id", str(workspace_id)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert f"{workspace_id}\tretrieval_documents\t" in captured.out
    assert "WARNING" not in captured.err


@pytest.mark.usefixtures("backfill_log_row")
def test_override_runs_without_the_flag_with_a_warning(
    workspace_id: UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _flag(monkeypatch, None)
    code = rebuild.main(["--workspace-id", str(workspace_id), "--allow-without-isolation"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "WARNING" in captured.err
    assert f"{workspace_id}\tretrieval_documents\t" in captured.out


@pytest.mark.usefixtures("without_backfill_log")
def test_runs_without_the_flag_before_any_backfill(
    workspace_id: UUID, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A default flag-off deployment: no backfill, no private evidence."""
    _flag(monkeypatch, None)
    code = rebuild.main(["--workspace-id", str(workspace_id)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "refusing" not in captured.err
    assert f"{workspace_id}\ttimeline_entries\t" in captured.out


@pytest.mark.usefixtures("without_backfill_log")
def test_refuses_without_the_flag_when_private_evidence_exists_and_no_backfill_ran(
    workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The flag was on before any flag-off Gmail rows existed: the backfill
    log can be empty while private evidence exists. A flag-off rebuild
    would copy the claims it backs into the shared retrieval body."""
    _seed_evidence(workspace_id, accounts, source_type="gmail_sync", visibility="private")
    _flag(monkeypatch, None)
    code = rebuild.main(["--workspace-id", str(workspace_id)])
    captured = capsys.readouterr()
    assert code == rebuild.EXIT_REFUSED
    assert "refusing to run" in captured.err
    assert captured.out == ""

    code = rebuild.main(["--workspace-id", str(workspace_id), "--allow-without-isolation"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "WARNING" in captured.err
    assert f"{workspace_id}\tretrieval_documents\t" in captured.out

    _flag(monkeypatch, "true")
    code = rebuild.main(["--workspace-id", str(workspace_id)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "WARNING" not in captured.err


@pytest.mark.usefixtures("without_backfill_log")
@pytest.mark.parametrize(
    ("source_type", "refused"),
    [("gmail_sync", True), ("manual", False)],
)
def test_narrowed_evidence_refuses_only_when_it_is_gmail_sourced(
    workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_type: str,
    refused: bool,
) -> None:
    """`shared_explicitly` evidence: an ordinary narrowed grant on
    non-personal evidence (a flag-off app already puts it in shared bodies)
    must not block every later flag-off rebuild; narrowed Gmail evidence
    does."""
    _seed_evidence(workspace_id, accounts, source_type=source_type, visibility="shared_explicitly")
    _flag(monkeypatch, None)
    code = rebuild.main(["--workspace-id", str(workspace_id)])
    captured = capsys.readouterr()
    if refused:
        assert code == rebuild.EXIT_REFUSED
        assert "refusing to run" in captured.err
    else:
        assert code == 0, captured.err
        assert "refusing" not in captured.err
        assert f"{workspace_id}\tretrieval_documents\t" in captured.out


@pytest.mark.usefixtures("without_backfill_log")
def test_private_evidence_in_another_workspace_does_not_block_a_scoped_rebuild(
    workspace_id: UUID,
    other_workspace_id: UUID,
    accounts: list[UUID],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_evidence(other_workspace_id, accounts, source_type="gmail_sync", visibility="private")
    _flag(monkeypatch, None)
    code = rebuild.main(["--workspace-id", str(workspace_id)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "refusing" not in captured.err
    code = rebuild.main(["--workspace-id", str(other_workspace_id)])
    assert code == rebuild.EXIT_REFUSED
