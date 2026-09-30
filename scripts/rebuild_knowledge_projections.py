"""Deterministically rebuild Phase 2 knowledge-platform projections.

Rebuildable per docs/domain/PKOS-SCHEMA.md's "projections are rebuildable"
rule and phase-002/DATA-MODEL.md's "Derived search, embedding and timeline
projections are rebuildable" data-model rule: timeline_entries is derived
from audit_events, retrieval_documents from pkos_nodes/knowledge_claims,
and embedding_projections from retrieval_documents -- all ultimately
authoritative tables -- and can always be regenerated from scratch.
embedding_projections rebuilds as embedded=0 whenever
Settings.embeddings_enabled is off or the model can't load, rather than
failing (ecc.domains.knowledge.embeddings.queue_embedding's degrade-not-fail
contract).

CLI usage (from a repository checkout):

    PYTHONPATH=backend ECC_DATABASE_URL=<url> [ECC_PERSONAL_DATA_ISOLATION=true] \\
        uv run python scripts/rebuild_knowledge_projections.py [--workspace-id <UUID>]

Without `--workspace-id` every workspace is rebuilt -- in ONE transaction,
so on large deployments prefer one workspace at a time. The effective
database (host and name only), the isolation flag and where it came from
(environment vs `.env`/default settings) are printed on stderr first.

Personal-data isolation (Spec A): the retrieval rebuild honours
`ECC_PERSONAL_DATA_ISOLATION` from this process's settings. With it off,
`retrieval._build_body` copies every claim into the shared search text,
including claims backed by evidence that is `private` only because the flag
was on -- Gmail evidence written with the flag on, or evidence the
personal-visibility backfill made private. So with the flag off this command
refuses (exit 2) while any `pkos_evidence` row in scope (the
`--workspace-id` workspace, else every workspace) is `private`, listing the
workspaces (top 10 by count, plus the total). Nothing else triggers it: a
flag-off application never writes `private` evidence, narrowed
(`shared_explicitly`) evidence is already in shared text on a flag-off
application, and a full rollback (`backfill_personal_visibility.py
--restore` of every run) returns the evidence it restores to `workspace`.
Evidence stays `private`, and keeps the guard on, where the restore reported
the row instead of restoring it (`previous_owner_inactive`,
`changed_since_backfill:*`) and where it was written while the flag was on
(restore never touches it); the refusal lists those workspaces with counts,
and `SELECT id FROM pkos_evidence WHERE workspace_id = '<ws>' AND visibility =
'private'` lists the rows. Re-enable the flag, or pass
`--allow-without-isolation` knowingly (those claims become shared search
text -- as the flag-off application's own writers would make them on their
next write anyway). The check is one short read in its own transaction
(`statement_timeout` 120 s); unscoped, it scans `pkos_evidence` once. Exit
codes are listed at the end of `--help` (`_parse_args`'s epilog).
"""

from __future__ import annotations

import argparse
import sys
from typing import Final
from uuid import UUID

from sqlalchemy import make_url, text
from sqlalchemy.orm import Session

from ecc.config import get_settings, setting_source
from ecc.database import SessionFactory
from ecc.domains.knowledge.embeddings import rebuild_embeddings
from ecc.domains.knowledge.retrieval import rebuild_retrieval_documents
from ecc.domains.knowledge.timeline import rebuild_timeline
from ecc.platform.connector_security import personal_data_isolation_enabled

EXIT_OK: Final = 0
EXIT_REFUSED: Final = 2
ISOLATION_FLAG_ENV: Final = "ECC_PERSONAL_DATA_ISOLATION"
GUARD_STATEMENT_TIMEOUT: Final = "120s"
TOP_WORKSPACES: Final = 10


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit codes: 0 rebuilt; 2 refused by the isolation guard, invalid "
        "arguments or unknown workspace; 1 unexpected error.",
    )
    parser.add_argument(
        "--workspace-id",
        type=UUID,
        default=None,
        metavar="UUID",
        help="Rebuild only this workspace (must exist). Default: every workspace, "
        "in one transaction.",
    )
    parser.add_argument(
        "--allow-without-isolation",
        action="store_true",
        help=f"Rebuild with {ISOLATION_FLAG_ENV} off even though `private` evidence "
        "exists in scope: claims backed by it are written into shared search text. "
        "Only after a deliberate decision (see the module docstring / docs/SETUP.md).",
    )
    return parser.parse_args(argv)


