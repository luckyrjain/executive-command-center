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
as an empty string; and messages are returned oldest-first (`sent_at
ASC`).
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
from ecc.domains.personal.email_action_tools import get_thread_content_tool

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
) -> UUID:
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
                "body": encrypt_field(body) if body is not None else None,
                "body_fetched_at": sent_at if body is not None else None,
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
