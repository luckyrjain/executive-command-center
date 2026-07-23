import time as time_module
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
    record_ranking,
)

from .policy import AttentionPolicy, get_active_policy

router = APIRouter(prefix="/api/v1/attention", tags=["attention"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]
# waiting_link/risk_review/meeting are scored starting Task 2/3/7 (Phase 3);
# widened here per Task 1 so `attention_items.entity_type` (an unconstrained
# String(32), verified against migration 0006 -- no CHECK to change) accepts
# them without a later migration.
EntityType = Literal["task", "commitment", "risk", "waiting_link", "risk_review", "meeting"]
FeedbackTargetType = Literal["attention_item"]
FeedbackLabel = Literal["useful", "not_useful", "incorrect"]


class AttentionItem(BaseModel):
    id: UUID
    entity_type: EntityType
    entity_id: UUID
    source_entity_version: int
    score: int
    confidence: float
    factors: list[dict[str, Any]]
    explanation: str
    generated_at: datetime
    expires_at: datetime
    pinned: bool
    dismissed_at: datetime | None
    dismissed_entity_version: int | None
    deferred_until: datetime | None
    policy_version: int
    override_reason: str | None


class AttentionList(BaseModel):
    items: list[AttentionItem]


class AttentionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deferred_until: datetime | None = None
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("deferred_until")
    @classmethod
    def validate_deferred_until(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("deferred_until must include a timezone offset")
        return value


class AttentionFeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: FeedbackLabel
    reason: str | None = Field(default=None, max_length=2000)


class AttentionFeedback(BaseModel):
    id: UUID
    target_type: FeedbackTargetType
    target_id: UUID
    label: FeedbackLabel
    reason: str | None
    policy_version: int
    created_at: datetime


def _workspace_day(session: Session, auth: AuthContext, now: datetime) -> tuple[date, datetime]:
    timezone = session.execute(
        text("SELECT timezone FROM workspaces WHERE id = :workspace_id"),
        {"workspace_id": auth.workspace_id},
    ).scalar_one()
    zone = ZoneInfo(timezone)
    local_now = now.astimezone(zone)
    local_day = local_now.date()
    end = datetime.combine(local_day + timedelta(days=1), time.min, zone).astimezone(UTC)
    return local_day, end


def _due_points(
    policy: AttentionPolicy,
    due_date: date | None,
    due_at: datetime | None,
    today: date,
    now: datetime,
) -> tuple[int, str | None]:
    if due_at is not None:
        delta = due_at - now
        if delta.total_seconds() < 0:
            return policy.due_overdue_points, "overdue"
        if delta <= timedelta(hours=48):
            return policy.due_48h_points, "due_48h"
    if due_date is not None:
        if due_date < today:
            return policy.due_overdue_points, "overdue"
        if due_date == today:
            return policy.due_today_points, "due_today"
    return 0, None


def _factor(code: str, label: str, points: int, source_field: str) -> dict[str, Any]:
    return {"code": code, "label": label, "points": points, "source_field": source_field}


def _recency_and_deferral_factors(
    policy: AttentionPolicy, row: dict[str, Any], now: datetime
) -> list[dict[str, Any]]:
    """New in Phase 3, additive to policy v1: ATTENTION-MODEL.md's
    ``bounded_recency`` and ``bounded_deferral_penalty`` terms. Both are
    small and capped by construction (a fixed single-application point
    value, not a scaling function) so neither can dominate a score --
    "bounded" is enforced by never applying more than once per scoring
    pass, not by a separate clamp step.
    """
    factors: list[dict[str, Any]] = []
    created_at = row.get("created_at")
    if created_at is not None:
        age = now - created_at
        if age <= timedelta(hours=policy.recently_created_window_hours):
            factors.append(
                _factor(
                    "recently_created",
                    "Recently created",
                    policy.recently_created_points,
                    "created_at",
                )
            )
    prior_deferred_until = row.get("prior_deferred_until")
    if prior_deferred_until is not None and prior_deferred_until <= now:
        factors.append(
            _factor(
                "previously_deferred",
                "Previously deferred",
                policy.previously_deferred_penalty,
                "deferred_until",
            )
        )
    return factors


def _score_task(
    row: dict[str, Any], today: date, now: datetime, policy: AttentionPolicy
) -> tuple[int, float, list[dict[str, Any]]]:
    factors: list[dict[str, Any]] = []
    priority = policy.task_priority_points[row["manual_priority"]]
    factors.append(
        _factor(
            "manual_priority",
            f"Manual priority {row['manual_priority']}",
            priority,
            "manual_priority",
        )
    )
    due_points, due_code = _due_points(policy, row["due_date"], row["due_at"], today, now)
    if due_points:
        factors.append(_factor(due_code or "due", "Due timing", due_points, "due_date,due_at"))
    if row["pinned"]:
        factors.append(_factor("pinned", "Explicitly pinned", policy.pinned_points, "pinned"))
    if row["blocked_on_person_id"] is not None:
        factors.append(
            _factor(
                "waiting_on",
                "Waiting on another person",
                policy.waiting_on_points,
                "blocked_on_person_id",
            )
        )
    if row["status"] == "blocked":
        factors.append(_factor("blocked", "Task is blocked", policy.blocked_points, "status"))
    age = now - row["updated_at"]
    if age >= timedelta(days=14):
        factors.append(
            _factor("stale_14d", "No movement for 14 days", policy.stale_14d_points, "updated_at")
        )
    elif age >= timedelta(days=7):
        factors.append(
            _factor("stale_7d", "No movement for 7 days", policy.stale_7d_points, "updated_at")
        )
    factors.extend(_recency_and_deferral_factors(policy, row, now))
    confidence = (
        policy.task_confidence_with_due_date
        if row["due_date"] is not None
        else policy.task_confidence_without_due_date
    )
    cap = policy.cap_pinned if row["pinned"] else policy.cap_unpinned
    score = min(cap, max(0, sum(item["points"] for item in factors)))
    return score, confidence, factors


def _score_commitment(
    row: dict[str, Any], today: date, now: datetime, policy: AttentionPolicy
) -> tuple[int, float, list[dict[str, Any]]]:
    factors: list[dict[str, Any]] = []
    importance = policy.commitment_importance_points[row["importance"]]
    factors.append(
        _factor("importance", f"Importance {row['importance']}", importance, "importance")
    )
    due_points, due_code = _due_points(policy, row["due_date"], row["due_at"], today, now)
    if due_points:
        factors.append(_factor(due_code or "due", "Due timing", due_points, "due_date,due_at"))
    if row["direction"] == "made_to_me":
        factors.append(
            _factor(
                "waiting_on", "Waiting on another person", policy.waiting_on_points, "direction"
            )
        )
    if row["pinned"]:
        factors.append(_factor("pinned", "Explicitly pinned", policy.pinned_points, "pinned"))
    factors.extend(_recency_and_deferral_factors(policy, row, now))
    confidence = (
        float(row["confidence"])
        if row["confidence"] is not None
        else policy.commitment_confidence_default
    )
    if row["due_date"] is not None:
        confidence = min(confidence, policy.commitment_confidence_due_date_cap)
    cap = policy.cap_pinned if row["pinned"] else policy.cap_unpinned
    score = min(cap, max(0, sum(item["points"] for item in factors)))
    return score, round(confidence, 2), factors


def _score_risk(
    row: dict[str, Any], now: datetime, policy: AttentionPolicy
) -> tuple[int, float, list[dict[str, Any]]]:
    factors: list[dict[str, Any]] = []
    impact = int(row["probability"]) * int(row["impact"])
    if impact >= policy.risk_impact_high_threshold:
        points = policy.risk_impact_high_points
    elif impact >= policy.risk_impact_medium_threshold:
        points = policy.risk_impact_medium_points
    elif impact >= policy.risk_impact_low_threshold:
        points = policy.risk_impact_low_points
    else:
        points = 0
    if points:
        factors.append(
            _factor("risk_impact", f"Risk impact {impact}", points, "probability,impact")
        )
    if row["review_at"] is not None:
        delta = row["review_at"] - now
        if delta.total_seconds() < 0:
            factors.append(
                _factor(
                    "review_overdue",
                    "Risk review overdue",
                    policy.review_overdue_points,
                    "review_at",
                )
            )
        elif delta <= timedelta(hours=48):
            factors.append(
                _factor(
                    "review_due_soon",
                    "Risk review due within 48 hours",
                    policy.review_due_soon_points,
                    "review_at",
                )
            )
    if row["pinned"]:
        factors.append(_factor("pinned", "Explicitly pinned", policy.pinned_points, "pinned"))
    factors.extend(_recency_and_deferral_factors(policy, row, now))
    cap = policy.cap_pinned if row["pinned"] else policy.cap_unpinned
    score = min(cap, max(0, sum(item["points"] for item in factors)))
    return score, policy.risk_confidence, factors


_WAITING_DIRECTION_POINTS_FRACTION = {
    "waiting_on_me": 1.0,
    "blocked_by": 1.0,
    "waiting_on_them": 1.0 / 3,
    "delegated": 0.0,
}


def _score_waiting(
    row: dict[str, Any], now: datetime, policy: AttentionPolicy
) -> tuple[int, float, list[dict[str, Any]]]:
    """New in Phase 3 Task 2: the fourth scored ``entity_type``, using the
    policy's reserved ``dependency_weight_cap`` (declared but inert in
    Task 1). ``waiting_on_me``/``blocked_by`` score at the full cap (my
    action is needed, or my own work can't proceed either way);
    ``waiting_on_them`` scores lower (visibility only, not blocking me);
    ``delegated`` contributes nothing (no longer my action). No pin
    concept for waiting links (not a column on ``waiting_links`` --
    Open decision 1 only defined pin as read-through from a *source*
    entity's own column, and a waiting link has no such column of its
    own), so the cap is always the unpinned one.
    """
    factors: list[dict[str, Any]] = []
    direction_points = round(
        policy.dependency_weight_cap * _WAITING_DIRECTION_POINTS_FRACTION[row["direction"]]
    )
    if direction_points:
        factors.append(
            _factor(
                "waiting_direction",
                f"Waiting: {row['direction'].replace('_', ' ')}",
                direction_points,
                "direction",
            )
        )
    expected_at = row["expected_at"]
    if expected_at is not None:
        delta = expected_at - now
        if delta.total_seconds() < 0:
            factors.append(
                _factor(
                    "overdue", "Expected timing passed", policy.due_overdue_points, "expected_at"
                )
            )
        elif delta <= timedelta(hours=48):
            factors.append(
                _factor("due_48h", "Expected within 48 hours", policy.due_48h_points, "expected_at")
            )
    age = now - row["since_at"]
    if age >= timedelta(days=14):
        factors.append(
            _factor("stale_14d", "Waiting for 14+ days", policy.stale_14d_points, "since_at")
        )
    elif age >= timedelta(days=7):
        factors.append(
            _factor("stale_7d", "Waiting for 7+ days", policy.stale_7d_points, "since_at")
        )
    score = min(policy.cap_unpinned, max(0, sum(item["points"] for item in factors)))
    return score, 1.0, factors


def _upsert_batch(
    session: Session,
    auth: AuthContext,
    entity_type: EntityType,
    rows: list[dict[str, Any]],
    scored: list[tuple[int, float, list[dict[str, Any]]]],
    now: datetime,
    expires_at: datetime,
    policy_version: int,
) -> None:
    """Upsert every ``rows``/``scored`` pair for one entity type in a single
    statement.

    Performance note: this used to be a Python ``for`` loop issuing one
    ``INSERT ... ON CONFLICT`` round trip per entity (see git history for the
    prior ``_upsert`` helper). At representative Phase 1 scale (10,000+
    eligible entities per type) that loop alone took multiple seconds per
    entity type -- measured directly at ~3s for 10,000 rows -- which blew
    past both the design doc's "ranking of 10,000 eligible entities below
    500 ms" budget and, combined across task/commitment/risk, the newly
    configured 5-second statement timeout (`backend/ecc/database.py`).
    Batching into one set-based statement per entity type (using
    ``unnest`` over parallel arrays) preserves the exact same per-row
    upsert/conflict semantics -- including the dismissed-state preservation
    logic -- while cutting the round-trip count from one-per-entity to one
    per entity type.

    ``override_reason`` is deliberately absent from the UPDATE SET list,
    matching ``deferred_until``'s existing behavior: both are user overrides
    set by the dismiss/defer mutations, not regenerated projections, so a
    regenerate call must never touch them.
    """
    if not rows:
        return
    entity_ids = [row["id"] for row in rows]
    versions = [row["version"] for row in rows]
    scores = [score for score, _, _ in scored]
    confidences = [confidence for _, confidence, _ in scored]
    factors_json = [dumps(factors) for _, _, factors in scored]
    explanations = [
        "; ".join(item["label"] for item in factors) or "No active priority factors"
        for _, _, factors in scored
    ]
    pinned = [bool(row["pinned"]) for row in rows]

    session.execute(
        text(
            """
            INSERT INTO attention_items (
                id, workspace_id, entity_type, entity_id, source_entity_version,
                score, confidence, factors, explanation, generated_at, expires_at, pinned,
                policy_version
            )
            SELECT gen_random_uuid(), :workspace_id, :entity_type,
                   t.entity_id, t.version, t.score, t.confidence,
                   t.factors::jsonb, t.explanation, :generated_at, :expires_at, t.pinned,
                   :policy_version
            FROM unnest(
                CAST(:entity_ids AS uuid[]),
                CAST(:versions AS bigint[]),
                CAST(:scores AS smallint[]),
                CAST(:confidences AS numeric[]),
                CAST(:factors AS text[]),
                CAST(:explanations AS text[]),
                CAST(:pinned AS boolean[])
            ) AS t(entity_id, version, score, confidence, factors, explanation, pinned)
            ON CONFLICT (workspace_id, entity_type, entity_id) DO UPDATE SET
                source_entity_version = EXCLUDED.source_entity_version,
                score = EXCLUDED.score,
                confidence = EXCLUDED.confidence,
                factors = EXCLUDED.factors,
                explanation = EXCLUDED.explanation,
                generated_at = EXCLUDED.generated_at,
                expires_at = EXCLUDED.expires_at,
                pinned = EXCLUDED.pinned,
                policy_version = EXCLUDED.policy_version,
                dismissed_at = CASE
                    WHEN attention_items.dismissed_entity_version = EXCLUDED.source_entity_version
                    THEN attention_items.dismissed_at ELSE NULL END,
                dismissed_entity_version = CASE
                    WHEN attention_items.dismissed_entity_version = EXCLUDED.source_entity_version
                    THEN attention_items.dismissed_entity_version ELSE NULL END
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "entity_type": entity_type,
            "entity_ids": entity_ids,
            "versions": versions,
            "scores": scores,
            "confidences": confidences,
            "factors": factors_json,
            "explanations": explanations,
            "pinned": pinned,
            "generated_at": now,
            "expires_at": expires_at,
            "policy_version": policy_version,
        },
    )


def _prior_deferred_until(
    session: Session, auth: AuthContext, entity_type: EntityType
) -> dict[UUID, datetime]:
    rows = session.execute(
        text(
            """
            SELECT entity_id, deferred_until FROM attention_items
            WHERE workspace_id = :workspace_id AND entity_type = :entity_type
              AND deferred_until IS NOT NULL
            """
        ),
        {"workspace_id": auth.workspace_id, "entity_type": entity_type},
    ).all()
    return {row[0]: row[1] for row in rows}


@router.post("/regenerate", response_model=AttentionList)
def regenerate_attention(auth: AuthDep, session: SessionDep, _csrf: CsrfDep) -> AttentionList:
    ranking_start = time_module.monotonic()
    now = datetime.now(UTC)
    policy = get_active_policy(1)
    with session.begin():
        today, day_end = _workspace_day(session, auth, now)
        expires_at = min(now + timedelta(minutes=30), day_end)
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"attention-regenerate:{auth.workspace_id}"},
        )
        tasks = (
            session.execute(
                text("""
                SELECT id, version, manual_priority, due_date, due_at, status,
                       blocked_on_person_id, pinned, updated_at, created_at
                FROM tasks
                WHERE workspace_id = :workspace_id AND archived_at IS NULL
                  AND status NOT IN ('completed','cancelled')
            """),
                {"workspace_id": auth.workspace_id},
            )
            .mappings()
            .all()
        )
        commitments = (
            session.execute(
                text("""
                SELECT id, version, importance, direction, due_date, due_at,
                       confidence, pinned, updated_at, created_at
                FROM commitments
                WHERE workspace_id = :workspace_id AND archived_at IS NULL
                  AND status NOT IN ('fulfilled','cancelled')
            """),
                {"workspace_id": auth.workspace_id},
            )
            .mappings()
            .all()
        )
        risks = (
            session.execute(
                text("""
                SELECT id, version, probability, impact, review_at, pinned, updated_at,
                       created_at
                FROM risks
                WHERE workspace_id = :workspace_id AND archived_at IS NULL
                  AND status <> 'closed'
            """),
                {"workspace_id": auth.workspace_id},
            )
            .mappings()
            .all()
        )
        waiting_links = (
            session.execute(
                text("""
                SELECT id, version, direction, since_at, expected_at, updated_at, created_at
                FROM waiting_links
                WHERE workspace_id = :workspace_id AND status = 'open'
            """),
                {"workspace_id": auth.workspace_id},
            )
            .mappings()
            .all()
        )
        session.execute(
            text(
                """
                DELETE FROM attention_items ai
                WHERE ai.workspace_id = :workspace_id
                  AND (
                    (ai.entity_type = 'task' AND NOT EXISTS (
                        SELECT 1 FROM tasks t
                        WHERE t.workspace_id = ai.workspace_id AND t.id = ai.entity_id
                          AND t.archived_at IS NULL
                          AND t.status NOT IN ('completed','cancelled')
                    ))
                    OR (ai.entity_type = 'commitment' AND NOT EXISTS (
                        SELECT 1 FROM commitments c
                        WHERE c.workspace_id = ai.workspace_id AND c.id = ai.entity_id
                          AND c.archived_at IS NULL
                          AND c.status NOT IN ('fulfilled','cancelled')
                    ))
                    OR (ai.entity_type = 'risk' AND NOT EXISTS (
                        SELECT 1 FROM risks r
                        WHERE r.workspace_id = ai.workspace_id AND r.id = ai.entity_id
                          AND r.archived_at IS NULL AND r.status <> 'closed'
                    ))
                    OR (ai.entity_type = 'waiting_link' AND NOT EXISTS (
                        SELECT 1 FROM waiting_links wl
                        WHERE wl.workspace_id = ai.workspace_id AND wl.id = ai.entity_id
                          AND wl.status = 'open'
                    ))
                  )
                """
            ),
            {"workspace_id": auth.workspace_id},
        )
        eligible_entity_types: tuple[EntityType, EntityType, EntityType] = (
            "task",
            "commitment",
            "risk",
        )
        prior_deferred_by_type = {
            entity_type: _prior_deferred_until(session, auth, entity_type)
            for entity_type in eligible_entity_types
        }
        task_rows = [
            {**dict(raw), "prior_deferred_until": prior_deferred_by_type["task"].get(raw["id"])}
            for raw in tasks
        ]
        commitment_rows = [
            {
                **dict(raw),
                "prior_deferred_until": prior_deferred_by_type["commitment"].get(raw["id"]),
            }
            for raw in commitments
        ]
        risk_rows = [
            {**dict(raw), "prior_deferred_until": prior_deferred_by_type["risk"].get(raw["id"])}
            for raw in risks
        ]
        # waiting_links has no pinned column of its own (Open decision 1:
        # pin is only ever read-through from a *source* entity's own
        # column); _upsert_batch's INSERT still writes an ``attention_items
        # .pinned`` value for every entity_type, so this stays permanently
        # False here rather than making pinned optional across every
        # scorer for one entity_type's sake.
        waiting_rows = [{**dict(raw), "pinned": False} for raw in waiting_links]
        _upsert_batch(
            session,
            auth,
            "task",
            task_rows,
            [_score_task(row, today, now, policy) for row in task_rows],
            now,
            expires_at,
            policy.version,
        )
        _upsert_batch(
            session,
            auth,
            "commitment",
            commitment_rows,
            [_score_commitment(row, today, now, policy) for row in commitment_rows],
            now,
            expires_at,
            policy.version,
        )
        _upsert_batch(
            session,
            auth,
            "risk",
            risk_rows,
            [_score_risk(row, now, policy) for row in risk_rows],
            now,
            expires_at,
            policy.version,
        )
        _upsert_batch(
            session,
            auth,
            "waiting_link",
            waiting_rows,
            [_score_waiting(row, now, policy) for row in waiting_rows],
            now,
            expires_at,
            policy.version,
        )
    record_ranking(
        time_module.monotonic() - ranking_start,
        len(tasks) + len(commitments) + len(risks) + len(waiting_links),
    )
    return list_attention(auth, session, 50)


_ATTENTION_ITEM_FIELDS = """
    ai.id, ai.entity_type, ai.entity_id, ai.source_entity_version, ai.score,
    ai.confidence, ai.factors, ai.explanation, ai.generated_at, ai.expires_at,
    ai.pinned, ai.dismissed_at, ai.dismissed_entity_version, ai.deferred_until,
    ai.policy_version, ai.override_reason
"""


@router.get("", response_model=AttentionList)
def list_attention(
    auth: AuthDep, session: SessionDep, limit: int = Query(default=50, ge=1, le=100)
) -> AttentionList:
    now = datetime.now(UTC)
    rows = (
        session.execute(
            text(f"""
            SELECT {_ATTENTION_ITEM_FIELDS}
            FROM attention_items ai
            JOIN workspaces w ON w.id = ai.workspace_id
            LEFT JOIN tasks t ON ai.entity_type = 'task'
                AND t.workspace_id = ai.workspace_id AND t.id = ai.entity_id
            LEFT JOIN commitments c ON ai.entity_type = 'commitment'
                AND c.workspace_id = ai.workspace_id AND c.id = ai.entity_id
            LEFT JOIN risks r ON ai.entity_type = 'risk'
                AND r.workspace_id = ai.workspace_id AND r.id = ai.entity_id
            LEFT JOIN waiting_links wl ON ai.entity_type = 'waiting_link'
                AND wl.workspace_id = ai.workspace_id AND wl.id = ai.entity_id
            WHERE ai.workspace_id = :workspace_id
              AND ai.expires_at > :now
              AND (ai.dismissed_at IS NULL
                   OR ai.dismissed_entity_version <> ai.source_entity_version)
              AND (ai.deferred_until IS NULL OR ai.deferred_until <= :now)
            ORDER BY ai.pinned DESC, ai.score DESC,
              COALESCE(
                t.due_at,
                (t.due_date::timestamp + time '23:59:59') AT TIME ZONE w.timezone,
                c.due_at,
                (c.due_date::timestamp + time '23:59:59') AT TIME ZONE w.timezone,
                r.review_at,
                wl.expected_at
              ) ASC NULLS LAST,
              CASE
                WHEN t.manual_priority = 'critical' THEN 4
                WHEN t.manual_priority = 'high' THEN 3
                WHEN t.manual_priority = 'medium' THEN 2
                WHEN t.manual_priority = 'low' THEN 1
                ELSE 0
              END DESC,
              COALESCE(t.created_at, c.created_at, r.created_at, wl.created_at) ASC,
              ai.entity_id ASC
            LIMIT :limit
        """),
            {"workspace_id": auth.workspace_id, "now": now, "limit": limit},
        )
        .mappings()
        .all()
    )
    return AttentionList(items=[AttentionItem.model_validate(dict(row)) for row in rows])


@router.get("/{item_id}", response_model=AttentionItem)
def get_attention_item(item_id: UUID, auth: AuthDep, session: SessionDep) -> AttentionItem:
    row = (
        session.execute(
            text(f"""
            SELECT {_ATTENTION_ITEM_FIELDS}
            FROM attention_items ai
            WHERE ai.workspace_id = :workspace_id AND ai.id = :item_id
            """),
            {"workspace_id": auth.workspace_id, "item_id": item_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="ATTENTION_ITEM_NOT_FOUND")
    return AttentionItem.model_validate(dict(row))


def _mutate_attention(
    item_id: UUID,
    action: str,
    payload: AttentionAction,
    request: Request,
    auth: AuthContext,
    session: Session,
) -> AttentionItem:
    now = datetime.now(UTC)
    with session.begin():
        row = (
            session.execute(
                text(f"""
                SELECT {_ATTENTION_ITEM_FIELDS}
                FROM attention_items ai
                WHERE ai.workspace_id = :workspace_id AND ai.id = :item_id
                FOR UPDATE
            """),
                {"workspace_id": auth.workspace_id, "item_id": item_id},
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="ATTENTION_ITEM_NOT_FOUND")
        if action == "dismiss":
            if (
                row["dismissed_at"] is not None
                and row["dismissed_entity_version"] == row["source_entity_version"]
            ):
                return AttentionItem.model_validate(dict(row))
            values = {
                "dismissed_at": now,
                "dismissed_entity_version": row["source_entity_version"],
                "override_reason": payload.reason,
            }
        elif action == "defer":
            if payload.deferred_until is None or payload.deferred_until <= now:
                raise HTTPException(status_code=422, detail="DEFER_UNTIL_MUST_BE_FUTURE")
            if row["deferred_until"] == payload.deferred_until:
                return AttentionItem.model_validate(dict(row))
            values = {"deferred_until": payload.deferred_until, "override_reason": payload.reason}
        else:
            if (
                row["dismissed_at"] is None
                and row["dismissed_entity_version"] is None
                and row["deferred_until"] is None
            ):
                return AttentionItem.model_validate(dict(row))
            values = {
                "dismissed_at": None,
                "dismissed_entity_version": None,
                "deferred_until": None,
                "override_reason": None,
            }
        updated = (
            session.execute(
                text(f"""
                UPDATE attention_items SET
                    dismissed_at = :dismissed_at,
                    dismissed_entity_version = :dismissed_entity_version,
                    deferred_until = :deferred_until,
                    override_reason = :override_reason
                WHERE workspace_id = :workspace_id AND id = :item_id
                RETURNING {_ATTENTION_ITEM_FIELDS.replace("ai.", "")}
            """),
                {
                    "workspace_id": auth.workspace_id,
                    "item_id": item_id,
                    "dismissed_at": values.get("dismissed_at", row["dismissed_at"]),
                    "dismissed_entity_version": values.get(
                        "dismissed_entity_version", row["dismissed_entity_version"]
                    ),
                    "deferred_until": values.get("deferred_until", row["deferred_until"]),
                    "override_reason": values.get("override_reason", row["override_reason"]),
                },
            )
            .mappings()
            .one()
        )
        request_id = UUID(request.state.request_id)
        correlation_id = UUID(request.state.correlation_id)
        event_type = f"attention_item.{action}"
        try:
            session.execute(
                text("""
                    INSERT INTO audit_events (
                        id, workspace_id, event_type, aggregate_type, aggregate_id,
                        aggregate_version, actor_id, request_id, correlation_id,
                        changed_fields, authorization_result, source, metadata, occurred_at
                    ) VALUES (
                        :id, :workspace_id, :event_type, 'attention_item', :aggregate_id,
                        :aggregate_version, :actor_id, :request_id, :correlation_id,
                        :changed_fields, 'allowed', 'user', '{}'::jsonb, :occurred_at
                    )
                """),
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "event_type": event_type,
                    "aggregate_id": item_id,
                    "aggregate_version": row["source_entity_version"],
                    "actor_id": auth.user_id,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "changed_fields": (
                        ["dismissed_at", "dismissed_entity_version", "override_reason"]
                        if action == "dismiss"
                        else ["deferred_until", "override_reason"]
                        if action == "defer"
                        else [
                            "dismissed_at",
                            "dismissed_entity_version",
                            "deferred_until",
                            "override_reason",
                        ]
                    ),
                    "occurred_at": now,
                },
            )
        except SQLAlchemyError:
            record_audit_outbox_failure("attention")
            raise
        queue_lifecycle_event(session, "attention_item", event_type, "allowed")
    return AttentionItem.model_validate(dict(updated))


@router.post("/{item_id}/dismiss", response_model=AttentionItem)
def dismiss_attention(
    item_id: UUID,
    payload: AttentionAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> AttentionItem:
    return _mutate_attention(item_id, "dismiss", payload, request, auth, session)


@router.post("/{item_id}/defer", response_model=AttentionItem)
def defer_attention(
    item_id: UUID,
    payload: AttentionAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> AttentionItem:
    return _mutate_attention(item_id, "defer", payload, request, auth, session)


@router.post("/{item_id}/restore", response_model=AttentionItem)
def restore_attention(
    item_id: UUID,
    payload: AttentionAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> AttentionItem:
    return _mutate_attention(item_id, "restore", payload, request, auth, session)


def _feedback_request_hash(payload: AttentionFeedbackCreate) -> str:
    material = payload.model_dump(mode="json")
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _load_cached_feedback(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> AttentionFeedback | None:
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
        record_idempotency_conflict("attention")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return AttentionFeedback.model_validate(row["response_body"])


@router.post("/{item_id}/feedback", response_model=AttentionFeedback, status_code=201)
def record_attention_feedback(
    item_id: UUID,
    payload: AttentionFeedbackCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> AttentionFeedback:
    request_hash = _feedback_request_hash(payload)
    now = datetime.now(UTC)
    feedback_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached_feedback(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        item = (
            session.execute(
                text(
                    "SELECT id, policy_version FROM attention_items "
                    "WHERE workspace_id = :workspace_id AND id = :item_id"
                ),
                {"workspace_id": auth.workspace_id, "item_id": item_id},
            )
            .mappings()
            .one_or_none()
        )
        if item is None:
            raise HTTPException(status_code=404, detail="ATTENTION_ITEM_NOT_FOUND")
        row = (
            session.execute(
                text(
                    """
                    INSERT INTO attention_feedback (
                        id, workspace_id, target_type, target_id, label, reason,
                        actor_id, policy_version, created_at
                    ) VALUES (
                        :id, :workspace_id, 'attention_item', :target_id, :label, :reason,
                        :actor_id, :policy_version, :created_at
                    )
                    RETURNING id, target_type, target_id, label, reason, policy_version,
                              created_at
                    """
                ),
                {
                    "id": feedback_id,
                    "workspace_id": auth.workspace_id,
                    "target_id": item_id,
                    "label": payload.label,
                    "reason": payload.reason,
                    "actor_id": auth.user_id,
                    "policy_version": item["policy_version"],
                    "created_at": now,
                },
            )
            .mappings()
            .one()
        )
        response = AttentionFeedback.model_validate(dict(row))
        request_id, correlation_id = (
            UUID(request.state.request_id),
            UUID(request.state.correlation_id),
        )
        try:
            session.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        id, workspace_id, event_type, aggregate_type, aggregate_id,
                        aggregate_version, actor_id, request_id, correlation_id,
                        changed_fields, authorization_result, source, metadata, occurred_at
                    ) VALUES (
                        :id, :workspace_id, 'attention_feedback.recorded', 'attention_feedback',
                        :aggregate_id, 1, :actor_id, :request_id, :correlation_id,
                        ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "aggregate_id": feedback_id,
                    "actor_id": auth.user_id,
                    "request_id": request_id,
                    "correlation_id": correlation_id,
                    "occurred_at": now,
                },
            )
        except SQLAlchemyError:
            record_audit_outbox_failure("attention")
            raise
        queue_lifecycle_event(session, "attention_item", "attention_feedback.recorded", "allowed")
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
