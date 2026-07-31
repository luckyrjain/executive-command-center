"""The `habits` reference domain: `goals`/`routines`/`check_ins` plus
deterministic (non-AI) insights, proving the generic vault framework
(`domains.py`) end to end before a `sensitive`/`high_stakes` domain
reuses it (`docs/superpowers/specs/2026-07-31-phase-7-personal-
intelligence-design.md` Decision 1).

`GET|POST /api/v1/personal/goals`, `GET|POST /api/v1/personal/routines`,
`GET|POST /api/v1/personal/check-ins`, `GET /api/v1/personal/insights`,
`POST .../{id}/dismiss` (`docs/phases/phase-007/API-SCHEMAS.md`).

**`check_ins` gets idempotency-key handling (a network retry must not
double-log the same day's check-in) but not the full `audit_events`/
`event_outbox` write every other mutation in this codebase performs.**
Disclosed scope reduction, not an oversight: `audit_events`/`event_outbox`
exist for workspace-shared, cross-actor-visible business records (a
commitment, a connector account, a note another teammate's audit view
might need to reconstruct) -- a personal habit check-in is none of those
things, and its own row (`owner_id`, `created_by`, `created_at`, immutable
once written) already carries everything an audit trail for this specific
content would ever need. `goals`/`routines` (genuinely mutable, edited
after creation) keep the same lighter treatment for the identical reason,
scoped consistently across this one reference domain rather than applying
the heavier `domains.py`/generic-record treatment inconsistently between
sibling endpoints in the same file.

**Insights are computed on every `GET`, not on a schedule.** No scheduler
exists in this package (`domains.py`'s own `RETENTION_NUDGE_DAYS`
docstring notes the same gap for retention nudges) -- `list_insights_
endpoint` recomputes from `routines`/`check_ins` and `UPSERT`s by
`insight_key` on each call, matching migration `0054_phase7_personal_
domains.py`'s own docstring for why that key exists.
"""

from datetime import UTC, datetime
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep

from .domains import (
    DomainKey,
    IdempotencyHeader,
    SessionDep,
    load_cached,
    lock_idempotency,
    request_hash,
    require_enabled_domain,
    store_idempotency,
)

# A second `APIRouter` on the identical `/api/v1/personal` prefix, not a
# shared mutable router object imported from `domains.py` -- mirrors
# `connector_accounts.py`/`decisions_incidents.py`'s own precedent (two
# independently-registered routers sharing one prefix) rather than a
# module importing another module's router purely to decorate it with
# more routes as a side effect.
router = APIRouter(prefix="/api/v1/personal", tags=["personal"])

GoalStatus = Literal["active", "completed", "abandoned"]
Cadence = Literal["daily", "weekly", "monthly"]
InsightKind = Literal["observation", "trend", "correlation", "reminder", "planning_suggestion"]
Confidence = Literal["low", "medium", "high"]

# A gap (days since the routine's last check-in) at or beyond this
# threshold is the one deterministic observation this task computes --
# chosen loosely above a single missed day (which is not yet a meaningful
# gap for any cadence) and well under a month, so the insight stays timely
# for `daily`/`weekly` routines without this task needing a per-cadence
# threshold table for a single observation kind.
_GAP_OBSERVATION_THRESHOLD_DAYS = 3


class GoalCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain_key: DomainKey
    title: str = Field(min_length=1, max_length=255)
    target_count: int | None = Field(default=None, ge=1)
    target_at: datetime | None = None


class GoalResponse(BaseModel):
    id: UUID
    domain_key: str
    title: str
    target_count: int | None
    target_at: datetime | None
    status: GoalStatus
    version: int
    created_at: datetime
    updated_at: datetime


class GoalListResponse(BaseModel):
    goals: list[GoalResponse]


class RoutineCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domain_key: DomainKey
    goal_id: UUID | None = None
    title: str = Field(min_length=1, max_length=255)
    cadence: Cadence


