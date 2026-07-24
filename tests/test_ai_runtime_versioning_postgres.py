"""Phase 4 Task 2: prompt/tool versioning (design doc Decision 3).

Covers, per `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`'s
Task 2:

1. The database-level immutability trigger (`trg_prompt_versions_
   immutability`/`trg_tool_definitions_immutability`, migration
   `0029_phase4_prompt_tool_versions.py`) -- exercised with **direct SQL**
   `UPDATE` statements against the tables, bypassing `prompts.py`/
   `tools.py` entirely, so a bug in that application code could never
   silently defeat this property.
2. Migration `0029`'s seeded `attention.explain_item.v1` prompt and
   `attention.get_item`/`knowledge.get_entity` tool rows.
3. `prompts.py`/`tools.py`'s read/activation functions.
4. The partial unique index enforcing exactly one `active` version per
   `prompt_id`/tool `name`.
5. `POST /api/v1/ai/policies/{prompt_id_or_tool_name}/activate`.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from json import dumps
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime import prompts as ai_prompts
from ecc.domains.ai_runtime import tools as ai_tools
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

SEEDED_PROMPT_ID = "attention.explain_item.v1"
SEEDED_TOOL_ATTENTION = "attention.get_item"
SEEDED_TOOL_KNOWLEDGE = "knowledge.get_entity"


# ---------------------------------------------------------------------------
# Test-scoped prompt/tool rows -- distinct prompt_id/name namespaces from the
# migration's seeds, so activation/immutability tests never disturb the
# seeded `attention.explain_item.v1`/`attention.get_item`/`knowledge.get_
# entity` rows other tests (and other test files) depend on being active.
# ---------------------------------------------------------------------------


def _insert_prompt_row(
    *,
    prompt_id: str,
    version: int,
    status: str,
    template: str = "draft template",
    input_schema_ref: str = "test.input.v1",
    output_schema_ref: str = "test.output.v1",
) -> UUID:
    row_id = uuid4()
    template_hash = ai_prompts.compute_template_hash(
        template=template, input_schema_ref=input_schema_ref, output_schema_ref=output_schema_ref
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO prompt_versions (
                    id, prompt_id, version, template, template_hash,
                    input_schema_ref, output_schema_ref, status, created_at, updated_at
                ) VALUES (
                    :id, :prompt_id, :version, :template, :template_hash,
                    :input_schema_ref, :output_schema_ref, :status, now(), now()
                )
                """
            ),
            {
                "id": row_id,
                "prompt_id": prompt_id,
                "version": version,
                "template": template,
                "template_hash": template_hash,
                "input_schema_ref": input_schema_ref,
                "output_schema_ref": output_schema_ref,
                "status": status,
            },
        )
    return row_id


