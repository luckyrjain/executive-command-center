from datetime import UTC, date, datetime, time, timedelta
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/attention", tags=["attention"])
SessionDep = Annotated[Session, Depends(get_session)]
EntityType = Literal["task", "commitment", "risk"]


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


class AttentionList(BaseModel):
    items: list[AttentionItem]


class AttentionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    deferred_until: datetime | None = None

    @field_validator("deferred_until")
    @classmethod
    def validate_deferred_until(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("deferred_until must include a timezone offset")
        return value


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
    due_date: date | None, due_at: datetime | None, today: date, now: datetime
) -> tuple[int, str | None]:
    if due_at is not None:
        delta = due_at - now
        if delta.total_seconds() < 0:
            return 35, "overdue"
        if delta <= timedelta(hours=48):
            return 15, "due_48h"
    if due_date is not None:
        if due_date < today:
            return 35, "overdue"
        if due_date == today:
            return 25, "due_today"
    return 0, None


def _factor(code: str, label: str, points: int, source_field: str) -> dict[str, Any]:
    return {"code": code, "label": label, "points": points, "source_field": source_field}


def _score_task(
    row: dict[str, Any], today: date, now: datetime
) -> tuple[int, float, list[dict[str, Any]]]:
    factors: list[dict[str, Any]] = []
    priority = {"critical": 35, "high": 25, "medium": 15, "low": 5}[row["manual_priority"]]
    factors.append(
        _factor(
            "manual_priority",
            f"Manual priority {row['manual_priority']}",
            priority,
            "manual_priority",
        )
    )
    due_points, due_code = _due_points(row["due_date"], row["due_at"], today, now)
    if due_points:
        factors.append(_factor(due_code or "due", "Due timing", due_points, "due_date,due_at"))
    if row["pinned"]:
        factors.append(_factor("pinned", "Explicitly pinned", 20, "pinned"))
    if row["blocked_on_person_id"] is not None:
        factors.append(
            _factor("waiting_on", "Waiting on another person", 8, "blocked_on_person_id")
        )
    if row["status"] == "blocked":
        factors.append(_factor("blocked", "Task is blocked", -12, "status"))
    age = now - row["updated_at"]
    if age >= timedelta(days=14):
        factors.append(_factor("stale_14d", "No movement for 14 days", 8, "updated_at"))
    elif age >= timedelta(days=7):
        factors.append(_factor("stale_7d", "No movement for 7 days", 4, "updated_at"))
    confidence = 0.8 if row["due_date"] is not None else 1.0
    score = min(100 if row["pinned"] else 95, max(0, sum(item["points"] for item in factors)))
    return score, confidence, factors


def _score_commitment(
    row: dict[str, Any], today: date, now: datetime
) -> tuple[int, float, list[dict[str, Any]]]:
    factors: list[dict[str, Any]] = []
    importance = {"critical": 25, "high": 18, "medium": 10, "low": 4}[row["importance"]]
    factors.append(
        _factor("importance", f"Importance {row['importance']}", importance, "importance")
    )
    due_points, due_code = _due_points(row["due_date"], row["due_at"], today, now)
    if due_points:
        factors.append(_factor(due_code or "due", "Due timing", due_points, "due_date,due_at"))
    if row["direction"] == "made_to_me":
        factors.append(_factor("waiting_on", "Waiting on another person", 8, "direction"))
    if row["pinned"]:
        factors.append(_factor("pinned", "Explicitly pinned", 20, "pinned"))
    confidence = float(row["confidence"]) if row["confidence"] is not None else 0.6
    if row["due_date"] is not None:
        confidence = min(confidence, 0.8)
    score = min(100 if row["pinned"] else 95, sum(item["points"] for item in factors))
    return score, round(confidence, 2), factors


