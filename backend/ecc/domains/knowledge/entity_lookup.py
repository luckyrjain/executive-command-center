"""Shared pkos_nodes entity-lookup helpers.

Consolidates lookup queries against ``pkos_nodes`` that were previously
copy-pasted verbatim across claims.py, relationships.py, resolution.py,
evidence.py, entity_operations.py, entities.py, and entities_mutations.py.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.knowledge.embeddings import queue_embedding
from ecc.domains.knowledge.retrieval import queue_retrieval_document

ENTITY_FIELDS = """
id, node_type, canonical_name, attributes, status, confidence,
version, created_at, updated_at
"""


def get_entity_row(
    session: Session,
    auth: AuthContext,
    entity_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT {ENTITY_FIELDS}
                FROM pkos_nodes
                WHERE workspace_id = :workspace_id AND id = :entity_id
                {suffix}
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def entity_version(session: Session, auth: AuthContext, entity_id: UUID) -> int | None:
    row = session.execute(
        text(
            "SELECT version FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = :entity_id"
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).one_or_none()
    return row[0] if row is not None else None


def entity_status(session: Session, auth: AuthContext, entity_id: UUID) -> str | None:
    row = session.execute(
        text(
            "SELECT status FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = :entity_id"
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).one_or_none()
    return row[0] if row is not None else None


def entity_retrieval_fields(
    session: Session, auth: AuthContext, entity_id: UUID
) -> tuple[str, str, str | None, int] | None:
    row = session.execute(
        text(
            """
            SELECT node_type, canonical_name, attributes, version FROM pkos_nodes
            WHERE workspace_id = :workspace_id AND id = :entity_id
            """
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).one_or_none()
    if row is None:
        return None
    node_type, canonical_name, attributes, version = row
    summary = (attributes or {}).get("summary")
    return node_type, canonical_name, summary, version


def refresh_projections(
    session: Session, auth: AuthContext, entity_id: UUID, now: datetime
) -> None:
    """Re-derive an entity's retrieval_document/embedding after a write that
    doesn't go through claims.py's/relationships.py's own mutation
    endpoints (e.g. entity_operations.py's split, which moves data between
    entities via direct UPDATE)."""
    fields = entity_retrieval_fields(session, auth, entity_id)
    if fields is not None:
        kind, canonical_name, summary, version = fields
        queue_retrieval_document(
            session, auth.workspace_id, entity_id, kind, canonical_name, summary, version, now
        )
        queue_embedding(session, auth.workspace_id, entity_id, now)
