"""Tests for scripts/remediate_connector_ownership.py (Spec A S2 remediation):
A/E rows (and mismatched B/D connectors) are disconnected without purge, the
grant is revoked iff safe (`site="remediation"`), B/D reviews are recorded,
unflagged / unknown / other-workspace rows are refused, per-row confirmation
is required, `--dry-run` writes nothing, re-runs are idempotent and output
carries ids only.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from identity_fixtures import create_identity
from sqlalchemy import Connection, text
from sqlalchemy.exc import OperationalError

from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.personal import gmail_revocation

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, Path(f"scripts/{name}.py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# The remediation script falls back to `import audit_connector_ownership`
# when `scripts` is not importable as a package; register it first.
audit = _load("audit_connector_ownership")
remediate = _load("remediate_connector_ownership")

_TOKEN = "plaintext-refresh-token-must-never-print"


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _workspace(conn: Connection, now: datetime) -> UUID:
    ws = uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, timezone, created_at) "
            "VALUES (:id, 'Remediation Test', 'UTC', :now)"
        ),
        {"id": ws, "now": now},
    )
    return ws


def _user(
    conn: Connection,
    ws: UUID,
    now: datetime,
    email: str,
    *,
    role: str = "member",
    status: str = "active",
) -> UUID:
    user_id = uuid4()
    create_identity(
        conn, workspace_id=ws, user_id=user_id, email=email, now=now, role=role, status=status
    )
    return user_id


def _connector(
    conn: Connection,
    ws: UUID,
    owner: UUID,
    now: datetime,
    external_account_id: str,
    *,
    credential: bytes | None = None,
) -> UUID:
    connector_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO connector_accounts (
                id, workspace_id, provider, external_account_id, display_name,
                granted_scopes, encrypted_credentials, status, version,
                created_by, updated_by, created_at, updated_at, owner_id, visibility
            ) VALUES (
                :id, :ws, 'gmail', :ext, 'Remediation test', ARRAY[]::text[], :cred,
                'active', 1, :owner, :owner, :now, :now, :owner, 'workspace'
            )
            """
        ),
        {
            "id": connector_id,
            "ws": ws,
            "ext": external_account_id,
            "cred": credential if credential is not None else encrypt_credential(_TOKEN),
            "owner": owner,
            "now": now,
        },
    )
    return connector_id


def _sync_run(conn: Connection, ws: UUID, connector_id: UUID, now: datetime) -> UUID:
    run_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO sync_runs (id, workspace_id, connector_account_id, run_type, "
            "status, items_processed, started_at, created_at) VALUES "
            "(:id, :ws, :c, 'backfill', 'succeeded', 0, :now, :now)"
        ),
        {"id": run_id, "ws": ws, "c": connector_id, "now": now},
    )
    return run_id


def _account_id(conn: Connection, user_id: UUID) -> UUID:
    value = conn.execute(
        text("SELECT account_id FROM users WHERE id = :id"), {"id": user_id}
    ).scalar_one()
    assert isinstance(value, UUID)
    return value


def _email_recommendation(conn: Connection, ws: UUID, owner: UUID, now: datetime) -> UUID:
    rec_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO recommendations (
                id, workspace_id, recommendation_type, target_type, target_id,
                proposed_action, rationale, confidence, status, evidence_ids,
                source, created_by, updated_by, created_at, updated_at, version
            ) VALUES (
                :id, :ws, 'email_action_detected', 'task', NULL, '{}'::jsonb, 'r', 0.9,
                'proposed', ARRAY[]::uuid[], 'ai', :owner, :owner, :now, :now, 1
            )
            """
        ),
        {"id": rec_id, "ws": ws, "owner": owner, "now": now},
    )
    return rec_id


def _transfer(
    conn: Connection,
    ws: UUID,
    resource_type: str,
    resource_id: UUID,
    frm: UUID,
    to: UUID,
    now: datetime,
) -> UUID:
    transfer_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO ownership_transfers (
                id, workspace_id, resource_type, resource_id, from_account_id,
                to_account_id, status, initiated_by, created_at, completed_at
            ) VALUES (:id, :ws, :rt, :rid, :f, :t, 'completed', :by, :now, :now)
            """
        ),
        {
            "id": transfer_id,
            "ws": ws,
            "rt": resource_type,
            "rid": resource_id,
            "f": _account_id(conn, frm),
            "t": _account_id(conn, to),
            "by": frm,
            "now": now,
        },
    )
    return transfer_id


