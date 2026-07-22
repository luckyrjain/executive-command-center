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
"""

from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import text

from ecc.database import SessionFactory
from ecc.domains.knowledge.embeddings import rebuild_embeddings
from ecc.domains.knowledge.retrieval import rebuild_retrieval_documents
from ecc.domains.knowledge.timeline import rebuild_timeline


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-id", default=None, help="Rebuild only this workspace.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    with SessionFactory() as session:
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