def _insert_tool_row(
    *,
    name: str,
    version: int,
    status: str,
    scopes: list[str] | None = None,
    input_schema: dict | None = None,
    output_schema: dict | None = None,
    handler_ref: str = "ecc.domains.test:handler",
) -> UUID:
    row_id = uuid4()
    scopes = scopes if scopes is not None else ["read:test"]
    input_schema = input_schema if input_schema is not None else {"type": "object"}
    output_schema = output_schema if output_schema is not None else {"type": "object"}
    definition_hash = ai_tools.compute_definition_hash(
        input_schema=input_schema,
        output_schema=output_schema,
        scopes=scopes,
        handler_ref=handler_ref,
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tool_definitions (
                    id, name, version, scopes, input_schema, output_schema,
                    handler_ref, definition_hash, status, created_at, updated_at
                ) VALUES (
                    :id, :name, :version, :scopes, CAST(:input_schema AS jsonb),
                    CAST(:output_schema AS jsonb), :handler_ref, :definition_hash,
                    :status, now(), now()
                )
                """
            ),
            {
                "id": row_id,
                "name": name,
                "version": version,
                "scopes": scopes,
                "input_schema": dumps(input_schema),
                "output_schema": dumps(output_schema),
                "handler_ref": handler_ref,
                "definition_hash": definition_hash,
                "status": status,
            },
        )
    return row_id


@pytest.fixture
def cleanup_prompt_ids() -> Iterator[list[str]]:
    prompt_ids: list[str] = []
    yield prompt_ids
    if prompt_ids:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM prompt_versions WHERE prompt_id = ANY(:prompt_ids)"),
                {"prompt_ids": prompt_ids},
            )


@pytest.fixture
def cleanup_tool_names() -> Iterator[list[str]]:
    names: list[str] = []
    yield names
    if names:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM tool_definitions WHERE name = ANY(:names)"),
                {"names": names},
            )


# ---------------------------------------------------------------------------
# Step 1/2: the database-level immutability trigger -- direct SQL only.
# ---------------------------------------------------------------------------


def test_prompt_versions_draft_row_content_is_freely_editable(
    cleanup_prompt_ids: list[str],
) -> None:
    """A `draft` row (`OLD.status = 'draft'`) is never restricted -- editing
    before first activation is normal (Decision 3: immutability begins only
    "once a row's status leaves draft").
    """
    prompt_id = f"test.draft-editable.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="draft", template="v1")

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE prompt_versions SET template = 'v2' WHERE prompt_id = :prompt_id"),
            {"prompt_id": prompt_id},
        )
        row = connection.execute(
            text("SELECT template FROM prompt_versions WHERE prompt_id = :prompt_id"),
            {"prompt_id": prompt_id},
        ).scalar_one()
    assert row == "v2"


def test_prompt_versions_active_row_template_update_rejected_by_trigger(
    cleanup_prompt_ids: list[str],
) -> None:
    """The core property this trigger exists for: a direct `UPDATE`
    statement against `template`, bypassing `prompts.py` entirely, is
    rejected once `status <> 'draft'` -- not merely discouraged at the
    application layer.
    """
    prompt_id = f"test.active-immutable.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active", template="original")

    with pytest.raises(ProgrammingError) as excinfo:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE prompt_versions SET template = 'tampered' WHERE prompt_id = :prompt_id"
                ),
                {"prompt_id": prompt_id},
            )
    assert "immutable" in str(excinfo.value).lower()

    with engine.connect() as connection:
        template = connection.execute(
            text("SELECT template FROM prompt_versions WHERE prompt_id = :prompt_id"),
            {"prompt_id": prompt_id},
        ).scalar_one()
    assert template == "original"


@pytest.mark.parametrize(
    "column,value",
    [
        ("template_hash", "0" * 64),
        ("input_schema_ref", "tampered.input.v1"),
        ("output_schema_ref", "tampered.output.v1"),
    ],
)
def test_prompt_versions_retired_row_every_hashed_column_rejected(
    cleanup_prompt_ids: list[str], column: str, value: str
) -> None:
    """`retired` rows are just as immutable as `active` ones -- the trigger
    guards on `OLD.status <> 'draft'`, not `OLD.status = 'active'`
    specifically. Covers every column the hashed identity envelope
    includes, not only `template`/`template_hash` (this migration's
    module docstring explains why the guarded set is deliberately wider
    than Decision 3's two headline column names).
    """
    prompt_id = f"test.retired-immutable.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="retired")

    with pytest.raises(ProgrammingError):
        with engine.begin() as connection:
            connection.execute(
                text(f"UPDATE prompt_versions SET {column} = :value WHERE prompt_id = :prompt_id"),  # noqa: S608
                {"value": value, "prompt_id": prompt_id},
            )


def test_prompt_versions_status_and_updated_at_remain_editable_post_activation(
    cleanup_prompt_ids: list[str],
) -> None:
    """The trigger must **not** block `status`/`updated_at` -- confirms the
    property `activate_prompt_version` depends on: it retires the outgoing
    row and activates the incoming one via `status`-only `UPDATE`s.
    """
    prompt_id = f"test.status-editable.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE prompt_versions SET status = 'retired', updated_at = now() "
                "WHERE prompt_id = :prompt_id"
            ),
            {"prompt_id": prompt_id},
        )
        status = connection.execute(
            text("SELECT status FROM prompt_versions WHERE prompt_id = :prompt_id"),
            {"prompt_id": prompt_id},
        ).scalar_one()
    assert status == "retired"


def test_tool_definitions_draft_row_content_is_freely_editable(
    cleanup_tool_names: list[str],
) -> None:
    name = f"test.draft-editable.{uuid4().hex}"
    cleanup_tool_names.append(name)
    _insert_tool_row(name=name, version=1, status="draft")

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE tool_definitions SET handler_ref = 'new:ref' WHERE name = :name"),
            {"name": name},
        )
        handler_ref = connection.execute(
            text("SELECT handler_ref FROM tool_definitions WHERE name = :name"), {"name": name}
        ).scalar_one()
    assert handler_ref == "new:ref"


@pytest.mark.parametrize(
    "column,sql_value",
    [
        ("scopes", "ARRAY['write:everything']"),
        ("handler_ref", "'tampered:ref'"),
        ("input_schema", '\'{"type": "string"}\'::jsonb'),
        ("output_schema", '\'{"type": "string"}\'::jsonb'),
        ("definition_hash", "'" + "0" * 64 + "'"),
    ],
)
def test_tool_definitions_active_row_every_hashed_column_rejected(
    cleanup_tool_names: list[str], column: str, sql_value: str
) -> None:
    """The tool-side analogue of the prompt trigger test above -- every
    column in `tools.py:compute_definition_hash`'s envelope (`input_
    schema`, `output_schema`, `scopes`, `handler_ref`) plus the hash column
    itself is guarded once `status <> 'draft'`.
    """
    name = f"test.active-immutable.{uuid4().hex}"
    cleanup_tool_names.append(name)
    _insert_tool_row(name=name, version=1, status="active")

    with pytest.raises(ProgrammingError):
        with engine.begin() as connection:
            connection.execute(
                text(  # noqa: S608
                    f"UPDATE tool_definitions SET {column} = {sql_value} WHERE name = :name"
                ),
                {"name": name},
            )


def test_tool_definitions_status_remains_editable_post_activation(
    cleanup_tool_names: list[str],
) -> None:
    name = f"test.status-editable.{uuid4().hex}"
    cleanup_tool_names.append(name)
    _insert_tool_row(name=name, version=1, status="active")

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tool_definitions SET status = 'retired', updated_at = now() "
                "WHERE name = :name"
            ),
            {"name": name},
        )
        status = connection.execute(
            text("SELECT status FROM tool_definitions WHERE name = :name"), {"name": name}
        ).scalar_one()
    assert status == "retired"


# ---------------------------------------------------------------------------
# Partial unique index: exactly one active version per prompt_id/tool name.
# ---------------------------------------------------------------------------


def test_prompt_versions_partial_unique_index_rejects_second_active_row(
    cleanup_prompt_ids: list[str],
) -> None:
    prompt_id = f"test.dup-active.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO prompt_versions (
                        id, prompt_id, version, template, template_hash,
                        input_schema_ref, output_schema_ref, status, created_at, updated_at
                    ) VALUES (
                        :id, :prompt_id, 2, 'v2', :hash, 'i', 'o', 'active', now(), now()
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "prompt_id": prompt_id,
                    "hash": ai_prompts.compute_template_hash(
                        template="v2", input_schema_ref="i", output_schema_ref="o"
                    ),
                },
            )


def test_tool_definitions_partial_unique_index_rejects_second_active_row(
    cleanup_tool_names: list[str],
) -> None:
    name = f"test.dup-active.{uuid4().hex}"
    cleanup_tool_names.append(name)
    _insert_tool_row(name=name, version=1, status="active")

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO tool_definitions (
                        id, name, version, scopes, input_schema, output_schema,
                        handler_ref, definition_hash, status, created_at, updated_at
                    ) VALUES (
                        :id, :name, 2, ARRAY['read:test'], '{}'::jsonb, '{}'::jsonb,
                        'h', :hash, 'active', now(), now()
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "name": name,
                    "hash": ai_tools.compute_definition_hash(
                        input_schema={}, output_schema={}, scopes=["read:test"], handler_ref="h"
                    ),
                },
            )


# ---------------------------------------------------------------------------
# Migration seed data.
# ---------------------------------------------------------------------------


def test_prompt_versions_seeded_row() -> None:
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT prompt_id, version, template_hash, input_schema_ref, "
                    "output_schema_ref, status FROM prompt_versions WHERE prompt_id = :prompt_id"
                ),
                {"prompt_id": SEEDED_PROMPT_ID},
            )
            .mappings()
            .one()
        )
    assert row["version"] == 1
    assert row["status"] == "active"
    assert row["input_schema_ref"] == "attention.explain_item.input.v1"
    assert row["output_schema_ref"] == "attention.explain_item.output.v1"
    assert len(row["template_hash"]) == 64


def test_tool_definitions_seeded_rows() -> None:
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT name, version, scopes, status, definition_hash "
                    "FROM tool_definitions WHERE name = ANY(:names) ORDER BY name"
                ),
                {"names": [SEEDED_TOOL_ATTENTION, SEEDED_TOOL_KNOWLEDGE]},
            )
            .mappings()
            .all()
        )
    assert [row["name"] for row in rows] == [SEEDED_TOOL_ATTENTION, SEEDED_TOOL_KNOWLEDGE]
    for row in rows:
        assert row["version"] == 1
        assert row["status"] == "active"
        assert len(row["definition_hash"]) == 64
    assert list(rows[0]["scopes"]) == ["read:attention"]
    assert list(rows[1]["scopes"]) == ["read:knowledge"]


# ---------------------------------------------------------------------------
# prompts.py / tools.py functions.
# ---------------------------------------------------------------------------


def test_get_active_prompt_reads_the_seeded_row() -> None:
    with SessionFactory() as session:
        active = ai_prompts.get_active_prompt(session, SEEDED_PROMPT_ID)
        assert active is not None
        assert active.version == 1
        assert active.status == "active"
        assert ai_prompts.get_active_prompt(session, "nonexistent.prompt") is None


def test_get_active_tool_reads_the_seeded_rows() -> None:
    with SessionFactory() as session:
        active = ai_tools.get_active_tool(session, SEEDED_TOOL_ATTENTION)
        assert active is not None
        assert active.version == 1
        assert active.scopes == ("read:attention",)
        assert ai_tools.get_active_tool(session, "nonexistent.tool") is None


def test_activate_prompt_version_retires_outgoing_and_activates_incoming(
    cleanup_prompt_ids: list[str],
) -> None:
    prompt_id = f"test.activate.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    v1_id = _insert_prompt_row(prompt_id=prompt_id, version=1, status="active", template="v1")
    v2_id = _insert_prompt_row(prompt_id=prompt_id, version=2, status="draft", template="v2")

    with SessionFactory() as session:
        with session.begin():
            result = ai_prompts.activate_prompt_version(session, prompt_id, 2)
        assert isinstance(result, ai_prompts.PromptVersion)
        assert result.id == v2_id
        assert result.status == "active"
        assert result.template == "v2"  # content untouched by activation

    with engine.connect() as connection:
        v1_status = connection.execute(
            text("SELECT status FROM prompt_versions WHERE id = :id"), {"id": v1_id}
        ).scalar_one()
        v2_status = connection.execute(
            text("SELECT status FROM prompt_versions WHERE id = :id"), {"id": v2_id}
        ).scalar_one()
    assert v1_status == "retired"
    assert v2_status == "active"


def test_activate_prompt_version_unknown_version_returns_not_found(
    cleanup_prompt_ids: list[str],
) -> None:
    prompt_id = f"test.activate-missing.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")

    with SessionFactory() as session:
        with session.begin():
            result = ai_prompts.activate_prompt_version(session, prompt_id, 99)
        assert isinstance(result, ai_prompts.PromptVersionNotFound)
        assert result.version == 99


def test_activate_tool_version_retires_outgoing_and_activates_incoming(
    cleanup_tool_names: list[str],
) -> None:
    name = f"test.activate.{uuid4().hex}"
    cleanup_tool_names.append(name)
    v1_id = _insert_tool_row(name=name, version=1, status="active")
    v2_id = _insert_tool_row(name=name, version=2, status="draft")

    with SessionFactory() as session:
        with session.begin():
            result = ai_tools.activate_tool_version(session, name, 2)
        assert isinstance(result, ai_tools.ToolDefinition)
        assert result.id == v2_id
        assert result.status == "active"

    with engine.connect() as connection:
        v1_status = connection.execute(
            text("SELECT status FROM tool_definitions WHERE id = :id"), {"id": v1_id}
        ).scalar_one()
        v2_status = connection.execute(
            text("SELECT status FROM tool_definitions WHERE id = :id"), {"id": v2_id}
        ).scalar_one()
    assert v1_status == "retired"
    assert v2_status == "active"


def test_activate_tool_version_unknown_version_returns_not_found(
    cleanup_tool_names: list[str],
) -> None:
    name = f"test.activate-missing.{uuid4().hex}"
    cleanup_tool_names.append(name)
    _insert_tool_row(name=name, version=1, status="active")

    with SessionFactory() as session:
        with session.begin():
            result = ai_tools.activate_tool_version(session, name, 42)
        assert isinstance(result, ai_tools.ToolVersionNotFound)
        assert result.version == 42


def test_activate_prompt_version_reactivating_already_active_row_is_a_noop(
    cleanup_prompt_ids: list[str],
) -> None:
    """Activating a version that is already the active one must not raise
    and must not touch any other row (there is no "outgoing" row when the
    target already holds the active pointer)."""
    prompt_id = f"test.reactivate-noop.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    row_id = _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")

    with SessionFactory() as session:
        with session.begin():
            result = ai_prompts.activate_prompt_version(session, prompt_id, 1)
        assert isinstance(result, ai_prompts.PromptVersion)
        assert result.id == row_id
        assert result.status == "active"


# ---------------------------------------------------------------------------
# POST /api/v1/ai/policies/{prompt_id_or_tool_name}/activate
# ---------------------------------------------------------------------------


@pytest.fixture
def activation_test_context() -> Iterator[tuple[TestClient, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'AI Runtime Activation Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :created_at)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "created_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in ("event_outbox", "audit_events", "idempotency_records"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM sessions WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM users WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def test_activate_prompt_via_endpoint(
    activation_test_context: tuple[TestClient, UUID, str], cleanup_prompt_ids: list[str]
) -> None:
    client, _workspace_id, token = activation_test_context
    prompt_id = f"test.http-activate.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")
    _insert_prompt_row(prompt_id=prompt_id, version=2, status="draft")

    response = client.post(
        f"/api/v1/ai/policies/{prompt_id}/activate",
        json={"version": 2, "expected_active_version": 1},
        headers=_headers(token, key="activate-prompt-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "prompt"
    assert body["name"] == prompt_id
    assert body["active_version"] == 2
    assert body["status"] == "active"

    with SessionFactory() as session:
        active = ai_prompts.get_active_prompt(session, prompt_id)
        assert active is not None
        assert active.version == 2


def test_activate_tool_via_endpoint(
    activation_test_context: tuple[TestClient, UUID, str], cleanup_tool_names: list[str]
) -> None:
    client, _workspace_id, token = activation_test_context
    name = f"test.http-activate.{uuid4().hex}"
    cleanup_tool_names.append(name)
    _insert_tool_row(name=name, version=1, status="active")
    _insert_tool_row(name=name, version=2, status="draft")

    response = client.post(
        f"/api/v1/ai/policies/{name}/activate",
        json={"version": 2},
        headers=_headers(token, key="activate-tool-1"),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "tool"
    assert body["name"] == name
    assert body["active_version"] == 2
    assert body["status"] == "active"


def test_activate_unknown_name_is_404(
    activation_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _workspace_id, token = activation_test_context
    response = client.post(
        "/api/v1/ai/policies/nonexistent.family/activate",
        json={"version": 1},
        headers=_headers(token, key="activate-unknown"),
    )
    assert response.status_code == 404


def test_activate_unknown_version_of_known_family_is_404(
    activation_test_context: tuple[TestClient, UUID, str], cleanup_prompt_ids: list[str]
) -> None:
    client, _workspace_id, token = activation_test_context
    prompt_id = f"test.http-missing-version.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")

    response = client.post(
        f"/api/v1/ai/policies/{prompt_id}/activate",
        json={"version": 99},
        headers=_headers(token, key="activate-missing-version"),
    )
    assert response.status_code == 404


def test_activate_requires_csrf(
    activation_test_context: tuple[TestClient, UUID, str], cleanup_prompt_ids: list[str]
) -> None:
    client, _workspace_id, _token = activation_test_context
    prompt_id = f"test.http-no-csrf.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")
    _insert_prompt_row(prompt_id=prompt_id, version=2, status="draft")

    response = client.post(
        f"/api/v1/ai/policies/{prompt_id}/activate",
        json={"version": 2},
        headers={"Idempotency-Key": "no-csrf"},
    )
    assert response.status_code == 403


def test_activate_requires_authentication(
    activation_test_context: tuple[TestClient, UUID, str],
) -> None:
    client, _workspace_id, token = activation_test_context
    client.cookies.clear()
    response = client.post(
        "/api/v1/ai/policies/attention.get_item/activate",
        json={"version": 1},
        headers=_headers(token, key="no-auth"),
    )
    assert response.status_code == 401


def test_activate_version_conflict_on_stale_expected_active_version(
    activation_test_context: tuple[TestClient, UUID, str], cleanup_prompt_ids: list[str]
) -> None:
    client, _workspace_id, token = activation_test_context
    prompt_id = f"test.http-version-conflict.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")
    _insert_prompt_row(prompt_id=prompt_id, version=2, status="draft")

    response = client.post(
        f"/api/v1/ai/policies/{prompt_id}/activate",
        json={"version": 2, "expected_active_version": 999},
        headers=_headers(token, key="version-conflict"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"


def test_activate_idempotency_replay_returns_identical_response(
    activation_test_context: tuple[TestClient, UUID, str], cleanup_prompt_ids: list[str]
) -> None:
    client, _workspace_id, token = activation_test_context
    prompt_id = f"test.http-idempotent.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")
    _insert_prompt_row(prompt_id=prompt_id, version=2, status="draft")

    headers = _headers(token, key="replay-key")
    first = client.post(
        f"/api/v1/ai/policies/{prompt_id}/activate", json={"version": 2}, headers=headers
    )
    second = client.post(
        f"/api/v1/ai/policies/{prompt_id}/activate", json={"version": 2}, headers=headers
    )
    assert first.status_code == 200
    assert second.status_code == 200
    ignored = {"request_id", "correlation_id"}
    first_body = {k: v for k, v in first.json().items() if k not in ignored}
    second_body = {k: v for k, v in second.json().items() if k not in ignored}
    assert first_body == second_body

    # A replay must not have retired-then-reactivated a second time -- only
    # one audit event exists for this activation.
    with engine.connect() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE event_type = 'ai_prompt.activated' "
                "AND metadata->>'name' = :name"
            ),
            {"name": prompt_id},
        ).scalar_one()
    assert count == 1


def test_activate_writes_audit_event(
    activation_test_context: tuple[TestClient, UUID, str], cleanup_prompt_ids: list[str]
) -> None:
    client, workspace_id, token = activation_test_context
    prompt_id = f"test.http-audit.{uuid4().hex}.v1"
    cleanup_prompt_ids.append(prompt_id)
    _insert_prompt_row(prompt_id=prompt_id, version=1, status="active")
    _insert_prompt_row(prompt_id=prompt_id, version=2, status="draft")

    response = client.post(
        f"/api/v1/ai/policies/{prompt_id}/activate",
        json={"version": 2},
        headers=_headers(token, key="audit-check"),
    )
    assert response.status_code == 200

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT event_type, aggregate_type, aggregate_version, workspace_id "
                    "FROM audit_events WHERE workspace_id = :workspace_id "
                    "AND event_type = 'ai_prompt.activated'"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .one()
        )
    assert row["aggregate_type"] == "prompt_version"
    assert row["aggregate_version"] == 2
