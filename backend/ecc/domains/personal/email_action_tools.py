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
order a human reads a thread in Gmail's own UI, matching `runtime.py:
_render_thread_content_block`'s docstring. A message whose `body` is still
`NULL` (never fetched -- most messages in a thread other than the one that
triggered this call, since `gmail.metadata`-only sync leaves `body` `NULL`
until Task 5's own proactive fetch populates it for the triggering message)
is omitted entirely rather than rendered as an empty body a small model
might otherwise mistake for "this message has no content" -- an absence of
fetched content is not the same claim as an email that is genuinely empty.
"""

from typing import Any
from uuid import UUID

from cryptography.fernet import InvalidToken
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult

from .crypto import decrypt_field


def get_thread_content_tool(
    session: Session, auth: AuthContext, thread_id: UUID
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

    rows = (
        session.execute(
            text(
                """
                SELECT id, sender, sent_at, direction, body FROM email_messages
                WHERE workspace_id = :workspace_id AND owner_id = :owner_id
                  AND thread_id = :thread_id AND body IS NOT NULL
                ORDER BY sent_at ASC
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "thread_id": thread_id,
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