class RoutineResponse(BaseModel):
    id: UUID
    domain_key: str
    goal_id: UUID | None
    title: str
    cadence: Cadence
    version: int
    archived_at: datetime | None
    created_at: datetime
    updated_at: datetime


class RoutineListResponse(BaseModel):
    routines: list[RoutineResponse]


class CheckInCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    routine_id: UUID
    occurred_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)


class CheckInResponse(BaseModel):
    id: UUID
    routine_id: UUID
    occurred_at: datetime
    note: str | None
    created_at: datetime


class CheckInListResponse(BaseModel):
    check_ins: list[CheckInResponse]


class InsightResponse(BaseModel):
    id: UUID
    domain_key: str
    kind: InsightKind
    title: str
    evidence: dict[str, Any]
    source_period_start: datetime
    source_period_end: datetime
    missing_data: str | None
    confidence: Confidence
    limitations: str
    # `None` for every deterministic insight this module computes, and for
    # any AI-generated (`trend`/`correlation`, Task 5 part 2) insight whose
    # sources are all `standard`/`sensitive` -- non-empty specifically when
    # a source is `high_stakes` (`INSIGHT-CONTRACT.md`'s safety rubric,
    # enforced before persistence by `validator.py:check_personal_insight_
    # grounding`, not by this response model).
    professional_referral_note: str | None
    computed_at: datetime
    dismissed_at: datetime | None


class InsightListResponse(BaseModel):
    insights: list[InsightResponse]


class FeedbackCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    useful: bool
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackResponse(BaseModel):
    id: UUID
    insight_id: UUID
    useful: bool
    comment: str | None
    created_at: datetime


def _get_goal_row(session: Session, auth: AuthContext, goal_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT id, domain_key, title, target_count, target_at, status, version, "
                "created_at, updated_at FROM goals "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND id = :id"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": goal_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


@router.post("/goals", response_model=GoalResponse, status_code=status.HTTP_201_CREATED)
def create_goal_endpoint(
    payload: GoalCreateRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> GoalResponse:
    req_hash = request_hash(payload, "create_goal")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="personal_goal")
        if cached is not None:
            return GoalResponse.model_validate(cached)

        require_enabled_domain(session, auth, payload.domain_key)
        goal_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO goals (
                    id, workspace_id, owner_id, domain_key, title, target_count, target_at,
                    status, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, :title, :target_count, :target_at,
                    'active', :actor_id, :actor_id, :now, :now, 1
                )
                """
            ),
            {
                "id": goal_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": payload.domain_key,
                "title": payload.title,
                "target_count": payload.target_count,
                "target_at": payload.target_at,
                "now": now,
                "actor_id": auth.user_id,
            },
        )
        row = _get_goal_row(session, auth, goal_id)
        assert row is not None
        response = GoalResponse(**row)
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            response_status=status.HTTP_201_CREATED,
        )
        return response


@router.get("/goals", response_model=GoalListResponse)
def list_goals_endpoint(
    auth: AuthDep,
    session: SessionDep,
    domain_key: Annotated[DomainKey | None, Query()] = None,
) -> GoalListResponse:
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "owner_id": auth.user_id}
    where = "workspace_id = :workspace_id AND owner_id = :owner_id"
    if domain_key is not None:
        where += " AND domain_key = :domain_key"
        params["domain_key"] = domain_key
    rows = (
        session.execute(
            text(
                "SELECT id, domain_key, title, target_count, target_at, status, version, "
                f"created_at, updated_at FROM goals WHERE {where} ORDER BY created_at ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return GoalListResponse(goals=[GoalResponse(**dict(r)) for r in rows])


def _get_routine_row(
    session: Session, auth: AuthContext, routine_id: UUID
) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT id, domain_key, goal_id, title, cadence, version, archived_at, "
                "created_at, updated_at FROM routines "
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND id = :id"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": routine_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


@router.post("/routines", response_model=RoutineResponse, status_code=status.HTTP_201_CREATED)
def create_routine_endpoint(
    payload: RoutineCreateRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RoutineResponse:
    req_hash = request_hash(payload, "create_routine")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="personal_routine")
        if cached is not None:
            return RoutineResponse.model_validate(cached)

        require_enabled_domain(session, auth, payload.domain_key)
        if payload.goal_id is not None and _get_goal_row(session, auth, payload.goal_id) is None:
            raise HTTPException(status_code=422, detail="GOAL_NOT_FOUND")

        routine_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO routines (
                    id, workspace_id, owner_id, domain_key, goal_id, title, cadence,
                    created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, :goal_id, :title, :cadence,
                    :actor_id, :actor_id, :now, :now, 1
                )
                """
            ),
            {
                "id": routine_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": payload.domain_key,
                "goal_id": payload.goal_id,
                "title": payload.title,
                "cadence": payload.cadence,
                "now": now,
                "actor_id": auth.user_id,
            },
        )
        row = _get_routine_row(session, auth, routine_id)
        assert row is not None
        response = RoutineResponse(**row)
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            response_status=status.HTTP_201_CREATED,
        )
        return response


