from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

# Bumped whenever score_candidate's factors or weighting change -- stored on
# every resolution_candidates row per ENTITY-RESOLUTION-CONTRACT.md's
# "every candidate stores ... resolver version" requirement, so historical
# candidates remain interpretable after the scorer itself changes.
RESOLVER_VERSION = "phase2-resolution-v1"

CandidateStatus = Literal["open", "confirmed", "rejected", "expired"]


@dataclass(frozen=True)
class ResolutionThresholds:
    """Typed configuration, not inline literals, per
    ENTITY-RESOLUTION-CONTRACT.md: "Threshold values are typed configuration
    and require benchmark evidence before change."."""

    high_confidence: float = 0.75
    weight_name: float = 0.40
    weight_alias: float = 0.25
    weight_neighbor: float = 0.20
    weight_temporal: float = 0.15


DEFAULT_THRESHOLDS = ResolutionThresholds()


@dataclass(frozen=True)
class CandidateEntity:
    """Only the non-sensitive attributes score_candidate is allowed to see,
    per the contract's "Protected or sensitive attributes must not be used."
    Callers project pkos_nodes/entity_aliases/pkos_edges rows down to this
    shape before scoring."""

    id: UUID
    kind: str
    canonical_name: str
    aliases: frozenset[str] = field(default_factory=frozenset)
    neighbor_ids: frozenset[UUID] = field(default_factory=frozenset)
    active_from: datetime | None = None
    active_to: datetime | None = None


@dataclass(frozen=True)
class ScoreFactors:
    name_similarity: float
    alias_overlap: float
    neighbor_overlap: float
    temporal_compatibility: float


@dataclass(frozen=True)
class ScoreResult:
    score: float
    factors: ScoreFactors
    resolver_version: str


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _trigrams(value: str) -> frozenset[str]:
    padded = f"  {value}  "
    if len(padded) < 3:
        return frozenset()
    return frozenset(padded[i : i + 3] for i in range(len(padded) - 2))


