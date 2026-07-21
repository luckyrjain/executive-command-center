from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge-entity-operations"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_OPERATION_FIELDS = """
id, operation_type, status, inputs_json, outputs_json, actor_id, reason,
reverses_operation_id, created_at
"""


class EntityMergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: UUID
    target_entity_id: UUID
    expected_target_version: int = Field(ge=1)
    expected_source_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)


class EntityOperationResponse(BaseModel):
    id: UUID
    operation_type: str
    status: str
    source_entity_id: UUID | None
    target_entity_id: UUID | None
    actor_id: UUID
    reason: str
    reverses_operation_id: UUID | None
    created_at: datetime


class EntityOperationReverseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


def _project(row: dict[str, Any]) -> EntityOperationResponse:
    inputs = row["inputs_json"] if isinstance(row["inputs_json"], dict) else {}
    outputs = row["outputs_json"] if isinstance(row["outputs_json"], dict) else {}
    source_id = outputs.get("source_entity_id") or inputs.get("source_entity_id")
    target_id = outputs.get("target_entity_id") or inputs.get("target_entity_id")
    return EntityOperationResponse(
        id=row["id"],
        operation_type=row["operation_type"],
        status=row["status"],
        source_entity_id=UUID(source_id) if source_id else None,
        target_entity_id=UUID(target_id) if target_id else None,
        actor_id=row["actor_id"],
        reason=row["reason"],
        reverses_operation_id=row["reverses_operation_id"],
        created_at=row["created_at"],
    )


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> EntityOperationResponse | None:
    row = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body
                FROM idempotency_records
                WHERE workspace_id = :workspace_id
                  AND actor_id = :actor_id
                  AND key = :key
                  AND expires_at > :now
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": key,
                "now": datetime.now(UTC),
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        record_idempotency_conflict("knowledge_entity_operations")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return EntityOperationResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: EntityOperationResponse,
    now: datetime,
    status_code: int,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, :response_status,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": request_hash,
            "response_status": status_code,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _lock_entity(session: Session, auth: AuthContext, entity_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                """
                SELECT id, node_type, canonical_name, status, version
                FROM pkos_nodes
                WHERE workspace_id = :workspace_id AND id = :entity_id
                FOR UPDATE
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    operation_id: UUID,
    now: datetime,
) -> None:
    request_id, correlation_id = _request_ids(request)
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, 'entity_operation', :aggregate_id,
                    1, :actor_id, :request_id, :correlation_id,
                    ARRAY['status'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": operation_id,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "occurred_at": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_id, workspace_id, event_type, event_version,
                    correlation_id, payload, occurred_at, attempt_count
                ) VALUES (
                    :event_id, :workspace_id, :event_type, 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps({"operation_id": str(operation_id)}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("knowledge_entity_operations")
        raise
    queue_lifecycle_event(session, "entity_operation", event_type, "allowed")


def _rehome_aliases(session: Session, auth: AuthContext, source_id: UUID, target_id: UUID) -> int:
    """Move entity_aliases rows from source to target. entity_aliases carries
    a workspace-wide unique constraint on (alias_type, normalized_value)
    (migration 0011), so an alias the target already holds cannot also be
    rehomed from source -- DATA-MODEL.md's "resolves duplicates
    deterministically" is satisfied by simply leaving that specific alias
    row attached to the now-redirected source rather than failing the
    whole merge; the alias value itself is still discoverable (it already
    resolves to target through its own row)."""
    rehomed = session.execute(
        text(
            """
            UPDATE entity_aliases AS a
            SET entity_id = :target_id
            WHERE a.workspace_id = :workspace_id AND a.entity_id = :source_id
              AND NOT EXISTS (
                  SELECT 1 FROM entity_aliases AS existing
                  WHERE existing.workspace_id = a.workspace_id
                    AND existing.alias_type = a.alias_type
                    AND existing.normalized_value = a.normalized_value
                    AND existing.entity_id = :target_id
              )
            RETURNING a.id
            """
        ),
        {"workspace_id": auth.workspace_id, "source_id": source_id, "target_id": target_id},
    )
    return len(rehomed.all())


def _rehome_edges(session: Session, auth: AuthContext, source_id: UUID, target_id: UUID) -> int:
    """Move active pkos_edges rows referencing source to reference target
    instead. An edge that would exactly duplicate an already-active
    target edge (same edge_type, same other-side node) after rehoming is
    invalidated instead of rehomed, per DATA-MODEL.md's "resolves
    duplicates deterministically" -- the target's original edge is kept,
    the source's redundant one is marked invalidated rather than deleted
    (no authoritative record is ever destroyed)."""
    rehomed = 0
    rows = session.execute(
        text(
            """
            SELECT id, source_node_id, target_node_id, edge_type
            FROM pkos_edges
            WHERE workspace_id = :workspace_id AND status = 'active'
              AND (source_node_id = :source_id OR target_node_id = :source_id)
            """
        ),
        {"workspace_id": auth.workspace_id, "source_id": source_id},
    ).all()
    for edge_id, edge_source, edge_target, edge_type in rows:
        new_source = target_id if edge_source == source_id else edge_source
        new_target = target_id if edge_target == source_id else edge_target
        if new_source == new_target:
            # Would become a self-relationship after redirect -- invalidate
            # rather than create a degenerate edge.
            session.execute(
                text(
                    "UPDATE pkos_edges SET status = 'invalidated' "
                    "WHERE workspace_id = :workspace_id AND id = :edge_id"
                ),
                {"workspace_id": auth.workspace_id, "edge_id": edge_id},
            )
            continue
        duplicate = session.execute(
            text(
                """
                SELECT 1 FROM pkos_edges
                WHERE workspace_id = :workspace_id AND status = 'active' AND id != :edge_id
                  AND source_node_id = :new_source AND target_node_id = :new_target
                  AND edge_type = :edge_type
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "edge_id": edge_id,
                "new_source": new_source,
                "new_target": new_target,
                "edge_type": edge_type,
            },
        ).one_or_none()
        if duplicate is not None:
            session.execute(
                text(
                    "UPDATE pkos_edges SET status = 'invalidated' "
                    "WHERE workspace_id = :workspace_id AND id = :edge_id"
                ),
                {"workspace_id": auth.workspace_id, "edge_id": edge_id},
            )
            continue
        session.execute(
            text(
                """
                UPDATE pkos_edges
                SET source_node_id = :new_source, target_node_id = :new_target
                WHERE workspace_id = :workspace_id AND id = :edge_id
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "edge_id": edge_id,
                "new_source": new_source,
                "new_target": new_target,
            },
        )
        rehomed += 1
    return rehomed


@router.post("/entities/merge", response_model=EntityOperationResponse, status_code=201)
def merge_entities(
    payload: EntityMergeRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EntityOperationResponse:
    request_hash = _request_hash(payload, "merge")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        candidate = (
            session.execute(
                text(
                    """
                    SELECT id, left_entity_id, right_entity_id, status
                    FROM resolution_candidates
                    WHERE workspace_id = :workspace_id AND id = :candidate_id
                    FOR UPDATE
                    """
                ),
                {"workspace_id": auth.workspace_id, "candidate_id": payload.candidate_id},
            )
            .mappings()
            .one_or_none()
        )
        if candidate is None:
            raise HTTPException(status_code=404, detail="CANDIDATE_NOT_FOUND")
        # API-SCHEMAS.md's mutation rules: "Resolution confirmation is a
        # human-confirmed identity operation, not a generic update" -- a
        # merge may only originate from a candidate a human has already
        # confirmed, never directly from an open or rejected one.
        if candidate["status"] != "confirmed":
            raise HTTPException(status_code=409, detail="CANDIDATE_NOT_CONFIRMED")
        if payload.target_entity_id not in (
            candidate["left_entity_id"],
            candidate["right_entity_id"],
        ):
            raise HTTPException(status_code=422, detail="TARGET_NOT_IN_CANDIDATE_PAIR")
        target_id = payload.target_entity_id
        source_id = (
            candidate["right_entity_id"]
            if target_id == candidate["left_entity_id"]
            else candidate["left_entity_id"]
        )

        # Lock both entities in a fixed (sorted) order regardless of which
        # is target/source, so two concurrent merges touching an
        # overlapping pair can never deadlock against each other.
        first_id, second_id = sorted((target_id, source_id), key=str)
        locked = {first_id: _lock_entity(session, auth, first_id)}
        locked[second_id] = _lock_entity(session, auth, second_id)
        target = locked[target_id]
        source = locked[source_id]
        if target is None or source is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        if target["status"] != "active" or source["status"] != "active":
            raise HTTPException(status_code=409, detail="ENTITY_NOT_ACTIVE")
        if target["version"] != payload.expected_target_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        if source["version"] != payload.expected_source_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")

        session.execute(
            text(
                """
                UPDATE pkos_nodes
                SET status = 'redirected', updated_at = :now, version = version + 1
                WHERE workspace_id = :workspace_id AND id = :source_id
                """
            ),
            {"workspace_id": auth.workspace_id, "source_id": source_id, "now": now},
        )
        rehomed_aliases = _rehome_aliases(session, auth, source_id, target_id)
        rehomed_edges = _rehome_edges(session, auth, source_id, target_id)

        operation_id = uuid4()
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO entity_operations (
                        id, workspace_id, operation_type, status, inputs_json,
                        outputs_json, actor_id, reason, created_at
                    ) VALUES (
                        :id, :workspace_id, 'merge', 'active', CAST(:inputs_json AS jsonb),
                        CAST(:outputs_json AS jsonb), :actor_id, :reason, :now
                    )
                    RETURNING {_OPERATION_FIELDS}
                    """
                ),
                {
                    "id": operation_id,
                    "workspace_id": auth.workspace_id,
                    "inputs_json": dumps(
                        {
                            "candidate_id": str(payload.candidate_id),
                            "source_entity_id": str(source_id),
                            "target_entity_id": str(target_id),
                            "source_version": payload.expected_source_version,
                            "target_version": payload.expected_target_version,
                        }
                    ),
                    "outputs_json": dumps(
                        {
                            "source_entity_id": str(source_id),
                            "target_entity_id": str(target_id),
                            "redirected_alias_count": rehomed_aliases,
                            "redirected_edge_count": rehomed_edges,
                        }
                    ),
                    "actor_id": auth.user_id,
                    "reason": payload.reason,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _project(dict(row))
        _write_side_effects(session, auth, request, "entity_operation.merged", operation_id, now)
        queue_timeline_entry(
            session,
            auth.workspace_id,
            target_id,
            "entity_operation.merged",
            f"merged {source_id} into {target_id}",
            now,
        )
        queue_timeline_entry(
            session,
            auth.workspace_id,
            source_id,
            "entity_operation.merged",
            f"redirected to {target_id}",
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, now, 201)
        return response


def _has_post_merge_dependent_activity(
    session: Session, auth: AuthContext, target_id: UUID, merge_created_at: datetime
) -> bool:
    """DATA-MODEL.md: reversal "validates that later operations do not make
    reversal unsafe" -- e.g. a claim recorded against the target entity
    after the merge, with no clear attribution back to which of the
    now-separate identities it actually describes. Claims and entity
    mutations recorded against the target both write audit_events with
    aggregate_type='knowledge_entity' and aggregate_id=target_id (see
    claims.py/entities_mutations.py's _write_side_effects), so any such
    row occurring after the merge is exactly that signal."""
    row = session.execute(
        text(
            """
            SELECT 1 FROM audit_events
            WHERE workspace_id = :workspace_id AND aggregate_type = 'knowledge_entity'
              AND aggregate_id = :target_id AND occurred_at > :merge_created_at
            LIMIT 1
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "target_id": target_id,
            "merge_created_at": merge_created_at,
        },
    ).one_or_none()
    return row is not None


@router.post(
    "/entity-operations/{operation_id}/reverse",
    response_model=EntityOperationResponse,
    status_code=201,
)
def reverse_operation(
    operation_id: UUID,
    payload: EntityOperationReverseRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EntityOperationResponse:
    request_hash = _request_hash(payload, f"reverse:{operation_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        merge_op = (
            session.execute(
                text(
                    f"""
                    SELECT {_OPERATION_FIELDS}
                    FROM entity_operations
                    WHERE workspace_id = :workspace_id AND id = :operation_id
                    FOR UPDATE
                    """
                ),
                {"workspace_id": auth.workspace_id, "operation_id": operation_id},
            )
            .mappings()
            .one_or_none()
        )
        if merge_op is None:
            raise HTTPException(status_code=404, detail="OPERATION_NOT_FOUND")
        if merge_op["operation_type"] != "merge":
            raise HTTPException(status_code=422, detail="NOT_A_MERGE_OPERATION")
        if merge_op["status"] != "active":
            raise HTTPException(status_code=409, detail="OPERATION_ALREADY_REVERSED")

        inputs = merge_op["inputs_json"] if isinstance(merge_op["inputs_json"], dict) else {}
        source_id = UUID(inputs["source_entity_id"])
        target_id = UUID(inputs["target_entity_id"])

        if _has_post_merge_dependent_activity(session, auth, target_id, merge_op["created_at"]):
            raise HTTPException(status_code=422, detail="UNSAFE_REVERSAL")

        # Lock both entities in the same fixed (sorted) order merge_entities
        # uses, so a reversal can never deadlock against a concurrent merge.
        first_id, second_id = sorted((target_id, source_id), key=str)
        locked = {first_id: _lock_entity(session, auth, first_id)}
        locked[second_id] = _lock_entity(session, auth, second_id)
        source = locked[source_id]
        if source is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        if source["status"] != "redirected":
            raise HTTPException(status_code=409, detail="SOURCE_NOT_REDIRECTED")

        session.execute(
            text(
                """
                UPDATE pkos_nodes
                SET status = 'active', updated_at = :now, version = version + 1
                WHERE workspace_id = :workspace_id AND id = :source_id
                """
            ),
            {"workspace_id": auth.workspace_id, "source_id": source_id, "now": now},
        )
        session.execute(
            text(
                "UPDATE entity_operations SET status = 'reversed' "
                "WHERE workspace_id = :workspace_id AND id = :operation_id"
            ),
            {"workspace_id": auth.workspace_id, "operation_id": operation_id},
        )

        reverse_id = uuid4()
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO entity_operations (
                        id, workspace_id, operation_type, status, inputs_json,
                        outputs_json, actor_id, reason, reverses_operation_id, created_at
                    ) VALUES (
                        :id, :workspace_id, 'reverse', 'active', CAST(:inputs_json AS jsonb),
                        CAST(:outputs_json AS jsonb), :actor_id, :reason, :reverses_id, :now
                    )
                    RETURNING {_OPERATION_FIELDS}
                    """
                ),
                {
                    "id": reverse_id,
                    "workspace_id": auth.workspace_id,
                    "inputs_json": dumps({"operation_id": str(operation_id)}),
                    "outputs_json": dumps(
                        {"source_entity_id": str(source_id), "target_entity_id": str(target_id)}
                    ),
                    "actor_id": auth.user_id,
                    "reason": payload.reason,
                    "reverses_id": operation_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _project(dict(row))
        _write_side_effects(session, auth, request, "entity_operation.reversed", reverse_id, now)
        queue_timeline_entry(
            session,
            auth.workspace_id,
            source_id,
            "entity_operation.reversed",
            f"restored from redirect to {target_id}",
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, now, 201)
        return response
