import time as time_module
from datetime import UTC, date, datetime, time, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_brief_generated,
    record_brief_stale,
    record_idempotency_conflict,
)

router = APIRouter(prefix="/api/v1", tags=["dashboard", "briefs"])
SessionDep = Annotated[Session, Depends(get_session)]
DateQuery = Annotated[date | None, Query(alias="date")]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]
ALGORITHM_VERSION = "phase1-deterministic-v1"


class DashboardResponse(BaseModel):
    date: date
    timezone: str
    generated_at: datetime
    stale: bool
    sections: dict[str, Any]


class MorningBriefResponse(BaseModel):
    id: UUID
    briefing_date: date
    generation_version: int
    sections: dict[str, Any]
    source_versions: dict[str, int]
    evidence_ids: list[UUID]
    generated_at: datetime
    timezone: str
    algorithm_version: str
    ai_status: str
    stale: bool
    stale_reason: str | None


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise HTTPException(status_code=500, detail="WORKSPACE_TIMEZONE_INVALID") from None


def _target_date(value: date | None, timezone: str) -> date:
    return value or datetime.now(_zone(timezone)).date()


def _bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = _zone(timezone)
    start = datetime.combine(day, time.min, zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, zone).astimezone(UTC)
    return start, end


def _entity_ref(entity_type: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "entity_type": entity_type,
        "entity_id": str(row["id"]),
        "version": int(row.get("version", 1)),
    }


