"""Deterministic plan proposals (Phase 3 Task 5).

``propose_plan`` is a pure function implementing PLANNING-CONTRACT.md's
seven-step deterministic order over plain data -- no DB access, no
mutation -- mirroring Phase 2's ``resolution.py:score_candidate`` pure/impure
split. The impure side (``POST /plans``) fetches capacity/constraints/
calendar/attention-ranking from the database, calls this function, and
persists the result as a new ``plans``/``plan_blocks`` snapshot.

Anchor-time note: ``capacity_profiles`` (Task 4) stores a per-weekday
*budget* in minutes, not a start-of-day clock time -- DATA-MODEL.md's field
list for that table is exactly ``weekday, available_minutes, focus_minutes,
timezone, version``. Absent an explicit start time, each day's workable
window is anchored at a fixed local ``_WORKDAY_START`` and runs for that
day's ``available_minutes``, then hard reservations are subtracted from it.
This is a real, load-bearing scoping decision (not an oversight) -- a
configurable start time is deferred until a real user need for it appears,
consistent with this codebase's "no speculative field" discipline.
"""


from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session
from ecc.observability import queue_lifecycle_event, record_audit_outbox_failure

_WORKDAY_START = time(9, 0)
DEFAULT_EFFORT_MINUTES = 30

ConflictCode = Literal["capacity_exceeded", "missed_deadline", "constraint_conflict"]
BlockSourceType = Literal["task", "commitment", "waiting_link", "constraint", "calendar_event"]


@dataclass(frozen=True)
class CapacityDayInput:
    weekday: int
    available_minutes: int


@dataclass(frozen=True)
class ReservedBlockInput:
    """A hard, unmovable reservation: a calendar event or a hard fixed_time
    constraint. Both are reserved in the same pass (PLANNING-CONTRACT.md's
    steps 2 and 3 differ only in provenance, not in how they're handled)."""

    source_type: BlockSourceType
    source_id: UUID | None
    label: str
    starts_at: datetime
    ends_at: datetime


@dataclass(frozen=True)
class DeadlineConstraintInput:
    source_id: UUID
    label: str
    due_at: datetime
    priority: int


@dataclass(frozen=True)
class CandidateItemInput:
    entity_type: str
    entity_id: UUID
    label: str
    score: int
    pinned: bool = False
    due_at: datetime | None = None
    effort_minutes: int | None = None


@dataclass(frozen=True)
class PlanBlockOutput:
    source_type: BlockSourceType
    source_id: UUID | None
    label: str
    starts_at: datetime
    ends_at: datetime
    rationale: str
    is_default_effort: bool


@dataclass(frozen=True)
class UnscheduledOutput:
    source_type: BlockSourceType
    source_id: UUID | None
    label: str
    reason: str


@dataclass(frozen=True)
class ConflictOutput:
    code: ConflictCode
    detail: str
    source_type: BlockSourceType | None = None
    source_id: UUID | None = None


@dataclass(frozen=True)
class PlanProposal:
    blocks: list[PlanBlockOutput]
    unscheduled: list[UnscheduledOutput]
    conflicts: list[ConflictOutput]
    capacity_minutes: int


@dataclass
class _Interval:
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


def _day_window(local_date: date, zone: ZoneInfo, available_minutes: int) -> _Interval:
    start = datetime.combine(local_date, _WORKDAY_START, zone)
    return _Interval(start, start + timedelta(minutes=available_minutes))


def _subtract_reservation(intervals: list[_Interval], reservation: _Interval) -> list[_Interval]:
    result: list[_Interval] = []
    for interval in intervals:
        if reservation.end <= interval.start or reservation.start >= interval.end:
            result.append(interval)
            continue
        if reservation.start > interval.start:
            result.append(_Interval(interval.start, reservation.start))
        if reservation.end < interval.end:
            result.append(_Interval(reservation.end, interval.end))
    return [i for i in result if i.minutes > 0]


def _place(
    intervals: list[_Interval], duration_minutes: int, not_after: datetime | None
) -> tuple[list[_Interval], _Interval | None]:
    """Find the earliest free interval with enough room (and, for
    deadline-bound items, that finishes by ``not_after``), splitting it to
    reserve exactly ``duration_minutes``. Returns the updated interval list
    and the placed slot, or ``None`` if nothing fit.
    """
    ordered = sorted(intervals, key=lambda i: i.start)
    for index, interval in enumerate(ordered):
        if interval.minutes < duration_minutes:
            continue
        placed_end = interval.start + timedelta(minutes=duration_minutes)
        if not_after is not None and placed_end > not_after:
            continue
        placed = _Interval(interval.start, placed_end)
        remaining = ordered[:index] + ordered[index + 1 :]
        if placed_end < interval.end:
            remaining.append(_Interval(placed_end, interval.end))
        return remaining, placed
    return intervals, None


