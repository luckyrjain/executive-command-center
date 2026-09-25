"""Tests for scripts/audit_connector_ownership.py (Spec A S2): each check
A-E detects exactly its seeded rows, evaluation-harness rows are excluded
by the harness's exact shape (pinned against the harness's own insert),
output carries ids only, and the script's session is read-only.
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import os
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID, uuid4

import pytest
from fixtures.phase10_evaluation_email_detect_action import EXAMPLES
from identity_fixtures import create_identity
from sqlalchemy import Connection, make_url, text
from sqlalchemy.exc import DBAPIError

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime import evaluation

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _load_module() -> ModuleType:
    path = Path("scripts/audit_connector_ownership.py")
    spec = importlib.util.spec_from_file_location("audit_connector_ownership", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit = _load_module()


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


def _create_workspace(conn: Connection, now: datetime) -> UUID:
    workspace_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workspaces (id, name, timezone, created_at) "
            "VALUES (:id, 'Connector Ownership Audit Test', 'UTC', :now)"
        ),
        {"id": workspace_id, "now": now},
    )
    return workspace_id


def _user(
    conn: Connection,
    workspace_id: UUID,
    now: datetime,
    *,
    email: str,
    role: str = "member",
    status: str = "active",
) -> UUID:
    user_id = uuid4()
    create_identity(
        conn,
        workspace_id=workspace_id,
        user_id=user_id,
        email=email,
        now=now,
        role=role,
        status=status,
    )
    return user_id


def _connector(
    conn: Connection,
    workspace_id: UUID,
    owner_id: UUID,
    now: datetime,
    *,
    external_account_id: str,
    provider: str = "gmail",
    display_name: str = "Audit test",
    visibility: str = "workspace",
    status: str = "active",
    connector_id: UUID | None = None,
) -> UUID:
    connector_id = connector_id or uuid4()
    conn.execute(
        text(
            """
            INSERT INTO connector_accounts (
                id, workspace_id, provider, external_account_id, display_name,
                granted_scopes, encrypted_credentials, status, version,
                created_by, updated_by, created_at, updated_at, owner_id, visibility
            ) VALUES (
                :id, :ws, :provider, :external_account_id, :display_name,
                ARRAY[]::text[], :credential, :status, 1,
                :owner, :owner, :now, :now, :owner, :visibility
            )
            """
        ),
        {
            "id": connector_id,
            "ws": workspace_id,
            "provider": provider,
            "external_account_id": external_account_id,
            "display_name": display_name,
            "credential": b"not-a-real-ciphertext",
            "status": status,
            "owner": owner_id,
            "now": now,
            "visibility": visibility,
        },
    )
    return connector_id


def _account_id(conn: Connection, user_id: UUID) -> UUID:
    value = conn.execute(
        text("SELECT account_id FROM users WHERE id = :id"), {"id": user_id}
    ).scalar_one()
    assert isinstance(value, UUID)
    return value


def _transfer(
    conn: Connection,
    workspace_id: UUID,
    resource_type: str,
    resource_id: UUID,
    from_user: UUID,
    to_user: UUID,
    now: datetime,
) -> UUID:
    transfer_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO ownership_transfers (
                id, workspace_id, resource_type, resource_id, from_account_id,
                to_account_id, status, initiated_by, created_at, completed_at
            ) VALUES (
                :id, :ws, :resource_type, :resource_id, :from_account,
                :to_account, 'completed', :initiated_by, :now, :now
            )
            """
        ),
        {
            "id": transfer_id,
            "ws": workspace_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "from_account": _account_id(conn, from_user),
            "to_account": _account_id(conn, to_user),
            "initiated_by": from_user,
            "now": now,
        },
    )
    return transfer_id