def private_evidence_counts(session: Session, workspace_id: UUID | None) -> list[tuple[UUID, int]]:
    """`(workspace_id, count)` of `private` `pkos_evidence` in scope, largest
    first -- evidence a flag-off rebuild would copy into shared search text.
    Its own short transaction, under `GUARD_STATEMENT_TIMEOUT`."""
    with session.begin():
        session.execute(text(f"SET LOCAL statement_timeout = '{GUARD_STATEMENT_TIMEOUT}'"))
        rows = session.execute(
            text(
                "SELECT workspace_id, count(*) FROM pkos_evidence WHERE visibility = 'private' "
                "AND (CAST(:ws AS uuid) IS NULL OR workspace_id = CAST(:ws AS uuid)) "
                "GROUP BY workspace_id ORDER BY count(*) DESC, workspace_id"
            ),
            {"ws": workspace_id},
        ).all()
    return [(row[0], int(row[1])) for row in rows]


def _describe_counts(counts: list[tuple[UUID, int]]) -> str:
    top = ", ".join(f"{ws}={n}" for ws, n in counts[:TOP_WORKSPACES])
    more = (
        f" (+{len(counts) - TOP_WORKSPACES} more workspaces)"
        if len(counts) > TOP_WORKSPACES
        else ""
    )
    total = sum(n for _, n in counts)
    return f"private pkos_evidence: total={total} in {len(counts)} workspace(s): {top}{more}"


def _announce() -> bool:
    """Prints the effective database (host/name, never credentials) and the
    isolation flag with its source; returns the flag."""
    url = make_url(get_settings().database_url)
    enabled = personal_data_isolation_enabled()
    print(
        f"rebuild_knowledge_projections: database host={url.host or '-'} "
        f"name={url.database or '-'} isolation={'on' if enabled else 'off'} "
        f"(from {setting_source(ISOLATION_FLAG_ENV)})",
        file=sys.stderr,
    )
    return enabled


def _isolation_guard(session: Session, args: argparse.Namespace, *, enabled: bool) -> int | None:
    """EXIT_REFUSED when a flag-off rebuild would copy `private` evidence
    into shared search text (see the module docstring); None to proceed."""
    if enabled:
        return None
    counts = private_evidence_counts(session, args.workspace_id)
    if not counts:
        return None
    reasons = _describe_counts(counts)
    if not args.allow_without_isolation:
        print(
            f"rebuild_knowledge_projections: refusing to run: {ISOLATION_FLAG_ENV} is off but "
            f"{reasons} -- the rebuild would write claims backed by it into shared search "
            f"text. Re-run with {ISOLATION_FLAG_ENV}=true, or pass --allow-without-isolation "
            "after a deliberate decision (docs/SETUP.md).",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    print(
        f"rebuild_knowledge_projections: WARNING: --allow-without-isolation: rebuilding with "
        f"{ISOLATION_FLAG_ENV} off although {reasons}; claims backed by it become shared "
        "search text",
        file=sys.stderr,
    )
    return None


def _workspace_ids(session: Session, workspace_id: UUID | None) -> list[UUID] | None:
    """The workspaces to rebuild; None when `workspace_id` does not exist."""
    if workspace_id is None:
        return [row[0] for row in session.execute(text("SELECT id FROM workspaces")).all()]
    exists = session.execute(
        text("SELECT EXISTS (SELECT 1 FROM workspaces WHERE id = :id)"), {"id": workspace_id}
    ).scalar_one()
    return [workspace_id] if exists else None


def _rebuild(session: Session, workspace_id: UUID) -> None:
    timeline_report = rebuild_timeline(session, workspace_id)
    print(f"{timeline_report.workspace_id}\ttimeline_entries\t{timeline_report.entries_written}")
    retrieval_report = rebuild_retrieval_documents(session, workspace_id)
    print(
        f"{retrieval_report.workspace_id}\tretrieval_documents\t"
        f"{retrieval_report.documents_written}"
    )
    embedding_report = rebuild_embeddings(session, workspace_id)
    print(
        f"{embedding_report.workspace_id}\tembedding_projections\t"
        f"embedded={embedding_report.embedded} skipped={embedding_report.skipped}"
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    enabled = _announce()
    with SessionFactory() as guard_session:
        refused = _isolation_guard(guard_session, args, enabled=enabled)
    if refused is not None:
        return refused
    with SessionFactory() as session:
        workspace_ids = _workspace_ids(session, args.workspace_id)
        if workspace_ids is None:
            print("rebuild_knowledge_projections: workspace not found", file=sys.stderr)
            return EXIT_REFUSED
        for workspace_id in workspace_ids:
            _rebuild(session, workspace_id)
        session.commit()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