def _score_risk(row: dict[str, Any], now: datetime) -> tuple[int, float, list[dict[str, Any]]]:
    factors: list[dict[str, Any]] = []
    impact = int(row["probability"]) * int(row["impact"])
    points = 25 if impact >= 20 else 15 if impact >= 12 else 8 if impact >= 6 else 0
    if points:
        factors.append(
            _factor("risk_impact", f"Risk impact {impact}", points, "probability,impact")
        )
    if row["review_at"] is not None:
        delta = row["review_at"] - now
        if delta.total_seconds() < 0:
            factors.append(_factor("review_overdue", "Risk review overdue", 35, "review_at"))
        elif delta <= timedelta(hours=48):
            factors.append(
                _factor("review_due_soon", "Risk review due within 48 hours", 15, "review_at")
            )
    if row["pinned"]:
        factors.append(_factor("pinned", "Explicitly pinned", 20, "pinned"))
    score = min(100 if row["pinned"] else 95, sum(item["points"] for item in factors))
    return score, 1.0, factors


def _upsert(
    session: Session,
    auth: AuthContext,
    entity_type: EntityType,
    row: dict[str, Any],
    score: int,
    confidence: float,
    factors: list[dict[str, Any]],
    now: datetime,
    expires_at: datetime,
) -> None:
    explanation = "; ".join(item["label"] for item in factors) or "No active priority factors"
    session.execute(
        text(
            """
            INSERT INTO attention_items (
                id, workspace_id, entity_type, entity_id, source_entity_version,
                score, confidence, factors, explanation, generated_at, expires_at, pinned
            ) VALUES (
                :id, :workspace_id, :entity_type, :entity_id, :version,
                :score, :confidence, CAST(:factors AS jsonb), :explanation,
                :generated_at, :expires_at, :pinned
            )
            ON CONFLICT (workspace_id, entity_type, entity_id) DO UPDATE SET
                source_entity_version = EXCLUDED.source_entity_version,
                score = EXCLUDED.score,
                confidence = EXCLUDED.confidence,
                factors = EXCLUDED.factors,
                explanation = EXCLUDED.explanation,
                generated_at = EXCLUDED.generated_at,
                expires_at = EXCLUDED.expires_at,
                pinned = EXCLUDED.pinned,
                dismissed_at = CASE
                    WHEN attention_items.dismissed_entity_version = EXCLUDED.source_entity_version
                    THEN attention_items.dismissed_at ELSE NULL END,
                dismissed_entity_version = CASE
                    WHEN attention_items.dismissed_entity_version = EXCLUDED.source_entity_version
                    THEN attention_items.dismissed_entity_version ELSE NULL END
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": auth.workspace_id,
            "entity_type": entity_type,
            "entity_id": row["id"],
            "version": row["version"],
            "score": score,
            "confidence": confidence,
            "factors": dumps(factors),
            "explanation": explanation,
            "generated_at": now,
            "expires_at": expires_at,
            "pinned": row["pinned"],
        },
    )


@router.post("/regenerate", response_model=AttentionList)
def regenerate_attention(auth: AuthDep, session: SessionDep, _csrf: CsrfDep) -> AttentionList:
    now = datetime.now(UTC)
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
                       blocked_on_person_id, pinned, updated_at
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
                       confidence, pinned, updated_at
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
                SELECT id, version, probability, impact, review_at, pinned, updated_at
                FROM risks
                WHERE workspace_id = :workspace_id AND archived_at IS NULL
                  AND status <> 'closed'
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
                  )
                """
            ),
            {"workspace_id": auth.workspace_id},
        )
        for raw in tasks:
            row = dict(raw)
            _upsert(session, auth, "task", row, *_score_task(row, today, now), now, expires_at)
        for raw in commitments:
            row = dict(raw)
            _upsert(
                session,
                auth,
                "commitment",
                row,
                *_score_commitment(row, today, now),
                now,
                expires_at,
            )
        for raw in risks:
            row = dict(raw)
            _upsert(session, auth, "risk", row, *_score_risk(row, now), now, expires_at)
    return list_attention(auth, session, 50)