def _grant(
    conn: Connection,
    workspace_id: UUID,
    resource_type: str,
    resource_id: UUID,
    granted_by: UUID,
    grantee: UUID,
    now: datetime,
    *,
    revoked: bool = False,
) -> UUID:
    grant_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO resource_grants (
                id, workspace_id, grantee_account_id, resource_type, resource_id,
                actions, granted_by, revoked_at, created_at
            ) VALUES (
                :id, :ws, :grantee, :resource_type, :resource_id,
                ARRAY['read'], :granted_by, :revoked_at, :now
            )
            """
        ),
        {
            "id": grant_id,
            "ws": workspace_id,
            "grantee": _account_id(conn, grantee),
            "resource_type": resource_type,
            "resource_id": resource_id,
            "granted_by": granted_by,
            "revoked_at": now if revoked else None,
            "now": now,
        },
    )
    return grant_id


def _reconnect_audit(
    conn: Connection,
    workspace_id: UUID,
    connector_id: UUID,
    actor_id: UUID,
    now: datetime,
    *,
    authorization_result: str = "allowed",
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
                :id, :ws, 'connector_account.reconnected', 'connector_account', :aggregate_id,
                2, :actor_id, :request_id, :correlation_id,
                ARRAY['*'], :authorization_result, 'user', '{}'::jsonb, :now
            )
            """
        ),
        {
            "id": event_id,
            "ws": workspace_id,
            "aggregate_id": connector_id,
            "actor_id": actor_id,
            "request_id": uuid4(),
            "correlation_id": uuid4(),
            "authorization_result": authorization_result,
            "now": now,
        },
    )
    return event_id


def _attention_item(
    conn: Connection, workspace_id: UUID, owner_id: UUID, entity_type: str, now: datetime
) -> UUID:
    item_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO attention_items (
                id, workspace_id, entity_type, entity_id, source_entity_version,
                score, confidence, factors, explanation, generated_at, expires_at,
                owner_id, visibility
            ) VALUES (
                :id, :ws, :entity_type, :entity_id, 1,
                80, 0.9, '{}'::jsonb, 'Test', :now, :expires_at, :owner, 'workspace'
            )
            """
        ),
        {
            "id": item_id,
            "ws": workspace_id,
            "entity_type": entity_type,
            "entity_id": uuid4(),
            "now": now,
            "expires_at": now + timedelta(days=7),
            "owner": owner_id,
        },
    )
    return item_id


def _recommendation(
    conn: Connection, workspace_id: UUID, owner_id: UUID, recommendation_type: str, now: datetime
) -> UUID:
    recommendation_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO recommendations (
                id, workspace_id, recommendation_type, target_type, target_id,
                proposed_action, rationale, confidence, status, evidence_ids,
                source, created_by, updated_by, created_at, updated_at, version
            ) VALUES (
                :id, :ws, :recommendation_type, 'task', NULL,
                '{}'::jsonb, 'Test rationale', 0.9, 'proposed', ARRAY[]::uuid[],
                'ai', :owner, :owner, :now, :now, 1
            )
            """
        ),
        {
            "id": recommendation_id,
            "ws": workspace_id,
            "recommendation_type": recommendation_type,
            "owner": owner_id,
            "now": now,
        },
    )
    return recommendation_id


def _sync_run(conn: Connection, workspace_id: UUID, connector_id: UUID, now: datetime) -> UUID:
    run_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO sync_runs (id, workspace_id, connector_account_id, run_type, "
            "status, items_processed, started_at, created_at) VALUES "
            "(:id, :ws, :connector_id, 'backfill', 'succeeded', 0, :now, :now)"
        ),
        {"id": run_id, "ws": workspace_id, "connector_id": connector_id, "now": now},
    )
    return run_id


