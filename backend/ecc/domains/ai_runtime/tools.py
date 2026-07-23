"""Tool contract reads/activation (`tool_definitions`).

`docs/phases/phase-004/DATA-MODEL.md`: `tool_definitions` is the platform's
allowlisted tool catalog -- **not** workspace-scoped user data (see
migration `0029_phase4_prompt_tool_versions.py`'s module docstring for why),
the same kind of global platform catalog as Task 1's `model_definitions`.
Two rows are seeded by that migration: `attention.get_item`
(`scopes=['read:attention']`) and `knowledge.get_entity`
(`scopes=['read:knowledge']`) (design doc Decision 6).

Implements the design doc's Decision 3 mechanism for tools specifically:
`definition_hash` is `sha256` over the canonical UTF-8 sorted-object-keys
bytes of `{input_schema, output_schema, scopes, handler_ref}`, and once a
row's `status` leaves `draft` that envelope is immutable -- enforced by the
`trg_tool_definitions_immutability` PostgreSQL trigger the migration
creates, not only by this module declining to expose an edit path. Editing
a tool's contract always means inserting a new row with `version =
previous + 1`; `activate_tool_version` never edits an existing row's
schema/scope columns, only `status`/`updated_at` on the outgoing and
incoming rows (Decision 3: "Activating a new version ... does not
retroactively change which version any already-completed `ai_run`
recorded").
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

ToolStatus = Literal["draft", "active", "retired"]

_TOOL_FIELDS = """
    id, name, version, scopes, input_schema, output_schema, handler_ref,
    definition_hash, status
"""


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A single versioned, immutable-once-activated tool contract row."""

    id: UUID
    name: str
    version: int
    scopes: tuple[str, ...]
    input_schema: dict
    output_schema: dict
    handler_ref: str
    definition_hash: str
    status: ToolStatus


@dataclass(frozen=True, slots=True)
class ToolVersionNotFound:
    """No `tool_definitions` row exists for the given `(name, version)`
    pair -- distinct from "not currently active" (`get_active_tool`
    returning `None`), so `activate_tool_version`'s caller can tell a
    genuinely unknown version apart from an existing-but-inactive one.
    """

    name: str
    version: int


def compute_definition_hash(
    *,
    input_schema: dict,
    output_schema: dict,
    scopes: Sequence[str],
    handler_ref: str,
) -> str:
    """`sha256` over the canonical (UTF-8, sorted-object-keys) JSON bytes of
    `{input_schema, output_schema, scopes, handler_ref}` -- design doc
    Decision 3's hashing scheme for tools. Mirrored (not imported, per this
    codebase's migration-self-containment convention) by migration
    `0029_phase4_prompt_tool_versions.py`'s `_canonical_hash` for the seed
    rows; keep both in sync if this scheme ever changes.
    """
    material = {
        "input_schema": input_schema,
        "output_schema": output_schema,
        "scopes": list(scopes),
        "handler_ref": handler_ref,
    }
    canonical = dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_tool(row: dict) -> ToolDefinition:
    return ToolDefinition(
        id=row["id"],
        name=row["name"],
        version=row["version"],
        scopes=tuple(row["scopes"]),
        input_schema=row["input_schema"],
        output_schema=row["output_schema"],
        handler_ref=row["handler_ref"],
        definition_hash=row["definition_hash"],
        status=row["status"],
    )


def get_active_tool(session: Session, name: str) -> ToolDefinition | None:
    """The currently active version of a tool contract, or `None` if the
    tool has no active version (unregistered name, or every version is
    `draft`/`retired`). Never raises on a missing row, matching Task 1's
    `registry.py:get_model` convention.
    """
    row = (
        session.execute(
            text(
                f"SELECT {_TOOL_FIELDS} FROM tool_definitions "
                "WHERE name = :name AND status = 'active'"
            ),
            {"name": name},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_tool(dict(row)) if row is not None else None


def get_tool_version(session: Session, name: str, version: int) -> ToolDefinition | None:
    """A specific `(name, version)` row regardless of status -- used by
    `activate_tool_version` to find the row it is about to activate.
    """
    row = (
        session.execute(
            text(
                f"SELECT {_TOOL_FIELDS} FROM tool_definitions "
                "WHERE name = :name AND version = :version"
            ),
            {"name": name, "version": version},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_tool(dict(row)) if row is not None else None


def tool_family_exists(session: Session, name: str) -> bool:
    """Whether any row (any status) exists for this tool `name` -- used by
    `POST /ai/policies/{prompt_id_or_tool_name}/activate` (`prompts.py`) to
    decide whether the path parameter names a tool or a prompt before
    dispatching to the matching activation function.
    """
    return (
        session.execute(
            text("SELECT 1 FROM tool_definitions WHERE name = :name LIMIT 1"), {"name": name}
        ).first()
        is not None
    )


def activate_tool_version(
    session: Session, name: str, version: int
) -> ToolDefinition | ToolVersionNotFound:
    """Explicit administrative activation (design doc Decision 3): retires
    whichever version is currently `active` for `name` (if any and if it
    is not already the target row) and marks the target row `active`, each
    via an `UPDATE` touching only `status`/`updated_at` -- the two columns
    `trg_tool_definitions_immutability` never guards, confirmed by reading
    the trigger this module's migration creates (it only rejects changes to
    `input_schema`/`output_schema`/`scopes`/`handler_ref`/`definition_hash`
    once `OLD.status <> 'draft'`). Never edits `template`-equivalent
    content columns of any row. Caller (the HTTP endpoint in `prompts.py`)
    is responsible for the surrounding transaction, idempotency key and
    audit event -- this function is the pure data-layer mutation only.

    `FOR UPDATE` locks both the target row and the current active row (if
    distinct) for the rest of the caller's transaction, closing the same
    lost-update race `attention/capacity.py`'s `_current_profile(for_
    update=True)` documents: two concurrent activations racing to flip the
    same tool's active pointer.
    """
    target_row = (
        session.execute(
            text(
                f"SELECT {_TOOL_FIELDS} FROM tool_definitions "
                "WHERE name = :name AND version = :version FOR UPDATE"
            ),
            {"name": name, "version": version},
        )
        .mappings()
        .one_or_none()
    )
    if target_row is None:
        return ToolVersionNotFound(name=name, version=version)

    now = datetime.now(UTC)
    if target_row["status"] != "active":
        current_active = (
            session.execute(
                text(
                    "SELECT id FROM tool_definitions "
                    "WHERE name = :name AND status = 'active' FOR UPDATE"
                ),
                {"name": name},
            )
            .mappings()
            .one_or_none()
        )
        if current_active is not None and current_active["id"] != target_row["id"]:
            session.execute(
                text(
                    "UPDATE tool_definitions SET status = 'retired', updated_at = :now "
                    "WHERE id = :id"
                ),
                {"id": current_active["id"], "now": now},
            )
        session.execute(
            text("UPDATE tool_definitions SET status = 'active', updated_at = :now WHERE id = :id"),
            {"id": target_row["id"], "now": now},
        )

    final_row = (
        session.execute(
            text(f"SELECT {_TOOL_FIELDS} FROM tool_definitions WHERE id = :id"),
            {"id": target_row["id"]},
        )
        .mappings()
        .one()
    )
    return _row_to_tool(dict(final_row))
