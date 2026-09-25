"""Pre-deploy connector-ownership audit (Spec A S2) -- strictly read-only.

Reports personal-data rows (Spec A "Shared definitions" -> "Personal data
set", taken verbatim from `ecc.platform.connector_security`) that must be
reviewed or remediated before `ECC_PERSONAL_DATA_ISOLATION` is enabled:

    A  identity_mismatch   personal connector whose `external_account_id`
                           differs from its owner's account email
                           (lower/trim). Remediation: disconnect + revoke
                           iff safe; notify owner.
    B  transferred         `ownership_transfers` row on a personal-data row.
                           Remediation: as A if mismatched, else record.
    C  shared              active (`revoked_at IS NULL`) `resource_grants`
                           row on a personal-data row, or a personal
                           connector with `visibility='shared_explicitly'`.
                           Report only (superseded by the S1.8(b) backfill).
    D  reactivated_by_non_owner
                           allowed `connector_account.reconnected` audit
                           event on a personal connector whose actor is not
                           the connector's owner. Remediation: review; as A
                           if mismatched.
    E  removed_member      personal connector whose owner's membership in
                           that workspace is `removed`. Remediation:
                           disconnect + revoke iff safe.

Checks A, B, D and E must be remediated (not just reviewed) before the
isolation flag is enabled. Rows the evaluation harness writes are excluded
by the exact shape it writes (see `_EVALUATION_*` below), never a `LIKE`.

Judging remediation: the audit lists matching rows; it does not track
whether they were handled. A and E rows stay listed after the spec's
disconnect remediation (no purge), with `row_status=disconnected`; B and D
read history tables (`ownership_transfers`, `audit_events`) and never
disappear; C is report-only (superseded by the S1.8(b) backfill). So on a
re-run, filter by `check` and `row_status`: A/E are remediated when every
row is `disconnected`; B/D rows are remediated when the connector they name
is `disconnected` (identity_mismatch=true) or its review is recorded.

Read-only guarantees: one `REPEATABLE READ, READ ONLY` transaction (a
consistent snapshot across all five checks) that is always rolled back; no
application code that writes or audits is called. Run it under a read-only
database role where one exists. Do not run it while migrations are running
(it waits at most `lock_timeout` 5s for a lock, then fails), and do not
start a migration or deploy while it runs: its read locks would make an
ACCESS EXCLUSIVE migration queue, and the app's queries queue behind that.
On large databases run it in `--workspace-id` batches to keep each run short.

Output: CSV of ids only (plus code-defined enum/boolean columns) -- never
emails, tokens, credentials, display names or message content. Errors print
only the exception class and SQLSTATE (the driver message can carry row
values and the connection string).

CLI usage -- from a repository checkout (`scripts/` is not in the backend
image, whose container is also read-only), pointing at the target database:

    PYTHONPATH=backend ECC_DATABASE_URL=<url> \
        uv run python scripts/audit_connector_ownership.py [--out PATH]

    PYTHONPATH=backend ECC_DATABASE_URL=<url> \
        uv run python scripts/audit_connector_ownership.py --workspace-id <UUID>

`ECC_DATABASE_URL` must be set explicitly in the environment (no `.env` /
default fallback; exit 2 otherwise). CSV goes to stdout (or `--out PATH`,
set to mode 0600 whether created or overwritten; a symlink is refused); the
audited host/port/database name (never user or password) and a per-check
count summary go to stderr.

Exit codes: 0 no rows listed, 1 rows listed ("findings listed", not
"unresolved" -- see "Judging remediation" above), 2 error (nothing written
to stdout / `--out`; startup, settings and import errors, and argparse
usage errors, also exit 2). A statement timeout (120s per statement)
exits 2 with `sqlstate=57014`: re-run per workspace with `--workspace-id`.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import astuple, dataclass, fields
from typing import Any, Final, TextIO
from uuid import UUID

from sqlalchemy import Connection, Row, create_engine, make_url, text
from sqlalchemy.pool import NullPool

# `ecc.*` is imported lazily (`_personal_data_set`): importing it reads
# settings and builds engines, and any failure there must surface through
# `main()`'s guarded region (exit 2, class only), never as a traceback that
# could echo a rejected setting's value.

EXIT_CLEAN: Final = 0
EXIT_FINDINGS: Final = 1
EXIT_ERROR: Final = 2

# Must be set explicitly in the process environment: the app's settings
# fall back to `.env` / a localhost default, which would silently audit the
# wrong database.
DATABASE_URL_ENV: Final = "ECC_DATABASE_URL"

# Generous per-statement budget for a one-off audit over a full database
# (the app engine's 5 s budget is for request latency); applied with
# `SET LOCAL`, so it lives and dies with the read-only transaction.
_STATEMENT_TIMEOUT: Final = "120s"
# Never queue behind (or hold up) a migration's locks for long.
_LOCK_TIMEOUT: Final = "5s"

# The evaluation harness's synthetic Gmail connector, exactly as
# `ecc.domains.ai_runtime.evaluation._insert_synthetic_email_thread` writes
# it: provider 'gmail', display_name 'evaluation harness', and
# external_account_id = 'evaluation-' || <that row's own id>. The harness
# has no importable constant for these literals, so they are mirrored here
# and pinned by `tests/test_audit_connector_ownership_postgres.py`, which
# runs the harness's own insert and asserts this predicate excludes it. A
# real mailbox such as `evaluation-x@gmail.com` cannot match: its
# external_account_id is never `'evaluation-' || id::text`.
_EVALUATION_PROVIDER: Final = "gmail"
_EVALUATION_DISPLAY_NAME: Final = "evaluation harness"
_EVALUATION_EXTERNAL_ID_PREFIX: Final = "evaluation-"


def _personal_data_set() -> tuple[Mapping[str, str], dict[str, object]]:
    """Spec A's personal data set, from its single source of truth."""
    from ecc.platform.connector_security import (  # noqa: PLC0415 -- see module note
        PERSONAL_ROW_PREDICATES,
        personal_sql_params,
    )

    return PERSONAL_ROW_PREDICATES, personal_sql_params()


