"""Generate the Phase 1 backup/restore recovery evidence report.

This script is the "recovery command" referenced by the Phase 1 completion
design doc: it emits a timestamped, machine-readable (JSON) and
human-readable (Markdown) report proving a backup -> restore -> verify cycle
against populated data completed successfully and within the development
RTO budget.

The report contains only counts, checksums (content-hash digests, never raw
content), timestamps, database revisions, and pass/fail invariants -- it
never includes note bodies, task titles, commitment summaries, or any other
seeded field value, in keeping with the project-wide rule that evidence
artifacts must not carry user content.

This script only depends on ``psycopg`` (already a project dependency) and
the standard library (``hashlib``, ``json``, string templating) for the
checksum and report-formatting work -- no new third-party dependency is
introduced, consistent with ``scripts/backup.sh``'s hand-rolled
``sha256sum``/``shasum`` approach.

CLI usage:

    uv run python scripts/phase1_evidence.py \\
        --source-url postgresql://ecc:ecc@localhost:5432/ecc \\
        --target-url postgresql://ecc:ecc@localhost:5432/ecc_restore \\
        --archive .local/backups/ecc-...dump \\
        --elapsed-seconds 42.5 \\
        --output-json .local/evidence/phase1-recovery.json \\
        --output-md .local/evidence/phase1-recovery.md

Exits 0 if every invariant passed, 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

# scripts/ is a plain directory (not necessarily on sys.path when this
# module is loaded via importlib rather than executed directly), so make the
# sibling seed module importable regardless of invocation style.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import seed_phase1_acceptance as seed_fixtures  # noqa: E402

DEFAULT_RTO_BUDGET_SECONDS = 600


def _git_head_sha(root: Path = Path(".")) -> str | None:
    """Return the current git HEAD commit SHA, or None if unavailable.

    Recorded in the evidence report so the Phase 1 acceptance checker
    (scripts/check_phase1_acceptance.py) can detect a stale recovery-drill
    result -- one recorded against a commit other than the one currently
    checked out -- rather than silently trusting an old artifact.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = result.stdout.strip()
    return sha or None


def _pg_url(value: str) -> str:
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    return value


def _alembic_revision(conn: psycopg.Connection[Any]) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT version_num FROM alembic_version")
        row = cur.fetchone()
        return row[0] if row else None


