"""`email.get_thread_content` tool handler (Phase 10 Task 5): the
deterministic required-input fetch for the `email.detect_action` AI task
type (`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`
Task 5: "Grounding check against the source email's own content").

Reads every `email_messages` row for the requested `thread_id`, scoped to
the caller's own `workspace_id`/`owner_id` (a thread belonging to a
different workspace, or to a different user within the same workspace,
is `ToolNotFound` -- the same non-disclosing-404 convention every other
Phase 1-3 read tool in this runtime already follows, per `tools.py:
ToolNotFound`'s own docstring).

Decrypts each message's `body` (`crypto.decrypt_field`, the same raw-string
primitive `domains.py:_decrypt_payload` uses for `domain_records` fields)
-- this tool's output is fed to a prompt that must reason about the actual
email content, not a redaction marker, mirroring `insight_tools.py:get_
insight_sources_tool`'s identical "decrypt for the model, never redact"
reasoning.

Messages ordered oldest-first (`sent_at ASC`) -- the same top-to-bottom
order a human reads a thread in Gmail's own UI. `runtime.py:_render_
thread_content_block` renders `messages` in whatever order this tool's
own output supplies them, with no ordering of its own (round 8 review:
its docstring only covers the untrusted-data wrapping rationale, not
ordering -- corrected here after a prior version of this docstring
pointed there as if it did), so this `ORDER BY` is the entire ordering
guarantee. A message whose `body` is still
`NULL` (never fetched -- most messages in a thread other than the one that
triggered this call, since `gmail.metadata`-only sync leaves `body` `NULL`
until Task 5's own proactive fetch populates it for the triggering message)
is omitted entirely rather than rendered as an empty body a small model
might otherwise mistake for "this message has no content" -- an absence of
fetched content is not the same claim as an email that is genuinely empty.

Capped at `_MAX_THREAD_MESSAGES` -- round 13 review found this was the one
deterministic tool in the whole runtime with no size bound at all, a direct
gap against the design doc's own Decision 6 contract ("every tool result
is ... size-bounded (a hard cap on returned rows/characters, e.g.
`attention.get_item` returns exactly one item, `knowledge.get_entity`
truncates claims/evidence lists to a fixed page size)"), unlike every
sibling deterministic tool (`attention/tools.py:_MAX_FACTORS`,
`knowledge/tools.py:_MAX_CLAIMS`/`_MAX_EVIDENCE`, `insight_tools.py:_MAX_
RECORDS_PER_DOMAIN`, `meeting_prep.py:_MAX_EVIDENCE`, all `20`-`50`) --
an organically long-running thread (a support/notification/reply-all
thread, whose message count an external sender fully controls simply by
replying into it) would otherwise decrypt and render every historical
message every single time any new message in that thread became eligible,
paid before `execute_run`'s own token-budget check even runs. Selects the
most recent `_MAX_THREAD_MESSAGES` messages (by `sent_at DESC LIMIT`),
then re-sorts that capped set back to the same oldest-first order
documented above, so a thread within the cap is unaffected and one over
the cap still keeps its most recent (most relevant) messages, in the same
reading order.

`trigger_message_id`, when given, is always included in the capped set,
never counted against it as "just another message" -- round 14 review
found that "most recent `_MAX_THREAD_MESSAGES` by `sent_at`" alone (round
13's original fix) can silently exclude the specific message that
triggered this call: `detect_actions_since`'s own eligibility query
(`gmail_adapter.py`) orders `(created_at >= since) DESC, sent_at ASC`,
deliberately prioritizing an account's freshly-synced messages over older
same-thread backlog (the round 3/4 "backlog worked down over subsequent
calls, not lost" design) -- so on a busy, long-running thread, an old
backlog message can still be waiting its turn long after 50+ *newer*
messages in that same thread have already had their bodies fetched (and
thus already occupy every slot the cap allows) by the time it's finally
processed. Without this guarantee, `_detect_action_for_message` would run
the model over thread content that never includes the very email the run
exists to evaluate -- and if grounding then passes on some other, visible
message, the created recommendation's own evidence (which cites the
triggering message specifically, per `_register_message_evidence`) would
point at a message the model was never actually shown. Reserves one slot
for it (`_MAX_THREAD_MESSAGES - 1` from the ordinary "most recent" pool,
excluding the trigger from that pool so it is never counted twice) rather
than appending it as message `_MAX_THREAD_MESSAGES + 1`, so the *total*
size bound this cap exists to enforce is never itself violated by the
guarantee meant to fix a different bug.
"""

from typing import Any
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult

from .crypto import decrypt_field

# Matches `insight_tools.py:_MAX_RECORDS_PER_DOMAIN`/`meeting_prep.py:_MAX_
# EVIDENCE`'s own `50` -- see this module's own docstring for why a thread
# needs a cap at all and why it's applied newest-first, not oldest-first.
_MAX_THREAD_MESSAGES = 50


def get_thread_content_tool(
    session: Session, auth: AuthContext, thread_id: UUID, trigger_message_id: UUID | None = None
) -> ToolResult | ToolNotFound:
    thread_row = session.execute(
        text(
            "SELECT subject FROM email_threads "
            "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND id = :thread_id"
        ),
        {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "thread_id": thread_id},
    ).one_or_none()
    if thread_row is None:
        return ToolNotFound(tool="email.get_thread_content")

    # `trigger_message_id` reserves one of `_MAX_THREAD_MESSAGES`'s own
    # slots for itself (see this module's own docstring) -- the "most
    # recent" pool is capped one lower and explicitly excludes it, so the
    # `UNION ALL` below can never double-count it or push the total past
    # the cap this whole mechanism exists to enforce.
    most_recent_limit = _MAX_THREAD_MESSAGES - (1 if trigger_message_id is not None else 0)
    rows = (
        session.execute(
            text(
                """
                SELECT id, sender, sent_at, direction, body FROM (
                    SELECT id, sender, sent_at, direction, body FROM email_messages
                    WHERE workspace_id = :workspace_id AND owner_id = :owner_id
                      AND thread_id = :thread_id AND body IS NOT NULL
                      AND (CAST(:trigger_message_id AS uuid) IS NULL OR id <> :trigger_message_id)
                    ORDER BY sent_at DESC
                    LIMIT :limit
                ) AS most_recent
                UNION ALL
                SELECT id, sender, sent_at, direction, body FROM email_messages
                WHERE workspace_id = :workspace_id AND owner_id = :owner_id
                  AND thread_id = :thread_id AND body IS NOT NULL
                  AND id = :trigger_message_id
                ORDER BY sent_at ASC
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "thread_id": thread_id,
                "trigger_message_id": trigger_message_id,
                "limit": most_recent_limit,
            },
        )
        .mappings()
        .all()
    )

    messages: list[dict[str, Any]] = []
    for row in rows:
        try:
            body = decrypt_field(row["body"])
        except InvalidToken:
            body = "[unable to decrypt -- contact support]"
        messages.append(
            {
                "id": str(row["id"]),
                "sender": row["sender"],
                "sent_at": row["sent_at"].isoformat(),
                "direction": row["direction"],
                "body": body,
            }
        )

    return ToolResult(output={"subject": thread_row[0], "messages": messages})