def _evaluation_connector(alias: str) -> str:
    return (
        f"({alias}.provider = :eval_provider "
        f"AND {alias}.display_name = :eval_display_name "
        f"AND {alias}.external_account_id = :eval_prefix || CAST({alias}.id AS text))"
    )


def _not_evaluation_row(table: str) -> str:
    """Excludes evaluation-harness rows of `table` (unaliased): the
    harness connector itself, and runs/cursors hanging off it."""
    if table == "connector_accounts":
        return f"NOT {_evaluation_connector('connector_accounts')}"
    if table in ("sync_runs", "sync_cursors"):
        return (
            "NOT EXISTS (SELECT 1 FROM connector_accounts eca "
            f"WHERE eca.id = {table}.connector_account_id "
            f"AND eca.workspace_id = {table}.workspace_id "
            f"AND {_evaluation_connector('eca')})"
        )
    return "TRUE"


def _workspace_filter(column: str) -> str:
    return f"(CAST(:workspace_id AS uuid) IS NULL OR {column} = CAST(:workspace_id AS uuid))"


def _identity_mismatch(alias: str) -> str:
    """Spec S2 check A's predicate over a `connector_accounts` row -- also
    reported on B-E connector rows ("as A if mismatched")."""
    return (
        "EXISTS (SELECT 1 FROM users u JOIN accounts a ON a.id = u.account_id "
        f"WHERE u.id = {alias}.owner_id AND u.workspace_id = {alias}.workspace_id "
        f"AND lower(trim({alias}.external_account_id)) <> lower(trim(a.email)))"
    )


_IDENTITY_MISMATCH: Final = _identity_mismatch("ca")

_PERSONAL_CONNECTOR: Final = "ca.provider = ANY(:providers)"
_NOT_EVAL_CA: Final = f"NOT {_evaluation_connector('ca')}"


@dataclass(frozen=True, order=True)
class Finding:
    """One CSV row. Every field is an id, a code-defined check/table name,
    a `connector_accounts.status` enum value, or a boolean -- never row
    content."""

    check: str
    table: str
    row_id: str
    workspace_id: str
    owner_id: str
    ref_table: str
    ref_id: str
    row_status: str
    identity_mismatch: str


CSV_HEADER: Final[tuple[str, ...]] = tuple(f.name for f in fields(Finding))

CHECKS: Final[dict[str, str]] = {
    "A": "identity_mismatch",
    "B": "transferred",
    "C": "shared",
    "D": "reactivated_by_non_owner",
    "E": "removed_member",
}