def propose_plan(
    *,
    period_start: date,
    period_end: date,
    timezone: str,
    capacity_days: list[CapacityDayInput],
    reserved_blocks: list[ReservedBlockInput],
    deadline_constraints: list[DeadlineConstraintInput],
    candidates: list[CandidateItemInput],
    default_effort_minutes: int = DEFAULT_EFFORT_MINUTES,
) -> PlanProposal:
    zone = ZoneInfo(timezone)  # Step 1: validate timezone (raises ZoneInfoNotFoundError if not).
    capacity_by_weekday = {day.weekday: day.available_minutes for day in capacity_days}

    # Step 2 + 3: build each day's free window, then reserve hard calendar
    # blocks and hard fixed_time constraints out of it (same subtraction
    # pass for both -- see ReservedBlockInput's docstring).
    free_intervals: list[_Interval] = []
    capacity_minutes = 0
    current = period_start
    while current <= period_end:
        available = capacity_by_weekday.get(current.weekday(), 0)
        if available > 0:
            capacity_minutes += available
            free_intervals.append(_day_window(current, zone, available))
        current += timedelta(days=1)

    conflicts: list[ConflictOutput] = []
    sorted_reservations = sorted(
        reserved_blocks, key=lambda r: (r.starts_at, r.source_id or UUID(int=0))
    )
    placed_reservations: list[ReservedBlockInput] = []
    for reservation in sorted_reservations:
        for other in placed_reservations:
            if reservation.starts_at < other.ends_at and other.starts_at < reservation.ends_at:
                conflicts.append(
                    ConflictOutput(
                        code="constraint_conflict",
                        detail=f"{reservation.label!r} overlaps {other.label!r}",
                        source_type=reservation.source_type,
                        source_id=reservation.source_id,
                    )
                )
        placed_reservations.append(reservation)
        free_intervals = _subtract_reservation(
            free_intervals, _Interval(reservation.starts_at, reservation.ends_at)
        )

    blocks: list[PlanBlockOutput] = []
    unscheduled: list[UnscheduledOutput] = []

    # Step 4: deadline-critical work, earliest deadline first, then priority
    # desc, then source_id asc as the stable final tie-breaker.
    for deadline in sorted(
        deadline_constraints, key=lambda d: (d.due_at, -d.priority, str(d.source_id))
    ):
        free_intervals, placed = _place(
            free_intervals, default_effort_minutes, not_after=deadline.due_at
        )
        if placed is None:
            conflicts.append(
                ConflictOutput(
                    code="missed_deadline",
                    detail=f"No feasible window before {deadline.label!r}'s deadline",
                    source_type="constraint",
                    source_id=deadline.source_id,
                )
            )
            unscheduled.append(
                UnscheduledOutput(
                    "constraint", deadline.source_id, deadline.label, "missed_deadline"
                )
            )
            continue
        blocks.append(
            PlanBlockOutput(
                "constraint",
                deadline.source_id,
                deadline.label,
                placed.start,
                placed.end,
                rationale=f"Deadline: {deadline.label}",
                is_default_effort=True,
            )
        )

    # Step 5: pinned items, score desc then entity_id asc (matches
    # list_attention's own stable final tie-break field).
    pinned = sorted((c for c in candidates if c.pinned), key=lambda c: (-c.score, str(c.entity_id)))
    # Step 6: everything else, same ordering.
    remaining = sorted(
        (c for c in candidates if not c.pinned), key=lambda c: (-c.score, str(c.entity_id))
    )

    capacity_exhausted = False
    for candidate in [*pinned, *remaining]:
        duration = candidate.effort_minutes or default_effort_minutes
        is_default = candidate.effort_minutes is None
        free_intervals, placed = _place(free_intervals, duration, not_after=candidate.due_at)
        if placed is None:
            reason = "missed_deadline" if candidate.due_at is not None else "no_capacity"
            if reason == "no_capacity":
                capacity_exhausted = True
            else:
                conflicts.append(
                    ConflictOutput(
                        code="missed_deadline",
                        detail=f"No feasible window before {candidate.label!r}'s due time",
                        source_type=candidate.entity_type,  # type: ignore[arg-type]
                        source_id=candidate.entity_id,
                    )
                )
            unscheduled.append(
                UnscheduledOutput(
                    candidate.entity_type,  # type: ignore[arg-type]
                    candidate.entity_id,
                    candidate.label,
                    reason,
                )
            )
            continue
        blocks.append(
            PlanBlockOutput(
                candidate.entity_type,  # type: ignore[arg-type]
                candidate.entity_id,
                candidate.label,
                placed.start,
                placed.end,
                rationale=("Pinned" if candidate.pinned else f"Score {candidate.score}"),
                is_default_effort=is_default,
            )
        )

    # Step 7: never hide an over-capacity conflict behind a pile of
    # individually-unscheduled items -- one summary conflict covers it.
    if capacity_exhausted:
        conflicts.append(
            ConflictOutput(
                code="capacity_exceeded",
                detail="Not enough remaining capacity to place every eligible item",
            )
        )

    return PlanProposal(
        blocks=blocks,
        unscheduled=unscheduled,
        conflicts=conflicts,
        capacity_minutes=capacity_minutes,
    )


# --------------------------------------------------------------------------
# Impure side: fetches inputs from the database, calls propose_plan, persists
# the result. POST /plans always creates a fresh plan in 'proposed' status;
# Task 6 adds accept/supersede/edit over the same table without a new
# migration (see the migration's module docstring).
# --------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/plans", tags=["planning"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

PlanStatus = Literal["draft", "proposed", "accepted", "completed", "superseded"]
_MAX_PERIOD_DAYS = 7

_PLAN_FIELDS = """
    id, period_start, period_end, status, policy_version, capacity_minutes,
    source_versions, conflicts, unscheduled, superseded_by, accepted_at,
    created_at, updated_at, version
"""
_BLOCK_FIELDS = """
    id, source_type, source_id, starts_at, ends_at, status, rationale, is_default_effort
"""


class PlanBlockResponse(BaseModel):
    id: UUID
    source_type: BlockSourceType
    source_id: UUID | None
    starts_at: datetime
    ends_at: datetime
    status: Literal["proposed", "accepted"]
    rationale: str
    is_default_effort: bool


