"""`personal.get_insight_sources` tool handler (`ecc.domains.personal.
insight_tools`), Phase 7 Task 5 part 2 -- direct-call coverage of its
fail-closed grant/enablement matrix, mirroring `test_ai_runtime_tools_
postgres.py`'s established "call the handler function directly, assert
`ToolResult`/`ToolNotFound`" pattern rather than going through `execute_
run`/HTTP.

This module exists specifically to close a coverage gap the production-
endpoint tests (`test_personal_ai_insights_postgres.py`) and the
evaluation-harness tests (`test_ai_runtime_personal_insight_evaluation_
postgres.py`) both leave open: neither ever exercises a domain that is
*disabled but still has an active grant* (the module's own docstring
calls this out by name), a grant whose `granted_categories` is narrower
than what a domain actually has recorded (the single most security-
relevant property this tool has), or a multi-domain request where only
some of the requested domains are granted.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult
from ecc.domains.personal.domains import encrypt_record_payload
from ecc.domains.personal.insight_tools import get_insight_sources_tool

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def tool_context() -> Iterator[dict]:
    workspace_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Insight Tools Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
        )

    yield {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "auth": AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="UTC"),
    }

    with engine.begin() as connection:
        for table in (
            "domain_records",
            "cross_domain_grants",
            "personal_domains",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"),
            {"workspace_id": workspace_id},
        )


def _enable_domain(
    workspace_id: UUID, user_id: UUID, domain_key: str, *, classification: str = "standard"
) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO personal_domains (
                    id, workspace_id, owner_id, domain_key, classification,
                    enabled, enabled_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, :classification,
                    true, :now, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "domain_key": domain_key,
                "classification": classification,
                "now": now,
            },
        )


def _disable_domain(workspace_id: UUID, user_id: UUID, domain_key: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE personal_domains SET enabled = false "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                "AND domain_key = :domain_key"
            ),
            {"workspace_id": workspace_id, "owner_id": user_id, "domain_key": domain_key},
        )


def _grant(workspace_id: UUID, user_id: UUID, domain_key: str, categories: list[str]) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO cross_domain_grants (
                    id, workspace_id, owner_id, source_domain_key, purpose,
                    granted_categories, granted_at, expires_at, created_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, 'insight_generation',
                    CAST(:categories AS jsonb), :now, NULL, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "domain_key": domain_key,
                "categories": dumps(categories),
                "now": now,
            },
        )


def _record(
    workspace_id: UUID, user_id: UUID, domain_key: str, record_type: str, payload: dict[str, Any]
) -> UUID:
    record_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO domain_records (
                    id, workspace_id, owner_id, domain_key, record_type, payload,
                    effective_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, :record_type,
                    CAST(:payload AS jsonb), :now, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": record_id,
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "domain_key": domain_key,
                "record_type": record_type,
                "payload": dumps(payload),
                "now": now,
            },
        )
    return record_id


def test_no_grant_at_all_returns_not_found(tool_context: dict) -> None:
    _enable_domain(tool_context["workspace_id"], tool_context["user_id"], "habits")
    with SessionFactory() as session:
        result = get_insight_sources_tool(session, tool_context["auth"], ["habits"])
    assert isinstance(result, ToolNotFound)


def test_disabled_domain_with_active_grant_returns_not_found(tool_context: dict) -> None:
    """The exact case this tool's own module docstring calls out: disabling
    a domain does not revoke any `cross_domain_grants` row naming it, so
    the tool itself must still refuse to read a disabled domain even
    though an active grant for it exists.
    """
    workspace_id, user_id = tool_context["workspace_id"], tool_context["user_id"]
    _enable_domain(workspace_id, user_id, "habits")
    _grant(workspace_id, user_id, "habits", ["habit_summary"])
    _record(workspace_id, user_id, "habits", "habit_summary", {"routine": "Run"})
    _disable_domain(workspace_id, user_id, "habits")

    with SessionFactory() as session:
        result = get_insight_sources_tool(session, tool_context["auth"], ["habits"])
    assert isinstance(result, ToolNotFound)


def test_ungranted_category_is_excluded_from_output(tool_context: dict) -> None:
    """The single most security-relevant property of this tool: a grant
    naming only `["habit_summary"]` must never expose a different
    `record_type` recorded under the same domain, even one with real data.
    """
    workspace_id, user_id = tool_context["workspace_id"], tool_context["user_id"]
    _enable_domain(workspace_id, user_id, "habits")
    _grant(workspace_id, user_id, "habits", ["habit_summary"])
    granted_id = _record(workspace_id, user_id, "habits", "habit_summary", {"routine": "Run"})
    _record(workspace_id, user_id, "habits", "private_note", {"secret": "should not leak"})

    with SessionFactory() as session:
        result = get_insight_sources_tool(session, tool_context["auth"], ["habits"])
    assert isinstance(result, ToolResult)
    sources = result.output["sources"]
    assert len(sources) == 1
    record_ids = {record["id"] for record in sources[0]["records"]}
    record_types = {record["record_type"] for record in sources[0]["records"]}
    assert record_ids == {str(granted_id)}
    assert "private_note" not in record_types


def test_multi_domain_request_with_only_one_granted_fails_the_whole_call(
    tool_context: dict,
) -> None:
    """`INSIGHT-CONTRACT.md`: "Cross-domain insight requires an active
    grant covering every source" -- a conjunction, not a best-effort
    partial read. `habits` is granted; `learning` is enabled but never
    granted; the whole two-domain request must fail, not silently return
    `habits`-only data.
    """
    workspace_id, user_id = tool_context["workspace_id"], tool_context["user_id"]
    _enable_domain(workspace_id, user_id, "habits")
    _grant(workspace_id, user_id, "habits", ["habit_summary"])
    _record(workspace_id, user_id, "habits", "habit_summary", {"routine": "Run"})
    _enable_domain(workspace_id, user_id, "learning")

    with SessionFactory() as session:
        result = get_insight_sources_tool(session, tool_context["auth"], ["habits", "learning"])
    assert isinstance(result, ToolNotFound)


def test_granted_domain_returns_decrypted_payload_and_real_classification(
    tool_context: dict,
) -> None:
    workspace_id, user_id = tool_context["workspace_id"], tool_context["user_id"]
    _enable_domain(workspace_id, user_id, "habits", classification="standard")
    _grant(workspace_id, user_id, "habits", ["habit_summary"])
    record_id = _record(
        workspace_id, user_id, "habits", "habit_summary", {"routine": "Morning run"}
    )

    with SessionFactory() as session:
        result = get_insight_sources_tool(session, tool_context["auth"], ["habits"])
    assert isinstance(result, ToolResult)
    source = result.output["sources"][0]
    assert source["domain_key"] == "habits"
    assert source["classification"] == "standard"
    record = source["records"][0]
    assert record["id"] == str(record_id)
    assert record["payload"] == {"routine": "Morning run"}


def test_granted_high_stakes_domain_returns_genuinely_decrypted_narrative_field(
    tool_context: dict,
) -> None:
    """The one prior decrypted-payload test above uses `habits` (`standard`
    classification, no encrypted field at all) -- passing there proves
    nothing about whether this tool's own `decrypt_record_payload` call
    (`insight_tools.py`) actually decrypts anything, only that plaintext
    passes through unchanged. This test inserts a `health` (`high_stakes`)
    `vital_reading` record with its `notes` field genuinely Fernet-
    encrypted at rest (via the same `encrypt_record_payload` the real
    `create_record_endpoint` write path uses), then asserts the tool's own
    output contains the real plaintext, not the ciphertext -- the exact
    property `personal.generate_insight`'s prompt-assembly code depends on
    to reason about real narrative content rather than an opaque token.
    """
    workspace_id, user_id = tool_context["workspace_id"], tool_context["user_id"]
    _enable_domain(workspace_id, user_id, "health", classification="high_stakes")
    _grant(workspace_id, user_id, "health", ["vital_reading"])
    plaintext_notes = "felt lightheaded after standing up quickly"
    encrypted_payload = encrypt_record_payload(
        "vital_reading",
        {"metric": "resting_heart_rate", "value": "62", "notes": plaintext_notes},
    )
    # Sanity check: genuinely encrypted before insert, not merely unchanged.
    assert encrypted_payload["notes"] != plaintext_notes
    record_id = _record(workspace_id, user_id, "health", "vital_reading", encrypted_payload)

    with SessionFactory() as session:
        result = get_insight_sources_tool(session, tool_context["auth"], ["health"])
    assert isinstance(result, ToolResult)
    source = result.output["sources"][0]
    assert source["domain_key"] == "health"
    assert source["classification"] == "high_stakes"
    record = source["records"][0]
    assert record["id"] == str(record_id)
    assert record["payload"]["metric"] == "resting_heart_rate"
    assert record["payload"]["notes"] == plaintext_notes