def _s(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _finding(check: str, table: str, row: Row[Any], ref_table: str = "") -> Finding:
    mapping = row._mapping
    return Finding(
        check=check,
        table=table,
        row_id=_s(mapping["row_id"]),
        workspace_id=_s(mapping["workspace_id"]),
        owner_id=_s(mapping["owner_id"]),
        ref_table=ref_table,
        ref_id=_s(mapping.get("ref_id")),
        row_status=_s(mapping["status"]),
        identity_mismatch=_s(mapping["mismatch"]),
    )


def _check_a(conn: Connection, params: dict[str, object]) -> list[Finding]:
    rows = conn.execute(
        text(
            f"""
            SELECT ca.id AS row_id, ca.workspace_id, ca.owner_id, ca.status, TRUE AS mismatch
            FROM connector_accounts ca
            WHERE {_PERSONAL_CONNECTOR} AND {_NOT_EVAL_CA}
              AND {_workspace_filter("ca.workspace_id")}
              AND {_IDENTITY_MISMATCH}
            """  # noqa: S608 -- code-defined fragments only; values are bound
        ),
        params,
    ).all()
    return [_finding("A", "connector_accounts", row) for row in rows]


def _personal_row_join(ref_alias: str, table: str) -> str:
    """`JOIN <table>` (unaliased, as `PERSONAL_ROW_PREDICATES` requires) on
    `<ref_alias>.resource_type/resource_id`, restricted to personal-data
    rows that are not evaluation-harness rows."""
    return (
        f"JOIN {table} ON {table}.id = {ref_alias}.resource_id "
        f"AND {table}.workspace_id = {ref_alias}.workspace_id "
        f"AND {ref_alias}.resource_type = '{table}' "
        f"AND ({_personal_data_set()[0][table]}) AND {_not_evaluation_row(table)}"
    )


def _row_extras(table: str) -> str:
    """`status` and `mismatch` columns -- only meaningful for connectors."""
    if table != "connector_accounts":
        return "NULL AS status, NULL AS mismatch"
    return (
        "connector_accounts.status AS status, "
        f"{_identity_mismatch('connector_accounts')} AS mismatch"
    )


def _personal_ref_findings(
    conn: Connection,
    params: dict[str, object],
    *,
    check: str,
    ref_table: str,
    ref_where: str,
) -> list[Finding]:
    findings: list[Finding] = []
    for table in sorted(_personal_data_set()[0]):
        rows = conn.execute(
            text(
                f"""
                SELECT {table}.id AS row_id, {table}.workspace_id, {table}.owner_id,
                       r.id AS ref_id, {_row_extras(table)}
                FROM {ref_table} r
                {_personal_row_join("r", table)}
                WHERE {ref_where} AND {_workspace_filter("r.workspace_id")}
                """  # noqa: S608 -- table names come from PERSONAL_ROW_PREDICATES' keys
            ),
            params,
        ).all()
        findings.extend(_finding(check, table, row, ref_table) for row in rows)
    return findings


def _check_b(conn: Connection, params: dict[str, object]) -> list[Finding]:
    return _personal_ref_findings(
        conn, params, check="B", ref_table="ownership_transfers", ref_where="TRUE"
    )


def _check_c(conn: Connection, params: dict[str, object]) -> list[Finding]:
    findings = _personal_ref_findings(
        conn, params, check="C", ref_table="resource_grants", ref_where="r.revoked_at IS NULL"
    )
    rows = conn.execute(
        text(
            f"""
            SELECT ca.id AS row_id, ca.workspace_id, ca.owner_id, ca.status,
                   {_IDENTITY_MISMATCH} AS mismatch
            FROM connector_accounts ca
            WHERE {_PERSONAL_CONNECTOR} AND {_NOT_EVAL_CA}
              AND ca.visibility = 'shared_explicitly'
              AND {_workspace_filter("ca.workspace_id")}
            """  # noqa: S608 -- code-defined fragments only; values are bound
        ),
        params,
    ).all()
    findings.extend(_finding("C", "connector_accounts", row) for row in rows)
    return findings


def _check_d(conn: Connection, params: dict[str, object]) -> list[Finding]:
    rows = conn.execute(
        text(
            f"""
            SELECT ca.id AS row_id, ca.workspace_id, ca.owner_id, ca.status,
                   ae.id AS ref_id, {_IDENTITY_MISMATCH} AS mismatch
            FROM audit_events ae
            JOIN connector_accounts ca
              ON ca.id = ae.aggregate_id AND ca.workspace_id = ae.workspace_id
            WHERE ae.event_type = 'connector_account.reconnected'
              AND ae.aggregate_type = 'connector_account'
              AND ae.authorization_result = 'allowed'
              AND ae.actor_id IS DISTINCT FROM ca.owner_id
              AND {_PERSONAL_CONNECTOR} AND {_NOT_EVAL_CA}
              AND {_workspace_filter("ca.workspace_id")}
            """  # noqa: S608 -- code-defined fragments only; values are bound
        ),
        params,
    ).all()
    return [_finding("D", "connector_accounts", row, "audit_events") for row in rows]


def _check_e(conn: Connection, params: dict[str, object]) -> list[Finding]:
    rows = conn.execute(
        text(
            f"""
            SELECT ca.id AS row_id, ca.workspace_id, ca.owner_id, ca.status,
                   wm.id AS ref_id, {_IDENTITY_MISMATCH} AS mismatch
            FROM connector_accounts ca
            JOIN users u ON u.id = ca.owner_id AND u.workspace_id = ca.workspace_id
            JOIN workspace_memberships wm
              ON wm.workspace_id = ca.workspace_id AND wm.account_id = u.account_id
            WHERE wm.status = 'removed'
              AND {_PERSONAL_CONNECTOR} AND {_NOT_EVAL_CA}
              AND {_workspace_filter("ca.workspace_id")}
            """  # noqa: S608 -- code-defined fragments only; values are bound
        ),
        params,
    ).all()
    return [_finding("E", "connector_accounts", row, "workspace_memberships") for row in rows]


@contextmanager
def readonly_connection(database_url: str) -> Iterator[Connection]:
    """A connection inside one `REPEATABLE READ, READ ONLY` transaction
    that is always rolled back. Refuses to proceed unless the server
    confirms the transaction is read-only."""
    engine = create_engine(database_url, poolclass=NullPool, hide_parameters=True)
    try:
        with engine.connect() as conn:
            try:
                # First statement of the (auto-begun) transaction.
                conn.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
                if conn.execute(text("SHOW transaction_read_only")).scalar_one() != "on":
                    raise RuntimeError("transaction is not read-only")
                conn.execute(text(f"SET LOCAL statement_timeout = '{_STATEMENT_TIMEOUT}'"))
                conn.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
                yield conn
            finally:
                conn.rollback()
    finally:
        engine.dispose()


def run_checks(conn: Connection, *, workspace_id: UUID | None = None) -> list[Finding]:
    params: dict[str, object] = {
        **_personal_data_set()[1],
        "workspace_id": workspace_id,
        "eval_provider": _EVALUATION_PROVIDER,
        "eval_display_name": _EVALUATION_DISPLAY_NAME,
        "eval_prefix": _EVALUATION_EXTERNAL_ID_PREFIX,
    }
    findings: list[Finding] = []
    for check in (_check_a, _check_b, _check_c, _check_d, _check_e):
        findings.extend(check(conn, params))
    return sorted(findings)


def render_csv(findings: Sequence[Finding]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_HEADER)
    writer.writerows(astuple(finding) for finding in findings)
    return buffer.getvalue()


def _write_output(body: str, out_path: str | None) -> None:
    if out_path is None:
        sys.stdout.write(body)
        sys.stdout.flush()
        return
    # No O_TRUNC: an existing file is truncated only after its mode is
    # tightened, so a failed fchmod leaves its prior content intact. The
    # open() mode applies only on create. O_NOFOLLOW refuses a symlink.
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
    except OSError:
        os.close(fd)
        raise
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)