DiffChange = Literal["added", "removed", "moved", "unchanged", "newly_conflicted"]


class PlanDiffEntry(BaseModel):
    source_type: BlockSourceType
    source_id: UUID | None
    label: str
    change: DiffChange


class Plan(BaseModel):
    id: UUID
    period_start: date
    period_end: date
    status: PlanStatus
    policy_version: int
    capacity_minutes: int
    conflicts: list[dict[str, Any]]
    unscheduled: list[dict[str, Any]]
    superseded_by: UUID | None
    accepted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    version: int
    blocks: list[PlanBlockResponse]
    diff: list[PlanDiffEntry] | None = None


class PlanList(BaseModel):
    items: list[Plan]
    next_cursor: str | None = None


class PlanAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class BlockMove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    starts_at: datetime
    ends_at: datetime

    @model_validator(mode="after")
    def _validate_range(self) -> BlockMove:
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class BlockRemove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class PlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_start: date
    period_end: date

    @model_validator(mode="after")
    def _validate_period(self) -> PlanCreate:
        if self.period_end < self.period_start:
            raise ValueError("period_end must not be before period_start")
        if (self.period_end - self.period_start).days + 1 > _MAX_PERIOD_DAYS:
            raise ValueError(f"a plan may cover at most {_MAX_PERIOD_DAYS} days")
        return self


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _load_cached(session: Session, auth: AuthContext, key: str, request_hash: str) -> Plan | None:
    row = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body FROM idempotency_records
                WHERE workspace_id = :workspace_id AND actor_id = :actor_id
                  AND key = :key AND expires_at > :now
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
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return Plan.model_validate(row["response_body"])


def _fetch_capacity_days(session: Session, auth: AuthContext) -> list[CapacityDayInput]:
    rows = session.execute(
        text(
            "SELECT weekday, available_minutes FROM capacity_profiles "
            "WHERE workspace_id = :workspace_id AND user_id = :user_id"
        ),
        {"workspace_id": auth.workspace_id, "user_id": auth.user_id},
    ).all()
    return [CapacityDayInput(weekday=row[0], available_minutes=row[1]) for row in rows]


