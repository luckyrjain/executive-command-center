"""`knowledge.get_entity` tool handler (design doc Decision 6): a thin,
read-only wrapper over Phase 2's existing `GET /knowledge/entities/{id}`
read path (`entities.py:get_entity`, plus `claims.py:list_claims` for the
claims this tool's output shape also names), matching that endpoint's
workspace-scoped / cross-workspace-404 convention exactly. Registered by
migration `0029_phase4_prompt_tool_versions.py` but not wired into any
evaluated task in this slice (Decision 6: "registered ... so the allowlist/
registry mechanism is proven against more than one contract from day one,
but not wired to an evaluated task until the slice that needs it") --
`attention.explain_item`'s `eligible_tools` never names `knowledge.
get_entity`, so `runtime.py` never calls this handler in production; it
exists so the allowlist-rejection tests (Task 4 Steps 1/5) have a real,
second, out-of-scope tool to name.

Like `attention/tools.py:get_item_tool`, this handler never raises
`HTTPException` -- it returns `ai_runtime.tools.ToolNotFound` as data, for
whichever caller (`runtime.py`'s dispatch step) decides what "not found"
means in that context.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult

# Decision 6: "knowledge.get_entity truncates claims/evidence lists to a
# fixed page size" -- the size bound this tool's own output schema requires
# (`phase-004/DATA-MODEL.md`'s "size-bounded" tool-result rule), unlike
# `entities.py`'s own paginated `list_claims`/evidence reads, which have no
# fixed cap of their own.
_MAX_CLAIMS = 20
_MAX_EVIDENCE = 20


def get_entity_tool(
    session: Session, auth: AuthContext, entity_id: UUID
) -> ToolResult | ToolNotFound:
    entity_row = (
        session.execute(
            text(
                """
                SELECT canonical_name
                FROM pkos_nodes
                WHERE workspace_id = :workspace_id AND id = :entity_id
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .one_or_none()
    )
    if entity_row is None:
        return ToolNotFound(tool="knowledge.get_entity")

    claim_rows = (
        session.execute(
            text(
                """
                SELECT predicate, value_json, confidence
                FROM knowledge_claims
                WHERE workspace_id = :workspace_id AND subject_id = :entity_id
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id, "limit": _MAX_CLAIMS},
        )
        .mappings()
        .all()
    )
    evidence_rows = (
        session.execute(
            text(
                """
                SELECT id, source_type, captured_at, evidence_state
                FROM pkos_evidence
                WHERE workspace_id = :workspace_id AND node_id = :entity_id
                ORDER BY captured_at DESC
                LIMIT :limit
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id, "limit": _MAX_EVIDENCE},
        )
        .mappings()
        .all()
    )

    output: dict[str, Any] = {
        "title": entity_row["canonical_name"],
        "claims": [
            {
                "predicate": row["predicate"],
                "value": row["value_json"],
                "confidence": float(row["confidence"]),
            }
            for row in claim_rows
        ],
        "evidence": [
            {
                "id": str(row["id"]),
                "source_type": row["source_type"],
                "captured_at": row["captured_at"].isoformat(),
                "status": row["evidence_state"],
            }
            for row in evidence_rows
        ],
    }
    return ToolResult(output=output)
