"""Operator remediation of Spec A S2 audit findings -- disconnect WITHOUT purge.

Pairs with `scripts/audit_connector_ownership.py` (read-only). Rollout step
R4 ("Remediate S2 A, B, D, E -- per-row operator confirmation; irreversible
revokes") must be done before `ECC_PERSONAL_DATA_ISOLATION` is enabled: once
personal rows become private, a wrongly-owned connector locks the real
mailbox owner out. No application endpoint can do this: the engineering
disable endpoint refuses Gmail rows (409
`GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT`), the owner's email-domain disable
PURGES the Gmail-derived data (Spec A DS2 says retain), and for check E the
owner has already been removed and cannot act.

Runbook:

 1. Audit (read-only) one workspace and keep the CSV:
        PYTHONPATH=backend ECC_DATABASE_URL=<url> uv run python \\
            scripts/audit_connector_ownership.py --workspace-id <WS> --out audit.csv
 2. Review every A/B/D/E row. Preview what this command would do:
        PYTHONPATH=backend ECC_DATABASE_URL=<url> uv run python \\
            scripts/remediate_connector_ownership.py --workspace-id <WS> \\
            --csv audit.csv --dry-run
 3. Remediate, confirming EACH row explicitly (`--confirm CHECK:ROW_ID`,
    repeated once per distinct row; or omit `--confirm` on a terminal to be
    asked y/N per row -- prompts, each showing the row's checks, owner_id,
    row_status and identity_mismatch, go to stderr and answers are read
    from stdin, so stdout stays pure CSV; end-of-input or Ctrl-C at a
    prompt aborts the whole run with exit 2 and nothing written).
    Confirmation is per distinct row: a row flagged by several checks (say
    A and E) is asked once, and `--confirm` with any one of its
    `CHECK:ROW_ID` pairs confirms all of that row's targets; its first
    target does the work and the others report `already_disconnected`
    (or `already_recorded`). Ctrl-C after the prompts stops the run with
    exit 2: rows committed so far are still printed, with the run id; the
    last one's revoke may not have happened (`revoke=interrupted`):
        PYTHONPATH=backend ECC_DATABASE_URL=<url> uv run python \\
            scripts/remediate_connector_ownership.py --workspace-id <WS> \\
            --csv audit.csv --confirm A:<row_id> --confirm E:<row_id> ...
    (`--target CHECK:ROW_ID`, repeated, instead of `--csv` names rows
    directly.) Run it with the application's own environment (the
    connector-token encryption key and `ECC_GMAIL_REVOKE_SCOPE`): the
    credential is decrypted to revoke the Google grant.
 4. Before closing the change record, check the CSV's `revoke` column and
    the revoke counts on stderr: `error` / `credential_unavailable` means
    the Google grant was NOT revoked, and a re-run will not retry it (a row
    already disconnected is never re-revoked) -- have the mailbox owner
    revoke the app's access in their Google account instead.
    `skipped_unsafe` is expected while another live row uses that account.
    `revoke=ok` means Google confirmed the grant is gone (a 2xx, or its
    `invalid_token` reply for an already-revoked token); a Google refusal,
    a transport error or an unusable stored credential is `error` (FX6).
 5. Re-run the audit: A/E rows (and mismatched B/D connectors) now show
    `row_status=disconnected`. Re-running this command reports every
    handled row as `already_disconnected` / `already_recorded`.

Actions (Spec A S2 "Remediation" column):

    A identity_mismatch  "Disconnect (no purge) + revoke iff safe; notify
                         row owner in-app" -> disconnect.
    B transferred        "As A if mismatched; else record" -> disconnect
                         when the row is a connector whose identity
                         mismatches, else record the review.
    C shared             "report only" (superseded by the S1.8(b)
                         backfill) -> refused here.
    D reactivated_by_non_owner
                         "Review; as A if mismatched" -> as B.
    E removed_member     "Disconnect + revoke iff safe" -> disconnect.

Disconnect: `status='disconnected'`, `disconnected_at`, `updated_at`,
`version+1` on the connector row, and one `connector_account.disabled`
audit event (+ outbox event, same shape as the removal/disable paths) with
`reason='operator_remediation'`, the check, the finding's ref ids and this
run's id. After the row's transaction commits, the provider grant is revoked
iff `revoke_is_safe(token_kind="disconnected_row", exclude_row_id=<row>)`
(under `ECC_GMAIL_REVOKE_SCOPE=global`, skipped while any other
non-disconnected row in any workspace uses the same Google account),
counted as `ecc_connector_revoke_total{site="remediation"}`. That counter is
process-local and this is a one-off process nobody scrapes, so the same
counts are also printed in the summary on stderr -- copy them into the
change record. A credential that cannot be decrypted still disconnects the
row; it is counted `result="error"` and reported `credential_unavailable`
(the exception class only is logged, never its message). The row owner
gets an in-app `member_notifications` row (`connector_account.disconnected`,
`connector_accounts:<id>`; Spec A C15-f: the row owner only) when they are
still an active member -- so never for E.

Record (B/D not mismatched): one audit-only (no outbox)
`connector_ownership.review_recorded` event per finding, on the flagged row,
with `metadata = {reason, check, table, ref_table, ref_id, decision, run_id}`;
`decision` is `reviewed_no_identity_mismatch` on a connector row and
`reviewed_not_applicable` on any other personal row (a B transfer of, e.g.,
a recommendation: there is no identity to compare). No new table: `audit_events` is
the immutable, admin-readable record and needs no migration.

Actor: `connector_accounts.updated_by` is a NOT NULL foreign key to a user
of the workspace, and the operator is not necessarily one, so it is left
unchanged (writing anyone else there would attribute the change to them).
The audit events carry `actor_id = NULL`, `source = 'system'` and
`metadata.reason = 'operator_remediation'` plus `run_id` (printed first on
stderr) -- tie the run id to the operator in the change record.

What it does NOT do: purge any data (DS2 -- email threads, messages,
recommendations, evidence, runs and cursors stay, and become private with
the S1.8(b) backfill); re-own anything (no connector, node or alias changes
owner; the removal path's person-node re-own is not repeated); remediate
rows the audit does not currently flag, rows of another workspace, or rows
of check C; act without an explicit per-row confirmation (there is no "fix
everything" mode); retry the revoke of a row that is already disconnected
(re-connect/revoke at Google by the user in that case); notify anyone but
the row owner.

Safety: one short transaction per row -- the SHARED side of the workspace's
membership-mutation advisory lock first (the key member removal takes
exclusively), then `FOR UPDATE` on the flagged row, then the audit check is
re-evaluated so only a row that is still flagged is changed; `SET LOCAL`
statement (120s) and lock (5s) timeouts. The provider revoke happens after
the commit, with no transaction open. `--dry-run` runs everything in one
`REPEATABLE READ, READ ONLY` transaction (always rolled back) and reports
`would_disconnect` / `would_record` and whether a revoke would be safe now.

Output: CSV on stdout, ids and code-defined values only (never emails,
tokens, credentials or content): `check, table, row_id, workspace_id,
ref_ids, outcome, revoke`. Outcomes: disconnected, already_disconnected,
review_recorded, already_recorded, would_disconnect, would_record,
not_flagged (unknown id, another workspace, or not flagged by that check --
deliberately not distinguished), not_confirmed (declined at the prompt).
Revoke: ok (Google confirmed -- see step 4 above), error, skipped_unsafe,
credential_unavailable, interrupted, would_revoke, would_skip_unsafe, or
empty. `--dry-run` simulates the run: a row flagged by several checks is
`would_disconnect` once and `already_disconnected` for its other targets.

`ECC_DATABASE_URL` must be set explicitly (no `.env`/default fallback).
Exit codes: 0 every target remediated (or already was) and no revoke error;
1 some target not_flagged / not_confirmed, or a revoke error /
credential_unavailable; 2 error (environment, arguments, missing
confirmation, unknown workspace, a database error, or Ctrl-C -- in a real
run, rows committed before the stop stay committed under the printed run id
and are still printed as CSV with their revoke column and the summary; a
dry run, or a stop before any write, reports "nothing was written"; re-run
to continue). Errors print only the exception class and SQLSTATE.
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import astuple, dataclass, fields, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID, uuid4

from sqlalchemy import Engine, create_engine, make_url, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

try:
    from scripts import audit_connector_ownership as audit
except ModuleNotFoundError:  # Direct execution adds scripts/, not the repository root, to sys.path.
    import audit_connector_ownership as audit  # type: ignore[no-redef,import-not-found]

if TYPE_CHECKING:
    from ecc.auth import AuthContext
    from ecc.domains.engineering.connectors import ConnectorAccountContext

# `ecc.*` is imported lazily (inside functions): importing it reads settings
# and builds engines, and any failure there must surface through `main()`'s
# guarded region (exit 2, class only), never as a traceback that could echo
# a rejected setting's value.

EXIT_CLEAN: Final = 0
EXIT_ATTENTION: Final = 1
EXIT_ERROR: Final = 2

DATABASE_URL_ENV: Final = "ECC_DATABASE_URL"

_STATEMENT_TIMEOUT: Final = "120s"
_LOCK_TIMEOUT: Final = "5s"

REMEDIABLE_CHECKS: Final[tuple[str, ...]] = ("A", "B", "D", "E")
_DISCONNECT_CHECKS: Final = frozenset({"A", "E"})
REASON: Final = "operator_remediation"
REVIEW_EVENT_TYPE: Final = "connector_ownership.review_recorded"
REVIEW_DECISION_CONNECTOR: Final = "reviewed_no_identity_mismatch"
REVIEW_DECISION_OTHER: Final = "reviewed_not_applicable"
NOTIFICATION_TYPE: Final = "connector_account.disconnected"

_logger = logging.getLogger("scripts.remediate_connector_ownership")


@dataclass(frozen=True, order=True)
class Target:
    check: str
    row_id: UUID

    def __str__(self) -> str:
        return f"{self.check}:{self.row_id}"


@dataclass(frozen=True, order=True)
class Result:
    """One CSV row: ids and code-defined values only."""

    check: str
    table: str
    row_id: str
    workspace_id: str
    ref_ids: str
    outcome: str
    revoke: str


CSV_HEADER: Final[tuple[str, ...]] = tuple(f.name for f in fields(Result))


@dataclass
class _PendingRevoke:
    provider: str
    row_id: UUID
    external_account_id: str
    context: ConnectorAccountContext | None
    error_class: str | None


class InputError(Exception):
    """An operator input problem; the message carries ids only."""


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def parse_target(value: str) -> Target:
    check, sep, row = value.strip().partition(":")
    check = check.strip().upper()
    if not sep or check not in audit.CHECKS:
        raise argparse.ArgumentTypeError("expected CHECK:ROW_ID, CHECK one of A B C D E")
    try:
        return Target(check, UUID(row.strip()))
    except ValueError:
        raise argparse.ArgumentTypeError("ROW_ID must be a UUID") from None


def targets_from_csv(path: str, workspace_id: UUID) -> tuple[list[Target], int]:
    """A/B/D/E targets from an audit CSV (one per `(check, row_id)`), plus
    the number of check C rows skipped (report-only)."""
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != audit.CSV_HEADER:
            raise InputError("csv header does not match audit_connector_ownership output")
        targets: set[Target] = set()
        skipped_c = 0
        for line, row in enumerate(reader, start=2):
            check = row["check"]
            if check not in audit.CHECKS:
                raise InputError(f"csv line {line}: unknown check")
            try:
                row_ws = UUID(row["workspace_id"])
                row_id = UUID(row["row_id"])
            except (ValueError, TypeError, AttributeError):  # short row -> None
                raise InputError(f"csv line {line}: invalid id") from None
            if row_ws != workspace_id:
                raise InputError(
                    f"csv line {line}: row of another workspace (re-run the audit with "
                    "--workspace-id)"
                )
            if check == "C":
                skipped_c += 1
                continue
            targets.add(Target(check, row_id))
    return sorted(targets), skipped_c


def collect_targets(args: argparse.Namespace) -> tuple[list[Target], int]:
    targets: set[Target] = set(args.target or [])
    for target in targets:
        if target.check not in REMEDIABLE_CHECKS:
            raise InputError(
                f"{target}: check C is report-only (superseded by the S1.8(b) backfill)"
            )
    skipped_c = 0
    if args.csv is not None:
        from_csv, skipped_c = targets_from_csv(args.csv, args.workspace_id)
        targets.update(from_csv)
    if not targets:
        raise InputError("no A/B/D/E targets given (use --target or --csv)")
    return sorted(targets), skipped_c


def _by_row(targets: Sequence[Target]) -> dict[UUID, list[Target]]:
    rows: dict[UUID, list[Target]] = {}
    for target in sorted(targets):
        rows.setdefault(target.row_id, []).append(target)
    return rows


def confirmed_targets(
    targets: Sequence[Target],
    confirmations: Sequence[Target],
    *,
    interactive: bool,
) -> set[Target] | None:
    """Per-row confirmation (rollout R4), per DISTINCT row: every targeted
    row needs one `--confirm` naming any of its target `CHECK:ROW_ID` pairs
    (which confirms all of that row's targets), and every `--confirm` must
    name a target. Without `--confirm`: None on a terminal (the caller then
    asks each row with `prompt_targets`), otherwise an error."""
    wanted = set(targets)
    if confirmations:
        given = set(confirmations)
        confirmed_rows = {t.row_id for t in given & wanted}
        missing = [
            "/".join(map(str, row_targets))
            for row_id, row_targets in _by_row(targets).items()
            if row_id not in confirmed_rows
        ]
        extra = sorted(given - wanted)
        if missing or extra:
            parts = []
            if missing:
                parts.append("unconfirmed: " + " ".join(missing))
            if extra:
                parts.append("confirmed but not targeted: " + " ".join(map(str, extra)))
            raise InputError("; ".join(parts))
        return wanted
    if not interactive:
        raise InputError(
            "per-row confirmation required: pass --confirm CHECK:ROW_ID for every target "
            "(or run on a terminal to be asked per row)"
        )
    return None


class ConfirmationAborted(Exception):
    """End of input or Ctrl-C at a prompt: the whole run stops, nothing is
    written."""


def ask_on_stderr(prompt: str) -> str:
    """Prompt on stderr, answer from stdin: stdout carries only the CSV
    (`> result.csv` must neither hide the prompt nor capture it)."""
    sys.stderr.write(prompt)
    sys.stderr.flush()
    line = sys.stdin.readline()
    if not line:
        raise EOFError
    return line


def prompt_targets(
    targets: Sequence[Target],
    describe: Callable[[UUID], str],
    ask: Callable[[str], str],
) -> set[Target]:
    """One prompt per distinct row, listing all of its checks."""
    accepted: set[Target] = set()
    for row_id, row_targets in _by_row(targets).items():
        checks = ",".join(t.check for t in row_targets)
        action = (
            "disconnect (no purge) + revoke iff safe"
            if any(t.check in _DISCONNECT_CHECKS for t in row_targets)
            else "disconnect if identity mismatched, else record review"
        )
        try:
            answer = ask(
                f"remediate row {row_id} checks={checks} [{describe(row_id)}] ({action})? [y/N] "
            )
        except (EOFError, KeyboardInterrupt):
            raise ConfirmationAborted from None
        if answer.strip().lower() in {"y", "yes"}:
            accepted.update(row_targets)
    return accepted


def describe_targets(
    engine: Engine, targets: Sequence[Target], workspace_id: UUID
) -> dict[UUID, str]:
    """Per distinct row, ids/codes only, from the audit's own checks, for
    the prompts."""
    described: dict[UUID, str] = {}
    with readonly_session(engine) as session:
        for row_id, row_targets in _by_row(targets).items():
            described[row_id] = "not flagged now"
            for target in row_targets:
                findings = _findings(session, target, workspace_id)
                if findings:
                    f = findings[0]
                    described[row_id] = (
                        f"table={f.table} owner_id={f.owner_id} "
                        f"row_status={f.row_status or '-'} "
                        f"identity_mismatch={f.identity_mismatch or '-'}"
                    )
                    break
    return described


# ---------------------------------------------------------------------------
# Remediation
# ---------------------------------------------------------------------------


def _set_local_timeouts(session: Session) -> None:
    session.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
    session.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))


def _findings(session: Session, target: Target, workspace_id: UUID) -> list[audit.Finding]:
    return [
        f
        for f in audit.run_check(session.connection(), target.check, workspace_id=workspace_id)
        if f.row_id == str(target.row_id)
    ]


# Literal statements only (no table name is ever interpolated), one per
# table of the personal data set; `main()` refuses to run if that set has
# grown a table missing here.
_LOCK_ROW_SQL: Final[dict[str, str]] = {
    "ai_run_steps": "SELECT id FROM ai_run_steps WHERE id = :id AND workspace_id = :ws FOR UPDATE",
    "ai_runs": "SELECT id FROM ai_runs WHERE id = :id AND workspace_id = :ws FOR UPDATE",
    "attention_items": (
        "SELECT id FROM attention_items WHERE id = :id AND workspace_id = :ws FOR UPDATE"
    ),
    "connector_accounts": (
        "SELECT id FROM connector_accounts WHERE id = :id AND workspace_id = :ws FOR UPDATE"
    ),
    "pkos_evidence": (
        "SELECT id FROM pkos_evidence WHERE id = :id AND workspace_id = :ws FOR UPDATE"
    ),
    "recommendations": (
        "SELECT id FROM recommendations WHERE id = :id AND workspace_id = :ws FOR UPDATE"
    ),
    "sync_cursors": "SELECT id FROM sync_cursors WHERE id = :id AND workspace_id = :ws FOR UPDATE",
    "sync_runs": "SELECT id FROM sync_runs WHERE id = :id AND workspace_id = :ws FOR UPDATE",
}


def _lock_rows(session: Session, findings: Sequence[audit.Finding], workspace_id: UUID) -> None:
    for table in sorted({f.table for f in findings}):
        statement = _LOCK_ROW_SQL.get(table)
        if statement is None:
            raise RuntimeError("finding names a table outside the personal data set")
        session.execute(text(statement), {"id": UUID(findings[0].row_id), "ws": workspace_id})


def _system_auth(workspace_id: UUID) -> AuthContext:
    from ecc.auth import AuthContext  # noqa: PLC0415

    # `user_id` is never read: every write below passes `actor_id=None`.
    return AuthContext(workspace_id=workspace_id, user_id=uuid4(), timezone="UTC")


def _audit_table_type(table: str) -> str:
    return "connector_account" if table == "connector_accounts" else table


def _review_exists(session: Session, workspace_id: UUID, finding: audit.Finding) -> bool:
    return bool(
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM audit_events WHERE workspace_id = :ws "
                "AND event_type = :event_type AND aggregate_id = :row_id "
                "AND metadata->>'check' = :check "
                "AND metadata->>'ref_id' IS NOT DISTINCT FROM CAST(:ref_id AS text))"
            ),
            {
                "ws": workspace_id,
                "event_type": REVIEW_EVENT_TYPE,
                "row_id": UUID(finding.row_id),
                "check": finding.check,
                "ref_id": finding.ref_id or None,
            },
        ).scalar_one()
    )


def _record_reviews(
    session: Session,
    findings: Sequence[audit.Finding],
    workspace_id: UUID,
    *,
    dry_run: bool,
    run_id: UUID,
    now: datetime,
) -> str:
    from ecc.platform import audit_outbox  # noqa: PLC0415

    new = [f for f in findings if not _review_exists(session, workspace_id, f)]
    if not new:
        return "already_recorded"
    if dry_run:
        return "would_record"
    for finding in new:
        audit_outbox.write_audit_and_outbox(
            session,
            _system_auth(workspace_id),
            None,
            event_type=REVIEW_EVENT_TYPE,
            aggregate_type=_audit_table_type(finding.table),
            aggregate_id=UUID(finding.row_id),
            aggregate_version=1,
            changed_fields=[],
            payload={"aggregate_id": finding.row_id, "check": finding.check},
            now=now,
            domain="connector_ownership_remediation",
            metadata={
                "reason": REASON,
                "check": finding.check,
                "table": finding.table,
                "ref_table": finding.ref_table,
                "ref_id": finding.ref_id or None,
                "decision": (
                    REVIEW_DECISION_CONNECTOR
                    if finding.table == "connector_accounts"
                    else REVIEW_DECISION_OTHER
                ),
                "run_id": str(run_id),
            },
            source="system",
            actor_id=None,
            emit_outbox=False,
        )
    return "review_recorded"


def _notify_owner(
    session: Session, *, workspace_id: UUID, owner_id: UUID, row_id: UUID, now: datetime
) -> None:
    """Spec A C15-f: the row owner only, and only while an active member
    (a removed member -- check E -- can no longer read it)."""
    from ecc.platform.connector_security import member_is_active  # noqa: PLC0415

    if not member_is_active(session, workspace_id=workspace_id, users_id=owner_id):
        return
    session.execute(
        text(
            """
            INSERT INTO member_notifications (
                id, workspace_id, account_id, notification_type, resource_ref, created_at
            )
            SELECT :id, :ws, u.account_id, :notification_type, :resource_ref, :now
            FROM users u WHERE u.id = :owner_id AND u.workspace_id = :ws
            ON CONFLICT (workspace_id, account_id, notification_type, resource_ref) DO NOTHING
            """
        ),
        {
            "id": uuid4(),
            "ws": workspace_id,
            "notification_type": NOTIFICATION_TYPE,
            "resource_ref": f"connector_accounts:{row_id}",
            "now": now,
            "owner_id": owner_id,
        },
    )


def _disconnect(
    session: Session,
    target: Target,
    findings: Sequence[audit.Finding],
    workspace_id: UUID,
    *,
    dry_run: bool,
    run_id: UUID,
    now: datetime,
) -> tuple[str, str, _PendingRevoke | None]:
    from ecc.domains.engineering.connectors import ConnectorAccountContext  # noqa: PLC0415
    from ecc.domains.engineering.crypto import decrypt_credential  # noqa: PLC0415
    from ecc.platform import audit_outbox  # noqa: PLC0415
    from ecc.platform.connector_security import (  # noqa: PLC0415
        revoke_is_safe,
        revoke_scope_for,
    )

    row = (
        session.execute(
            text(
                "SELECT provider, external_account_id, status, owner_id, encrypted_credentials "
                "FROM connector_accounts WHERE id = :id AND workspace_id = :ws"
            ),
            {"id": target.row_id, "ws": workspace_id},
        )
        .mappings()
        .one()
    )
    if row["status"] == "disconnected":
        return "already_disconnected", "", None
    provider: str = row["provider"]
    external_account_id: str = row["external_account_id"]
    if dry_run:
        safe = revoke_is_safe(
            session,
            provider=provider,
            external_account_id=external_account_id,
            token_kind="disconnected_row",
            exclude_row_id=target.row_id,
            scope=revoke_scope_for(provider),
        )
        return "would_disconnect", "would_revoke" if safe else "would_skip_unsafe", None
    context: ConnectorAccountContext | None = None
    error_class: str | None = None
    try:
        context = ConnectorAccountContext(
            workspace_id=workspace_id,
            connector_account_id=target.row_id,
            external_account_id=external_account_id,
            credential=decrypt_credential(bytes(row["encrypted_credentials"])),
        )
    except Exception as exc:  # noqa: BLE001 -- best-effort; never blocks the disconnect
        error_class = type(exc).__name__
    version = session.execute(
        text(
            "UPDATE connector_accounts SET status = 'disconnected', disconnected_at = :now, "
            "updated_at = :now, version = version + 1 "
            "WHERE id = :id AND workspace_id = :ws RETURNING version"
        ),
        {"now": now, "id": target.row_id, "ws": workspace_id},
    ).scalar_one()
    ref_ids = sorted({f.ref_id for f in findings if f.ref_id})
    audit_outbox.write_audit_and_outbox(
        session,
        _system_auth(workspace_id),
        None,
        event_type="connector_account.disabled",
        aggregate_type="connector_account",
        aggregate_id=target.row_id,
        aggregate_version=version,
        changed_fields=["*"],
        payload={
            "aggregate_id": str(target.row_id),
            "version": version,
            "reason": REASON,
            "check": target.check,
        },
        now=now,
        domain="engineering_connector_account",
        metadata={
            "reason": REASON,
            "check": target.check,
            "ref_ids": ref_ids,
            "run_id": str(run_id),
        },
        source="system",
        actor_id=None,
    )
    _notify_owner(
        session, workspace_id=workspace_id, owner_id=row["owner_id"], row_id=target.row_id, now=now
    )
    pending = _PendingRevoke(
        provider=provider,
        row_id=target.row_id,
        external_account_id=external_account_id,
        context=context,
        error_class=error_class,
    )
    return "disconnected", "", pending


def _remediate_in_txn(
    session: Session,
    target: Target,
    workspace_id: UUID,
    *,
    dry_run: bool,
    run_id: UUID,
    now: datetime,
) -> tuple[Result, _PendingRevoke | None]:
    from ecc.platform.connector_security import lock_membership_shared  # noqa: PLC0415

    def result(table: str, findings: Sequence[audit.Finding], outcome: str, revoke: str) -> Result:
        refs = ";".join(sorted({f.ref_id for f in findings if f.ref_id}))
        return Result(
            target.check, table, str(target.row_id), str(workspace_id), refs, outcome, revoke
        )

    if not dry_run:
        # Advisory lock before row locks (member removal's order).
        lock_membership_shared(session, workspace_id)
        pre = _findings(session, target, workspace_id)
        if not pre:
            return result("", [], "not_flagged", ""), None
        _lock_rows(session, pre, workspace_id)
    # (Re-)evaluated after the row lock: only a row that is still flagged
    # is changed.
    findings = _findings(session, target, workspace_id)
    if not findings:
        return result("", [], "not_flagged", ""), None
    table = findings[0].table
    mismatched = table == "connector_accounts" and any(
        f.identity_mismatch == "true" for f in findings
    )
    if target.check in _DISCONNECT_CHECKS or mismatched:
        outcome, revoke, pending = _disconnect(
            session, target, findings, workspace_id, dry_run=dry_run, run_id=run_id, now=now
        )
        return result(table, findings, outcome, revoke), pending
    outcome = _record_reviews(
        session, findings, workspace_id, dry_run=dry_run, run_id=run_id, now=now
    )
    return result(table, findings, outcome, ""), None


def _revoke(pending: _PendingRevoke) -> str:
    """After the row's transaction committed. Never raises."""
    from ecc.domains.personal import gmail_revocation  # noqa: PLC0415
    from ecc.observability import record_connector_revoke  # noqa: PLC0415
    from ecc.platform.connector_security import revoke_if_safe  # noqa: PLC0415

    if pending.provider != "gmail":
        return ""  # no revoking adapter wired for another personal provider
    if pending.context is None:
        # Exception class only -- never its message or the credential.
        _logger.warning(
            "remediation_revoke_credential_unavailable error_class=%s", pending.error_class
        )
        record_connector_revoke(pending.provider, "remediation", "error")
        return "credential_unavailable"
    # Read at call time so the module-level adapter stays patchable.
    return revoke_if_safe(
        gmail_revocation._adapter,
        pending.context,
        provider=pending.provider,
        external_account_id=pending.external_account_id,
        token_kind="disconnected_row",
        exclude_row_id=pending.row_id,
        site="remediation",
    )


@contextmanager
def readonly_session(engine: Engine) -> Iterator[Session]:
    """One `REPEATABLE READ, READ ONLY` transaction, always rolled back;
    refuses to proceed unless the server confirms it is read-only."""
    with engine.connect() as conn:
        try:
            conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
            if conn.execute(text("SHOW transaction_read_only")).scalar_one() != "on":
                raise RuntimeError("transaction is not read-only")
            with Session(bind=conn) as session:
                _set_local_timeouts(session)
                yield session
        finally:
            conn.rollback()


def remediate(
    engine: Engine,
    targets: Sequence[Target],
    *,
    workspace_id: UUID,
    dry_run: bool,
    run_id: UUID,
    declined: frozenset[Target] = frozenset(),
    results: list[Result] | None = None,
) -> list[Result]:
    """Appends to `results` (when given) as it goes, so a caller that is
    interrupted still has every committed row's result."""
    if results is None:
        results = []
    for target in targets:
        if target in declined:
            results.append(
                Result(
                    target.check, "", str(target.row_id), str(workspace_id), "", "not_confirmed", ""
                )
            )
    todo = [t for t in targets if t not in declined]
    if dry_run:
        # Simulates the real run: a row's first target disconnects it, so
        # its other targets would find it already disconnected.
        would_disconnect: set[str] = set()
        with readonly_session(engine) as session:
            for target in todo:
                result, _ = _remediate_in_txn(
                    session,
                    target,
                    workspace_id,
                    dry_run=True,
                    run_id=run_id,
                    now=datetime.now(UTC),
                )
                if result.outcome == "would_disconnect":
                    if result.row_id in would_disconnect:
                        result = replace(result, outcome="already_disconnected", revoke="")
                    would_disconnect.add(result.row_id)
                results.append(result)
        return sorted(results)
    for target in todo:
        with Session(engine) as session, session.begin():
            _set_local_timeouts(session)
            result, pending = _remediate_in_txn(
                session, target, workspace_id, dry_run=False, run_id=run_id, now=datetime.now(UTC)
            )
        if pending is None:
            results.append(result)
            continue
        # Committed: recorded now, so an interrupt during the revoke still
        # reports the row (as `interrupted`).
        results.append(replace(result, revoke="interrupted"))
        results[-1] = replace(result, revoke=_revoke(pending))
    return sorted(results)


# ---------------------------------------------------------------------------
# Output, CLI
# ---------------------------------------------------------------------------


def render_csv(results: Sequence[Result]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    writer.writerows(astuple(r) for r in results)
    return buffer.getvalue()


_OK_OUTCOMES: Final = frozenset(
    {
        "disconnected",
        "already_disconnected",
        "review_recorded",
        "already_recorded",
        "would_disconnect",
        "would_record",
    }
)
_BAD_REVOKES: Final = frozenset({"error", "credential_unavailable", "interrupted"})


def exit_code(results: Sequence[Result]) -> int:
    if all(r.outcome in _OK_OUTCOMES and r.revoke not in _BAD_REVOKES for r in results):
        return EXIT_CLEAN
    return EXIT_ATTENTION


def _describe_target(database_url: str) -> str:
    """Host, port and database name only -- never the user or password."""
    url = make_url(database_url)
    return f"database: host={url.host or '-'} port={url.port or '-'} name={url.database or '-'}"


def _summarize(results: Sequence[Result], *, mode: str, run_id: UUID, skipped_c: int) -> None:
    print(f"mode: {mode} run_id: {run_id}", file=sys.stderr)
    for outcome, count in sorted(Counter(r.outcome for r in results).items()):
        print(f"outcome {outcome}: {count}", file=sys.stderr)
    # Per distinct row: a connector flagged by two checks is one revoke.
    revokes = Counter(revoke for _, revoke in {(r.row_id, r.revoke) for r in results if r.revoke})
    for revoke, count in sorted(revokes.items()):
        label = "connector_revoke_total{site=remediation}" if mode == "remediate" else "revoke"
        print(f"{label} {revoke} (distinct rows): {count}", file=sys.stderr)
    if skipped_c:
        print(f"check C rows skipped (report-only): {skipped_c}", file=sys.stderr)
    if mode == "remediate":
        print(
            "re-run audit_connector_ownership.py: remediated A/E rows show row_status=disconnected",
            file=sys.stderr,
        )


_WRITE_OUTCOMES: Final = frozenset({"disconnected", "review_recorded"})


def _partial_output(results: Sequence[Result], *, mode: str, run_id: UUID, skipped_c: int) -> None:
    """A real run stopped by an error or Ctrl-C: rows already committed stay
    committed, so print their CSV (with the revoke column -- a re-run reports
    them `already_disconnected` and never retries the revoke) and the
    summary. A dry run, or a stop before any write, wrote nothing."""
    wrote = mode == "remediate" and any(r.outcome in _WRITE_OUTCOMES for r in results)
    if not wrote:
        print(
            f"remediate_connector_ownership: stopped; nothing was written (run_id={run_id})",
            file=sys.stderr,
        )
        return
    ordered = sorted(results)
    sys.stdout.write(render_csv(ordered))
    sys.stdout.flush()
    _summarize(ordered, mode=mode, run_id=run_id, skipped_c=skipped_c)
    print(
        f"remediate_connector_ownership: stopped; rows listed above with outcome "
        f"disconnected/review_recorded were committed under run_id={run_id}; the revoke "
        "of the last committed row may not have happened (revoke=interrupted); check the "
        "revoke column, then re-run to continue",
        file=sys.stderr,
    )


def _report_error(exc: BaseException) -> None:
    """Class + SQLSTATE only: driver messages can carry row values and the
    connection string (with its password)."""
    orig = getattr(exc, "orig", None)
    source = orig if orig is not None else exc
    sqlstate = getattr(source, "sqlstate", None)
    print(
        f"remediate_connector_ownership: error class={type(source).__name__} "
        f"sqlstate={sqlstate if isinstance(sqlstate, str) else '-'}",
        file=sys.stderr,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Operator remediation of S2 audit findings (Spec A): disconnect without purge.",
        epilog="Exit codes: 0 done, 1 rows need attention, 2 error.",
    )
    parser.add_argument("--workspace-id", type=UUID, required=True, help="Workspace of the rows.")
    parser.add_argument(
        "--target",
        type=parse_target,
        action="append",
        metavar="CHECK:ROW_ID",
        help="A row to remediate (repeatable).",
    )
    parser.add_argument("--csv", default=None, help="CSV written by audit_connector_ownership.py.")
    parser.add_argument(
        "--confirm",
        type=parse_target,
        action="append",
        default=[],
        metavar="CHECK:ROW_ID",
        help=(
            "Confirm one row (repeat once per distinct row; any of the row's CHECK:ROW_ID "
            "pairs confirms all its targets; required unless on a terminal)."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; write nothing.")
    return parser.parse_args(argv)


def _workspace_exists(engine: Engine, workspace_id: UUID) -> bool:
    with engine.connect() as conn:
        exists = conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM workspaces WHERE id = :id)"), {"id": workspace_id}
        ).scalar_one()
        conn.rollback()
    return bool(exists)


def main(argv: list[str] | None = None, *, ask: Callable[[str], str] = ask_on_stderr) -> int:
    args = _parse_args(argv)
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not database_url:
        print(
            f"remediate_connector_ownership: {DATABASE_URL_ENV} must be set explicitly "
            "in the environment",
            file=sys.stderr,
        )
        return EXIT_ERROR
    mode = "dry-run" if args.dry_run else "remediate"
    run_id = uuid4()
    accepted: set[Target] | None = None
    results: list[Result] = []
    skipped_c = 0
    try:
        targets, skipped_c = collect_targets(args)
        if not args.dry_run:
            accepted = confirmed_targets(targets, args.confirm, interactive=sys.stdin.isatty())
    except InputError as exc:
        print(f"remediate_connector_ownership: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # malformed input: class only, never a traceback or content
        print(
            f"remediate_connector_ownership: invalid input class={type(exc).__name__}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        target = _describe_target(database_url)
        # Surfaces any ecc import/settings error here, and refuses to run if
        # the personal data set has grown a table this command cannot lock.
        if set(audit._personal_data_set()[0]) - set(_LOCK_ROW_SQL):
            raise RuntimeError("personal data set has tables this command does not handle")
        engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
        try:
            if not _workspace_exists(engine, args.workspace_id):
                print("remediate_connector_ownership: workspace not found", file=sys.stderr)
                return EXIT_ERROR
            print(target, file=sys.stderr)
            if not args.dry_run and accepted is None:
                described = describe_targets(engine, targets, args.workspace_id)
                accepted = prompt_targets(targets, described.__getitem__, ask)
            declined = frozenset(set(targets) - accepted) if accepted is not None else frozenset()
            print(f"remediate_connector_ownership: mode={mode} run_id={run_id}", file=sys.stderr)
            remediate(
                engine,
                targets,
                workspace_id=args.workspace_id,
                dry_run=args.dry_run,
                run_id=run_id,
                declined=declined,
                results=results,
            )
        finally:
            engine.dispose()
    except KeyboardInterrupt:
        print("remediate_connector_ownership: interrupted", file=sys.stderr)
        _partial_output(results, mode=mode, run_id=run_id, skipped_c=skipped_c)
        return EXIT_ERROR
    except ConfirmationAborted:
        print(
            "remediate_connector_ownership: confirmation aborted; nothing written",
            file=sys.stderr,
        )
        return EXIT_ERROR
    except Exception as exc:  # never let a traceback print row data, a setting or the URL
        _report_error(exc)
        _partial_output(results, mode=mode, run_id=run_id, skipped_c=skipped_c)
        return EXIT_ERROR
    results = sorted(results)
    sys.stdout.write(render_csv(results))
    sys.stdout.flush()
    _summarize(results, mode=mode, run_id=run_id, skipped_c=skipped_c)
    return exit_code(results)


if __name__ == "__main__":
    raise SystemExit(main())