def _gmail_evidence(conn: Connection, workspace_id: UUID, owner_id: UUID, now: datetime) -> UUID:
    node_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO pkos_nodes (
                id, workspace_id, node_type, canonical_name, attributes,
                status, confidence, version, created_at, updated_at,
                owner_id, visibility
            ) VALUES (
                :id, :ws, 'person', 'Audit Person', '{}'::jsonb,
                'active', 1.00, 1, :now, :now, :owner, 'workspace'
            )
            """
        ),
        {"id": node_id, "ws": workspace_id, "owner": owner_id, "now": now},
    )
    evidence_id = uuid4()
    source_ref = f"gmail:{evidence_id}"
    conn.execute(
        text(
            """
            INSERT INTO pkos_evidence (
                id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
            ) VALUES (:id, :ws, :node_id, 'gmail_sync', :source_ref, :sha256, :now)
            """
        ),
        {
            "id": evidence_id,
            "ws": workspace_id,
            "node_id": node_id,
            "source_ref": source_ref,
            "sha256": hashlib.sha256(source_ref.encode()).hexdigest(),
            "now": now,
        },
    )
    return evidence_id


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as conn:
        account_ids = [
            row[0]
            for row in conn.execute(
                text("SELECT account_id FROM users WHERE workspace_id = :ws"), {"ws": workspace_id}
            )
        ]
        for table in (
            "resource_grants",
            "ownership_transfers",
            "audit_events",
            "event_outbox",
            "email_messages",
            "email_threads",
            "personal_domains",
            "recommendations",
            "pkos_evidence",
            "pkos_nodes",
            "attention_items",
            "sync_runs",
            "sync_cursors",
            "connector_accounts",
            "workspace_memberships",
            "users",
        ):
            conn.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                {"ws": workspace_id},
            )
        conn.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
        if account_ids:
            conn.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids})


# ---------------------------------------------------------------------------
# Running the script
# ---------------------------------------------------------------------------


def _run(
    capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, list[dict[str, str]], str, str]:
    code = audit.main(list(argv))
    captured = capsys.readouterr()
    rows = list(csv.DictReader(io.StringIO(captured.out)))
    return code, rows, captured.out, captured.err


def _keys(rows: list[dict[str, str]], check: str) -> set[tuple[str, str, str]]:
    return {(r["table"], r["row_id"], r["ref_id"]) for r in rows if r["check"] == check}


# ---------------------------------------------------------------------------
# Seeded scenario: every check has positives and near-miss negatives
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded() -> Iterator[dict[str, Any]]:
    now = datetime.now(UTC)
    ids: dict[str, Any] = {}
    with engine.begin() as conn:
        ws = _create_workspace(conn, now)
        ids["ws"] = ws
        alice = _user(conn, ws, now, email="alice-audit@example.test", role="owner")
        bob = _user(conn, ws, now, email="bob-audit@example.test")
        carol = _user(conn, ws, now, email="carol-audit@example.test", status="removed")
        ids.update(alice=alice, bob=bob, carol=carol)

        # --- A: identity mismatch -------------------------------------------
        ids["a_mismatch"] = _connector(
            conn, ws, alice, now, external_account_id="someone-else@example.test"
        )
        # A real mailbox that merely *starts with* "evaluation-" is audited.
        ids["a_real_evaluation_prefix"] = _connector(
            conn,
            ws,
            alice,
            now,
            external_account_id="evaluation-x@gmail.com",
            display_name="evaluation harness",
        )
        # Harness display name + "evaluation-<uuid>" that is NOT its own id.
        ids["a_eval_near_miss"] = _connector(
            conn,
            ws,
            alice,
            now,
            external_account_id=f"evaluation-{uuid4()}",
            display_name="evaluation harness",
        )
        # Negatives: case/whitespace-only difference; non-personal provider.
        ids["alice_gmail"] = _connector(
            conn, ws, alice, now, external_account_id="  Alice-Audit@Example.TEST "
        )
        _connector(conn, ws, alice, now, provider="github", external_account_id="alice-gh-login")
        # Exact evaluation-harness shape: excluded from every check even
        # though it would otherwise hit A, C, D.
        eval_id = uuid4()
        ids["eval"] = _connector(
            conn,
            ws,
            alice,
            now,
            connector_id=eval_id,
            external_account_id=f"evaluation-{eval_id}",
            display_name="evaluation harness",
            visibility="shared_explicitly",
        )

        # --- B: transfers on personal rows ------------------------------------
        bob_gmail = _connector(conn, ws, bob, now, external_account_id="bob-audit@example.test")
        ids["bob_gmail"] = bob_gmail
        ids["b_connector_transfer"] = _transfer(
            conn, ws, "connector_accounts", bob_gmail, alice, bob, now
        )
        email_rec = _recommendation(conn, ws, bob, "email_action_detected", now)
        ids["email_rec"] = email_rec
        ids["b_rec_transfer"] = _transfer(conn, ws, "recommendations", email_rec, alice, bob, now)
        github = _connector(
            conn, ws, bob, now, provider="github", external_account_id="bob-gh-login"
        )
        _transfer(conn, ws, "connector_accounts", github, alice, bob, now)
        other_rec = _recommendation(conn, ws, bob, "task_suggestion", now)
        _transfer(conn, ws, "recommendations", other_rec, alice, bob, now)

        # --- C: active grants on personal rows + shared_explicitly -----------
        ids["c_connector_grant"] = _grant(
            conn, ws, "connector_accounts", ids["alice_gmail"], alice, bob, now
        )
        email_item = _attention_item(conn, ws, alice, "email_thread", now)
        ids["email_item"] = email_item
        ids["c_item_grant"] = _grant(conn, ws, "attention_items", email_item, alice, bob, now)
        run_id = _sync_run(conn, ws, ids["alice_gmail"], now)
        ids["sync_run"] = run_id
        ids["c_run_grant"] = _grant(conn, ws, "sync_runs", run_id, alice, bob, now)
        ids["evidence"] = _gmail_evidence(conn, ws, alice, now)
        ids["c_evidence_grant"] = _grant(
            conn, ws, "pkos_evidence", ids["evidence"], alice, bob, now
        )
        ids["c_shared"] = _connector(
            conn,
            ws,
            bob,
            now,
            external_account_id="bob-audit@example.test ",
            visibility="shared_explicitly",
            status="disconnected",
            provider="gmail",
        )
        # Negatives: revoked grant, non-email attention item, github grant,
        # grants on the harness connector and on its sync run.
        _grant(conn, ws, "connector_accounts", bob_gmail, bob, alice, now, revoked=True)
        other_item = _attention_item(conn, ws, alice, "task", now)
        _grant(conn, ws, "attention_items", other_item, alice, bob, now)
        _grant(conn, ws, "connector_accounts", github, bob, alice, now)
        _grant(conn, ws, "connector_accounts", ids["eval"], alice, bob, now)
        eval_run = _sync_run(conn, ws, ids["eval"], now)
        _grant(conn, ws, "sync_runs", eval_run, alice, bob, now)

        # --- D: reconnected by non-owner --------------------------------------
        ids["d_event"] = _reconnect_audit(conn, ws, ids["alice_gmail"], bob, now)
        _reconnect_audit(conn, ws, ids["alice_gmail"], alice, now)  # by owner
        # Non-owner reconnects that were NOT allowed: never reported.
        _reconnect_audit(conn, ws, bob_gmail, alice, now, authorization_result="denied")
        ids["d_denied_event"] = _reconnect_audit(
            conn, ws, ids["alice_gmail"], bob, now, authorization_result="denied"
        )
        _reconnect_audit(conn, ws, github, alice, now)  # engineering: C15-a allows it
        _reconnect_audit(conn, ws, ids["eval"], bob, now)  # harness row

        # --- E: removed member's personal connector ---------------------------
        ids["carol_gmail"] = _connector(
            conn, ws, carol, now, external_account_id="carol-audit@example.test"
        )
        ids["carol_membership"] = conn.execute(
            text("SELECT id FROM workspace_memberships WHERE workspace_id = :ws AND users_id = :u"),
            {"ws": ws, "u": carol},
        ).scalar_one()
        _connector(conn, ws, carol, now, provider="github", external_account_id="carol-gh")
    try:
        yield ids
    finally:
        _cleanup_workspace(ws)


def test_check_a_identity_mismatch(
    seeded: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    code, rows, _, _ = _run(capsys, "--workspace-id", str(seeded["ws"]))
    assert code == audit.EXIT_FINDINGS
    assert _keys(rows, "A") == {
        ("connector_accounts", str(seeded["a_mismatch"]), ""),
        ("connector_accounts", str(seeded["a_real_evaluation_prefix"]), ""),
        ("connector_accounts", str(seeded["a_eval_near_miss"]), ""),
    }
    assert {r["identity_mismatch"] for r in rows if r["check"] == "A"} == {"true"}


def test_check_b_transfers(seeded: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    _, rows, _, _ = _run(capsys, "--workspace-id", str(seeded["ws"]))
    assert _keys(rows, "B") == {
        ("connector_accounts", str(seeded["bob_gmail"]), str(seeded["b_connector_transfer"])),
        ("recommendations", str(seeded["email_rec"]), str(seeded["b_rec_transfer"])),
    }
    by_table = {r["table"]: r for r in rows if r["check"] == "B"}
    assert by_table["connector_accounts"]["identity_mismatch"] == "false"
    assert by_table["connector_accounts"]["ref_table"] == "ownership_transfers"
    assert by_table["recommendations"]["identity_mismatch"] == ""


def test_check_c_shared(seeded: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    _, rows, _, _ = _run(capsys, "--workspace-id", str(seeded["ws"]))
    assert _keys(rows, "C") == {
        ("connector_accounts", str(seeded["alice_gmail"]), str(seeded["c_connector_grant"])),
        ("attention_items", str(seeded["email_item"]), str(seeded["c_item_grant"])),
        ("sync_runs", str(seeded["sync_run"]), str(seeded["c_run_grant"])),
        ("pkos_evidence", str(seeded["evidence"]), str(seeded["c_evidence_grant"])),
        ("connector_accounts", str(seeded["c_shared"]), ""),
    }
    shared = next(r for r in rows if r["check"] == "C" and r["row_id"] == str(seeded["c_shared"]))
    assert shared["row_status"] == "disconnected"


def test_check_d_reactivated_by_non_owner(
    seeded: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _, rows, _, _ = _run(capsys, "--workspace-id", str(seeded["ws"]))
    assert _keys(rows, "D") == {
        ("connector_accounts", str(seeded["alice_gmail"]), str(seeded["d_event"])),
    }
    assert str(seeded["d_denied_event"]) not in {r["ref_id"] for r in rows}


def test_check_e_removed_member(seeded: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    _, rows, _, _ = _run(capsys, "--workspace-id", str(seeded["ws"]))
    assert _keys(rows, "E") == {
        ("connector_accounts", str(seeded["carol_gmail"]), str(seeded["carol_membership"])),
    }
    carol_row = next(r for r in rows if r["check"] == "E")
    assert carol_row["owner_id"] == str(seeded["carol"])
    assert carol_row["workspace_id"] == str(seeded["ws"])


def test_evaluation_harness_rows_excluded_everywhere(
    seeded: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    _, rows, _, _ = _run(capsys, "--workspace-id", str(seeded["ws"]))
    assert str(seeded["eval"]) not in {r["row_id"] for r in rows}
    assert {r["check"] for r in rows} == set(audit.CHECKS)


def test_output_is_ids_only(seeded: dict[str, Any], capsys: pytest.CaptureFixture[str]) -> None:
    # Whole database, not just this workspace: whatever else is in the
    # test DB must not leak either.
    _, rows, out, err = _run(capsys)
    assert rows
    assert "@" not in out
    assert "@" not in err
    assert "evaluation harness" not in out
    for row in rows:
        assert tuple(row) == audit.CSV_HEADER
        for column in ("row_id", "workspace_id", "owner_id"):
            UUID(row[column])
        if row["ref_id"]:
            UUID(row["ref_id"])
        assert row["check"] in audit.CHECKS


def test_out_path_and_summary(
    seeded: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out_file = tmp_path / "audit.csv"
    code = audit.main(["--workspace-id", str(seeded["ws"]), "--out", str(out_file)])
    captured = capsys.readouterr()
    assert code == audit.EXIT_FINDINGS
    assert captured.out == ""
    rows = list(csv.DictReader(io.StringIO(out_file.read_text())))
    assert len(rows) == 3 + 2 + 5 + 1 + 1
    assert out_file.stat().st_mode & 0o777 == 0o600
    assert "check A (identity_mismatch): 3" in captured.err
    assert "check C (shared, report-only, superseded by backfill): 5" in captured.err
    assert "total: 12" in captured.err


def test_out_path_tightens_an_existing_files_mode(
    seeded: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    out_file = tmp_path / "existing.csv"
    out_file.write_text("stale\n")
    out_file.chmod(0o644)
    code = audit.main(["--workspace-id", str(seeded["ws"]), "--out", str(out_file)])
    capsys.readouterr()
    assert code == audit.EXIT_FINDINGS
    assert out_file.stat().st_mode & 0o777 == 0o600
    assert "stale" not in out_file.read_text()


def test_clean_workspace_exits_zero_with_empty_body(capsys: pytest.CaptureFixture[str]) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        ws = _create_workspace(conn, now)
        dave = _user(conn, ws, now, email="dave-audit@example.test", role="owner")
        _connector(conn, ws, dave, now, external_account_id="dave-audit@example.test")
    try:
        code, rows, out, err = _run(capsys, "--workspace-id", str(ws))
        assert code == audit.EXIT_CLEAN
        assert rows == []
        assert out == ",".join(audit.CSV_HEADER) + "\n"
        assert "total: 0" in err
    finally:
        _cleanup_workspace(ws)


# ---------------------------------------------------------------------------
# Pin: the exclusion matches what the evaluation harness really writes
# ---------------------------------------------------------------------------


def test_exclusion_matches_the_evaluation_harness_insert(
    capsys: pytest.CaptureFixture[str],
) -> None:
    now = datetime.now(UTC)
    with engine.begin() as conn:
        ws = _create_workspace(conn, now)
        # An owner whose email can never equal the harness's external id,
        # so without the exclusion the harness row would be an A finding.
        erin = _user(conn, ws, now, email="erin-audit@example.test", role="owner")
    try:
        auth = AuthContext(workspace_id=ws, user_id=erin, timezone="UTC")
        with SessionFactory() as session:
            connector_id, *_ = evaluation._insert_synthetic_email_thread(
                session, auth, dict(EXAMPLES[0]), now=now
            )
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT provider, display_name, external_account_id "
                    "FROM connector_accounts WHERE id = :id"
                ),
                {"id": connector_id},
            ).one()
        assert row.provider == audit._EVALUATION_PROVIDER
        assert row.display_name == audit._EVALUATION_DISPLAY_NAME
        assert row.external_account_id == f"{audit._EVALUATION_EXTERNAL_ID_PREFIX}{connector_id}"

        code, rows, _, _ = _run(capsys, "--workspace-id", str(ws))
        assert code == audit.EXIT_CLEAN
        assert rows == []
    finally:
        _cleanup_workspace(ws)


# ---------------------------------------------------------------------------
# Read-only guarantees and error paths
# ---------------------------------------------------------------------------


def test_readonly_connection_rejects_writes() -> None:
    with audit.readonly_connection(settings.database_url) as conn:
        assert conn.execute(text("SHOW transaction_read_only")).scalar_one() == "on"
        assert conn.execute(text("SHOW transaction_isolation")).scalar_one() == "repeatable read"
        assert conn.execute(text("SHOW lock_timeout")).scalar_one() == "5s"
        with pytest.raises(DBAPIError) as excinfo:
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, timezone, created_at) "
                    "VALUES (:id, 'must not exist', 'UTC', now())"
                ),
                {"id": uuid4()},
            )
    assert getattr(excinfo.value.orig, "sqlstate", None) == "25006"  # read_only_sql_transaction


def test_write_inside_the_scripts_session_fails_and_reports_only_class_and_sqlstate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target_id = uuid4()

    def _writing_checks(conn: Connection, *, workspace_id: UUID | None = None) -> list[Any]:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'leak-me@example.test', 'UTC', now())"
            ),
            {"id": target_id},
        )
        return []

    monkeypatch.setattr(audit, "run_checks", _writing_checks)
    code, rows, out, err = _run(capsys)
    assert code == audit.EXIT_ERROR
    assert out == ""
    assert rows == []
    assert "sqlstate=25006" in err
    assert "@" not in err
    assert "leak-me" not in err
    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT count(*) FROM workspaces WHERE id = :id"), {"id": target_id}
            ).scalar_one()
            == 0
        )


def test_connection_error_reports_no_url_or_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(
        "ECC_DATABASE_URL", "postgresql+psycopg://audit-user:s3cret-pw@127.0.0.1:1/no_such_db"
    )
    code, _, out, err = _run(capsys)
    assert code == audit.EXIT_ERROR
    assert out == ""
    assert "error class=OperationalError" in err
    assert "s3cret" not in err
    assert "audit-user" not in err


# ---------------------------------------------------------------------------
# Startup guards, target description, --out failure safety
# ---------------------------------------------------------------------------


def test_missing_database_url_env_refuses_to_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ECC_DATABASE_URL", raising=False)
    code, rows, out, err = _run(capsys)
    assert code == audit.EXIT_ERROR
    assert out == ""
    assert rows == []
    assert "ECC_DATABASE_URL must be set explicitly" in err


def test_summary_names_the_database_without_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    url = make_url(settings.database_url)
    monkeypatch.setenv("ECC_DATABASE_URL", settings.database_url)
    code, _, _, err = _run(capsys, "--workspace-id", str(uuid4()))
    assert code == audit.EXIT_CLEAN
    lines = err.splitlines()
    assert f"database: host={url.host} port={url.port} name={url.database}" in lines
    assert url.password is None or f":{url.password}@" not in err
    assert f"{url.username}:" not in err
    assert "@" not in err


def test_startup_settings_error_exits_2_without_echoing_the_value() -> None:
    rejected = "bogus-scope-value-do-not-echo"
    env = {
        **os.environ,
        "PYTHONPATH": "backend",
        "ECC_DATABASE_URL": settings.database_url,
        "ECC_GMAIL_REVOKE_SCOPE": rejected,
    }
    result = subprocess.run(  # noqa: S603 -- fixed argv, this interpreter
        [sys.executable, "scripts/audit_connector_ownership.py"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == audit.EXIT_ERROR
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert rejected not in result.stderr
    assert "error class=ValidationError" in result.stderr


def test_out_fchmod_failure_leaves_prior_content_intact(
    seeded: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    out_file = tmp_path / "keep.csv"
    out_file.write_text("prior content\n")

    def _failing_fchmod(fd: int, mode: int) -> None:
        raise PermissionError("simulated")

    monkeypatch.setattr(audit.os, "fchmod", _failing_fchmod)
    code = audit.main(["--workspace-id", str(seeded["ws"]), "--out", str(out_file)])
    captured = capsys.readouterr()
    assert code == audit.EXIT_ERROR
    assert "error class=PermissionError" in captured.err
    assert out_file.read_text() == "prior content\n"


def test_out_refuses_a_symlink(
    seeded: dict[str, Any], capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    target = tmp_path / "target.csv"
    target.write_text("untouched\n")
    link = tmp_path / "link.csv"
    link.symlink_to(target)
    code = audit.main(["--workspace-id", str(seeded["ws"]), "--out", str(link)])
    capsys.readouterr()
    assert code == audit.EXIT_ERROR
    assert target.read_text() == "untouched\n"