def _build_sections(
    session: Session,
    workspace_id: UUID,
    day: date,
    timezone: str,
) -> tuple[dict[str, Any], dict[str, int], list[UUID]]:
    start, end = _bounds(day, timezone)
    now = datetime.now(UTC)
    seen: set[tuple[str, UUID]] = set()
    versions: dict[str, int] = {}
    evidence_ids: set[UUID] = set()

    schedule_rows = (
        session.execute(
            text(
                """
                SELECT m.id, m.title, m.status, m.version,
                       ce.id AS calendar_event_id,
                       ce.version AS calendar_event_version,
                       coalesce(ce.starts_at, m.standalone_starts_at) AS starts_at,
                       coalesce(ce.ends_at, m.standalone_ends_at) AS ends_at,
                       coalesce(ce.timezone, m.standalone_timezone) AS timezone
                FROM meetings m
                LEFT JOIN calendar_events ce
                  ON ce.workspace_id = m.workspace_id
                 AND ce.id = m.calendar_event_id
                WHERE m.workspace_id = :workspace_id
                  AND m.archived_at IS NULL
                  AND coalesce(ce.starts_at, m.standalone_starts_at) < :end_at
                  AND coalesce(ce.ends_at, m.standalone_ends_at) > :start_at
                ORDER BY starts_at ASC, m.id ASC
                LIMIT 8
                """
            ),
            {"workspace_id": workspace_id, "start_at": start, "end_at": end},
        )
        .mappings()
        .all()
    )
    schedule: list[dict[str, Any]] = []
    for row in schedule_rows:
        seen.add(("meeting", row["id"]))
        versions[f"meeting:{row['id']}"] = int(row["version"])
        if row["calendar_event_id"] is not None:
            versions[f"calendar_event:{row['calendar_event_id']}"] = int(
                row["calendar_event_version"]
            )
        schedule.append(
            {
                **_entity_ref("meeting", dict(row)),
                "title": row["title"],
                "status": row["status"],
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "timezone": row["timezone"],
            }
        )

    attention_rows = (
        session.execute(
            text(
                """
                SELECT entity_type, entity_id, source_entity_version, score,
                       confidence, factors, explanation, pinned
                FROM attention_items
                WHERE workspace_id = :workspace_id
                  AND expires_at > :now
                  AND dismissed_at IS NULL
                  AND (deferred_until IS NULL OR deferred_until <= :now)
                ORDER BY pinned DESC, score DESC, entity_type ASC, entity_id ASC
                LIMIT 20
                """
            ),
            {"workspace_id": workspace_id, "now": now},
        )
        .mappings()
        .all()
    )
    priorities: list[dict[str, Any]] = []
    for row in attention_rows:
        key = (row["entity_type"], row["entity_id"])
        if key in seen:
            continue
        seen.add(key)
        versions[f"{row['entity_type']}:{row['entity_id']}"] = int(row["source_entity_version"])
        priorities.append(
            {
                "entity_type": row["entity_type"],
                "entity_id": str(row["entity_id"]),
                "version": int(row["source_entity_version"]),
                "score": int(row["score"]),
                "confidence": float(row["confidence"]),
                "factors": row["factors"],
                "why": row["explanation"],
                "pinned": bool(row["pinned"]),
            }
        )
        if len(priorities) == 7:
            break

    commitment_rows = (
        session.execute(
            text(
                """
                SELECT c.id, c.summary, c.status, c.direction, c.importance,
                       c.due_date, c.due_at, c.version, c.evidence_id,
                       ai.score AS attention_score
                FROM commitments c
                LEFT JOIN attention_items ai
                  ON ai.workspace_id = c.workspace_id
                 AND ai.entity_type = 'commitment'
                 AND ai.entity_id = c.id
                WHERE c.workspace_id = :workspace_id
                  AND c.archived_at IS NULL
                  AND c.status IN ('detected', 'confirmed', 'active', 'broken')
                  AND ((c.due_at IS NOT NULL AND c.due_at < :start_at)
                    OR (c.due_date IS NOT NULL AND c.due_date < :day))
                ORDER BY ai.score DESC NULLS LAST,
                         coalesce(c.due_at, c.due_date::timestamp) ASC,
                         c.id ASC
                LIMIT 20
                """
            ),
            {"workspace_id": workspace_id, "day": day, "start_at": start},
        )
        .mappings()
        .all()
    )
    overdue: list[dict[str, Any]] = []
    for row in commitment_rows:
        key = ("commitment", row["id"])
        if key in seen:
            continue
        seen.add(key)
        versions[f"commitment:{row['id']}"] = int(row["version"])
        if row["evidence_id"]:
            evidence_ids.add(row["evidence_id"])
        overdue.append(
            {
                **_entity_ref("commitment", dict(row)),
                "title": row["summary"],
                "status": row["status"],
                "importance": row["importance"],
                "due_date": row["due_date"],
                "due_at": row["due_at"],
                "score": (
                    int(row["attention_score"]) if row["attention_score"] is not None else None
                ),
                "evidence_ids": ([str(row["evidence_id"])] if row["evidence_id"] else []),
            }
        )
        if len(overdue) == 5:
            break

    waiting_rows = (
        session.execute(
            text(
                """
                SELECT 'commitment' AS entity_type, id, summary AS title,
                       status, version, due_date, due_at,
                       NULL::text AS blocked_reason
                FROM commitments
                WHERE workspace_id = :workspace_id
                  AND archived_at IS NULL
                  AND direction = 'made_to_me'
                  AND status IN ('detected', 'confirmed', 'active', 'broken')
                UNION ALL
                SELECT 'task' AS entity_type, id, title, status, version,
                       due_date, due_at, blocked_reason
                FROM tasks
                WHERE workspace_id = :workspace_id
                  AND archived_at IS NULL
                  AND status = 'blocked'
                  AND blocked_on_person_id IS NOT NULL
                ORDER BY entity_type ASC, id ASC
                LIMIT 20
                """
            ),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .all()
    )
    waiting: list[dict[str, Any]] = []
    for row in waiting_rows:
        key = (row["entity_type"], row["id"])
        if key in seen:
            continue
        seen.add(key)
        versions[f"{row['entity_type']}:{row['id']}"] = int(row["version"])
        waiting.append(
            {
                **_entity_ref(row["entity_type"], dict(row)),
                "title": row["title"],
                "status": row["status"],
                "due_date": row["due_date"],
                "due_at": row["due_at"],
                "blocked_reason": row["blocked_reason"],
            }
        )
        if len(waiting) == 5:
            break

    risk_rows = (
        session.execute(
            text(
                """
                SELECT id, description, probability, impact, status,
                       review_at, version
                FROM risks
                WHERE workspace_id = :workspace_id
                  AND archived_at IS NULL
                  AND status <> 'closed'
                ORDER BY probability * impact DESC,
                         review_at ASC NULLS LAST,
                         id ASC
                LIMIT 10
                """
            ),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .all()
    )
    risks: list[dict[str, Any]] = []
    for row in risk_rows:
        key = ("risk", row["id"])
        if key in seen:
            continue
        seen.add(key)
        versions[f"risk:{row['id']}"] = int(row["version"])
        risks.append(
            {
                **_entity_ref("risk", dict(row)),
                "title": row["description"],
                "status": row["status"],
                "score": int(row["probability"]) * int(row["impact"]),
                "review_at": row["review_at"],
            }
        )
        if len(risks) == 5:
            break

    changed_rows = (
        session.execute(
            text(
                """
                SELECT id, event_type, aggregate_type, aggregate_id,
                       aggregate_version, changed_fields, occurred_at
                FROM audit_events
                WHERE workspace_id = :workspace_id
                  AND occurred_at >= :since
                ORDER BY occurred_at DESC, id DESC
                LIMIT 20
                """
            ),
            {"workspace_id": workspace_id, "since": now - timedelta(hours=24)},
        )
        .mappings()
        .all()
    )
    changed: list[dict[str, Any]] = []
    for row in changed_rows:
        key = (row["aggregate_type"], row["aggregate_id"])
        if key in seen:
            continue
        seen.add(key)
        changed.append(
            {
                "event_type": row["event_type"],
                "entity_type": row["aggregate_type"],
                "entity_id": str(row["aggregate_id"]),
                "version": int(row["aggregate_version"]),
                "changed_fields": row["changed_fields"],
                "occurred_at": row["occurred_at"],
            }
        )
        if len(changed) == 5:
            break

    sections: dict[str, Any] = {
        "today_schedule": schedule
        or [{"empty": True, "message": "No meetings scheduled for today."}],
        "top_priorities": priorities
        or [{"empty": True, "message": "No ranked priorities available."}],
    }
    optional = {
        "overdue_commitments": overdue,
        "risks": risks,
        "waiting_on": waiting,
        "recently_changed": changed,
    }
    sections.update({name: items for name, items in optional.items() if items})
    return sections, versions, sorted(evidence_ids, key=str)


def _brief_staleness(
    session: Session,
    workspace_id: UUID,
    generated_at: datetime,
    source_versions: dict[str, int],
) -> tuple[bool, str | None]:
    if datetime.now(UTC) - generated_at >= timedelta(minutes=30):
        return True, "stale_by_age"
    table_by_type = {
        "task": "tasks",
        "commitment": "commitments",
        "meeting": "meetings",
        "calendar_event": "calendar_events",
        "risk": "risks",
    }
    for key, expected in source_versions.items():
        entity_type, raw_id = key.split(":", 1)
        table = table_by_type.get(entity_type)
        if table is None:
            continue
        current = session.execute(
            text(f"SELECT version FROM {table} WHERE workspace_id=:w AND id=:i"),
            {"w": workspace_id, "i": UUID(raw_id)},
        ).scalar_one_or_none()
        if current is None or int(current) != int(expected):
            return True, "source_version_changed"
    return False, None


def _response(
    row: dict[str, Any],
    stale: bool,
    reason: str | None,
) -> MorningBriefResponse:
    return MorningBriefResponse(
        id=row["id"],
        briefing_date=row["briefing_date"],
        generation_version=row["generation_version"],
        sections=row["sections"],
        source_versions={key: int(value) for key, value in row["source_versions"].items()},
        evidence_ids=row["evidence_ids"],
        generated_at=row["generated_at"],
        timezone=row["timezone"],
        algorithm_version=row["algorithm_version"],
        ai_status=row["ai_status"],
        stale=stale,
        stale_reason=reason or row["stale_reason"],
    )


def _generate(
    request: Request,
    session: Session,
    workspace_id: UUID,
    user_id: UUID,
    timezone: str,
    day: date,
    *,
    commit: bool = True,
) -> MorningBriefResponse:
    generation_start = time_module.monotonic()
    now = datetime.now(UTC)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": f"brief:{workspace_id}:{user_id}:{day.isoformat()}"},
    )
    version = int(
        session.execute(
            text(
                """
                SELECT coalesce(max(generation_version), 0) + 1
                FROM morning_briefs
                WHERE workspace_id=:w AND user_id=:u AND briefing_date=:d
                """
            ),
            {"w": workspace_id, "u": user_id, "d": day},
        ).scalar_one()
    )
    sections, source_versions, evidence_ids = _build_sections(
        session,
        workspace_id,
        day,
        timezone,
    )
    brief_id = uuid4()
    row = (
        session.execute(
            text(
                """
                INSERT INTO morning_briefs (
                    id, workspace_id, user_id, briefing_date,
                    generation_version, sections, source_versions,
                    evidence_ids, generated_at, timezone,
                    algorithm_version, ai_status, created_at, updated_at
                ) VALUES (
                    :id, :w, :u, :d, :v, CAST(:sections AS jsonb),
                    CAST(:versions AS jsonb), :evidence_ids, :now,
                    :timezone, :algorithm, 'disabled', :now, :now
                )
                RETURNING *
                """
            ),
            {
                "id": brief_id,
                "w": workspace_id,
                "u": user_id,
                "d": day,
                "v": version,
                "sections": dumps(sections, default=str),
                "versions": dumps(source_versions),
                "evidence_ids": evidence_ids,
                "now": now,
                "timezone": timezone,
                "algorithm": ALGORITHM_VERSION,
            },
        )
        .mappings()
        .one()
    )
    request_id = UUID(request.state.request_id)
    correlation_id = UUID(request.state.correlation_id)
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type,
                    aggregate_id, aggregate_version, actor_id,
                    request_id, correlation_id, before, after,
                    changed_fields, authorization_result, source,
                    metadata, occurred_at
                ) VALUES (
                    :id, :w, 'morning_brief.generated', 'morning_brief',
                    :aggregate_id, :version, :actor, :request_id,
                    :correlation_id, NULL, CAST(:after AS jsonb),
                    :fields, 'allowed', 'user', '{}'::jsonb, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "w": workspace_id,
                "aggregate_id": brief_id,
                "version": version,
                "actor": user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "after": dumps(
                    {
                        "briefing_date": day.isoformat(),
                        "generation_version": version,
                    }
                ),
                "fields": ["sections", "source_versions", "generated_at"],
                "now": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_id, workspace_id, event_type, event_version,
                    correlation_id, causation_id, payload, occurred_at,
                    attempt_count
                ) VALUES (
                    :event_id, :w, 'morning_brief.generated', 1,
                    :correlation_id, :causation_id,
                    CAST(:payload AS jsonb), :now, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "w": workspace_id,
                "correlation_id": correlation_id,
                "causation_id": request_id,
                "payload": dumps(
                    {
                        "brief_id": str(brief_id),
                        "briefing_date": day.isoformat(),
                        "version": version,
                    }
                ),
                "now": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("briefs")
        raise
    queue_lifecycle_event(session, "brief", "morning_brief.generated", "allowed")
    record_brief_generated(time_module.monotonic() - generation_start)
    response = _response(dict(row), False, None)
    if commit:
        session.commit()
    return response


@router.get("/dashboard/today", response_model=DashboardResponse)
def dashboard_today(
    auth: AuthDep,
    session: SessionDep,
    day: DateQuery = None,
) -> DashboardResponse:
    target = _target_date(day, auth.timezone)
    sections, _, _ = _build_sections(
        session,
        auth.workspace_id,
        target,
        auth.timezone,
    )
    session.rollback()
    return DashboardResponse(
        date=target,
        timezone=auth.timezone,
        generated_at=datetime.now(UTC),
        stale=False,
        sections=sections,
    )


@router.get("/briefs/morning", response_model=MorningBriefResponse)
def get_morning_brief(
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    day: DateQuery = None,
) -> MorningBriefResponse:
    target = _target_date(day, auth.timezone)
    row = (
        session.execute(
            text(
                """
                SELECT * FROM morning_briefs
                WHERE workspace_id=:w AND user_id=:u
                  AND briefing_date=:d
                ORDER BY generation_version DESC
                LIMIT 1
                """
            ),
            {"w": auth.workspace_id, "u": auth.user_id, "d": target},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return _generate(
            request,
            session,
            auth.workspace_id,
            auth.user_id,
            auth.timezone,
            target,
        )
    stale, reason = _brief_staleness(
        session,
        auth.workspace_id,
        row["generated_at"],
        row["source_versions"],
    )
    session.rollback()
    if stale and reason is not None:
        record_brief_stale(reason)
    return _response(dict(row), stale, reason)


@router.post("/briefs/morning", response_model=MorningBriefResponse)
def refresh_morning_brief(
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
    day: DateQuery = None,
) -> MorningBriefResponse:
    target = _target_date(day, auth.timezone)
    request_hash = sha256(target.isoformat().encode()).hexdigest()
    now = datetime.now(UTC)
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {"key": (f"brief-refresh:{auth.workspace_id}:{auth.user_id}:{idempotency_key}")},
    )
    existing = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body
                FROM idempotency_records
                WHERE workspace_id=:w AND actor_id=:u
                  AND key=:key AND expires_at > :now
                """
            ),
            {
                "w": auth.workspace_id,
                "u": auth.user_id,
                "key": idempotency_key,
                "now": now,
            },
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        if existing["request_hash"] != request_hash:
            session.rollback()
            record_idempotency_conflict("briefs")
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        session.rollback()
        return MorningBriefResponse.model_validate(existing["response_body"])

    response = _generate(
        request,
        session,
        auth.workspace_id,
        auth.user_id,
        auth.timezone,
        target,
        commit=False,
    )
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash,
                response_status, response_body, created_at, expires_at
            ) VALUES (
                :w, :u, :key, :request_hash, 200,
                CAST(:body AS jsonb), :now, :expires_at
            )
            """
        ),
        {
            "w": auth.workspace_id,
            "u": auth.user_id,
            "key": idempotency_key,
            "request_hash": request_hash,
            "body": response.model_dump_json(),
            "now": now,
            "expires_at": now + timedelta(hours=24),
        },
    )
    session.commit()
    return response