def _fetch_reserved_blocks(
    session: Session, auth: AuthContext, period_start: datetime, period_end: datetime
) -> list[ReservedBlockInput]:
    calendar_rows = session.execute(
        text(
            """
            SELECT id, title, starts_at, ends_at FROM calendar_events
            WHERE workspace_id = :workspace_id AND archived_at IS NULL AND status <> 'cancelled'
              AND starts_at < :period_end AND ends_at > :period_start
            """
        ),
        {"workspace_id": auth.workspace_id, "period_start": period_start, "period_end": period_end},
    ).all()
    constraint_rows = session.execute(
        text(
            """
            SELECT id, label, starts_at, ends_at FROM planning_constraints
            WHERE workspace_id = :workspace_id AND user_id = :user_id AND archived_at IS NULL
              AND kind = 'fixed_time' AND hardness = 'hard'
              AND starts_at < :period_end AND ends_at > :period_start
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
            "period_start": period_start,
            "period_end": period_end,
        },
    ).all()
    return [
        ReservedBlockInput("calendar_event", row[0], row[1], row[2], row[3])
        for row in calendar_rows
    ] + [
        ReservedBlockInput("constraint", row[0], row[1], row[2], row[3]) for row in constraint_rows
    ]


def _fetch_deadline_constraints(
    session: Session, auth: AuthContext, period_end: datetime
) -> list[DeadlineConstraintInput]:
    rows = session.execute(
        text(
            """
            SELECT id, label, ends_at, priority FROM planning_constraints
            WHERE workspace_id = :workspace_id AND user_id = :user_id AND archived_at IS NULL
              AND kind = 'deadline' AND ends_at <= :period_end
            """
        ),
        {"workspace_id": auth.workspace_id, "user_id": auth.user_id, "period_end": period_end},
    ).all()
    return [DeadlineConstraintInput(row[0], row[1], row[2], row[3]) for row in rows]


def _fetch_candidates(
    session: Session, auth: AuthContext, now: datetime
) -> list[CandidateItemInput]:
    """Same visibility filter as ``attention.py:list_attention`` (non-expired,
    non-dismissed, non-deferred) -- since ``expires_at`` is set at generation
    time, this is also how Step 1's "source freshness" is honored: a
    candidate whose attention score hasn't been regenerated recently simply
    isn't fresh enough to appear here, with no separate staleness check
    needed. Only entity_types that represent schedulable, doable work are
    candidates; risk/risk_review/meeting are attention-queue items but not
    something you block time to "do".
    """
    rows = session.execute(
        text(
            """
            SELECT ai.entity_type, ai.entity_id, ai.score, ai.pinned,
                   COALESCE(t.title, c.summary, 'Waiting: ' || wl.direction) AS label,
                   COALESCE(
                     t.due_at, (t.due_date::timestamp + time '23:59:59') AT TIME ZONE :tz,
                     c.due_at, (c.due_date::timestamp + time '23:59:59') AT TIME ZONE :tz,
                     wl.expected_at
                   ) AS due_at
            FROM attention_items ai
            LEFT JOIN tasks t ON ai.entity_type = 'task'
                AND t.workspace_id = ai.workspace_id AND t.id = ai.entity_id
            LEFT JOIN commitments c ON ai.entity_type = 'commitment'
                AND c.workspace_id = ai.workspace_id AND c.id = ai.entity_id
            LEFT JOIN waiting_links wl ON ai.entity_type = 'waiting_link'
                AND wl.workspace_id = ai.workspace_id AND wl.id = ai.entity_id
            WHERE ai.workspace_id = :workspace_id
              AND ai.entity_type IN ('task', 'commitment', 'waiting_link')
              AND ai.expires_at > :now
              AND (ai.dismissed_at IS NULL
                   OR ai.dismissed_entity_version <> ai.source_entity_version)
              AND (ai.deferred_until IS NULL OR ai.deferred_until <= :now)
            ORDER BY ai.score DESC, ai.entity_id ASC
            """
        ),
        {"workspace_id": auth.workspace_id, "now": now, "tz": auth.timezone},
    ).all()
    return [
        CandidateItemInput(
            entity_type=row[0],
            entity_id=row[1],
            label=row[4] or f"{row[0]} {row[1]}",
            score=row[2],
            pinned=row[3],
            due_at=row[5],
        )
        for row in rows
    ]


def _row_to_plan(session: Session, auth: AuthContext, row: dict[str, Any]) -> Plan:
    block_rows = (
        session.execute(
            text(
                f"SELECT {_BLOCK_FIELDS} FROM plan_blocks "
                "WHERE workspace_id = :workspace_id AND plan_id = :plan_id ORDER BY starts_at"
            ),
            {"workspace_id": auth.workspace_id, "plan_id": row["id"]},
        )
        .mappings()
        .all()
    )
    return Plan.model_validate(
        {**row, "blocks": [PlanBlockResponse.model_validate(dict(b)) for b in block_rows]}
    )


def _source_fingerprint(
    candidates: list[CandidateItemInput],
    reserved_blocks: list[ReservedBlockInput],
    deadline_constraints: list[DeadlineConstraintInput],
    capacity_days: list[CapacityDayInput],
) -> dict[str, str]:
    """A cheap, order-independent fingerprint of everything propose_plan
    read, stored on the plan as ``source_versions`` so a later accept or
    replan can detect "source changes mark a proposal stale"
    (PLANNING-CONTRACT.md's Replanning section) without persisting full
    copies of each input -- just a hash per input category.
    """
    candidate_sig = sorted(
        f"{c.entity_type}:{c.entity_id}:{c.score}:{c.due_at}" for c in candidates
    )
    reservation_sig = sorted(
        f"{r.source_type}:{r.source_id}:{r.starts_at}:{r.ends_at}" for r in reserved_blocks
    )
    deadline_sig = sorted(f"{d.source_id}:{d.due_at}:{d.priority}" for d in deadline_constraints)
    capacity_sig = sorted(f"{c.weekday}:{c.available_minutes}" for c in capacity_days)
    return {
        "candidates": sha256("|".join(candidate_sig).encode()).hexdigest(),
        "reservations": sha256("|".join(reservation_sig).encode()).hexdigest(),
        "deadlines": sha256("|".join(deadline_sig).encode()).hexdigest(),
        "capacity": sha256("|".join(capacity_sig).encode()).hexdigest(),
    }


@dataclass
class _GeneratedProposal:
    proposal: PlanProposal
    fingerprint: dict[str, str]


def _generate_proposal(
    session: Session, auth: AuthContext, period_start: date, period_end: date, now: datetime
) -> _GeneratedProposal:
    zone = ZoneInfo(auth.timezone)
    period_start_dt = datetime.combine(period_start, time.min, zone)
    period_end_dt = datetime.combine(period_end + timedelta(days=1), time.min, zone)

    capacity_days = _fetch_capacity_days(session, auth)
    reserved_blocks = _fetch_reserved_blocks(session, auth, period_start_dt, period_end_dt)
    deadline_constraints = _fetch_deadline_constraints(session, auth, period_end_dt)
    candidates = _fetch_candidates(session, auth, now)

    proposal = propose_plan(
        period_start=period_start,
        period_end=period_end,
        timezone=auth.timezone,
        capacity_days=capacity_days,
        reserved_blocks=reserved_blocks,
        deadline_constraints=deadline_constraints,
        candidates=candidates,
    )
    fingerprint = _source_fingerprint(
        candidates, reserved_blocks, deadline_constraints, capacity_days
    )
    return _GeneratedProposal(proposal=proposal, fingerprint=fingerprint)


def _is_stale(session: Session, auth: AuthContext, plan_row: dict[str, Any]) -> bool:
    stored = plan_row.get("source_versions") or {}
    if not isinstance(stored, dict) or "candidates" not in stored:
        return False  # older/placeholder snapshot shape: nothing to compare against.
    now = datetime.now(UTC)
    current = _generate_proposal(
        session, auth, plan_row["period_start"], plan_row["period_end"], now
    )
    return current.fingerprint != stored


@router.post("", response_model=Plan, status_code=status.HTTP_201_CREATED)
def create_plan(
    payload: PlanCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> Plan:
    request_hash = _request_hash(payload, "create")
    now = datetime.now(UTC)
    plan_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        generated = _generate_proposal(session, auth, payload.period_start, payload.period_end, now)
        proposal = generated.proposal

        # Policy version is a plain snapshot for now (Task 1's policy v1);
        # a real join to the workspace's active policy version is a Task 1
        # extension point, not something this task needs to introduce.
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO plans (
                        id, workspace_id, user_id, period_start, period_end, status,
                        policy_version, capacity_minutes, source_versions, conflicts,
                        unscheduled, created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :user_id, :period_start, :period_end, 'proposed',
                        1, :capacity_minutes, :source_versions, :conflicts,
                        :unscheduled, :actor_id, :actor_id, :now, :now, 1
                    )
                    RETURNING {_PLAN_FIELDS}
                    """
                ),
                {
                    "id": plan_id,
                    "workspace_id": auth.workspace_id,
                    "user_id": auth.user_id,
                    "period_start": payload.period_start,
                    "period_end": payload.period_end,
                    "capacity_minutes": proposal.capacity_minutes,
                    "source_versions": dumps(generated.fingerprint),
                    "conflicts": dumps(
                        [
                            {
                                "code": c.code,
                                "detail": c.detail,
                                "source_type": c.source_type,
                                "source_id": str(c.source_id) if c.source_id else None,
                            }
                            for c in proposal.conflicts
                        ]
                    ),
                    "unscheduled": dumps(
                        [
                            {
                                "source_type": u.source_type,
                                "source_id": str(u.source_id) if u.source_id else None,
                                "label": u.label,
                                "reason": u.reason,
                            }
                            for u in proposal.unscheduled
                        ]
                    ),
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )

        if proposal.blocks:
            session.execute(
                text(
                    """
                    INSERT INTO plan_blocks (
                        id, workspace_id, plan_id, source_type, source_id,
                        starts_at, ends_at, status, rationale, is_default_effort,
                        created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :plan_id, :source_type, :source_id,
                        :starts_at, :ends_at, 'proposed', :rationale, :is_default_effort,
                        :now, :now
                    )
                    """
                ),
                [
                    {
                        "id": uuid4(),
                        "workspace_id": auth.workspace_id,
                        "plan_id": plan_id,
                        "source_type": b.source_type,
                        "source_id": b.source_id,
                        "starts_at": b.starts_at,
                        "ends_at": b.ends_at,
                        "rationale": b.rationale,
                        "is_default_effort": b.is_default_effort,
                        "now": now,
                    }
                    for b in proposal.blocks
                ],
            )

        response = _row_to_plan(session, auth, dict(row))
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
                        :id, :workspace_id, 'plan.proposed', 'plan', :aggregate_id,
                        1, :actor_id, :request_id, :correlation_id,
                        ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "aggregate_id": plan_id,
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
                        :event_id, :workspace_id, 'plan.proposed.v1', 1,
                        :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                    )
                    """
                ),
                {
                    "event_id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "correlation_id": correlation_id,
                    "payload": dumps({"plan_id": str(plan_id)}),
                    "occurred_at": now,
                },
            )
        except SQLAlchemyError:
            record_audit_outbox_failure("planning")
            raise
        queue_lifecycle_event(session, "plan", "plan.proposed", "allowed")

        session.execute(
            text(
                """
                INSERT INTO idempotency_records (
                    workspace_id, actor_id, key, request_hash, response_status,
                    response_body, created_at, expires_at
                ) VALUES (
                    :workspace_id, :actor_id, :key, :request_hash, 201,
                    CAST(:response_body AS jsonb), :created_at, :expires_at
                )
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": idempotency_key,
                "request_hash": request_hash,
                "response_body": dumps(response.model_dump(mode="json")),
                "created_at": now,
                "expires_at": now + timedelta(days=365),
            },
        )
        return response