def _reconnect_audit(
    conn: Connection, ws: UUID, connector_id: UUID, actor: UUID, now: datetime
) -> UUID:
    event_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO audit_events (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, actor_id, request_id, correlation_id,
                changed_fields, authorization_result, source, metadata, occurred_at
            ) VALUES (
                :id, :ws, 'connector_account.reconnected', 'connector_account', :agg,
                2, :actor, :rq, :co, ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :now
            )
            """
        ),
        {
            "id": event_id,
            "ws": ws,
            "agg": connector_id,
            "actor": actor,
            "rq": uuid4(),
            "co": uuid4(),
            "now": now,
        },
    )
    return event_id


def _cleanup(ws: UUID) -> None:
    with engine.begin() as conn:
        account_ids = [
            r[0]
            for r in conn.execute(
                text("SELECT account_id FROM users WHERE workspace_id = :ws"), {"ws": ws}
            )
        ]
        for table in (
            "member_notifications",
            "ownership_transfers",
            "audit_events",
            "event_outbox",
            "recommendations",
            "sync_runs",
            "connector_accounts",
            "workspace_memberships",
            "users",
        ):
            conn.execute(text(f"DELETE FROM {table} WHERE workspace_id = :ws"), {"ws": ws})  # noqa: S608
        conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
        if account_ids:
            conn.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids})


class _RecordingAdapter:
    def __init__(self) -> None:
        self.revoked: list[UUID] = []

    def disconnect(self, context: Any) -> None:
        self.revoked.append(context.connector_account_id)


@pytest.fixture
def adapter(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RecordingAdapter]:
    recording = _RecordingAdapter()
    monkeypatch.setattr(gmail_revocation, "_adapter", recording)
    monkeypatch.setenv("ECC_GMAIL_REVOKE_SCOPE", "global")
    monkeypatch.setenv(remediate.DATABASE_URL_ENV, settings.database_url)
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # non-interactive
    get_settings.cache_clear()
    try:
        yield recording
    finally:
        get_settings.cache_clear()


@pytest.fixture
def seeded() -> Iterator[dict[str, Any]]:
    now = datetime.now(UTC)
    tag = uuid4().hex[:10]
    ids: dict[str, Any] = {"tag": tag}
    with engine.begin() as conn:
        ws = _workspace(conn, now)
        other_ws = _workspace(conn, now)
        ids.update(ws=ws, other_ws=other_ws)
        alice = _user(conn, ws, now, f"alice-{tag}@example.test", role="owner")
        bob = _user(conn, ws, now, f"bob-{tag}@example.test")
        carol = _user(conn, ws, now, f"carol-{tag}@example.test", status="removed")
        dave = _user(conn, other_ws, now, f"dave-{tag}@example.test", role="owner")
        ids.update(alice=alice, bob=bob, carol=carol, dave=dave)
        # A: alice's connector for somebody else's mailbox.
        ids["a"] = _connector(conn, ws, alice, now, f"mismatch-{tag}@example.test")
        ids["a_run"] = _sync_run(conn, ws, ids["a"], now)
        # E: removed carol's own mailbox -- also live in another workspace,
        # so its grant is unsafe to revoke under the global scope.
        ids["e"] = _connector(conn, ws, carol, now, f"carol-{tag}@example.test")
        ids["e_run"] = _sync_run(conn, ws, ids["e"], now)
        ids["other_live"] = _connector(conn, other_ws, dave, now, f"carol-{tag}@example.test")
        # Flagged by A and E: removed carol's connector for another mailbox.
        ids["ae"] = _connector(conn, ws, carol, now, f"carol-other-{tag}@example.test")
        # Not flagged: alice's own mailbox; flagged but in another workspace.
        ids["alice_ok"] = _connector(conn, ws, alice, now, f"alice-{tag}@example.test")
        ids["other_ws_a"] = _connector(conn, other_ws, dave, now, f"elsewhere-{tag}@example.test")
        # B: an email recommendation transferred to bob (not a connector).
        ids["rec"] = _email_recommendation(conn, ws, bob, now)
        ids["b_transfer"] = _transfer(conn, ws, "recommendations", ids["rec"], alice, bob, now)
        # D: bob's own mailbox reconnected by alice (identity matches) ...
        ids["bob_gmail"] = _connector(conn, ws, bob, now, f"bob-{tag}@example.test")
        ids["d_event"] = _reconnect_audit(conn, ws, ids["bob_gmail"], alice, now)
        # ... and a mismatched connector of bob's reconnected by alice.
        ids["d_mismatch"] = _connector(conn, ws, bob, now, f"not-bob-{tag}@example.test")
        ids["d_mismatch_event"] = _reconnect_audit(conn, ws, ids["d_mismatch"], alice, now)
    try:
        yield ids
    finally:
        _cleanup(ws)
        _cleanup(other_ws)


def _run(
    capsys: pytest.CaptureFixture[str], *argv: str, ask: Any = None
) -> tuple[int, list[dict[str, str]], str, str]:
    kwargs = {"ask": ask} if ask is not None else {}
    code = remediate.main(list(argv), **kwargs)
    captured = capsys.readouterr()
    return code, list(csv.DictReader(io.StringIO(captured.out))), captured.out, captured.err


def _row(ws: UUID, connector_id: UUID) -> dict[str, Any]:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT status, version, disconnected_at, updated_by, owner_id "
                    "FROM connector_accounts WHERE id = :id AND workspace_id = :ws"
                ),
                {"id": connector_id, "ws": ws},
            )
            .mappings()
            .one()
        )


def _audit_rows(ws: UUID, event_type: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text(
                    "SELECT aggregate_id, aggregate_type, actor_id, source, metadata "
                    "FROM audit_events WHERE workspace_id = :ws AND event_type = :et "
                    "ORDER BY occurred_at"
                ),
                {"ws": ws, "et": event_type},
            ).mappings()
        ]


def _notifications(ws: UUID) -> list[tuple[UUID, str, str]]:
    with engine.connect() as conn:
        return [
            (r[0], r[1], r[2])
            for r in conn.execute(
                text(
                    "SELECT account_id, notification_type, resource_ref FROM member_notifications "
                    "WHERE workspace_id = :ws"
                ),
                {"ws": ws},
            )
        ]


def _revokes(result: str) -> float:
    return observability.connector_revoke_total._values.get(("gmail", "remediation", result), 0.0)


def _args(ws: UUID, *pairs: str, confirm: bool = True) -> list[str]:
    argv = ["--workspace-id", str(ws)]
    for pair in pairs:
        argv += ["--target", pair]
        if confirm:
            argv += ["--confirm", pair]
    return argv


def _by_row(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {r["row_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# A / E
# ---------------------------------------------------------------------------


def test_a_and_e_disconnect_without_purge_and_revoke_iff_safe(
    seeded: dict[str, Any], adapter: _RecordingAdapter, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = seeded["ws"]
    ok_before, unsafe_before = _revokes("ok"), _revokes("skipped_unsafe")
    before_a, before_e = _row(ws, seeded["a"]), _row(ws, seeded["e"])

    code, rows, out, err = _run(capsys, *_args(ws, f"A:{seeded['a']}", f"E:{seeded['e']}"))

    assert code == remediate.EXIT_CLEAN, err
    by_row = _by_row(rows)
    assert by_row[str(seeded["a"])]["outcome"] == "disconnected"
    assert by_row[str(seeded["a"])]["revoke"] == "ok"
    assert by_row[str(seeded["e"])]["outcome"] == "disconnected"
    assert by_row[str(seeded["e"])]["revoke"] == "skipped_unsafe"
    assert by_row[str(seeded["e"])]["ref_ids"]  # the removed membership id
    # Disconnected, version bumped, updated_by untouched (no fake actor).
    for key, before in (("a", before_a), ("e", before_e)):
        after = _row(ws, seeded[key])
        assert after["status"] == "disconnected"
        assert after["disconnected_at"] is not None
        assert after["version"] == before["version"] + 1
        assert after["updated_by"] == before["updated_by"]
    # Revoked only where safe (global scope: another live row blocks E).
    assert adapter.revoked == [seeded["a"]]
    assert _revokes("ok") == ok_before + 1
    assert _revokes("skipped_unsafe") == unsafe_before + 1
    # Other workspace's live row untouched.
    assert _row(seeded["other_ws"], seeded["other_live"])["status"] == "active"
    # No purge (DS2): the connectors' runs are still there.
    with engine.connect() as conn:
        runs = conn.execute(
            text("SELECT count(*) FROM sync_runs WHERE id = ANY(:ids)"),
            {"ids": [seeded["a_run"], seeded["e_run"]]},
        ).scalar_one()
    assert runs == 2
    # Audit: connector_account.disabled, system actor, reason + check.
    events = {e["aggregate_id"]: e for e in _audit_rows(ws, "connector_account.disabled")}
    assert set(events) == {seeded["a"], seeded["e"]}
    for key, check in (("a", "A"), ("e", "E")):
        event = events[seeded[key]]
        assert event["actor_id"] is None
        assert event["source"] == "system"
        assert event["metadata"]["reason"] == "operator_remediation"
        assert event["metadata"]["check"] == check
        assert UUID(event["metadata"]["run_id"])
    # C15-f: the active owner (alice, A) is notified; removed carol (E) is not.
    with engine.connect() as conn:
        alice_account = _account_id(conn, seeded["alice"])
    assert _notifications(ws) == [
        (alice_account, "connector_account.disconnected", f"connector_accounts:{seeded['a']}")
    ]
    # Ids only: no email, no token anywhere in the output.
    for text_out in (out, err):
        assert "@" not in text_out
        assert _TOKEN not in text_out
    assert "connector_revoke_total{site=remediation} ok (distinct rows): 1" in err

    # The audit now shows both rows as remediated (row_status=disconnected).
    with audit.readonly_connection(settings.database_url) as conn:
        findings = audit.run_checks(conn, workspace_id=ws)
    status = {(f.check, f.row_id): f.row_status for f in findings}
    assert status[("A", str(seeded["a"]))] == "disconnected"
    assert status[("E", str(seeded["e"]))] == "disconnected"

    # Re-run: idempotent -- nothing written, nothing revoked.
    code, rows, _, _ = _run(capsys, *_args(ws, f"A:{seeded['a']}", f"E:{seeded['e']}"))
    assert code == remediate.EXIT_CLEAN
    assert {r["outcome"] for r in rows} == {"already_disconnected"}
    assert {r["revoke"] for r in rows} == {""}
    assert adapter.revoked == [seeded["a"]]
    assert len(_audit_rows(ws, "connector_account.disabled")) == 2
    assert _row(ws, seeded["a"])["version"] == before_a["version"] + 1
    assert len(_notifications(ws)) == 1


def test_undecryptable_credential_still_disconnects_and_counts_error(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    ws = seeded["ws"]
    garbage = b"garbage-ciphertext-must-never-print"
    with engine.begin() as conn:
        bad = _connector(
            conn,
            ws,
            seeded["alice"],
            datetime.now(UTC),
            f"bad-{seeded['tag']}@example.test",
            credential=garbage,
        )
    error_before = _revokes("error")
    with caplog.at_level(logging.WARNING):
        code, rows, out, err = _run(capsys, *_args(ws, f"A:{bad}"))
    assert code == remediate.EXIT_ATTENTION
    assert rows[0]["outcome"] == "disconnected"
    assert rows[0]["revoke"] == "credential_unavailable"
    assert _row(ws, bad)["status"] == "disconnected"
    assert _revokes("error") == error_before + 1
    assert adapter.revoked == []
    logged = caplog.text + out + err
    assert "garbage-ciphertext" not in logged
    assert "error_class=InvalidToken" in caplog.text


# ---------------------------------------------------------------------------
# Refusals and confirmation
# ---------------------------------------------------------------------------


def test_refuses_unflagged_unknown_and_other_workspace_rows(
    seeded: dict[str, Any], adapter: _RecordingAdapter, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = seeded["ws"]
    targets = (
        f"A:{seeded['alice_ok']}",
        f"A:{uuid4()}",
        f"A:{seeded['other_ws_a']}",
        f"E:{seeded['a']}",
    )
    code, rows, _, _ = _run(capsys, *_args(ws, *targets))
    assert code == remediate.EXIT_ATTENTION
    assert len(rows) == 4
    assert {r["outcome"] for r in rows} == {"not_flagged"}
    assert _row(ws, seeded["alice_ok"])["status"] == "active"
    assert _row(ws, seeded["a"])["status"] == "active"  # flagged by A, not by E
    assert _row(seeded["other_ws"], seeded["other_ws_a"])["status"] == "active"
    assert _audit_rows(ws, "connector_account.disabled") == []
    assert adapter.revoked == []


def test_check_c_and_bulk_without_ids_are_refused(
    seeded: dict[str, Any], adapter: _RecordingAdapter, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = seeded["ws"]
    code, _, _, err = _run(capsys, *_args(ws, f"C:{seeded['a']}"))
    assert code == remediate.EXIT_ERROR
    assert "report-only" in err
    code, _, _, err = _run(capsys, "--workspace-id", str(ws))
    assert code == remediate.EXIT_ERROR
    assert "no A/B/D/E targets" in err


def test_per_row_confirmation_is_required(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = seeded["ws"]
    a, e = f"A:{seeded['a']}", f"E:{seeded['e']}"
    # Non-interactive without --confirm: refused before any write.
    code, _, out, err = _run(capsys, *_args(ws, a, e, confirm=False))
    assert code == remediate.EXIT_ERROR
    assert out == ""
    assert "per-row confirmation required" in err
    # --confirm must name every target, and only targets.
    code, _, _, err = _run(capsys, *_args(ws, a, e, confirm=False), "--confirm", a)
    assert code == remediate.EXIT_ERROR
    assert f"unconfirmed: {e}" in err
    code, _, _, err = _run(capsys, *_args(ws, a), "--confirm", e)
    assert code == remediate.EXIT_ERROR
    assert "confirmed but not targeted" in err
    assert _row(ws, seeded["a"])["status"] == "active"

    # Interactive: each row asked y/N; only "y" rows change.
    monkeypatch.setattr(sys, "stdin", _Tty())
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return "y" if str(seeded["a"]) in prompt else "n"

    code, rows, _, _ = _run(capsys, *_args(ws, a, e, confirm=False), ask=ask)
    assert len(prompts) == 2
    assert code == remediate.EXIT_ATTENTION
    by_row = _by_row(rows)
    assert by_row[str(seeded["a"])]["outcome"] == "disconnected"
    assert by_row[str(seeded["e"])]["outcome"] == "not_confirmed"
    assert _row(ws, seeded["e"])["status"] == "active"


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_interactive_prompts_go_to_stderr_and_stdout_is_only_csv(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = seeded["ws"]
    a, e = f"A:{seeded['a']}", f"E:{seeded['e']}"
    monkeypatch.setattr(sys, "stdin", _Tty("y\nn\n"))  # targets are sorted: A then E
    code, rows, out, err = _run(capsys, *_args(ws, a, e, confirm=False))  # default ask
    assert code == remediate.EXIT_ATTENTION
    lines = out.splitlines()
    assert lines[0] == ",".join(remediate.CSV_HEADER)
    assert len(lines) == 3 and "remediate " not in out and "[y/N]" not in out
    assert {(r["row_id"], r["outcome"]) for r in rows} == {
        (str(seeded["a"]), "disconnected"),
        (str(seeded["e"]), "not_confirmed"),
    }
    # Prompts on stderr, with ids/codes only.
    assert err.count("[y/N]") == 2
    assert f"owner_id={seeded['alice']} row_status=active identity_mismatch=true" in err
    assert f"owner_id={seeded['carol']} row_status=active identity_mismatch=false" in err
    assert "@" not in out + err


def test_end_of_input_at_a_prompt_aborts_with_nothing_written(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = seeded["ws"]
    a, e = f"A:{seeded['a']}", f"E:{seeded['e']}"
    monkeypatch.setattr(sys, "stdin", _Tty("y\n"))  # EOF at the second prompt
    code, _, out, err = _run(capsys, *_args(ws, a, e, confirm=False))
    assert code == remediate.EXIT_ERROR
    assert out == ""
    assert "confirmation aborted; nothing written" in err
    assert _row(ws, seeded["a"])["status"] == "active"
    assert _audit_rows(ws, "connector_account.disabled") == []


def test_malformed_csv_exits_2_without_traceback(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    bad = tmp_path / "short.csv"
    bad.write_text(",".join(audit.CSV_HEADER) + "\nA\n", encoding="utf-8")  # short row
    code, _, out, err = _run(
        capsys, "--workspace-id", str(seeded["ws"]), "--csv", str(bad), "--dry-run"
    )
    assert code == remediate.EXIT_ERROR
    assert out == ""
    assert "invalid id" in err and "Traceback" not in err
    undecodable = tmp_path / "binary.csv"
    undecodable.write_bytes(b"\xff\xfe\x00garbage")
    code, _, out, err = _run(
        capsys, "--workspace-id", str(seeded["ws"]), "--csv", str(undecodable), "--dry-run"
    )
    assert code == remediate.EXIT_ERROR
    assert out == ""
    assert "invalid input class=UnicodeDecodeError" in err and "garbage" not in err


def test_dry_run_counts_one_revoke_per_connector_flagged_twice(
    seeded: dict[str, Any], adapter: _RecordingAdapter, capsys: pytest.CaptureFixture[str]
) -> None:
    row = seeded["d_mismatch"]  # flagged by A and by D
    code, rows, _, err = _run(
        capsys, *_args(seeded["ws"], f"A:{row}", f"D:{row}", confirm=False), "--dry-run"
    )
    assert code == remediate.EXIT_CLEAN, err
    assert [(r["check"], r["outcome"], r["revoke"]) for r in rows] == [
        ("A", "would_disconnect", "would_revoke"),
        ("D", "already_disconnected", ""),
    ]
    assert "revoke would_revoke (distinct rows): 1" in err
    assert "outcome would_disconnect: 1" in err


def _ae_outcomes(rows: list[dict[str, str]], row_id: UUID) -> dict[str, tuple[str, str]]:
    return {r["check"]: (r["outcome"], r["revoke"]) for r in rows if r["row_id"] == str(row_id)}


def test_row_flagged_by_a_and_e_is_confirmed_once_interactively(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, row = seeded["ws"], seeded["ae"]
    monkeypatch.setattr(sys, "stdin", _Tty("y\n"))  # one answer for the one row
    code, rows, _, err = _run(capsys, *_args(ws, f"A:{row}", f"E:{row}", confirm=False))
    assert code == remediate.EXIT_CLEAN, err
    assert err.count("[y/N]") == 1
    assert f"remediate row {row} checks=A,E" in err
    assert _ae_outcomes(rows, row) == {
        "A": ("disconnected", "ok"),
        "E": ("already_disconnected", ""),
    }
    assert adapter.revoked == [row]
    assert len(_audit_rows(ws, "connector_account.disabled")) == 1


def test_row_flagged_by_a_and_e_needs_one_confirm_naming_either_check(
    seeded: dict[str, Any], adapter: _RecordingAdapter, capsys: pytest.CaptureFixture[str]
) -> None:
    ws, row = seeded["ws"], seeded["ae"]
    targets = ["--target", f"A:{row}", "--target", f"E:{row}"]
    code, _, _, err = _run(capsys, "--workspace-id", str(ws), *targets)
    assert code == remediate.EXIT_ERROR  # still needs a confirmation
    code, rows, _, err = _run(capsys, "--workspace-id", str(ws), *targets, "--confirm", f"E:{row}")
    assert code == remediate.EXIT_CLEAN, err
    assert _ae_outcomes(rows, row) == {
        "A": ("disconnected", "ok"),
        "E": ("already_disconnected", ""),
    }
    assert adapter.revoked == [row]


def test_dry_run_of_a_row_flagged_by_a_and_e_counts_it_once(
    seeded: dict[str, Any], adapter: _RecordingAdapter, capsys: pytest.CaptureFixture[str]
) -> None:
    row = seeded["ae"]
    code, rows, _, err = _run(
        capsys, *_args(seeded["ws"], f"A:{row}", f"E:{row}", confirm=False), "--dry-run"
    )
    assert code == remediate.EXIT_CLEAN, err
    assert _ae_outcomes(rows, row) == {
        "A": ("would_disconnect", "would_revoke"),
        "E": ("already_disconnected", ""),
    }
    assert "outcome would_disconnect: 1" in err
    assert "revoke would_revoke (distinct rows): 1" in err
    assert _row(seeded["ws"], row)["status"] == "active"


def test_ctrl_c_after_a_commit_prints_committed_rows_and_run_id(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = seeded["ws"]

    def interrupt(pending: Any) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr(remediate, "_revoke", interrupt)
    code, rows, _, err = _run(capsys, *_args(ws, f"A:{seeded['a']}", f"E:{seeded['e']}"))
    assert code == remediate.EXIT_ERROR
    # The first (sorted) target committed; its revoke never completed.
    assert [(r["row_id"], r["outcome"], r["revoke"]) for r in rows] == [
        (str(seeded["a"]), "disconnected", "interrupted")
    ]
    assert _row(ws, seeded["a"])["status"] == "disconnected"
    assert _row(ws, seeded["e"])["status"] == "active"
    assert "interrupted" in err and "run_id=" in err and "may not have happened" in err
    assert adapter.revoked == []


class _LockNotAvailable(Exception):
    sqlstate = "55P03"


def _fail_after_first_row(monkeypatch: pytest.MonkeyPatch) -> None:
    real = remediate._remediate_in_txn
    calls = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OperationalError("SELECT 1", {}, _LockNotAvailable())
        return real(*args, **kwargs)

    monkeypatch.setattr(remediate, "_remediate_in_txn", flaky)


def test_database_error_after_a_commit_prints_committed_rows_and_run_id(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws = seeded["ws"]
    _fail_after_first_row(monkeypatch)
    code, rows, _, err = _run(capsys, *_args(ws, f"A:{seeded['a']}", f"E:{seeded['e']}"))
    assert code == remediate.EXIT_ERROR
    # The committed first row is still reported, with its revoke outcome.
    assert [(r["row_id"], r["outcome"], r["revoke"]) for r in rows] == [
        (str(seeded["a"]), "disconnected", "ok")
    ]
    assert _row(ws, seeded["a"])["status"] == "disconnected"
    assert _row(ws, seeded["e"])["status"] == "active"
    assert "error class=_LockNotAvailable sqlstate=55P03" in err
    assert "outcome disconnected: 1" in err  # summary printed
    assert "run_id=" in err and "may not have happened" in err
    assert "SELECT 1" not in err


def test_database_error_in_a_dry_run_says_nothing_was_written(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_after_first_row(monkeypatch)
    code, _, out, err = _run(
        capsys,
        *_args(seeded["ws"], f"A:{seeded['a']}", f"E:{seeded['e']}", confirm=False),
        "--dry-run",
    )
    assert code == remediate.EXIT_ERROR
    assert out == ""
    assert "nothing was written" in err


def test_non_gmail_row_without_credential_reports_no_revoke_error() -> None:
    before = observability.connector_revoke_total._values.get(
        ("github", "remediation", "error"), 0.0
    )
    pending = remediate._PendingRevoke(
        provider="github",
        row_id=uuid4(),
        external_account_id="x",
        context=None,
        error_class="InvalidToken",
    )
    assert remediate._revoke(pending) == ""
    after = observability.connector_revoke_total._values.get(
        ("github", "remediation", "error"), 0.0
    )
    assert after == before


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(
    seeded: dict[str, Any], adapter: _RecordingAdapter, capsys: pytest.CaptureFixture[str]
) -> None:
    ws = seeded["ws"]
    targets = (
        f"A:{seeded['a']}",
        f"E:{seeded['e']}",
        f"B:{seeded['rec']}",
        f"D:{seeded['bob_gmail']}",
    )
    code, rows, _, err = _run(capsys, *_args(ws, *targets, confirm=False), "--dry-run")
    assert code == remediate.EXIT_CLEAN, err
    by_row = _by_row(rows)
    assert (by_row[str(seeded["a"])]["outcome"], by_row[str(seeded["a"])]["revoke"]) == (
        "would_disconnect",
        "would_revoke",
    )
    assert (by_row[str(seeded["e"])]["outcome"], by_row[str(seeded["e"])]["revoke"]) == (
        "would_disconnect",
        "would_skip_unsafe",
    )
    assert by_row[str(seeded["rec"])]["outcome"] == "would_record"
    assert by_row[str(seeded["bob_gmail"])]["outcome"] == "would_record"
    assert _row(ws, seeded["a"])["status"] == "active"
    assert _row(ws, seeded["e"])["status"] == "active"
    assert _audit_rows(ws, "connector_account.disabled") == []
    assert _audit_rows(ws, remediate.REVIEW_EVENT_TYPE) == []
    assert _notifications(ws) == []
    assert adapter.revoked == []


# ---------------------------------------------------------------------------
# B / D and the CSV round trip
# ---------------------------------------------------------------------------


def test_b_and_d_record_review_or_disconnect_when_mismatched_from_audit_csv(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    ws = seeded["ws"]
    audit_csv = tmp_path / "audit.csv"
    assert audit.main(["--workspace-id", str(ws), "--out", str(audit_csv)]) == audit.EXIT_FINDINGS
    capsys.readouterr()
    with audit_csv.open(encoding="utf-8") as handle:
        flagged = sorted(
            {(r["check"], r["row_id"]) for r in csv.DictReader(handle) if r["check"] in "ABDE"}
        )
    expected = {
        ("A", str(seeded["a"])),
        ("A", str(seeded["d_mismatch"])),
        ("B", str(seeded["rec"])),
        ("D", str(seeded["bob_gmail"])),
        ("D", str(seeded["d_mismatch"])),
        ("E", str(seeded["e"])),
        ("A", str(seeded["ae"])),
        ("E", str(seeded["ae"])),
    }
    assert set(flagged) == expected
    confirms = [arg for check, row in flagged for arg in ("--confirm", f"{check}:{row}")]
    argv = ["--workspace-id", str(ws), "--csv", str(audit_csv), *confirms]

    code, rows, out, err = _run(capsys, *argv)
    assert code == remediate.EXIT_CLEAN, err
    outcomes = {(r["check"], r["row_id"]): r["outcome"] for r in rows}
    assert outcomes[("B", str(seeded["rec"]))] == "review_recorded"
    assert outcomes[("D", str(seeded["bob_gmail"]))] == "review_recorded"
    # D on a mismatched connector is "as A": disconnected (by whichever of
    # its A/D targets ran first; the other finds it already disconnected).
    assert {
        outcomes[("A", str(seeded["d_mismatch"]))],
        outcomes[("D", str(seeded["d_mismatch"]))],
    } == {
        "disconnected",
        "already_disconnected",
    }
    assert _row(ws, seeded["d_mismatch"])["status"] == "disconnected"
    assert _row(ws, seeded["bob_gmail"])["status"] == "active"
    assert "@" not in out + err and _TOKEN not in out + err

    reviews = {e["aggregate_id"]: e for e in _audit_rows(ws, remediate.REVIEW_EVENT_TYPE)}
    assert set(reviews) == {seeded["rec"], seeded["bob_gmail"]}
    b_meta = reviews[seeded["rec"]]["metadata"]
    assert b_meta["check"] == "B"
    assert b_meta["ref_table"] == "ownership_transfers"
    assert b_meta["ref_id"] == str(seeded["b_transfer"])
    assert b_meta["reason"] == "operator_remediation"
    assert b_meta["decision"] == "reviewed_not_applicable"  # not a connector
    assert reviews[seeded["rec"]]["aggregate_type"] == "recommendations"
    d_meta = reviews[seeded["bob_gmail"]]["metadata"]
    assert (d_meta["check"], d_meta["ref_id"]) == ("D", str(seeded["d_event"]))
    assert d_meta["decision"] == "reviewed_no_identity_mismatch"
    assert reviews[seeded["bob_gmail"]]["actor_id"] is None

    # Re-run: every row already handled; nothing new written.
    code, rows, _, _ = _run(capsys, *argv)
    assert code == remediate.EXIT_CLEAN
    assert {r["outcome"] for r in rows} == {"already_disconnected", "already_recorded"}
    assert len(_audit_rows(ws, remediate.REVIEW_EVENT_TYPE)) == 2


def test_csv_from_another_workspace_is_refused(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    audit_csv = tmp_path / "audit.csv"
    audit.main(["--workspace-id", str(seeded["ws"]), "--out", str(audit_csv)])
    capsys.readouterr()
    code, _, _, err = _run(
        capsys, "--workspace-id", str(seeded["other_ws"]), "--csv", str(audit_csv), "--dry-run"
    )
    assert code == remediate.EXIT_ERROR
    assert "another workspace" in err


def test_missing_database_url_refuses(
    seeded: dict[str, Any],
    adapter: _RecordingAdapter,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(remediate.DATABASE_URL_ENV)
    code, _, _, err = _run(capsys, *_args(seeded["ws"], f"A:{seeded['a']}"))
    assert code == remediate.EXIT_ERROR
    assert "must be set explicitly" in err
