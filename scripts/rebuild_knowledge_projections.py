"""Deterministically rebuild Phase 2 knowledge-platform projections.

Rebuildable per docs/domain/PKOS-SCHEMA.md's "projections are rebuildable"
rule and phase-002/DATA-MODEL.md's "Derived search, embedding and timeline
projections are rebuildable" data-model rule: timeline_entries (and, once
Task 6 lands, retrieval_documents) are derived from authoritative tables
(currently audit_events) and can always be regenerated from scratch.

CLI usage:

    uv run python scripts/rebuild_knowledge_projections.py
        Rebuild timeline_entries for every workspace in ECC_DATABASE_URL.

    uv run python scripts/rebuild_knowledge_projections.py --workspace-id UUID
        Rebuild timeline_entries for a single workspace.
"""

from __future__ import annotations

import argparse
from uuid import UUID

from sqlalchemy import text

from ecc.database import SessionFactory
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
            report = rebuild_timeline(session, workspace_id)
            print(f"{report.workspace_id}\ttimeline_entries\t{report.entries_written}")
        session.commit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
