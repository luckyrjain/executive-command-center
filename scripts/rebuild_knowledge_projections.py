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

CLI usage:

    uv run python scripts/rebuild_knowledge_projections.py
        Rebuild every projection for every workspace in ECC_DATABASE_URL.

    uv run python scripts/rebuild_knowledge_projections.py --workspace-id UUID
        Rebuild every projection for a single workspace.

Personal-data isolation (Spec A): the retrieval rebuild honours
`ECC_PERSONAL_DATA_ISOLATION` from this process's settings -- with it off,
claims backed by non-`workspace` (e.g. private Gmail) evidence are written
into the shared search text. This command therefore refuses to run with the
flag off (exit 2) when any of these holds:
  - `scripts/backfill_personal_visibility.py` has run
    (`personal_visibility_backfill_log` has rows), or
  - any `pkos_evidence` row in scope (the `--workspace-id` workspace, else
    every workspace) is `private` (only the flag-on writers create it --
    e.g. the flag was on before any flag-off Gmail rows existed, so the
    backfill log is empty), or
  - any Gmail-sourced (`gmail_sync`, the personal data set's evidence
    predicate) evidence in scope is narrowed to specific people
    (`shared_explicitly`),
unless `--allow-without-isolation` is given -- the rollback path, after the
flag has been turned off on the application and every backfill run
restored. Ordinary narrowed grants on non-personal evidence
(`shared_explicitly`) do not trigger the refusal -- a flag-off app already
puts that evidence in shared bodies -- so a default flag-off deployment runs
as before. With isolation on, or after a backfill:

    ECC_PERSONAL_DATA_ISOLATION=true \
        uv run python scripts/rebuild_knowledge_projections.py --workspace-id UUID
"""

from __future__ import annotations

import argparse
import sys
from typing import Final
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.database import SessionFactory
from ecc.domains.knowledge.embeddings import rebuild_embeddings
from ecc.domains.knowledge.retrieval import rebuild_retrieval_documents
from ecc.domains.knowledge.timeline import rebuild_timeline
from ecc.platform.connector_security import (
    PERSONAL_ROW_PREDICATES,
    personal_data_isolation_enabled,
    personal_sql_params,
)

EXIT_REFUSED: Final = 2


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", default=None, help="Rebuild only this workspace.")
    parser.add_argument(
        "--allow-without-isolation",
        action="store_true",
        help=(
            "Run with ECC_PERSONAL_DATA_ISOLATION off even though the personal-data "
            "backfill has run or private evidence exists (rollback only: flag off on the "
            "app, every run restored)."
        ),
    )
    return parser.parse_args(argv)


def personal_backfill_has_run(session: Session) -> bool:
    """True iff `personal_visibility_backfill_log` exists and has any row."""
    if session.execute(text("SELECT to_regclass('personal_visibility_backfill_log')")).scalar():
        return bool(
            session.execute(
                text("SELECT EXISTS (SELECT 1 FROM personal_visibility_backfill_log)")
            ).scalar_one()
        )
    return False


# Evidence only the isolation flag keeps out of shared retrieval bodies
# (`retrieval._build_body`): `private` evidence -- which only the flag-on
# writers create (`connector_security.personal_content_scope`) -- and
# personal-source (Gmail) evidence narrowed to specific people. Ordinary
# `shared_explicitly` evidence (a `POST /grants` with `narrow_visibility` on
# non-personal evidence) is excluded: a flag-off app already puts it in the
# shared body, so refusing on it would protect nothing and block every
# later flag-off rebuild. The source test is the personal data set's own
# `pkos_evidence` fragment (single source of truth).
_ISOLATION_ONLY_EVIDENCE_SQL: Final = (
    "SELECT EXISTS (SELECT 1 FROM pkos_evidence "  # noqa: S608 -- code-defined fragment
    "WHERE (CAST(:ws AS uuid) IS NULL OR pkos_evidence.workspace_id = CAST(:ws AS uuid)) "
    "AND (pkos_evidence.visibility = 'private' "
    "OR (pkos_evidence.visibility = 'shared_explicitly' "
    f"AND ({PERSONAL_ROW_PREDICATES['pkos_evidence']}))))"
)


def isolation_only_evidence_exists(session: Session, workspace_id: UUID | None) -> bool:
    """True iff any `pkos_evidence` row (in `workspace_id`, or anywhere when
    None) is `private`, or is personal-source (Gmail) evidence narrowed to
    specific people (`shared_explicitly`) -- evidence a flag-off rebuild
    would copy into a shared retrieval body."""
    return bool(
        session.execute(
            text(_ISOLATION_ONLY_EVIDENCE_SQL), {"ws": workspace_id, **personal_sql_params()}
        ).scalar_one()
    )


def _isolation_guard(session: Session, args: argparse.Namespace) -> int | None:
    """EXIT_REFUSED when a flag-off rebuild would leak non-`workspace`
    evidence into shared search text (see the module docstring); None to
    proceed."""
    if personal_data_isolation_enabled():
        return None
    scope = UUID(args.workspace_id) if args.workspace_id else None
    if personal_backfill_has_run(session):
        why = "the personal-data backfill has run"
    elif isolation_only_evidence_exists(session, scope):
        why = "private or narrowed Gmail evidence exists"
    else:
        return None
    if not args.allow_without_isolation:
        print(
            f"rebuild_knowledge_projections: refusing to run: {why} but "
            "ECC_PERSONAL_DATA_ISOLATION is not enabled -- the rebuild would write claims "
            "backed by private evidence into shared search text. Re-run with "
            "ECC_PERSONAL_DATA_ISOLATION=true (or pass --allow-without-isolation after a "
            "full rollback).",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    print(
        f"rebuild_knowledge_projections: WARNING: running with ECC_PERSONAL_DATA_ISOLATION "
        f"off although {why} (--allow-without-isolation)",
        file=sys.stderr,
    )
    return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with SessionFactory() as session:
        refused = _isolation_guard(session, args)
        if refused is not None:
            return refused
        if args.workspace_id:
            workspace_ids = [UUID(args.workspace_id)]
        else:
            workspace_ids = [
                row[0] for row in session.execute(text("SELECT id FROM workspaces")).all()
            ]
        for workspace_id in workspace_ids:
            timeline_report = rebuild_timeline(session, workspace_id)
            print(
                f"{timeline_report.workspace_id}\ttimeline_entries\t{timeline_report.entries_written}"
            )
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
        session.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
