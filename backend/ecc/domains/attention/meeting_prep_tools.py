"""`meeting.get_prep_pack` tool handler (Phase 4-consuming wiring of
`meeting_prep.py`'s "Optional enrichment", `MEETING-PREP-CONTRACT.md`).

Reuses `meeting_prep.py`'s existing `generate_pack` -- the exact same
deterministic fetch/compose logic `create_prep`/`refresh_prep` already run
-- so the AI runtime's view of "what evidence may be summarized" is always
identical to what the deterministic pack itself contains, never a second,
independently-computed source of truth that could silently diverge from
it. Inside a create/refresh's enrichment run it reads the content that
request pinned (`pinned_pack_content`) -- the exact snapshot about to be
stored -- since the new `meeting_packs` row does not exist yet; called
outside such a run it recomputes with `generate_pack`. `generate_pack`/`get_meeting_row`/
`require_meeting_read` are public (no leading underscore) in
`meeting_prep.py` specifically because this file needs them as a declared
cross-module dependency -- a private name here would be exactly the
"fragile private import" bug class that broke the build once already this
session (see identity/accounts.py's/gmail_shared.py's own near-identical
fixes for the same class). `get_meeting_row` (not the bare `meeting_row`
an underscore-drop would otherwise produce) avoids colliding with the
`meeting_row = ...` local variable name every call site below already
uses.

Deliberately omits `evidence_gaps`/`open_questions` from the tool's output:
an evidence gap represents the *absence* of available evidence (nothing a
summary should cite as if it were a fact), and `open_questions` is always
empty in this activation (`meeting_prep.py`'s own module docstring) --
Decision 6's "every tool result ... re-inserted into the model's context"
principle only applies to content genuinely worth summarizing.

`_meeting_input` (inside `generate_pack`) can raise `HTTPException(409,
"LINKED_CALENDAR_EVENT_MISSING")` for a meeting whose linked calendar
event was deleted out from under it -- a real HTTP-layer exception, never
raised by any other tool handler in this codebase (`attention.get_item`'s
own docstring: "this handler never raises HTTPException ... 'not found'
is returned as data"). Caught here and translated to `ToolNotFound`,
matching that same never-raise discipline: a meeting in that broken state
genuinely cannot produce a valid pack to summarize, which is what
`ToolNotFound` already means for every other id-resolution failure.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult

from .meeting_prep import (
    generate_pack,
    get_meeting_row,
    pinned_pack_content,
    require_meeting_read,
)


def get_prep_pack_tool(
    session: Session, auth: AuthContext, meeting_id: UUID
) -> ToolResult | ToolNotFound:
    # Found in the fourth whole-phase review: every real call site in
    # meeting_prep.py calls `require_meeting_read` before `get_meeting_row` --
    # this handler called `get_meeting_row` alone, skipping the authz check
    # that helper exists specifically to add. Reachable from runtime.py's
    # own model-controlled second tool-dispatch path, same class of gap as
    # attention/tools.py's and knowledge/tools.py's identical fixes.
    # `require_meeting_read` raises HTTPException(404) rather than
    # returning a bool, so it folds into the same catch-and-translate-to-
    # ToolNotFound discipline this handler already uses for the 409
    # LINKED_CALENDAR_EVENT_MISSING case below.
    try:
        require_meeting_read(session, auth, meeting_id)
    except HTTPException:
        return ToolNotFound(tool="meeting.get_prep_pack")

    meeting_row = get_meeting_row(session, auth, meeting_id)
    if meeting_row is None:
        return ToolNotFound(tool="meeting.get_prep_pack")

    # Deliberately the SHARED snapshot (`generate_pack`), never the calling
    # member's own view (`meeting_prep._caller_view`): this tool's only
    # consumer is the `meeting.prep_summary` run (its sole `eligible_tools`
    # entry; `POST /ai/runs` accepts only `attention.explain_item`), and
    # every such run's output is shared -- `meeting_prep._compute_
    # enrichment` stores the summary in the pack every meeting reader gets,
    # and the run itself (summary, cited ids) is a `workspace`-visible
    # `ai_runs` row. Giving it the caller's private rows would copy them
    # into both (FX1). A per-member AI view would need its own run type
    # stored private to that member, not this tool.
    #
    # Inside a create/refresh's own enrichment run, summarize the exact
    # content that request is storing (FX7): a live regeneration could
    # include a row that never reached the stored snapshot, and the pack's
    # read-time visibility re-check only withholds the summary when a
    # *stored* row is dropped.
    content = pinned_pack_content(auth, meeting_id)
    if content is None:
        try:
            content = generate_pack(session, auth, meeting_id, meeting_row).content
        except HTTPException:
            return ToolNotFound(tool="meeting.get_prep_pack")
    output: dict[str, Any] = {
        "objective": content.objective,
        "participants": [
            {"id": str(p.id), "entity_name": p.entity_name, "role": p.role}
            for p in content.participants
        ],
        "timeline": [
            {
                "id": str(t.id),
                "effective_at": t.effective_at.isoformat(),
                "event_type": t.event_type,
                "summary": t.summary,
            }
            for t in content.timeline
        ],
        "commitments": [
            {
                "id": str(c.id),
                "direction": c.direction,
                "summary": c.summary,
                "status": c.status,
                "due_at": c.due_at.isoformat() if c.due_at is not None else None,
                "counterparty_name": c.counterparty_name,
            }
            for c in content.commitments
        ],
        "decisions": [
            {"id": str(n.id), "title": n.title, "body": n.body, "note_type": n.note_type}
            for n in content.decisions
        ],
        "notes": [
            {"id": str(n.id), "title": n.title, "body": n.body, "note_type": n.note_type}
            for n in content.notes
        ],
        "risks": [
            {
                "id": str(r.id),
                "description": r.description,
                "status": r.status,
                "probability": r.probability,
                "impact": r.impact,
            }
            for r in content.risks
        ],
        "dependencies": [
            {
                "id": str(d.id),
                "direction": d.direction,
                "note": d.note,
                "expected_at": d.expected_at.isoformat() if d.expected_at is not None else None,
            }
            for d in content.dependencies
        ],
    }
    return ToolResult(output=output)