@router.get("", response_model=AttentionList)
def list_attention(
    auth: AuthDep, session: SessionDep, limit: int = Query(default=50, ge=1, le=100)
) -> AttentionList:
    now = datetime.now(UTC)
    rows = (
        session.execute(
            text("""
            SELECT ai.id, ai.entity_type, ai.entity_id, ai.source_entity_version, ai.score,
                   ai.confidence, ai.factors, ai.explanation, ai.generated_at, ai.expires_at,
                   ai.pinned, ai.dismissed_at, ai.dismissed_entity_version, ai.deferred_until
            FROM attention_items ai
            JOIN workspaces w ON w.id = ai.workspace_id
            LEFT JOIN tasks t ON ai.entity_type = 'task'
                AND t.workspace_id = ai.workspace_id AND t.id = ai.entity_id
            LEFT JOIN commitments c ON ai.entity_type = 'commitment'
                AND c.workspace_id = ai.workspace_id AND c.id = ai.entity_id
            LEFT JOIN risks r ON ai.entity_type = 'risk'
                AND r.workspace_id = ai.workspace_id AND r.id = ai.entity_id
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
                r.review_at
              ) ASC NULLS LAST,
              CASE
                WHEN t.manual_priority = 'critical' THEN 4
                WHEN t.manual_priority = 'high' THEN 3
                WHEN t.manual_priority = 'medium' THEN 2
                WHEN t.manual_priority = 'low' THEN 1
                ELSE 0
              END DESC,
              COALESCE(t.created_at, c.created_at, r.created_at) ASC,
              ai.entity_id ASC
            LIMIT :limit
        """),
            {"workspace_id": auth.workspace_id, "now": now, "limit": limit},
        )
        .mappings()
        .all()
    )
    return AttentionList(items=[AttentionItem.model_validate(dict(row)) for row in rows])


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
                text("""
                SELECT id, entity_type, entity_id, source_entity_version, score,
                       confidence, factors, explanation, generated_at, expires_at,
                       pinned, dismissed_at, dismissed_entity_version, deferred_until
                FROM attention_items
                WHERE workspace_id = :workspace_id AND id = :item_id
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
            values = {"dismissed_at": now, "dismissed_entity_version": row["source_entity_version"]}
        elif action == "defer":
            if payload.deferred_until is None or payload.deferred_until <= now:
                raise HTTPException(status_code=422, detail="DEFER_UNTIL_MUST_BE_FUTURE")
            if row["deferred_until"] == payload.deferred_until:
                return AttentionItem.model_validate(dict(row))
            values = {"deferred_until": payload.deferred_until}
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
            }
        updated = (
            session.execute(
                text("""
                UPDATE attention_items SET
                    dismissed_at = :dismissed_at,
                    dismissed_entity_version = :dismissed_entity_version,
                    deferred_until = :deferred_until
                WHERE workspace_id = :workspace_id AND id = :item_id
                RETURNING id, entity_type, entity_id, source_entity_version, score,
                          confidence, factors, explanation, generated_at, expires_at,
                          pinned, dismissed_at, dismissed_entity_version, deferred_until
            """),
                {
                    "workspace_id": auth.workspace_id,
                    "item_id": item_id,
                    "dismissed_at": values.get("dismissed_at", row["dismissed_at"]),
                    "dismissed_entity_version": values.get(
                        "dismissed_entity_version", row["dismissed_entity_version"]
                    ),
                    "deferred_until": values.get("deferred_until", row["deferred_until"]),
                },
            )
            .mappings()
            .one()
        )
        request_id = UUID(request.state.request_id)
        correlation_id = UUID(request.state.correlation_id)
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
                "event_type": f"attention_item.{action}",
                "aggregate_id": item_id,
                "aggregate_version": row["source_entity_version"],
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "changed_fields": (
                    ["dismissed_at", "dismissed_entity_version"]
                    if action == "dismiss"
                    else ["deferred_until"]
                    if action == "defer"
                    else ["dismissed_at", "dismissed_entity_version", "deferred_until"]
                ),
                "occurred_at": now,
            },
        )
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