def _encode_cursor(created_at: datetime, plan_id: UUID) -> str:
    payload = dumps(
        {"created_at": created_at.isoformat(), "id": str(plan_id)}, separators=(",", ":")
    ).encode()
    secret = get_settings().session_secret.encode()
    signature = new(secret, payload, "sha256").hexdigest().encode()
    return urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = urlsafe_b64decode(padded.encode())
        payload, signature = raw.rsplit(b".", 1)
        expected = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest()
        if not compare_digest(signature.decode(), expected):
            raise ValueError
        decoded = loads(payload)
        return datetime.fromisoformat(decoded["created_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="CURSOR_INVALID") from exc


@router.get("", response_model=PlanList)
def list_plans(
    auth: AuthDep,
    session: SessionDep,
    status_filter: Annotated[PlanStatus | None, Query(alias="status")] = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> PlanList:
    clauses = ["workspace_id = :workspace_id", "user_id = :user_id"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "user_id": auth.user_id,
        "limit": limit + 1,
    }
    if status_filter:
        clauses.append("status = :status")
        params["status"] = status_filter
    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(created_at, id) < (:cursor_created_at, :cursor_id)")
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id

    rows = (
        session.execute(
            text(
                f"""
                SELECT {_PLAN_FIELDS} FROM plans
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
    has_more = len(rows) > limit
    page = rows[:limit]
    items = [_row_to_plan(session, auth, dict(row)) for row in page]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["created_at"], last["id"])
    return PlanList(items=items, next_cursor=next_cursor)


@router.get("/{plan_id}", response_model=Plan)
def get_plan(plan_id: UUID, auth: AuthDep, session: SessionDep) -> Plan:
    row = (
        session.execute(
            text(
                f"SELECT {_PLAN_FIELDS} FROM plans "
                "WHERE workspace_id = :workspace_id AND id = :plan_id"
            ),
            {"workspace_id": auth.workspace_id, "plan_id": plan_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="PLAN_NOT_FOUND")
    return _row_to_plan(session, auth, dict(row))


# --------------------------------------------------------------------------
# Task 6: acceptance, manual retirement, replan diff, block editing.
# plan_blocks carries no version of its own (see migration 0026's module
# docstring) -- moving/removing a block bumps the *plan's* version in
# place; replanning instead creates a brand-new plan row, superseding the
# old one, mirroring waiting_links'/knowledge_claims' supersede pattern.
# --------------------------------------------------------------------------


def _write_plan_event(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    plan_id: UUID,
    version: int,
    now: datetime,
    *,
    emit_outbox: bool = True,
) -> None:
    """``emit_outbox=False`` for block move/remove: those are audit-only,
    matching attention.py's dismiss/defer/restore precedent (audit_events
    written, no event_outbox row, no catalog entry) -- Task 6's plan only
    names ``plan.accepted.v1``/``plan.superseded.v1`` as new catalog
    events, not a block-level one.
    """
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
                    :id, :workspace_id, :event_type, 'plan', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": plan_id,
                "aggregate_version": version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "occurred_at": now,
            },
        )
        if not emit_outbox:
            queue_lifecycle_event(session, "plan", event_type, "allowed")
            return
        session.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_id, workspace_id, event_type, event_version,
                    correlation_id, payload, occurred_at, attempt_count
                ) VALUES (
                    :event_id, :workspace_id, :event_type_v1, 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type_v1": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps({"plan_id": str(plan_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("planning")
        raise
    queue_lifecycle_event(session, "plan", event_type, "allowed")


def _get_plan_for_update(
    session: Session, auth: AuthContext, plan_id: UUID
) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                f"SELECT {_PLAN_FIELDS} FROM plans "
                "WHERE workspace_id = :workspace_id AND id = :plan_id FOR UPDATE"
            ),
            {"workspace_id": auth.workspace_id, "plan_id": plan_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


@router.post("/{plan_id}/accept", response_model=Plan)
def accept_plan(
    plan_id: UUID,
    payload: PlanAccept,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> Plan:
    """Explicit, idempotent, audited human confirmation. Never writes an
    external calendar (this codebase has no such integration to write to
    in the first place -- acceptance only updates ECC's own planning
    state, per PLANNING-CONTRACT.md's Proposal and acceptance section).
    """
    request_hash = _request_hash(payload, f"accept:{plan_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        current = _get_plan_for_update(session, auth, plan_id)
        if current is None:
            raise HTTPException(status_code=404, detail="PLAN_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if current["status"] != "proposed":
            raise HTTPException(status_code=409, detail="PLAN_NOT_PROPOSED")
        if _is_stale(session, auth, current):
            raise HTTPException(status_code=409, detail="STALE_PLAN")

        row = (
            session.execute(
                text(
                    f"""
                    UPDATE plans SET status = 'accepted', accepted_at = :now,
                        updated_by = :actor_id, updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :plan_id
                    RETURNING {_PLAN_FIELDS}
                    """
                ),
                {
                    "now": now,
                    "actor_id": auth.user_id,
                    "workspace_id": auth.workspace_id,
                    "plan_id": plan_id,
                },
            )
            .mappings()
            .one()
        )
        session.execute(
            text(
                "UPDATE plan_blocks SET status = 'accepted', updated_at = :now "
                "WHERE workspace_id = :workspace_id AND plan_id = :plan_id"
            ),
            {"now": now, "workspace_id": auth.workspace_id, "plan_id": plan_id},
        )
        response = _row_to_plan(session, auth, dict(row))
        _write_plan_event(session, auth, request, "plan.accepted", plan_id, row["version"], now)
        _store_idempotent_plan(session, auth, idempotency_key, request_hash, response, now)
        return response


@router.post("/{plan_id}/supersede", response_model=Plan)
def supersede_plan(
    plan_id: UUID,
    payload: PlanAccept,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> Plan:
    """Manual retirement with no replacement (distinct from ``/propose``,
    which supersedes *and* creates a new proposal in the same call)."""
    request_hash = _request_hash(payload, f"supersede:{plan_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        current = _get_plan_for_update(session, auth, plan_id)
        if current is None:
            raise HTTPException(status_code=404, detail="PLAN_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if current["status"] not in ("proposed", "accepted"):
            raise HTTPException(status_code=409, detail="PLAN_NOT_ACTIVE")

        row = (
            session.execute(
                text(
                    f"""
                    UPDATE plans SET status = 'superseded',
                        updated_by = :actor_id, updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :plan_id
                    RETURNING {_PLAN_FIELDS}
                    """
                ),
                {
                    "now": now,
                    "actor_id": auth.user_id,
                    "workspace_id": auth.workspace_id,
                    "plan_id": plan_id,
                },
            )
            .mappings()
            .one()
        )
        response = _row_to_plan(session, auth, dict(row))
        _write_plan_event(session, auth, request, "plan.superseded", plan_id, row["version"], now)
        _store_idempotent_plan(session, auth, idempotency_key, request_hash, response, now)
        return response


def _diff_blocks(
    old_blocks: list[dict[str, Any]],
    new_blocks: list[PlanBlockOutput],
    new_unscheduled: list[UnscheduledOutput],
) -> list[PlanDiffEntry]:
    """Plain per-block-id comparison, no new dependency (per Task 6's plan:
    "replan and the diff computation... no new dependency")."""

    def key(source_type: str, source_id: UUID | None) -> tuple[str, str]:
        return source_type, str(source_id)

    old_by_key = {key(b["source_type"], b["source_id"]): b for b in old_blocks}
    new_by_key = {key(b.source_type, b.source_id): b for b in new_blocks}
    newly_conflicted_keys = {
        key(u.source_type, u.source_id)
        for u in new_unscheduled
        if key(u.source_type, u.source_id) in old_by_key
    }

    entries: list[PlanDiffEntry] = []
    for k, old_block in old_by_key.items():
        if k in newly_conflicted_keys:
            entries.append(
                PlanDiffEntry(
                    source_type=old_block["source_type"],
                    source_id=old_block["source_id"],
                    label=old_block.get("rationale", ""),
                    change="newly_conflicted",
                )
            )
        elif k not in new_by_key:
            entries.append(
                PlanDiffEntry(
                    source_type=old_block["source_type"],
                    source_id=old_block["source_id"],
                    label=old_block.get("rationale", ""),
                    change="removed",
                )
            )
        else:
            new_block = new_by_key[k]
            moved = (
                old_block["starts_at"] != new_block.starts_at
                or old_block["ends_at"] != new_block.ends_at
            )
            entries.append(
                PlanDiffEntry(
                    source_type=new_block.source_type,
                    source_id=new_block.source_id,
                    label=new_block.label,
                    change="moved" if moved else "unchanged",
                )
            )
    for k, new_block in new_by_key.items():
        if k not in old_by_key:
            entries.append(
                PlanDiffEntry(
                    source_type=new_block.source_type,
                    source_id=new_block.source_id,
                    label=new_block.label,
                    change="added",
                )
            )
    return entries


@router.post("/{plan_id}/propose", response_model=Plan, status_code=status.HTTP_201_CREATED)
def replan(
    plan_id: UUID,
    payload: PlanAccept,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> Plan:
    """Replanning: source changes mark a proposal stale; this creates a
    *new* proposal over the same period and supersedes the old one, never
    silently rewriting it (PLANNING-CONTRACT.md's Replanning section) --
    unlike block move/remove, which edit the same plan in place.
    """
    request_hash = _request_hash(payload, f"propose:{plan_id}")
    now = datetime.now(UTC)
    new_plan_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        old = _get_plan_for_update(session, auth, plan_id)
        if old is None:
            raise HTTPException(status_code=404, detail="PLAN_NOT_FOUND")
        if old["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": old["version"]},
            )
        if old["status"] not in ("proposed", "accepted"):
            raise HTTPException(status_code=409, detail="PLAN_NOT_ACTIVE")

        old_blocks = (
            session.execute(
                text(
                    f"SELECT {_BLOCK_FIELDS} FROM plan_blocks "
                    "WHERE workspace_id = :workspace_id AND plan_id = :plan_id"
                ),
                {"workspace_id": auth.workspace_id, "plan_id": plan_id},
            )
            .mappings()
            .all()
        )

        generated = _generate_proposal(session, auth, old["period_start"], old["period_end"], now)
        proposal = generated.proposal

        new_row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO plans (
                        id, workspace_id, user_id, period_start, period_end, status,
                        policy_version, capacity_minutes, source_versions, conflicts,
                        unscheduled, created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :user_id, :period_start, :period_end, 'proposed',
                        1, :capacity_minutes, :source_versions, :conflicts,
                        :unscheduled, :actor_id, :actor_id, :now, :now, 1
                    )
                    RETURNING {_PLAN_FIELDS}
                    """
                ),
                {
                    "id": new_plan_id,
                    "workspace_id": auth.workspace_id,
                    "user_id": auth.user_id,
                    "period_start": old["period_start"],
                    "period_end": old["period_end"],
                    "capacity_minutes": proposal.capacity_minutes,
                    "source_versions": dumps(generated.fingerprint),
                    "conflicts": dumps(
                        [
                            {
                                "code": c.code,
                                "detail": c.detail,
                                "source_type": c.source_type,
                                "source_id": str(c.source_id) if c.source_id else None,
                            }
                            for c in proposal.conflicts
                        ]
                    ),
                    "unscheduled": dumps(
                        [
                            {
                                "source_type": u.source_type,
                                "source_id": str(u.source_id) if u.source_id else None,
                                "label": u.label,
                                "reason": u.reason,
                            }
                            for u in proposal.unscheduled
                        ]
                    ),
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        if proposal.blocks:
            session.execute(
                text(
                    """
                    INSERT INTO plan_blocks (
                        id, workspace_id, plan_id, source_type, source_id,
                        starts_at, ends_at, status, rationale, is_default_effort,
                        created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :plan_id, :source_type, :source_id,
                        :starts_at, :ends_at, 'proposed', :rationale, :is_default_effort,
                        :now, :now
                    )
                    """
                ),
                [
                    {
                        "id": uuid4(),
                        "workspace_id": auth.workspace_id,
                        "plan_id": new_plan_id,
                        "source_type": b.source_type,
                        "source_id": b.source_id,
                        "starts_at": b.starts_at,
                        "ends_at": b.ends_at,
                        "rationale": b.rationale,
                        "is_default_effort": b.is_default_effort,
                        "now": now,
                    }
                    for b in proposal.blocks
                ],
            )

        session.execute(
            text(
                """
                UPDATE plans SET status = 'superseded', superseded_by = :new_plan_id,
                    updated_by = :actor_id, updated_at = :now, version = version + 1
                WHERE workspace_id = :workspace_id AND id = :plan_id
                """
            ),
            {
                "new_plan_id": new_plan_id,
                "actor_id": auth.user_id,
                "now": now,
                "workspace_id": auth.workspace_id,
                "plan_id": plan_id,
            },
        )

        diff = _diff_blocks([dict(b) for b in old_blocks], proposal.blocks, proposal.unscheduled)
        response = _row_to_plan(session, auth, dict(new_row))
        response = response.model_copy(update={"diff": diff})

        _write_plan_event(session, auth, request, "plan.proposed", new_plan_id, 1, now)
        _write_plan_event(
            session, auth, request, "plan.superseded", plan_id, old["version"] + 1, now
        )
        _store_idempotent_plan(session, auth, idempotency_key, request_hash, response, now)
        return response


def _store_idempotent_plan(
    session: Session, auth: AuthContext, key: str, request_hash: str, response: Plan, now: datetime
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, 200,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": request_hash,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _blocks_overlap(
    candidate_start: datetime, candidate_end: datetime, other: dict[str, Any]
) -> bool:
    return bool(candidate_start < other["ends_at"] and other["starts_at"] < candidate_end)


@router.post("/{plan_id}/blocks/{block_id}/move", response_model=Plan)
def move_block(
    plan_id: UUID,
    block_id: UUID,
    payload: BlockMove,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> Plan:
    """Moving a block produces a new plan *version* (edits the same plan
    row in place), not a new plan -- plan_blocks has no version of its
    own, matching migration 0026's "the parent plan is the versioned
    unit" design. Only a 'proposed' plan may be edited: "Accepted plans
    are not silently rewritten" (PLANNING-CONTRACT.md).
    """
    request_hash = _request_hash(payload, f"move:{plan_id}:{block_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        current = _get_plan_for_update(session, auth, plan_id)
        if current is None:
            raise HTTPException(status_code=404, detail="PLAN_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if current["status"] != "proposed":
            raise HTTPException(status_code=409, detail="PLAN_NOT_EDITABLE")

        other_blocks = (
            session.execute(
                text(
                    f"SELECT {_BLOCK_FIELDS} FROM plan_blocks "
                    "WHERE workspace_id = :workspace_id AND plan_id = :plan_id AND id <> :block_id"
                ),
                {"workspace_id": auth.workspace_id, "plan_id": plan_id, "block_id": block_id},
            )
            .mappings()
            .all()
        )
        if any(_blocks_overlap(payload.starts_at, payload.ends_at, dict(b)) for b in other_blocks):
            raise HTTPException(status_code=422, detail="BLOCK_OVERLAP")

        updated = session.execute(
            text(
                """
                UPDATE plan_blocks SET starts_at = :starts_at, ends_at = :ends_at, updated_at = :now
                WHERE workspace_id = :workspace_id AND plan_id = :plan_id AND id = :block_id
                RETURNING id
                """
            ),
            {
                "starts_at": payload.starts_at,
                "ends_at": payload.ends_at,
                "now": now,
                "workspace_id": auth.workspace_id,
                "plan_id": plan_id,
                "block_id": block_id,
            },
        ).one_or_none()
        if updated is None:
            raise HTTPException(status_code=404, detail="PLAN_BLOCK_NOT_FOUND")

        row = (
            session.execute(
                text(
                    f"""
                    UPDATE plans
                    SET updated_by = :actor_id, updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :plan_id
                    RETURNING {_PLAN_FIELDS}
                    """
                ),
                {
                    "actor_id": auth.user_id,
                    "now": now,
                    "workspace_id": auth.workspace_id,
                    "plan_id": plan_id,
                },
            )
            .mappings()
            .one()
        )
        response = _row_to_plan(session, auth, dict(row))
        _write_plan_event(
            session,
            auth,
            request,
            "plan.block_moved",
            plan_id,
            row["version"],
            now,
            emit_outbox=False,
        )
        _store_idempotent_plan(session, auth, idempotency_key, request_hash, response, now)
        return response


@router.post("/{plan_id}/blocks/{block_id}/remove", response_model=Plan)
def remove_block(
    plan_id: UUID,
    block_id: UUID,
    payload: BlockRemove,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> Plan:
    """Removing a block, like moving one, edits the same plan row in place
    and bumps its version -- only while the plan is still 'proposed'."""
    request_hash = _request_hash(payload, f"remove:{plan_id}:{block_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        current = _get_plan_for_update(session, auth, plan_id)
        if current is None:
            raise HTTPException(status_code=404, detail="PLAN_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if current["status"] != "proposed":
            raise HTTPException(status_code=409, detail="PLAN_NOT_EDITABLE")

        deleted = session.execute(
            text(
                """
                DELETE FROM plan_blocks
                WHERE workspace_id = :workspace_id AND plan_id = :plan_id AND id = :block_id
                RETURNING id
                """
            ),
            {"workspace_id": auth.workspace_id, "plan_id": plan_id, "block_id": block_id},
        ).one_or_none()
        if deleted is None:
            raise HTTPException(status_code=404, detail="PLAN_BLOCK_NOT_FOUND")

        row = (
            session.execute(
                text(
                    f"""
                    UPDATE plans
                    SET updated_by = :actor_id, updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :plan_id
                    RETURNING {_PLAN_FIELDS}
                    """
                ),
                {
                    "actor_id": auth.user_id,
                    "now": now,
                    "workspace_id": auth.workspace_id,
                    "plan_id": plan_id,
                },
            )
            .mappings()
            .one()
        )
        response = _row_to_plan(session, auth, dict(row))
        _write_plan_event(
            session,
            auth,
            request,
            "plan.block_removed",
            plan_id,
            row["version"],
            now,
            emit_outbox=False,
        )
        _store_idempotent_plan(session, auth, idempotency_key, request_hash, response, now)
        return response
