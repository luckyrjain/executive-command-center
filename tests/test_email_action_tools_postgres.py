"""`email.get_thread_content` tool handler (Phase 10 Task 5) -- the
deterministic required-input fetch for the `email.detect_action` AI task
type, mirroring `test_personal_insight_tools_postgres.py`'s convention of
calling the handler function directly (no AI call, no HTTP layer).

Covers: workspace/owner scoping (a thread belonging to a different
workspace, or a different owner within the same workspace, is `ToolNot
Found` -- the same non-disclosing convention `tools.py:ToolNotFound`'s
own docstring establishes for every other read tool in this runtime);
body decryption (the tool's own output must be the real plaintext, not
the Fernet ciphertext actually stored); a message whose `body` is still
`NULL` (never fetched) is omitted from the output entirely, not rendered
as an empty string; a message whose `body` is the empty-string sentinel
(round 4's `gmail_adapter.py` fix for a message with no extractable
`text/plain` part) is rendered as an empty message, not omitted -- the
opposite of the `NULL` case, and the exact distinction this module's own
docstring documents (round 5 review finding: previously untested); a
message whose stored `body` fails to decrypt under the current key falls
back to a fixed placeholder string rather than raising (round 5 review
finding: previously untested); messages are returned oldest-first
(`sent_at ASC`); and a thread with more than `MAX_THREAD_MESSAGES`
eligible messages returns only the most recent `MAX_THREAD_MESSAGES` of
them, still in oldest-first order, rather than every message the thread
has ever accumulated (round 13 review finding: previously unbounded,
unlike every sibling deterministic tool in this runtime); and passing a
`trigger_message_id` guarantees that specific message survives the cap
even when it would not otherwise be among the most recent `MAX_THREAD_
MESSAGES` (round 14 review finding: round 13's own cap alone could
silently exclude the very message a real call is triggered for, on a
thread where an old backlog message is processed long after many newer
same-thread messages already occupy the cap).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult
from ecc.domains.personal.crypto import encrypt_field
from ecc.domains.personal.email_action_tools import MAX_THREAD_MESSAGES, get_thread_content_tool

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def tool_context() -> Iterator[dict]:
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    user_id = uuid4()
    other_user_id = uuid4()
    # `users.id` is a global primary key, not workspace-scoped -- the same
    # UUID cannot own a `users` row in two different workspaces, so the
    # "belongs to a different workspace" scenario needs its own distinct
    # owner, not a reuse of `user_id` in `other_workspace_id`.
    other_workspace_owner_id = uuid4()
    connector_account_id = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        for ws_id in (workspace_id, other_workspace_id):
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, timezone, created_at) "
                    "VALUES (:id, 'Email Action Tools Test', 'UTC', :created_at)"
                ),
                {"id": ws_id, "created_at": now},
            )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=other_user_id,
            email=f"{other_user_id}@example.test",
            now=now,
        )
        create_identity(
            connection,
            workspace_id=other_workspace_id,
            user_id=other_workspace_owner_id,
            email=f"{other_workspace_owner_id}@other-workspace.test",
            now=now,
        )
        # `email_threads` has a 3-column FK to `personal_domains(workspace_id,
        # owner_id, domain_key)` (migration 0069) -- one row per (workspace,
        # owner) combination this fixture's tests create a thread under.
        for ws_id, owner_id in (
            (workspace_id, user_id),
            (workspace_id, other_user_id),
            (other_workspace_id, other_workspace_owner_id),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO personal_domains (
                        id, workspace_id, owner_id, domain_key, classification,
                        enabled, enabled_at, created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, 'email', 'high_stakes',
                        true, :now, :owner_id, :owner_id, :now, :now, 1
                    )
                    """
                ),
                {"id": uuid4(), "workspace_id": ws_id, "owner_id": owner_id, "now": now},
            )
        other_workspace_connector_account_id = uuid4()
        for ws_id, acct_id, actor_id in (
            (workspace_id, connector_account_id, user_id),
            (other_workspace_id, other_workspace_connector_account_id, other_workspace_owner_id),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO connector_accounts (
                        id, workspace_id, provider, external_account_id, display_name,
                        granted_scopes, encrypted_credentials, status, version,
                        created_by, updated_by, created_at, updated_at, owner_id, visibility
                    ) VALUES (
                        :id, :workspace_id, 'gmail', 'tool-test-account', 'Tool test account',
                        '{}', :encrypted_credentials, 'active', 1,
                        :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
                    )
                    """
                ),
                {
                    "id": acct_id,
                    "workspace_id": ws_id,
                    "encrypted_credentials": b"",
                    "actor_id": actor_id,
                    "now": now,
                },
            )

    yield {
        "workspace_id": workspace_id,
        "other_workspace_id": other_workspace_id,
        "user_id": user_id,
        "other_user_id": other_user_id,
        "other_workspace_owner_id": other_workspace_owner_id,
        "connector_account_id": connector_account_id,
        "other_workspace_connector_account_id": other_workspace_connector_account_id,
        "auth": AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="UTC"),
        "now": now,
    }

    with engine.begin() as connection:
        for ws_id in (workspace_id, other_workspace_id):
            for table in (
                "email_messages",
                "email_threads",
                "connector_accounts",
                "personal_domains",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": ws_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": ws_id}
            )


def _insert_thread(
    *, workspace_id: UUID, owner_id: UUID, connector_account_id: UUID, subject: str, now: datetime
) -> UUID:
    thread_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO email_threads (
                    id, workspace_id, owner_id, domain_key, connector_account_id,
                    external_thread_id, subject, last_message_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'email', :connector_account_id,
                    :external_thread_id, :subject, :now, :now, :now
                )
                """
            ),
            {
                "id": thread_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "connector_account_id": connector_account_id,
                "external_thread_id": f"tool-test-{thread_id}",
                "subject": subject,
                "now": now,
            },
        )
    return thread_id