def _table_counts(conn: psycopg.Connection[Any], tables: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for table in tables:
            cur.execute(sql.SQL("SELECT count(*) FROM {table}").format(table=sql.Identifier(table)))
            row = cur.fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def _archive_checksum_status(archive_path: Path) -> tuple[bool, str]:
    checksum_path = archive_path.with_name(archive_path.name + ".sha256")
    if not archive_path.is_file():
        return False, f"archive not found: {archive_path}"
    if not checksum_path.is_file():
        return False, f"checksum file not found: {checksum_path}"
    recorded = checksum_path.read_text().split()[0].strip()
    actual = sha256(archive_path.read_bytes()).hexdigest()
    if recorded != actual:
        return False, f"checksum mismatch: recorded={recorded} actual={actual}"
    return True, f"sha256={actual}"


def evaluate_invariants(
    *,
    archive_checksum_ok: bool,
    archive_checksum_detail: str,
    source_revision: str | None,
    target_revision: str | None,
    source_counts: dict[str, int],
    target_counts: dict[str, int],
    source_checksums: dict[str, str],
    target_checksums: dict[str, str],
    elapsed_seconds: float,
    rto_budget_seconds: int = DEFAULT_RTO_BUDGET_SECONDS,
) -> list[dict[str, Any]]:
    """Evaluate the recovery-drill invariants from already-gathered facts.

    Pure function (no I/O) so it can be exercised directly in tests with
    hand-crafted inputs, including deliberately mismatched ones, without
    needing two separate live databases.
    """
    invariants: list[dict[str, Any]] = []

    invariants.append(
        {
            "name": "archive_sha256",
            "passed": archive_checksum_ok,
            "detail": archive_checksum_detail,
        }
    )

    revisions_match = (
        source_revision is not None
        and target_revision is not None
        and source_revision == target_revision
    )
    invariants.append(
        {
            "name": "alembic_head_matches",
            "passed": revisions_match,
            "detail": f"source={source_revision} target={target_revision}",
        }
    )

    mismatched_counts = {
        table: {"source": source_counts.get(table), "target": target_counts.get(table)}
        for table in sorted(set(source_counts) | set(target_counts))
        if source_counts.get(table) != target_counts.get(table)
    }
    invariants.append(
        {
            "name": "row_counts_match",
            "passed": not mismatched_counts,
            "detail": (
                "all table row counts match"
                if not mismatched_counts
                else f"mismatched tables: {sorted(mismatched_counts)}"
            ),
        }
    )

    # fixture_row_checksums (tests/phase1_dataset.py / seed_phase1_acceptance.py)
    # emits the literal value "empty" for a table whose seeded-workspace row
    # count is zero. Two databases both missing seed data would both show
    # "empty" for every table, satisfying an equality check vacuously --
    # equality alone can't distinguish "checksums genuinely match" from
    # "neither database has the seed rows this invariant exists to verify."
    # Every checksum invariant below therefore also requires the compared
    # value not be this vacuous placeholder.
    empty_checksum_tables = sorted(
        table
        for table in sorted(set(source_checksums) | set(target_checksums))
        if source_checksums.get(table) == "empty" or target_checksums.get(table) == "empty"
    )

    mismatched_checksums = sorted(
        table
        for table in sorted(set(source_checksums) | set(target_checksums))
        if source_checksums.get(table) != target_checksums.get(table)
    )
    checksums_vacuous = not mismatched_checksums and bool(empty_checksum_tables)
    invariants.append(
        {
            "name": "representative_record_checksums_match",
            "passed": not mismatched_checksums and not checksums_vacuous,
            "detail": (
                f"mismatched tables: {mismatched_checksums}"
                if mismatched_checksums
                else f"vacuous: no seed rows found for {empty_checksum_tables}"
                if checksums_vacuous
                else "all representative row checksums match"
            ),
        }
    )

    audit_checksum = target_checksums.get("audit_events")
    audit_ok = (
        source_checksums.get("audit_events") == audit_checksum
        and audit_checksum is not None
        and audit_checksum != "empty"
    )
    invariants.append(
        {
            "name": "audit_events_append_only",
            "passed": audit_ok,
            "detail": (
                "restored audit_events rows are checksum-identical to source "
                "(no DB trigger enforces append-only-ness; this full-row "
                "checksum comparison is the documented substitute mechanism)"
                if audit_ok
                else "vacuous or mismatched: audit_events checksum is missing, "
                "empty, or does not match source"
            ),
        }
    )

    pkos_tables = ("pkos_nodes", "pkos_edges", "pkos_evidence")
    pkos_ok = all(
        source_checksums.get(t) == target_checksums.get(t)
        and target_checksums.get(t) not in (None, "empty")
        for t in pkos_tables
    )
    invariants.append(
        {
            "name": "pkos_mapped_columns_match",
            "passed": pkos_ok,
            "detail": f"pkos tables compared: {list(pkos_tables)}",
        }
    )

    rto_ok = elapsed_seconds <= rto_budget_seconds
    invariants.append(
        {
            "name": "rto_within_budget",
            "passed": rto_ok,
            "detail": f"elapsed_seconds={elapsed_seconds} budget={rto_budget_seconds}",
        }
    )

    return invariants


def build_report(
    *,
    source_url: str,
    target_url: str,
    archive_path: Path,
    elapsed_seconds: float,
    rto_budget_seconds: int = DEFAULT_RTO_BUDGET_SECONDS,
) -> dict[str, Any]:
    tables = seed_fixtures.ALL_PHASE1_TABLES
    with (
        psycopg.connect(_pg_url(source_url)) as source_conn,
        psycopg.connect(_pg_url(target_url)) as target_conn,
    ):
        source_revision = _alembic_revision(source_conn)
        target_revision = _alembic_revision(target_conn)
        source_counts = _table_counts(source_conn, tables)
        target_counts = _table_counts(target_conn, tables)
        source_checksums = seed_fixtures.fixture_row_checksums(source_conn)
        target_checksums = seed_fixtures.fixture_row_checksums(target_conn)

    archive_ok, archive_detail = _archive_checksum_status(archive_path)

    invariants = evaluate_invariants(
        archive_checksum_ok=archive_ok,
        archive_checksum_detail=archive_detail,
        source_revision=source_revision,
        target_revision=target_revision,
        source_counts=source_counts,
        target_counts=target_counts,
        source_checksums=source_checksums,
        target_checksums=target_checksums,
        elapsed_seconds=elapsed_seconds,
        rto_budget_seconds=rto_budget_seconds,
    )

    passed = all(item["passed"] for item in invariants)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "rto_budget_seconds": rto_budget_seconds,
        "revisions": {"source": source_revision, "target": target_revision},
        "row_counts": {"source": source_counts, "target": target_counts},
        "checksums": {"source": source_checksums, "target": target_checksums},
        "invariants": invariants,
        "passed": passed,
        # Recorded-result fields consumed by
        # scripts/check_phase1_acceptance.py's result-aware validation
        # (result_evidence.backup_restore): a status string plus the
        # commit this drill ran against.
        "status": "passed" if passed else "failed",
        "head_sha": _git_head_sha(),
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Phase 1 recovery evidence report",
        "",
        f"- Generated at: {report['generated_at']}",
        f"- Elapsed seconds: {report['elapsed_seconds']}",
        f"- RTO budget seconds: {report['rto_budget_seconds']}",
        f"- Overall result: {'PASSED' if report['passed'] else 'FAILED'}",
        "",
        "## Alembic revisions",
        "",
        f"- source: `{report['revisions']['source']}`",
        f"- target: `{report['revisions']['target']}`",
        "",
        "## Invariants",
        "",
        "| Invariant | Result | Detail |",
        "| --- | --- | --- |",
    ]
    for item in report["invariants"]:
        result = "PASS" if item["passed"] else "FAIL"
        lines.append(f"| {item['name']} | {result} | {item['detail']} |")

    lines.extend(["", "## Row counts", "", "| Table | Source | Target |", "| --- | --- | --- |"])
    source_counts = report["row_counts"]["source"]
    target_counts = report["row_counts"]["target"]
    for table in sorted(source_counts):
        lines.append(f"| {table} | {source_counts[table]} | {target_counts.get(table)} |")

    lines.extend(
        [
            "",
            "## Representative record checksums",
            "",
            "| Table | Source | Target |",
            "| --- | --- | --- |",
        ]
    )
    source_checksums = report["checksums"]["source"]
    target_checksums = report["checksums"]["target"]
    for table in sorted(source_checksums):
        lines.append(f"| {table} | {source_checksums[table]} | {target_checksums.get(table)} |")

    lines.append("")
    return "\n".join(lines)


def write_reports(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    md_path.write_text(render_markdown(report))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--target-url", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--elapsed-seconds", required=True, type=float)
    parser.add_argument(
        "--rto-budget-seconds",
        type=int,
        default=DEFAULT_RTO_BUDGET_SECONDS,
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path(".local/evidence/phase1-recovery.json"),
    )
    parser.add_argument(
        "--output-md",
        type=Path,
        default=Path(".local/evidence/phase1-recovery.md"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = build_report(
        source_url=args.source_url,
        target_url=args.target_url,
        archive_path=args.archive,
        elapsed_seconds=args.elapsed_seconds,
        rto_budget_seconds=args.rto_budget_seconds,
    )
    write_reports(report, args.output_json, args.output_md)
    status = "PASSED" if report["passed"] else "FAILED"
    print(f"Phase 1 recovery evidence report: {status}")
    print(f"  JSON: {args.output_json}")
    print(f"  Markdown: {args.output_md}")
    if not report["passed"]:
        for item in report["invariants"]:
            if not item["passed"]:
                print(f"  FAILED invariant: {item['name']} -- {item['detail']}", file=sys.stderr)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