@router.get("/routines", response_model=RoutineListResponse)
def list_routines_endpoint(
    auth: AuthDep,
    session: SessionDep,
    domain_key: Annotated[DomainKey | None, Query()] = None,
) -> RoutineListResponse:
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "owner_id": auth.user_id}
    where = "workspace_id = :workspace_id AND owner_id = :owner_id"
    if domain_key is not None:
        where += " AND domain_key = :domain_key"
        params["domain_key"] = domain_key
    rows = (
        session.execute(
            text(
                "SELECT id, domain_key, goal_id, title, cadence, version, archived_at, "
                f"created_at, updated_at FROM routines WHERE {where} ORDER BY created_at ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return RoutineListResponse(routines=[RoutineResponse(**dict(r)) for r in rows])


@router.post("/check-ins", response_model=CheckInResponse, status_code=status.HTTP_201_CREATED)
def create_check_in_endpoint(
    payload: CheckInCreateRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CheckInResponse:
    req_hash = request_hash(payload, "create_check_in")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="personal_check_in")
        if cached is not None:
            return CheckInResponse.model_validate(cached)

        routine = _get_routine_row(session, auth, payload.routine_id)
        if routine is None:
            raise HTTPException(status_code=404, detail="ROUTINE_NOT_FOUND")
        require_enabled_domain(session, auth, routine["domain_key"])

        check_in_id = uuid4()
        occurred_at = payload.occurred_at or now
        session.execute(
            text(
                """
                INSERT INTO check_ins (
                    id, workspace_id, owner_id, domain_key, routine_id, occurred_at, note,
                    created_by, created_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, :routine_id, :occurred_at, :note,
                    :actor_id, :now
                )
                """
            ),
            {
                "id": check_in_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": routine["domain_key"],
                "routine_id": payload.routine_id,
                "occurred_at": occurred_at,
                "note": payload.note,
                "actor_id": auth.user_id,
                "now": now,
            },
        )
        row = (
            session.execute(
                text(
                    "SELECT id, routine_id, occurred_at, note, created_at FROM check_ins "
                    "WHERE id = :id"
                ),
                {"id": check_in_id},
            )
            .mappings()
            .one()
        )
        response = CheckInResponse(**dict(row))
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            response_status=status.HTTP_201_CREATED,
        )
        return response


@router.get("/check-ins", response_model=CheckInListResponse)
def list_check_ins_endpoint(
    auth: AuthDep,
    session: SessionDep,
    routine_id: Annotated[UUID, Query()],
) -> CheckInListResponse:
    if _get_routine_row(session, auth, routine_id) is None:
        raise HTTPException(status_code=404, detail="ROUTINE_NOT_FOUND")
    rows = (
        session.execute(
            text(
                "SELECT id, routine_id, occurred_at, note, created_at FROM check_ins "
                "WHERE workspace_id = :workspace_id AND routine_id = :routine_id "
                "ORDER BY occurred_at DESC"
            ),
            {"workspace_id": auth.workspace_id, "routine_id": routine_id},
        )
        .mappings()
        .all()
    )
    return CheckInListResponse(check_ins=[CheckInResponse(**dict(r)) for r in rows])


# ---------------------------------------------------------------------------
# Deterministic insights
# ---------------------------------------------------------------------------


def _compute_gap_insights(session: Session, auth: AuthContext) -> list[dict[str, Any]]:
    """One deterministic observation per active `routine` whose most recent
    `check_ins` row (or, if it has none at all, its own `created_at`) is at
    least `_GAP_OBSERVATION_THRESHOLD_DAYS` old. No model call; the entire
    computation is a `SELECT` and a Python comparison.
    """
    now = datetime.now(UTC)
    rows = (
        session.execute(
            text(
                """
                SELECT r.id AS routine_id, r.domain_key, r.title, r.created_at,
                       MAX(c.occurred_at) AS last_occurred_at
                FROM routines r
                LEFT JOIN check_ins c ON c.routine_id = r.id AND c.workspace_id = r.workspace_id
                WHERE r.workspace_id = :workspace_id AND r.owner_id = :owner_id
                  AND r.archived_at IS NULL
                GROUP BY r.id, r.domain_key, r.title, r.created_at
                """
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id},
        )
        .mappings()
        .all()
    )
    insights: list[dict[str, Any]] = []
    for row in rows:
        anchor = row["last_occurred_at"] or row["created_at"]
        gap_days = (now - anchor).days
        if gap_days < _GAP_OBSERVATION_THRESHOLD_DAYS:
            continue
        insights.append(
            {
                "insight_key": f"habit_gap:{row['routine_id']}",
                "domain_key": row["domain_key"],
                "kind": "observation",
                "title": f'No check-in logged for "{row["title"]}" in {gap_days} days',
                "evidence": {"routine_id": str(row["routine_id"]), "gap_days": gap_days},
                "source_period_start": anchor,
                "source_period_end": now,
                "missing_data": (
                    None if row["last_occurred_at"] is not None else "no check-ins logged yet"
                ),
                "confidence": "high",
                "limitations": (
                    "Based only on logged check-ins for this routine; does not account for "
                    "planned breaks or check-ins recorded elsewhere."
                ),
            }
        )
    return insights


@router.get("/insights", response_model=InsightListResponse)
def list_insights_endpoint(auth: AuthDep, session: SessionDep) -> InsightListResponse:
    """One transaction for the whole recompute-then-read, not one per
    insight -- `_compute_gap_insights`'s own read already touches `session`
    before any `UPSERT` runs, and `Session`'s autobegin behavior means that
    read alone opens an implicit transaction; a second, explicit
    `session.begin()` call afterward would raise `InvalidRequestError`
    ("a transaction is already begun"). Wrapping the entire handler in one
    `with session.begin()` avoids that, and also makes the recompute+read
    atomic -- a concurrent check-in landing mid-recompute cannot produce a
    response reflecting only some of this call's own upserts.
    """
    now = datetime.now(UTC)
    with session.begin():
        for insight in _compute_gap_insights(session, auth):
            session.execute(
                text(
                    """
                    INSERT INTO personal_insights (
                        id, workspace_id, owner_id, domain_key, insight_key, kind, title,
                        evidence, source_period_start, source_period_end, missing_data,
                        confidence, limitations, computed_at
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :domain_key, :insight_key, :kind, :title,
                        CAST(:evidence AS jsonb), :source_period_start, :source_period_end,
                        :missing_data, :confidence, :limitations, :now
                    )
                    ON CONFLICT (workspace_id, owner_id, insight_key) DO UPDATE SET
                        title = EXCLUDED.title, evidence = EXCLUDED.evidence,
                        source_period_start = EXCLUDED.source_period_start,
                        source_period_end = EXCLUDED.source_period_end,
                        missing_data = EXCLUDED.missing_data, confidence = EXCLUDED.confidence,
                        limitations = EXCLUDED.limitations, computed_at = EXCLUDED.computed_at
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "owner_id": auth.user_id,
                    "domain_key": insight["domain_key"],
                    "insight_key": insight["insight_key"],
                    "kind": insight["kind"],
                    "title": insight["title"],
                    "evidence": dumps(insight["evidence"]),
                    "source_period_start": insight["source_period_start"],
                    "source_period_end": insight["source_period_end"],
                    "missing_data": insight["missing_data"],
                    "confidence": insight["confidence"],
                    "limitations": insight["limitations"],
                    "now": now,
                },
            )
        rows = (
            session.execute(
                text(
                    "SELECT id, domain_key, kind, title, evidence, source_period_start, "
                    "source_period_end, missing_data, confidence, limitations, "
                    "professional_referral_note, computed_at, dismissed_at FROM personal_insights "
                    "WHERE workspace_id = :workspace_id AND owner_id = :owner_id "
                    "AND dismissed_at IS NULL ORDER BY computed_at DESC"
                ),
                {"workspace_id": auth.workspace_id, "owner_id": auth.user_id},
            )
            .mappings()
            .all()
        )
        return InsightListResponse(insights=[InsightResponse(**dict(r)) for r in rows])


@router.post("/insights/{insight_id}/dismiss", response_model=InsightResponse)
def dismiss_insight_endpoint(
    insight_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> InsightResponse:
    now = datetime.now(UTC)
    with session.begin():
        row = (
            session.execute(
                text(
                    "SELECT id FROM personal_insights "
                    "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND id = :id"
                ),
                {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": insight_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="INSIGHT_NOT_FOUND")
        session.execute(
            text("UPDATE personal_insights SET dismissed_at = :now WHERE id = :id"),
            {"now": now, "id": insight_id},
        )
        result = (
            session.execute(
                text(
                    "SELECT id, domain_key, kind, title, evidence, source_period_start, "
                    "source_period_end, missing_data, confidence, limitations, "
                    "professional_referral_note, computed_at, dismissed_at "
                    "FROM personal_insights WHERE id = :id"
                ),
                {"id": insight_id},
            )
            .mappings()
            .one()
        )
        return InsightResponse(**dict(result))


@router.post(
    "/insights/{insight_id}/feedback",
    response_model=FeedbackResponse,
    status_code=status.HTTP_201_CREATED,
)
def feedback_insight_endpoint(
    insight_id: UUID,
    payload: FeedbackCreateRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> FeedbackResponse:
    """`personal_insight_feedback` is a separate, append-only table with no
    `kind` column of its own (migration `0059_phase7_personal_insight_
    feedback.py`'s own docstring) -- structurally incapable of rewriting an
    insight's own `kind`, matching `INSIGHT-CONTRACT.md`'s "`POST .../
    feedback` never rewrites an insight's own `kind`". No idempotency-key
    handling, matching `dismiss_insight_endpoint`'s own identical scope
    (this module's docstring: personal content this lightweight gets the
    same lighter treatment consistently across sibling endpoints).
    """
    now = datetime.now(UTC)
    with session.begin():
        row = (
            session.execute(
                text(
                    "SELECT id FROM personal_insights "
                    "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND id = :id"
                ),
                {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": insight_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="INSIGHT_NOT_FOUND")

        feedback_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO personal_insight_feedback (
                    id, workspace_id, owner_id, insight_id, useful, comment,
                    created_by, created_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :insight_id, :useful, :comment,
                    :actor_id, :now
                )
                """
            ),
            {
                "id": feedback_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "insight_id": insight_id,
                "useful": payload.useful,
                "comment": payload.comment,
                "actor_id": auth.user_id,
                "now": now,
            },
        )
        return FeedbackResponse(
            id=feedback_id,
            insight_id=insight_id,
            useful=payload.useful,
            comment=payload.comment,
            created_at=now,
        )