def _insert_message(
    *,
    workspace_id: UUID,
    owner_id: UUID,
    thread_id: UUID,
    sender: str,
    sent_at: datetime,
    body: str | None,
    raw_body: str | None = None,
) -> UUID:
    """`body`, when not `None` (including `""`, the round-4 empty-string
    sentinel), is encrypted via `encrypt_field` exactly as a real fetch
    would store it. `raw_body`, when given, is written to the `body`
    column verbatim instead -- bypassing `encrypt_field` entirely -- so a
    test can simulate a row whose stored ciphertext is not a valid Fernet
    token for the currently configured key (e.g. a rotated key without
    re-encryption), which `encrypt_field` itself can never produce.
    Mutually exclusive with `body`; callers pass exactly one.
    """
    message_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO email_messages (
                    id, workspace_id, owner_id, thread_id, external_message_id,
                    sender, recipients, sent_at, direction, snippet, body,
                    body_fetched_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :thread_id, :external_message_id,
                    :sender, :recipients, :sent_at, 'inbound', NULL, :body,
                    :body_fetched_at, :sent_at, :sent_at
                )
                """
            ),
            {
                "id": message_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "thread_id": thread_id,
                "external_message_id": f"tool-test-{message_id}",
                "sender": sender,
                "recipients": ["owner@tool-test.test"],
                "sent_at": sent_at,
                "body": raw_body
                if raw_body is not None
                else (encrypt_field(body) if body is not None else None),
                "body_fetched_at": sent_at if (body is not None or raw_body is not None) else None,
            },
        )
    return message_id


def test_unknown_thread_id_returns_not_found(tool_context: dict) -> None:
    with SessionFactory() as session:
        result = get_thread_content_tool(session, tool_context["auth"], thread_id=uuid4())
    assert isinstance(result, ToolNotFound)


def test_thread_in_a_different_workspace_returns_not_found(tool_context: dict) -> None:
    thread_id = _insert_thread(
        workspace_id=tool_context["other_workspace_id"],
        owner_id=tool_context["other_workspace_owner_id"],
        connector_account_id=tool_context["other_workspace_connector_account_id"],
        subject="Cross-workspace thread",
        now=tool_context["now"],
    )
    with SessionFactory() as session:
        result = get_thread_content_tool(session, tool_context["auth"], thread_id=thread_id)
    assert isinstance(result, ToolNotFound)


def test_thread_owned_by_a_different_user_in_the_same_workspace_returns_not_found(
    tool_context: dict,
) -> None:
    thread_id = _insert_thread(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["other_user_id"],
        connector_account_id=tool_context["connector_account_id"],
        subject="Someone else's thread",
        now=tool_context["now"],
    )
    with SessionFactory() as session:
        result = get_thread_content_tool(session, tool_context["auth"], thread_id=thread_id)
    assert isinstance(result, ToolNotFound)


def test_fetched_messages_return_genuinely_decrypted_bodies_in_sent_at_order(
    tool_context: dict,
) -> None:
    thread_id = _insert_thread(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        connector_account_id=tool_context["connector_account_id"],
        subject="Signed contract needed",
        now=tool_context["now"],
    )
    now = tool_context["now"]
    _insert_message(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        thread_id=thread_id,
        sender="later@partner-co.test",
        sent_at=now,
        body="Following up -- any update?",
    )
    _insert_message(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        thread_id=thread_id,
        sender="earlier@partner-co.test",
        sent_at=now - timedelta(days=2),
        body="Please sign and return the attached contract by Friday.",
    )

    with SessionFactory() as session:
        result = get_thread_content_tool(session, tool_context["auth"], thread_id=thread_id)

    assert isinstance(result, ToolResult)
    assert result.output["subject"] == "Signed contract needed"
    messages = result.output["messages"]
    assert len(messages) == 2
    assert messages[0]["sender"] == "earlier@partner-co.test"
    assert messages[0]["body"] == "Please sign and return the attached contract by Friday."
    assert messages[1]["sender"] == "later@partner-co.test"
    assert messages[1]["body"] == "Following up -- any update?"


def test_thread_with_more_than_the_cap_returns_only_the_most_recent_messages_in_order(
    tool_context: dict,
) -> None:
    """Round 13 review finding: this was the one deterministic tool in the
    whole AI runtime with no size bound at all, unlike every sibling
    (`_MAX_FACTORS`/`_MAX_CLAIMS`/`_MAX_EVIDENCE`/`_MAX_RECORDS_PER_DOMAIN`),
    a direct gap against the design doc's own Decision 6 "every tool
    result is ... size-bounded" contract. Inserts `MAX_THREAD_MESSAGES +
    10` messages -- proves the tool returns exactly `MAX_THREAD_MESSAGES`
    of them, that the *newest* ones survive the cap rather than the
    oldest (the newest is normally the one that triggered this call in
    the first place, so an oldest-first-then-truncate approach would drop
    exactly the wrong end), and that the surviving messages are still
    returned in the same oldest-first order every other test in this file
    already relies on.
    """
    thread_id = _insert_thread(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        connector_account_id=tool_context["connector_account_id"],
        subject="Long-running thread",
        now=tool_context["now"],
    )
    now = tool_context["now"]
    total_messages = MAX_THREAD_MESSAGES + 10
    for i in range(total_messages):
        _insert_message(
            workspace_id=tool_context["workspace_id"],
            owner_id=tool_context["user_id"],
            thread_id=thread_id,
            sender=f"sender-{i}@partner-co.test",
            sent_at=now + timedelta(minutes=i),
            body=f"message-{i}",
        )

    with SessionFactory() as session:
        result = get_thread_content_tool(session, tool_context["auth"], thread_id=thread_id)

    assert isinstance(result, ToolResult)
    messages = result.output["messages"]
    assert len(messages) == MAX_THREAD_MESSAGES
    # The oldest surviving message is the one dropped-up-to boundary, not
    # message-0 -- the ten oldest messages (message-0 .. message-9) must
    # not survive the cap.
    expected_bodies = [f"message-{i}" for i in range(10, total_messages)]
    assert [m["body"] for m in messages] == expected_bodies
    # The newest message (the one that would realistically have triggered
    # this call) is always present.
    assert messages[-1]["body"] == f"message-{total_messages - 1}"


def test_trigger_message_id_is_always_included_even_when_older_than_the_cap_window(
    tool_context: dict,
) -> None:
    """Round 14 review finding: round 13's own cap alone -- "most recent
    `MAX_THREAD_MESSAGES` by `sent_at`" -- can silently exclude the
    specific message that triggered this call. `detect_actions_since`'s
    own eligibility query prioritizes an account's freshly-synced messages
    over older same-thread backlog (`ORDER BY (created_at >= since) DESC,
    sent_at ASC`), so on a busy, long-running thread an old backlog
    message can still be waiting its turn long after `MAX_THREAD_
    MESSAGES` *newer* messages in that same thread already have non-null
    bodies -- by the time it's finally processed, it would fall outside
    the "most recent N" window entirely, and the model would never see
    the very email the run exists to evaluate. Simulates exactly that:
    the oldest message in a `MAX_THREAD_MESSAGES + 10`-message thread is
    the one this call is triggered for; without the `trigger_message_id`
    guarantee, this exact message would be the *first* one the cap drops
    (proven by the sibling test above, whose own oldest-10-dropped
    assertion this test's own message-0 is one of).
    """
    thread_id = _insert_thread(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        connector_account_id=tool_context["connector_account_id"],
        subject="Long-running thread, old backlog message",
        now=tool_context["now"],
    )
    now = tool_context["now"]
    total_messages = MAX_THREAD_MESSAGES + 10
    message_ids: list[UUID] = []
    for i in range(total_messages):
        message_id = _insert_message(
            workspace_id=tool_context["workspace_id"],
            owner_id=tool_context["user_id"],
            thread_id=thread_id,
            sender=f"sender-{i}@partner-co.test",
            sent_at=now + timedelta(minutes=i),
            body=f"message-{i}",
        )
        message_ids.append(message_id)
    trigger_id = message_ids[0]

    with SessionFactory() as session:
        result = get_thread_content_tool(
            session, tool_context["auth"], thread_id=thread_id, trigger_message_id=trigger_id
        )

    assert isinstance(result, ToolResult)
    messages = result.output["messages"]
    # Still capped at MAX_THREAD_MESSAGES total -- the guarantee reserves
    # a slot for the trigger rather than appending a 51st message.
    assert len(messages) == MAX_THREAD_MESSAGES
    # The trigger (message-0, the oldest) is present, first in the still-
    # oldest-first output, even though it would not otherwise have
    # survived the cap.
    assert messages[0]["body"] == "message-0"
    # The 49 most-recent other messages (message-11..message-59) fill the
    # remaining slots, still in oldest-first order.
    rest_start = total_messages - (MAX_THREAD_MESSAGES - 1)
    expected_rest = [f"message-{i}" for i in range(rest_start, total_messages)]
    assert [m["body"] for m in messages[1:]] == expected_rest


def test_message_with_no_fetched_body_is_omitted_not_rendered_empty(tool_context: dict) -> None:
    thread_id = _insert_thread(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        connector_account_id=tool_context["connector_account_id"],
        subject="Partially fetched thread",
        now=tool_context["now"],
    )
    now = tool_context["now"]
    _insert_message(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        thread_id=thread_id,
        sender="fetched@partner-co.test",
        sent_at=now,
        body="This one has a fetched body.",
    )
    _insert_message(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        thread_id=thread_id,
        sender="unfetched@partner-co.test",
        sent_at=now - timedelta(days=1),
        body=None,
    )

    with SessionFactory() as session:
        result = get_thread_content_tool(session, tool_context["auth"], thread_id=thread_id)

    assert isinstance(result, ToolResult)
    messages = result.output["messages"]
    assert len(messages) == 1
    assert messages[0]["sender"] == "fetched@partner-co.test"


def test_message_with_empty_string_body_is_rendered_as_an_empty_message_not_omitted(
    tool_context: dict,
) -> None:
    """Round 4's `gmail_adapter.py` fix writes an empty-string body
    sentinel for a message whose Gmail response parsed successfully but
    had no extractable `text/plain` part -- this module's own docstring
    distinguishes that ("genuinely empty") from `NULL` ("not yet
    fetched"): `body IS NOT NULL` alone selects it, so it must render as
    an empty message, the opposite of the previous test's `NULL` case.
    Round 5 review found this distinction had no test coverage.
    """
    thread_id = _insert_thread(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        connector_account_id=tool_context["connector_account_id"],
        subject="Thread with an HTML-only message",
        now=tool_context["now"],
    )
    now = tool_context["now"]
    _insert_message(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        thread_id=thread_id,
        sender="html-only@partner-co.test",
        sent_at=now,
        body="",
    )

    with SessionFactory() as session:
        result = get_thread_content_tool(session, tool_context["auth"], thread_id=thread_id)

    assert isinstance(result, ToolResult)
    messages = result.output["messages"]
    assert len(messages) == 1
    assert messages[0]["sender"] == "html-only@partner-co.test"
    assert messages[0]["body"] == ""


def test_message_body_that_fails_to_decrypt_renders_fallback_text(tool_context: dict) -> None:
    """A stored `body` that is not a valid Fernet token for the currently
    configured key (e.g. `ECC_PERSONAL_DATA_ENCRYPTION_KEY` rotated
    without re-encrypting stored rows) must not raise out of the tool --
    `get_thread_content_tool`'s own `InvalidToken` fallback branch,
    previously untested per round 5 review.
    """
    thread_id = _insert_thread(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        connector_account_id=tool_context["connector_account_id"],
        subject="Thread with an undecryptable message",
        now=tool_context["now"],
    )
    now = tool_context["now"]
    _insert_message(
        workspace_id=tool_context["workspace_id"],
        owner_id=tool_context["user_id"],
        thread_id=thread_id,
        sender="undecryptable@partner-co.test",
        sent_at=now,
        body=None,
        raw_body="not-a-valid-fernet-token",
    )

    with SessionFactory() as session:
        result = get_thread_content_tool(session, tool_context["auth"], thread_id=thread_id)

    assert isinstance(result, ToolResult)
    messages = result.output["messages"]
    assert len(messages) == 1
    assert messages[0]["sender"] == "undecryptable@partner-co.test"
    assert messages[0]["body"] == "[unable to decrypt -- contact support]"
