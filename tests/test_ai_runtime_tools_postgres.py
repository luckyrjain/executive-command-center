"""Phase 4 Task 4, Step 3: `attention.get_item`/`knowledge.get_entity` tool
handlers (design doc Decision 6).

Covers, per `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`'s
Task 4:

1. `attention/tools.py:get_item_tool` returns the tool's declared output
   shape (`{entity_type, score, confidence, factors, evidence_refs}`,
   migration `0029_phase4_prompt_tool_versions.py`'s seeded schema) for an
   item that belongs to the caller's own workspace.
2. `knowledge/tools.py:get_entity_tool` returns `{title, claims, evidence}`
   for an entity that belongs to the caller's own workspace.
3. Both handlers return `ai_runtime.tools.ToolNotFound` -- never raise, never
   distinguish "genuinely missing" from "belongs to a different workspace"
   -- for a cross-workspace id, matching every existing Phase 1-3 read
   endpoint's non-disclosing convention exactly (`attention.py:get_
   attention_item`, `entities.py:get_entity`).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from json import dumps
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.tools import ToolNotFound, ToolResult
from ecc.domains.attention.tools import get_item_tool
from ecc.domains.knowledge.tools import get_entity_tool

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def two_workspaces() -> Iterator[tuple[UUID, UUID, UUID, UUID]]:
    """`(workspace_a, user_a, workspace_b, user_b)` -- two fully separate
    workspaces so tool handlers can be exercised against both an owned
    reference and a cross-workspace one.
    """
    workspace_a, workspace_b = uuid4(), uuid4()
    user_a, user_b = uuid4(), uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for workspace_id, user_id in ((workspace_a, user_a), (workspace_b, user_b)):
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, timezone, created_at) "
                    "VALUES (:id, 'AI Runtime Tools Test', 'UTC', :created_at)"
                ),
                {"id": workspace_id, "created_at": now},
            )
            connection.execute(
                text(
                    "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                    "VALUES (:id, :workspace_id, :email, 'hash', :created_at)"
                ),
                {
                    "id": user_id,
                    "workspace_id": workspace_id,
                    "email": f"{user_id}@example.test",
                    "created_at": now,
                },
            )
    try:
        yield workspace_a, user_a, workspace_b, user_b
    finally:
        with engine.begin() as connection:
            for workspace_id in (workspace_a, workspace_b):
                for table in (
                    "ai_run_steps",
                    "ai_runs",
                    "knowledge_claims",
                    "entity_aliases",
                    "pkos_evidence",
                    "pkos_nodes",
                    "attention_items",
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


def _insert_attention_item(workspace_id: UUID, *, factors: list[dict] | None = None) -> UUID:
    item_id = uuid4()
    now = datetime.now(UTC)
    factors = (
        factors
        if factors is not None
        else [
            {
                "code": "overdue",
                "label": "Task is overdue",
                "points": 40,
                "source_field": "due_date",
            },
            {"code": "pinned", "label": "Manually pinned", "points": 10, "source_field": "pinned"},
        ]
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO attention_items (
                    id, workspace_id, entity_type, entity_id, source_entity_version,
                    score, confidence, factors, explanation, generated_at, expires_at,
                    pinned, policy_version
                ) VALUES (
                    :id, :workspace_id, 'task', :entity_id, 1, 62, 0.900,
                    CAST(:factors AS jsonb), 'because reasons', :now, :expires_at, false, 1
                )
                """
            ),
            {
                "id": item_id,
                "workspace_id": workspace_id,
                "entity_id": uuid4(),
                "factors": dumps(factors),
                "now": now,
                "expires_at": now + timedelta(days=1),
            },
        )
    return item_id


