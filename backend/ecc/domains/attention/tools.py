"""`attention.get_item` tool handler (design doc Decision 6): a thin,
read-only wrapper over Phase 3's existing `GET /attention/{id}` read path
(`attention.py:get_attention_item`), returning the exact shape `tool_
definitions`' seeded output schema (migration `0029_phase4_prompt_tool_
versions.py`) declares: `{entity_type, score, confidence, factors,
evidence_refs}`.

Workspace-scoped and cross-workspace-404-equivalent: identical WHERE-clause
scoping to `attention.py:get_attention_item` (`workspace_id = :workspace_id
AND id = :attention_item_id`, collapsing "genuinely missing" and "belongs
to a different workspace" into one outcome), matching every existing Phase
1-3 read endpoint's non-disclosing convention exactly. Unlike that HTTP
endpoint, this handler never raises `HTTPException` -- it is called from
`ai_runtime/runtime.py`'s orchestration loop, not directly by a browser
request, so "not found" is returned as data (`ai_runtime.tools.
ToolNotFound`) for the loop to translate into its own run-failure handling.

`evidence_refs` (Decision 6's output shape) has no dedicated column on
`attention_items` -- Phase 3 never introduced one, the `factors` list
itself already carries the item's real evidence -- so this always returns
an empty list rather than inventing a source that does not exist.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult

# Decision 6: "knowledge.get_entity truncates claims/evidence lists to a
# fixed page size" -- the same size-bounding rule applied here to factors,
# even though a real attention item's factor list is already small (Phase
# 3's scoring policy caps how many factors it ever writes); bounding it here
# too means this handler's own output size guarantee never depends on that
# upstream fact continuing to hold.
_MAX_FACTORS = 20


def get_item_tool(
    session: Session, auth: AuthContext, attention_item_id: UUID
) -> ToolResult | ToolNotFound:
    row = (
        session.execute(
            text(
                """
                SELECT entity_type, score, confidence, factors
                FROM attention_items
                WHERE workspace_id = :workspace_id AND id = :attention_item_id
                """
            ),
            {"workspace_id": auth.workspace_id, "attention_item_id": attention_item_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return ToolNotFound(tool="attention.get_item")

    factors: list[dict[str, Any]] = list(row["factors"] or [])[:_MAX_FACTORS]
    output: dict[str, Any] = {
        "entity_type": row["entity_type"],
        "score": float(row["score"]),
        "confidence": float(row["confidence"]),
        "factors": [
            {
                "code": factor["code"],
                "label": factor["label"],
                "points": factor["points"],
                "source_field": factor["source_field"],
            }
            for factor in factors
        ],
        "evidence_refs": [],
    }
    return ToolResult(output=output)