def _jaccard(left: frozenset[Any], right: frozenset[Any]) -> float:
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _name_similarity(left: str, right: str) -> float:
    left_norm = _normalize(left)
    right_norm = _normalize(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    return _jaccard(_trigrams(left_norm), _trigrams(right_norm))


def _alias_overlap(left: frozenset[str], right: frozenset[str]) -> float:
    return _jaccard(
        frozenset(_normalize(alias) for alias in left),
        frozenset(_normalize(alias) for alias in right),
    )


def _neighbor_overlap(left: frozenset[UUID], right: frozenset[UUID]) -> float:
    return _jaccard(left, right)


def _temporal_compatibility(left: CandidateEntity, right: CandidateEntity) -> float:
    """Two open intervals (no recorded active_from/active_to on either side)
    default to compatible: absence of temporal data is not evidence of
    incompatibility. Otherwise 1.0 if the two [active_from, active_to)
    intervals overlap at all, 0.0 if they are disjoint."""
    left_start = left.active_from or datetime.min.replace(tzinfo=UTC)
    left_end = left.active_to or datetime.max.replace(tzinfo=UTC)
    right_start = right.active_from or datetime.min.replace(tzinfo=UTC)
    right_end = right.active_to or datetime.max.replace(tzinfo=UTC)
    overlaps = left_start <= right_end and right_start <= left_end
    return 1.0 if overlaps else 0.0


def score_candidate(
    left: CandidateEntity,
    right: CandidateEntity,
    thresholds: ResolutionThresholds = DEFAULT_THRESHOLDS,
) -> ScoreResult:
    """Pure, no-I/O candidate scorer (ENTITY-RESOLUTION-CONTRACT.md's
    "Candidate scoring" section) -- unit-testable in isolation from the
    database. Kind mismatch forces score 0: this only proposes candidates
    within a single entity kind (a person is never a fuzzy match for a
    project), matching the contract's "compatible entity kind" framing for
    exact-alias matches, applied here to fuzzy candidates too."""
    if left.kind != right.kind:
        factors = ScoreFactors(0.0, 0.0, 0.0, 0.0)
        return ScoreResult(score=0.0, factors=factors, resolver_version=RESOLVER_VERSION)
    factors = ScoreFactors(
        name_similarity=_name_similarity(left.canonical_name, right.canonical_name),
        alias_overlap=_alias_overlap(left.aliases, right.aliases),
        neighbor_overlap=_neighbor_overlap(left.neighbor_ids, right.neighbor_ids),
        temporal_compatibility=_temporal_compatibility(left, right),
    )
    score = (
        thresholds.weight_name * factors.name_similarity
        + thresholds.weight_alias * factors.alias_overlap
        + thresholds.weight_neighbor * factors.neighbor_overlap
        + thresholds.weight_temporal * factors.temporal_compatibility
    )
    return ScoreResult(
        score=round(min(max(score, 0.0), 1.0), 4),
        factors=factors,
        resolver_version=RESOLVER_VERSION,
    )


router = APIRouter(prefix="/api/v1/knowledge/resolution", tags=["knowledge-resolution"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_CANDIDATE_FIELDS = """
id, left_entity_id, right_entity_id, score, factors_json, resolver_version,
status, created_at, resolved_at, resolved_by, reason
"""


class ResolutionCandidateCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left_entity_id: UUID
    right_entity_id: UUID


class ResolutionCandidateResponse(BaseModel):
    id: UUID
    left_entity_id: UUID
    right_entity_id: UUID
    score: float
    factors: dict[str, float]
    resolver_version: str
    status: CandidateStatus
    created_at: datetime
    resolved_at: datetime | None
    resolved_by: UUID | None
    reason: str | None


class ResolutionCandidateListResponse(BaseModel):
    items: list[ResolutionCandidateResponse]
    next_cursor: str | None = None


class ResolutionCandidateResult(BaseModel):
    """create_candidate's response shape. ENTITY-RESOLUTION-CONTRACT.md's
    match hierarchy levels 1-4 (user-confirmed mapping, trusted external
    identifier, verified workspace identifier, exact alias + compatible
    kind) "may propose but cannot auto-confirm a merge" -- but they are all
    deterministic enough that they must never create a reviewable
    resolution_candidates row (level 5's fuzzy scoring is the only level
    that does). When `deterministic` is true, `candidate` is always None:
    no row was written, there is nothing to review."""

    deterministic: bool
    candidate: ResolutionCandidateResponse | None = None


class ResolutionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


def _project(row: dict[str, Any]) -> ResolutionCandidateResponse:
    factors_raw = row["factors_json"]
    factors = loads(factors_raw) if isinstance(factors_raw, str) else dict(factors_raw)
    return ResolutionCandidateResponse(
        id=row["id"],
        left_entity_id=row["left_entity_id"],
        right_entity_id=row["right_entity_id"],
        score=float(row["score"]),
        factors=factors,
        resolver_version=row["resolver_version"],
        status=row["status"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        reason=row["reason"],
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
) -> ResolutionCandidateResponse | None:
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
        record_idempotency_conflict("knowledge_resolution")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return ResolutionCandidateResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: ResolutionCandidateResponse,
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


def _load_cached_result(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> ResolutionCandidateResult | None:
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
        record_idempotency_conflict("knowledge_resolution")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return ResolutionCandidateResult.model_validate(row["response_body"])


def _store_cached_result(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: ResolutionCandidateResult,
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


def _entity_row(session: Session, auth: AuthContext, entity_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                """
                SELECT id, node_type, canonical_name, version
                FROM pkos_nodes
                WHERE workspace_id = :workspace_id AND id = :entity_id
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _candidate_entity(session: Session, auth: AuthContext, entity_id: UUID) -> CandidateEntity:
    node = _entity_row(session, auth, entity_id)
    if node is None:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
    aliases = session.execute(
        text(
            "SELECT normalized_value FROM entity_aliases "
            "WHERE workspace_id = :workspace_id AND entity_id = :entity_id"
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).all()
    neighbors = session.execute(
        text(
            """
            SELECT target_node_id AS neighbor_id FROM pkos_edges
            WHERE workspace_id = :workspace_id AND source_node_id = :entity_id
            UNION
            SELECT source_node_id AS neighbor_id FROM pkos_edges
            WHERE workspace_id = :workspace_id AND target_node_id = :entity_id
            """
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).all()
    return CandidateEntity(
        id=node["id"],
        kind=node["node_type"],
        canonical_name=node["canonical_name"],
        aliases=frozenset(row[0] for row in aliases),
        neighbor_ids=frozenset(row[0] for row in neighbors),
    )


def _deterministic_alias_match(left: CandidateEntity, right: CandidateEntity) -> bool:
    """ENTITY-RESOLUTION-CONTRACT.md's match hierarchy levels 2-4 collapsed
    into one check, adapted to what this schema can actually produce: an
    exact alias collision between two *different* entities cannot occur in
    the first place, since entity_aliases carries a workspace-wide unique
    constraint on (alias_type, normalized_value) -- see migration 0011's
    uq_entity_aliases_workspace_type_value. Attaching an already-claimed
    alias to a second entity is rejected at write time, not surfaced as a
    resolution candidate. The exact-match signal that two already-distinct
    entities can still exhibit is an identical normalized canonical name
    with a compatible (identical) entity kind -- treated as deterministic
    here rather than run through score_candidate's fuzzy trigram path."""
    if left.kind != right.kind:
        return False
    return _normalize(left.canonical_name) == _normalize(right.canonical_name)


def _existing_candidate(
    session: Session, auth: AuthContext, left_id: UUID, right_id: UUID
) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                f"""
                SELECT {_CANDIDATE_FIELDS}
                FROM resolution_candidates
                WHERE workspace_id = :workspace_id
                  AND left_entity_id = :left_id AND right_entity_id = :right_id
                """
            ),
            {"workspace_id": auth.workspace_id, "left_id": left_id, "right_id": right_id},
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
    candidate_id: UUID,
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
                    :id, :workspace_id, :event_type, 'resolution_candidate', :aggregate_id,
                    1, :actor_id, :request_id, :correlation_id,
                    ARRAY['status'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": candidate_id,
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
                "payload": dumps({"candidate_id": str(candidate_id)}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("knowledge_resolution")
        raise
    queue_lifecycle_event(session, "resolution_candidate", event_type, "allowed")


@router.post("/candidates", response_model=ResolutionCandidateResult, status_code=201)
def create_candidate(
    payload: ResolutionCandidateCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ResolutionCandidateResult:
    if payload.left_entity_id == payload.right_entity_id:
        raise HTTPException(status_code=422, detail="SELF_CANDIDATE_NOT_ALLOWED")
    request_hash = _request_hash(payload, "create_candidate")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached_result(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        # Normalize pair ordering so (A, B) and (B, A) are always the same
        # candidate row, and so a prior rejection of this unchanged pair is
        # found and reused rather than silently re-proposed, per the
        # contract's "Rejection prevents the same unchanged pair from being
        # proposed again."
        left_id, right_id = sorted((payload.left_entity_id, payload.right_entity_id), key=str)
        existing = _existing_candidate(session, auth, left_id, right_id)
        if existing is not None:
            response = ResolutionCandidateResult(deterministic=False, candidate=_project(existing))
            _store_cached_result(session, auth, idempotency_key, request_hash, response, now, 201)
            return response

        left = _candidate_entity(session, auth, left_id)
        right = _candidate_entity(session, auth, right_id)

        # Match hierarchy levels 1-4: deterministic, never create a
        # reviewable row (see _deterministic_alias_match's docstring and
        # ResolutionCandidateResult's docstring).
        if _deterministic_alias_match(left, right):
            response = ResolutionCandidateResult(deterministic=True, candidate=None)
            _store_cached_result(session, auth, idempotency_key, request_hash, response, now, 201)
            return response

        result = score_candidate(left, right)
        candidate_id = uuid4()
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO resolution_candidates (
                        id, workspace_id, left_entity_id, right_entity_id, score,
                        factors_json, resolver_version, status, created_at
                    ) VALUES (
                        :id, :workspace_id, :left_id, :right_id, :score,
                        CAST(:factors_json AS jsonb), :resolver_version, 'open', :now
                    )
                    RETURNING {_CANDIDATE_FIELDS}
                    """
                ),
                {
                    "id": candidate_id,
                    "workspace_id": auth.workspace_id,
                    "left_id": left_id,
                    "right_id": right_id,
                    "score": result.score,
                    "factors_json": dumps(
                        {
                            "name_similarity": result.factors.name_similarity,
                            "alias_overlap": result.factors.alias_overlap,
                            "neighbor_overlap": result.factors.neighbor_overlap,
                            "temporal_compatibility": result.factors.temporal_compatibility,
                        }
                    ),
                    "resolver_version": result.resolver_version,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        candidate = _project(dict(row))
        _write_side_effects(
            session, auth, request, "resolution_candidate.created", candidate_id, now
        )
        response = ResolutionCandidateResult(deterministic=False, candidate=candidate)
        _store_cached_result(session, auth, idempotency_key, request_hash, response, now, 201)
        return response


@router.get("/candidates", response_model=ResolutionCandidateListResponse)
def list_candidates(
    auth: AuthDep,
    session: SessionDep,
    status: CandidateStatus | None = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> ResolutionCandidateListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}
    if status is not None:
        clauses.append("status = :status")
        params["status"] = status
    if cursor is not None:
        created_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(created_at, id) < (:cursor_created_at, :cursor_id)")
        params.update({"cursor_created_at": created_at, "cursor_id": cursor_id})
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_CANDIDATE_FIELDS}
                FROM resolution_candidates
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["created_at"], last["id"])
    return ResolutionCandidateListResponse(
        items=[_project(dict(row)) for row in page], next_cursor=next_cursor
    )


def _encode_cursor(created_at: datetime, candidate_id: UUID) -> str:
    payload = dumps({"created_at": created_at.isoformat(), "id": str(candidate_id)}).encode()
    signature = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest().encode()
    return urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode())
        payload, signature = raw.rsplit(b".", 1)
        expected = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest()
        if not compare_digest(signature.decode(), expected):
            raise ValueError
        decoded = loads(payload)
        return datetime.fromisoformat(decoded["created_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


def _decide_candidate(
    candidate_id: UUID,
    payload: ResolutionDecision,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    new_status: Literal["confirmed", "rejected"],
) -> ResolutionCandidateResponse:
    action = "confirm" if new_status == "confirmed" else "reject"
    request_hash = _request_hash(payload, f"{action}:{candidate_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = (
            session.execute(
                text(
                    f"""
                    SELECT {_CANDIDATE_FIELDS}
                    FROM resolution_candidates
                    WHERE workspace_id = :workspace_id AND id = :candidate_id
                    FOR UPDATE
                    """
                ),
                {"workspace_id": auth.workspace_id, "candidate_id": candidate_id},
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise HTTPException(status_code=404, detail="CANDIDATE_NOT_FOUND")
        # Confirm/reject are idempotent per the contract: deciding an
        # already-decided candidate the same way returns the existing
        # record rather than erroring.
        if current["status"] == new_status:
            response = _project(dict(current))
            _store_cached(session, auth, idempotency_key, request_hash, response, now, 200)
            return response
        if current["status"] != "open":
            raise HTTPException(status_code=409, detail="CANDIDATE_NOT_OPEN")
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE resolution_candidates
                    SET status = :status, resolved_at = :now, resolved_by = :actor_id,
                        reason = :reason
                    WHERE workspace_id = :workspace_id AND id = :candidate_id
                    RETURNING {_CANDIDATE_FIELDS}
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "candidate_id": candidate_id,
                    "status": new_status,
                    "now": now,
                    "actor_id": auth.user_id,
                    "reason": payload.reason,
                },
            )
            .mappings()
            .one()
        )
        response = _project(dict(row))
        _write_side_effects(
            session, auth, request, f"resolution_candidate.{new_status}", candidate_id, now
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, now, 200)
        return response


@router.post("/candidates/{candidate_id}/confirm", response_model=ResolutionCandidateResponse)
def confirm_candidate(
    candidate_id: UUID,
    payload: ResolutionDecision,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ResolutionCandidateResponse:
    return _decide_candidate(
        candidate_id, payload, request, auth, session, idempotency_key, "confirmed"
    )


@router.post("/candidates/{candidate_id}/reject", response_model=ResolutionCandidateResponse)
def reject_candidate(
    candidate_id: UUID,
    payload: ResolutionDecision,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ResolutionCandidateResponse:
    return _decide_candidate(
        candidate_id, payload, request, auth, session, idempotency_key, "rejected"
    )