def _insert_entity(workspace_id: UUID) -> UUID:
    entity_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, created_at, updated_at
                ) VALUES (:id, :workspace_id, 'person', 'Jane Executive', :now, :now)
                """
            ),
            {"id": entity_id, "workspace_id": workspace_id, "now": now},
        )
        evidence_id = uuid4()
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
                ) VALUES (:id, :workspace_id, :node_id, 'note', 'note-ref', :sha, :now)
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": entity_id,
                "sha": "0" * 64,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO knowledge_claims (
                    id, workspace_id, subject_id, predicate, value_json, source_id,
                    confidence, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :subject_id, 'title', CAST(:value AS jsonb),
                    :source_id, 0.90, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "subject_id": entity_id,
                "value": dumps({"value": "VP Engineering"}),
                "source_id": evidence_id,
                "now": now,
            },
        )
    return entity_id


def _auth(workspace_id: UUID, user_id: UUID) -> AuthContext:
    return AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="UTC")


# ---------------------------------------------------------------------------
# attention.get_item
# ---------------------------------------------------------------------------


def test_get_item_tool_returns_declared_shape_for_owned_item(
    two_workspaces: tuple[UUID, UUID, UUID, UUID],
) -> None:
    workspace_a, user_a, _workspace_b, _user_b = two_workspaces
    item_id = _insert_attention_item(workspace_a)

    with SessionFactory() as session:
        result = get_item_tool(session, _auth(workspace_a, user_a), item_id)

    assert isinstance(result, ToolResult)
    assert result.output["entity_type"] == "task"
    assert result.output["score"] == 62.0
    assert result.output["confidence"] == pytest.approx(0.9)
    assert result.output["evidence_refs"] == []
    codes = {factor["code"] for factor in result.output["factors"]}
    assert codes == {"overdue", "pinned"}


def test_get_item_tool_cross_workspace_returns_not_found(
    two_workspaces: tuple[UUID, UUID, UUID, UUID],
) -> None:
    workspace_a, _user_a, workspace_b, user_b = two_workspaces
    item_id = _insert_attention_item(workspace_a)

    with SessionFactory() as session:
        # user_b (workspace_b) attempts to read workspace_a's item.
        result = get_item_tool(session, _auth(workspace_b, user_b), item_id)

    assert isinstance(result, ToolNotFound)
    assert result.tool == "attention.get_item"


def test_get_item_tool_genuinely_missing_id_returns_not_found(
    two_workspaces: tuple[UUID, UUID, UUID, UUID],
) -> None:
    workspace_a, user_a, _workspace_b, _user_b = two_workspaces
    with SessionFactory() as session:
        result = get_item_tool(session, _auth(workspace_a, user_a), uuid4())
    assert isinstance(result, ToolNotFound)


def test_get_item_tool_truncates_factors_to_max_size(
    two_workspaces: tuple[UUID, UUID, UUID, UUID],
) -> None:
    workspace_a, user_a, _workspace_b, _user_b = two_workspaces
    many_factors = [
        {"code": f"f{i}", "label": f"factor {i}", "points": 1, "source_field": "x"}
        for i in range(30)
    ]
    item_id = _insert_attention_item(workspace_a, factors=many_factors)

    with SessionFactory() as session:
        result = get_item_tool(session, _auth(workspace_a, user_a), item_id)

    assert isinstance(result, ToolResult)
    assert len(result.output["factors"]) == 20


# ---------------------------------------------------------------------------
# knowledge.get_entity
# ---------------------------------------------------------------------------


def test_get_entity_tool_returns_declared_shape_for_owned_entity(
    two_workspaces: tuple[UUID, UUID, UUID, UUID],
) -> None:
    workspace_a, user_a, _workspace_b, _user_b = two_workspaces
    entity_id = _insert_entity(workspace_a)

    with SessionFactory() as session:
        result = get_entity_tool(session, _auth(workspace_a, user_a), entity_id)

    assert isinstance(result, ToolResult)
    assert result.output["title"] == "Jane Executive"
    assert len(result.output["claims"]) == 1
    assert result.output["claims"][0]["predicate"] == "title"
    assert len(result.output["evidence"]) == 1
    assert result.output["evidence"][0]["source_type"] == "note"
    assert result.output["evidence"][0]["status"] == "available"


def test_get_entity_tool_cross_workspace_returns_not_found(
    two_workspaces: tuple[UUID, UUID, UUID, UUID],
) -> None:
    workspace_a, _user_a, workspace_b, user_b = two_workspaces
    entity_id = _insert_entity(workspace_a)

    with SessionFactory() as session:
        result = get_entity_tool(session, _auth(workspace_b, user_b), entity_id)

    assert isinstance(result, ToolNotFound)
    assert result.tool == "knowledge.get_entity"


def test_get_entity_tool_genuinely_missing_id_returns_not_found(
    two_workspaces: tuple[UUID, UUID, UUID, UUID],
) -> None:
    workspace_a, user_a, _workspace_b, _user_b = two_workspaces
    with SessionFactory() as session:
        result = get_entity_tool(session, _auth(workspace_a, user_a), uuid4())
    assert isinstance(result, ToolNotFound)