def _describe_target(database_url: str) -> str:
    """Host, port and database name only -- never the user or password."""
    url = make_url(database_url)
    return f"database: host={url.host or '-'} port={url.port or '-'} name={url.database or '-'}"


def _summarize(findings: Sequence[Finding], stream: TextIO, target: str) -> None:
    print(target, file=stream)
    for check, name in CHECKS.items():
        count = sum(1 for finding in findings if finding.check == check)
        suffix = ", report-only, superseded by backfill" if check == "C" else ""
        print(f"check {check} ({name}{suffix}): {count}", file=stream)
    print(f"total: {len(findings)}", file=stream)
    if any(finding.check in "ABDE" for finding in findings):
        print(
            "checks A, B, D, E must be remediated before enabling ECC_PERSONAL_DATA_ISOLATION",
            file=stream,
        )


def _report_error(exc: BaseException) -> None:
    """Class + SQLSTATE only: driver messages can carry row values and the
    connection string (with its password)."""
    orig = getattr(exc, "orig", None)
    source = orig if orig is not None else exc
    sqlstate = getattr(source, "sqlstate", None)
    print(
        f"audit_connector_ownership: error class={type(source).__name__} "
        f"sqlstate={sqlstate if isinstance(sqlstate, str) else '-'}",
        file=sys.stderr,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only pre-deploy connector-ownership audit (Spec A S2).",
        epilog="Exit codes: 0 no findings, 1 findings reported, 2 error.",
    )
    parser.add_argument("--out", default=None, help="Write the CSV here instead of stdout.")
    parser.add_argument(
        "--workspace-id", type=UUID, default=None, help="Audit only this workspace."
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    database_url = os.environ.get(DATABASE_URL_ENV, "").strip()
    if not database_url:
        print(
            f"audit_connector_ownership: {DATABASE_URL_ENV} must be set explicitly "
            "in the environment",
            file=sys.stderr,
        )
        return EXIT_ERROR
    try:
        target = _describe_target(database_url)
        _personal_data_set()  # surface any ecc import/settings error here
        with readonly_connection(database_url) as conn:
            findings = run_checks(conn, workspace_id=args.workspace_id)
        _write_output(render_csv(findings), args.out)
    except Exception as exc:  # never let a traceback print row data, a setting or the URL
        _report_error(exc)
        return EXIT_ERROR
    _summarize(findings, sys.stderr, target)
    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
