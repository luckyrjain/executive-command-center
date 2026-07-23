"""Evidence-backed meeting preparation (Phase 3 Task 7).

Composes already-existing Phase 1/2 read queries (timeline, commitments,
notes, risks, evidence, Task 2's waiting_links) into one deterministic
preparation pack per MEETING-PREP-CONTRACT.md, snapshotted as a
``meeting_packs`` row. No new source-of-truth tables beyond
``meeting_participants``/``meeting_packs`` (plus the additive
``notes.restricted`` column -- see migration 0027's docstring).

Pure/impure split mirrors Task 5's ``planning.py``: ``build_pack`` is a pure
function over plain fetched rows (testable without a database, and the
place the restricted-note-exclusion and evidence-availability rules live);
the router's ``_fetch_*`` helpers do the actual querying and the route
functions persist the result.

Scoping decisions this module makes, each because the plan/contract named a
requirement with no existing data source to back it (documented here so
they read as decisions, not oversights):

- "Prior decisions" are sourced from ``notes`` rows with
  ``note_type = 'decision'`` -- the only decision discriminator that exists
  anywhere in the schema (no claim/relationship type for it).
- "Unresolved questions" has no backing data source in Phase 1/2 (no note
  type, claim predicate, or other signal represents an open question) and
  is always returned empty rather than invented from unrelated data.
- "Active risks" are workspace-wide active risks (bounded, ordered by
  review urgency), not filtered to meeting participants: ``risks.owner_id``
  is a ``users`` row and ``pkos_nodes`` (what meeting participants link to)
  has no resolvable link to ``users`` anywhere in this codebase, so a
  participant-scoped risk filter isn't queryable today.
- AI enrichment always reports ``feature_disabled`` in Phase 3 regardless
  of the config flag's value: Phase 4 (AI Runtime) does not exist yet to
  serve it. The flag is the documented off-by-default opt-in point for
  when it does, matching ``config.py``'s ``embeddings_enabled`` precedent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

router = APIRouter(prefix="/api/v1/meetings", tags=["meeting-prep"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

PackStatus = Literal["fresh", "stale", "refreshed", "archived"]

# Generation-time TTL threshold, in addition to the material-change
# staleness check below -- MEETING-PREP-CONTRACT.md: "A pack stores ...
# generation time and stale threshold."
_STALE_AFTER = timedelta(hours=24)
_MAX_TIMELINE_ENTRIES = 20
_MAX_COMMITMENTS = 20
_MAX_RISKS = 10
_MAX_NOTES = 20
_MAX_DEPENDENCIES = 20
_MAX_EVIDENCE = 50

_PACK_FIELDS = """
    id, meeting_id, status, generated_at, stale_at, source_versions, content,
    created_at, updated_at, version
"""


# ---------------------------------------------------------------------------
# Pure composition layer -- plain input rows in, a plain PackContent out.
# No DB access, so directly unit-testable (restricted-note exclusion,
# evidence-availability surfacing, prompt-injection-as-inert-data).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParticipantRow:
    id: UUID
    entity_id: UUID
    entity_name: str
    role: str


@dataclass(frozen=True)
class TimelineRow:
    id: UUID
    entity_id: UUID
    effective_at: datetime
    event_type: str
    summary: str


@dataclass(frozen=True)
class CommitmentRow:
    id: UUID
    direction: Literal["made_by_me", "made_to_me"]
    summary: str
    status: str
    due_at: datetime | None
    counterparty_name: str | None


@dataclass(frozen=True)
class NoteRow:
    id: UUID
    title: str | None
    body: str
    note_type: str
    restricted: bool
    created_at: datetime


@dataclass(frozen=True)
class RiskRow:
    id: UUID
    description: str
    status: str
    probability: int
    impact: int
    review_at: datetime | None


@dataclass(frozen=True)
class DependencyRow:
    id: UUID
    direction: Literal["waiting_on_me", "waiting_on_them", "blocked_by", "delegated"]
    note: str | None
    expected_at: datetime | None


@dataclass(frozen=True)
class EvidenceRow:
    id: UUID
    source_type: str
    evidence_state: Literal["available", "missing", "permission_denied", "deleted"]


@dataclass(frozen=True)
class MeetingInput:
    id: UUID
    title: str
    agenda: str | None
    starts_at: datetime
    ends_at: datetime
    timezone: str


@dataclass(frozen=True)
class PackContent:
    objective: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    participants: list[ParticipantRow]
    timeline: list[TimelineRow]
    commitments: list[CommitmentRow]
    decisions: list[NoteRow]
    open_questions: list[str]
    notes: list[NoteRow]
    risks: list[RiskRow]
    dependencies: list[DependencyRow]
    evidence_gaps: list[EvidenceRow]


def build_pack(
    meeting: MeetingInput,
    participants: list[ParticipantRow],
    timeline: list[TimelineRow],
    commitments: list[CommitmentRow],
    notes: list[NoteRow],
    risks: list[RiskRow],
    dependencies: list[DependencyRow],
    evidence: list[EvidenceRow],
) -> PackContent:
    # Safety: private/restricted notes never enter the pack, in either the
    # decisions or the general-notes section -- MEETING-PREP-CONTRACT.md's
    # Safety section, with no per-viewer override in this phase (see module
    # docstring).
    visible_notes = [n for n in notes if not n.restricted]
    decisions = [n for n in visible_notes if n.note_type == "decision"]
    general_notes = [n for n in visible_notes if n.note_type != "decision"]
    return PackContent(
        objective=meeting.agenda or meeting.title,
        starts_at=meeting.starts_at,
        ends_at=meeting.ends_at,
        timezone=meeting.timezone,
        participants=participants,
        timeline=timeline,
        commitments=commitments,
        decisions=decisions,
        open_questions=[],  # No backing data source today -- see module docstring.
        notes=general_notes,
        risks=risks,
        dependencies=dependencies,
        evidence_gaps=[e for e in evidence if e.evidence_state != "available"],
    )


def _source_fingerprint(
    meeting: MeetingInput,
    participants: list[ParticipantRow],
    timeline: list[TimelineRow],
    commitments: list[CommitmentRow],
    notes: list[NoteRow],
    risks: list[RiskRow],
    dependencies: list[DependencyRow],
    evidence: list[EvidenceRow],
) -> dict[str, str]:
    """Hashed per input category, exactly like ``planning.py``'s
    fingerprint. Every field that actually reaches the rendered pack
    (``_pack_row_to_response``'s output) must be included here -- a field
    that determines what's displayed but isn't hashed is a staleness gap:
    the underlying source can change in a way a viewer would see, and
    nothing marks the frozen snapshot ``stale`` for it (finding #6's
    fingerprint half). This previously hashed only id/status/date-shaped
    fields and omitted the actual display text (summary, entity_name,
    description, note body/title, ...) and evidence entirely. It also
    previously omitted the meeting row itself: ``build_pack`` puts
    ``objective`` (derived from ``meeting.agenda``/``meeting.title``),
    ``starts_at``, ``ends_at`` and ``timezone`` straight into the
    persisted/displayed pack content, so a reschedule or an agenda edit
    with no other source changing would leave the pack silently wrong
    without a ``meeting`` component here.
    """

    def _hash(parts: list[str]) -> str:
        return sha256("|".join(sorted(parts)).encode()).hexdigest()

    return {
        "meeting": _hash(
            [
                f"{meeting.id}:{meeting.title}:{meeting.agenda}:"
                f"{meeting.starts_at}:{meeting.ends_at}:{meeting.timezone}"
            ]
        ),
        "participants": _hash([f"{p.id}:{p.role}:{p.entity_name}" for p in participants]),
        "timeline": _hash(
            [f"{t.id}:{t.effective_at}:{t.event_type}:{t.summary}" for t in timeline]
        ),
        "commitments": _hash(
            [
                f"{c.id}:{c.status}:{c.due_at}:{c.direction}:{c.summary}:{c.counterparty_name}"
                for c in commitments
            ]
        ),
        "notes": _hash([f"{n.id}:{n.body}:{n.restricted}:{n.title}:{n.note_type}" for n in notes]),
        "risks": _hash(
            [
                f"{r.id}:{r.status}:{r.review_at}:{r.description}:{r.probability}:{r.impact}"
                for r in risks
            ]
        ),
        "dependencies": _hash(
            [f"{d.id}:{d.direction}:{d.expected_at}:{d.note}" for d in dependencies]
        ),
        "evidence": _hash([f"{e.id}:{e.source_type}:{e.evidence_state}" for e in evidence]),
    }


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class ParticipantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entity_id: UUID
    role: str = Field(default="attendee", min_length=1, max_length=100)


class ParticipantResponse(BaseModel):
    id: UUID
    entity_id: UUID
    entity_name: str
    role: str


class ParticipantList(BaseModel):
    items: list[ParticipantResponse]


class TimelineEntryOut(BaseModel):
    id: UUID
    entity_id: UUID
    effective_at: datetime
    event_type: str
    summary: str


class CommitmentOut(BaseModel):
    id: UUID
    direction: Literal["made_by_me", "made_to_me"]
    summary: str
    status: str
    due_at: datetime | None
    counterparty_name: str | None


class NoteOut(BaseModel):
    id: UUID
    title: str | None
    body: str
    note_type: str
    created_at: datetime


class RiskOut(BaseModel):
    id: UUID
    description: str
    status: str
    probability: int
    impact: int
    review_at: datetime | None


class DependencyOut(BaseModel):
    id: UUID
    direction: Literal["waiting_on_me", "waiting_on_them", "blocked_by", "delegated"]
    note: str | None
    expected_at: datetime | None


class EvidenceGapOut(BaseModel):
    id: UUID
    source_type: str
    evidence_state: Literal["available", "missing", "permission_denied", "deleted"]


class EnrichmentOut(BaseModel):
    available: bool
    summary: str | None
    error_code: str | None


class PackContentSnapshot(BaseModel):
    """The frozen, fully-rendered pack body -- everything ``build_pack``
    computed at generation time, persisted verbatim into
    ``meeting_packs.content`` and returned as-is by every subsequent GET
    (finding #6): a real snapshot, not a re-derivation of live data on
    every read. Only ``POST .../prep/refresh`` produces a new one.
    """

    objective: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    participants: list[ParticipantResponse]
    timeline: list[TimelineEntryOut]
    commitments: list[CommitmentOut]
    decisions: list[NoteOut]
    open_questions: list[str]
    notes: list[NoteOut]
    risks: list[RiskOut]
    dependencies: list[DependencyOut]
    evidence_gaps: list[EvidenceGapOut]


class MeetingPack(BaseModel):
    id: UUID
    meeting_id: UUID
    status: PackStatus
    generated_at: datetime
    stale_at: datetime
    source_versions: dict[str, str]
    objective: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    participants: list[ParticipantResponse]
    timeline: list[TimelineEntryOut]
    commitments: list[CommitmentOut]
    decisions: list[NoteOut]
    open_questions: list[str]
    notes: list[NoteOut]
    risks: list[RiskOut]
    dependencies: list[DependencyOut]
    evidence_gaps: list[EvidenceGapOut]
    enrichment: EnrichmentOut


# ---------------------------------------------------------------------------
# Idempotency / audit helpers (per-module, matching every other Phase 3
# domain file's convention -- no shared utility module).
# ---------------------------------------------------------------------------


def _violated_constraint(exc: IntegrityError) -> str | None:
    """Best-effort extraction of the specific DB constraint/index name a
    psycopg ``IntegrityError`` violated (``exc.orig.diag.constraint_name``
    for psycopg3), so the two savepoint-guarded races below can react only
    to the exact unique index they're each defending against instead of
    treating every ``IntegrityError`` -- including an unrelated FK
    violation, e.g. a race with a deleted meeting -- as the specific
    duplicate-pack/participant conflict they're not.
    """
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _request_hash(action: str, payload: dict[str, Any] | None = None) -> str:
    material = {"action": action, "payload": payload or {}}
    return sha256(
        dumps(material, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> dict[str, Any] | None:
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
        record_idempotency_conflict("meeting_prep")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return dict(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response_body: dict[str, Any],
    response_status: int,
    now: datetime,
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
            "response_status": response_status,
            "response_body": dumps(response_body, default=str),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _write_event(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    aggregate_type: str,
    meeting_id: UUID,
    aggregate_id: UUID,
    aggregate_version: int,
    now: datetime,
    *,
    emit_outbox: bool = True,
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
                    :id, :workspace_id, :event_type, :aggregate_type, :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "aggregate_version": aggregate_version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "occurred_at": now,
            },
        )
        if emit_outbox:
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
                    "payload": dumps({"meeting_id": str(meeting_id)}),
                    "occurred_at": now,
                },
            )
    except SQLAlchemyError:
        record_audit_outbox_failure("meeting_prep")
        raise
    queue_lifecycle_event(session, aggregate_type, event_type, "allowed")


# ---------------------------------------------------------------------------
# Fetch helpers (impure) -- one per composed domain, workspace-scoped.
# ---------------------------------------------------------------------------


def _meeting_row(
    session: Session, auth: AuthContext, meeting_id: UUID, *, for_update: bool = False
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT id, calendar_event_id, title, standalone_starts_at,
                       standalone_ends_at, standalone_timezone, agenda, archived_at
                FROM meetings
                WHERE workspace_id = :workspace_id AND id = :meeting_id
                {suffix}
                """
            ),
            {"workspace_id": auth.workspace_id, "meeting_id": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _meeting_input(session: Session, auth: AuthContext, row: dict[str, Any]) -> MeetingInput:
    if row["calendar_event_id"] is None:
        starts_at, ends_at, tz = (
            row["standalone_starts_at"],
            row["standalone_ends_at"],
            row["standalone_timezone"],
        )
    else:
        event = (
            session.execute(
                text(
                    "SELECT starts_at, ends_at, timezone FROM calendar_events "
                    "WHERE workspace_id = :workspace_id AND id = :event_id"
                ),
                {"workspace_id": auth.workspace_id, "event_id": row["calendar_event_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if event is None:
            raise HTTPException(status_code=409, detail="LINKED_CALENDAR_EVENT_MISSING")
        starts_at, ends_at, tz = event["starts_at"], event["ends_at"], event["timezone"]
    return MeetingInput(
        id=row["id"],
        title=row["title"],
        agenda=row["agenda"],
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=tz,
    )


def _fetch_participants(
    session: Session, auth: AuthContext, meeting_id: UUID
) -> list[ParticipantRow]:
    rows = (
        session.execute(
            text(
                """
                SELECT mp.id, mp.entity_id, mp.role, n.canonical_name
                FROM meeting_participants mp
                JOIN pkos_nodes n ON n.workspace_id = mp.workspace_id AND n.id = mp.entity_id
                WHERE mp.workspace_id = :workspace_id AND mp.meeting_id = :meeting_id
                ORDER BY mp.created_at, mp.id
                """
            ),
            {"workspace_id": auth.workspace_id, "meeting_id": meeting_id},
        )
        .mappings()
        .all()
    )
    return [
        ParticipantRow(
            id=r["id"], entity_id=r["entity_id"], entity_name=r["canonical_name"], role=r["role"]
        )
        for r in rows
    ]


def _participant_already_linked(
    session: Session, auth: AuthContext, meeting_id: UUID, entity_id: UUID
) -> bool:
    """The pre-insert existence check ``add_participant`` uses -- pulled
    into its own function both for readability and so a test can
    monkeypatch it to force the TOCTOU race finding #7 describes (this
    check passing stale while a concurrent request already inserted the
    same link), exercising the savepoint/``IntegrityError`` handling
    around the INSERT rather than only the common non-concurrent case.
    """
    return (
        session.execute(
            text(
                "SELECT 1 FROM meeting_participants "
                "WHERE workspace_id = :workspace_id AND meeting_id = :meeting_id "
                "AND entity_id = :entity_id"
            ),
            {
                "workspace_id": auth.workspace_id,
                "meeting_id": meeting_id,
                "entity_id": entity_id,
            },
        ).scalar_one_or_none()
        is not None
    )


def _fetch_timeline(
    session: Session, auth: AuthContext, entity_ids: list[UUID]
) -> list[TimelineRow]:
    if not entity_ids:
        return []
    rows = (
        session.execute(
            text(
                """
                SELECT id, entity_id, effective_at, event_type, summary
                FROM timeline_entries
                WHERE workspace_id = :workspace_id AND entity_id = ANY(:entity_ids)
                ORDER BY effective_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "entity_ids": entity_ids,
                "limit": _MAX_TIMELINE_ENTRIES,
            },
        )
        .mappings()
        .all()
    )
    return [
        TimelineRow(
            id=r["id"],
            entity_id=r["entity_id"],
            effective_at=r["effective_at"],
            event_type=r["event_type"],
            summary=r["summary"],
        )
        for r in rows
    ]


def _fetch_commitments(
    session: Session, auth: AuthContext, participant_entity_ids: list[UUID]
) -> list[CommitmentRow]:
    if not participant_entity_ids:
        return []
    rows = (
        session.execute(
            text(
                """
                SELECT id, direction, summary, status, due_at, counterparty_name
                FROM commitments
                WHERE workspace_id = :workspace_id
                  AND counterparty_person_id = ANY(:entity_ids)
                  AND status IN ('confirmed', 'active')
                  AND archived_at IS NULL
                ORDER BY due_at NULLS LAST, id
                LIMIT :limit
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "entity_ids": participant_entity_ids,
                "limit": _MAX_COMMITMENTS,
            },
        )
        .mappings()
        .all()
    )
    return [
        CommitmentRow(
            id=r["id"],
            direction=r["direction"],
            summary=r["summary"],
            status=r["status"],
            due_at=r["due_at"],
            counterparty_name=r["counterparty_name"],
        )
        for r in rows
    ]


def _fetch_notes(session: Session, auth: AuthContext, meeting_id: UUID) -> list[NoteRow]:
    # Restricted notes are excluded here, in the SQL WHERE clause, *before*
    # LIMIT is applied -- not filtered out afterward in Python (build_pack's
    # filter stays as a defense-in-depth belt-and-suspenders check, but
    # must never be the *only* place this happens). Filtering post-fetch
    # let LIMIT :limit count restricted rows against the cap, so a meeting
    # with _MAX_NOTES-or-more restricted notes could return fewer than
    # _MAX_NOTES visible notes even when more visible ones existed beyond
    # the truncated window (finding #8).
    rows = (
        session.execute(
            text(
                """
                SELECT id, title, body, note_type, restricted, created_at
                FROM notes
                WHERE workspace_id = :workspace_id AND meeting_id = :meeting_id
                  AND archived_at IS NULL AND restricted = false
                ORDER BY created_at DESC, id
                LIMIT :limit
                """
            ),
            {"workspace_id": auth.workspace_id, "meeting_id": meeting_id, "limit": _MAX_NOTES},
        )
        .mappings()
        .all()
    )
    return [
        NoteRow(
            id=r["id"],
            title=r["title"],
            body=r["body"],
            note_type=r["note_type"],
            restricted=r["restricted"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def _fetch_risks(session: Session, auth: AuthContext) -> list[RiskRow]:
    # Workspace-wide, not participant-scoped -- see module docstring.
    rows = (
        session.execute(
            text(
                """
                SELECT id, description, status, probability, impact, review_at
                FROM risks
                WHERE workspace_id = :workspace_id AND status <> 'closed' AND archived_at IS NULL
                ORDER BY review_at NULLS LAST, id
                LIMIT :limit
                """
            ),
            {"workspace_id": auth.workspace_id, "limit": _MAX_RISKS},
        )
        .mappings()
        .all()
    )
    return [
        RiskRow(
            id=r["id"],
            description=r["description"],
            status=r["status"],
            probability=r["probability"],
            impact=r["impact"],
            review_at=r["review_at"],
        )
        for r in rows
    ]


def _fetch_dependencies(
    session: Session, auth: AuthContext, participant_entity_ids: list[UUID]
) -> list[DependencyRow]:
    if not participant_entity_ids:
        return []
    rows = (
        session.execute(
            text(
                """
                SELECT id, direction, note, expected_at
                FROM waiting_links
                WHERE workspace_id = :workspace_id
                  AND status = 'open'
                  AND counterparty_entity_id = ANY(:entity_ids)
                ORDER BY expected_at NULLS LAST, id
                LIMIT :limit
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "entity_ids": participant_entity_ids,
                "limit": _MAX_DEPENDENCIES,
            },
        )
        .mappings()
        .all()
    )
    return [
        DependencyRow(
            id=r["id"], direction=r["direction"], note=r["note"], expected_at=r["expected_at"]
        )
        for r in rows
    ]


def _fetch_evidence(session: Session, auth: AuthContext, node_ids: list[UUID]) -> list[EvidenceRow]:
    if not node_ids:
        return []
    rows = (
        session.execute(
            text(
                """
                SELECT id, source_type, evidence_state
                FROM pkos_evidence
                WHERE workspace_id = :workspace_id AND node_id = ANY(:node_ids)
                ORDER BY id
                LIMIT :limit
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "node_ids": node_ids,
                "limit": _MAX_EVIDENCE,
            },
        )
        .mappings()
        .all()
    )
    return [
        EvidenceRow(id=r["id"], source_type=r["source_type"], evidence_state=r["evidence_state"])
        for r in rows
    ]


def _enrichment_section() -> EnrichmentOut:
    # Always feature_disabled in Phase 3 -- see module docstring.
    return EnrichmentOut(available=False, summary=None, error_code="feature_disabled")


@dataclass(frozen=True)
class _GeneratedPack:
    content: PackContent
    fingerprint: dict[str, str]


def _generate_pack(
    session: Session, auth: AuthContext, meeting_id: UUID, meeting_row: dict[str, Any]
) -> _GeneratedPack:
    meeting = _meeting_input(session, auth, meeting_row)
    participants = _fetch_participants(session, auth, meeting_id)
    participant_entity_ids = [p.entity_id for p in participants]
    timeline = _fetch_timeline(session, auth, participant_entity_ids)
    commitments = _fetch_commitments(session, auth, participant_entity_ids)
    notes = _fetch_notes(session, auth, meeting_id)
    risks = _fetch_risks(session, auth)
    dependencies = _fetch_dependencies(session, auth, participant_entity_ids)
    evidence = _fetch_evidence(session, auth, participant_entity_ids)
    content = build_pack(
        meeting, participants, timeline, commitments, notes, risks, dependencies, evidence
    )
    fingerprint = _source_fingerprint(
        meeting, participants, timeline, commitments, notes, risks, dependencies, evidence
    )
    return _GeneratedPack(content=content, fingerprint=fingerprint)


def _content_to_snapshot(content: PackContent) -> PackContentSnapshot:
    """Render a freshly-generated ``PackContent`` into the exact JSON-able
    shape persisted as ``meeting_packs.content`` (finding #6). Called once,
    at generation time (``create_prep``/``refresh_prep``) -- never at GET
    time, which loads the already-persisted snapshot instead of calling
    this again.
    """
    return PackContentSnapshot(
        objective=content.objective,
        starts_at=content.starts_at,
        ends_at=content.ends_at,
        timezone=content.timezone,
        participants=[
            ParticipantResponse(
                id=p.id, entity_id=p.entity_id, entity_name=p.entity_name, role=p.role
            )
            for p in content.participants
        ],
        timeline=[
            TimelineEntryOut(
                id=t.id,
                entity_id=t.entity_id,
                effective_at=t.effective_at,
                event_type=t.event_type,
                summary=t.summary,
            )
            for t in content.timeline
        ],
        commitments=[
            CommitmentOut(
                id=c.id,
                direction=c.direction,
                summary=c.summary,
                status=c.status,
                due_at=c.due_at,
                counterparty_name=c.counterparty_name,
            )
            for c in content.commitments
        ],
        decisions=[
            NoteOut(
                id=n.id, title=n.title, body=n.body, note_type=n.note_type, created_at=n.created_at
            )
            for n in content.decisions
        ],
        open_questions=content.open_questions,
        notes=[
            NoteOut(
                id=n.id, title=n.title, body=n.body, note_type=n.note_type, created_at=n.created_at
            )
            for n in content.notes
        ],
        risks=[
            RiskOut(
                id=r.id,
                description=r.description,
                status=r.status,
                probability=r.probability,
                impact=r.impact,
                review_at=r.review_at,
            )
            for r in content.risks
        ],
        dependencies=[
            DependencyOut(id=d.id, direction=d.direction, note=d.note, expected_at=d.expected_at)
            for d in content.dependencies
        ],
        evidence_gaps=[
            EvidenceGapOut(id=e.id, source_type=e.source_type, evidence_state=e.evidence_state)
            for e in content.evidence_gaps
        ],
    )


def _pack_row_to_response(row: dict[str, Any], snapshot: PackContentSnapshot) -> MeetingPack:
    """Merge a ``meeting_packs`` row's own columns with its persisted
    content snapshot. ``snapshot`` always comes from ``row["content"]``
    (the frozen body stored at generation time) -- never from a fresh
    ``_generate_pack`` call, which would defeat the point of storing it.
    """
    return MeetingPack(
        id=row["id"],
        meeting_id=row["meeting_id"],
        status=row["status"],
        generated_at=row["generated_at"],
        stale_at=row["stale_at"],
        source_versions=dict(row["source_versions"]),
        enrichment=_enrichment_section(),
        **snapshot.model_dump(),
    )


def _is_stale(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    pack_row: dict[str, Any],
    meeting_row: dict[str, Any],
    now: datetime,
) -> bool:
    if now >= pack_row["stale_at"]:
        return True
    generated = _generate_pack(session, auth, meeting_id, meeting_row)
    return generated.fingerprint != dict(pack_row["source_versions"])


def _current_pack_row(
    session: Session, auth: AuthContext, meeting_id: UUID, *, for_update: bool = False
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT {_PACK_FIELDS} FROM meeting_packs
                WHERE workspace_id = :workspace_id AND meeting_id = :meeting_id
                  AND status IN ('fresh', 'stale')
                ORDER BY generated_at DESC
                LIMIT 1
                {suffix}
                """
            ),
            {"workspace_id": auth.workspace_id, "meeting_id": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Participant endpoints
#
# Not listed in API-SCHEMAS.md's top-level "Proposed surface" bullet list
# (which only names GET|POST /meetings/{id}/prep and .../prep/refresh) --
# but Phase 1's meetings/calendar_events have no attendee data at all
# (design doc's Open decision 2), so some way to populate
# meeting_participants before a pack can say anything about "participants
# and known roles" is required for the feature to be usable or testable
# end-to-end. Treated as a real gap in that summary list, not an
# intentional omission, matching how this session has resolved every other
# genuine plan/contract underspecification with an explicit, documented
# choice rather than silent invention.
# ---------------------------------------------------------------------------


@router.post(
    "/{meeting_id}/participants",
    response_model=ParticipantResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_participant(
    meeting_id: UUID,
    payload: ParticipantCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ParticipantResponse:
    request_hash = _request_hash(f"add_participant:{meeting_id}", payload.model_dump(mode="json"))
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return ParticipantResponse.model_validate(cached)

        meeting_row = _meeting_row(session, auth, meeting_id)
        if meeting_row is None:
            raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
        entity = (
            session.execute(
                text(
                    "SELECT id, canonical_name FROM pkos_nodes "
                    "WHERE workspace_id = :workspace_id AND id = :entity_id"
                ),
                {"workspace_id": auth.workspace_id, "entity_id": payload.entity_id},
            )
            .mappings()
            .one_or_none()
        )
        if entity is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")

        if _participant_already_linked(session, auth, meeting_id, payload.entity_id):
            raise HTTPException(status_code=409, detail="PARTICIPANT_ALREADY_LINKED")

        participant_id = uuid4()
        try:
            # Nested transaction (SAVEPOINT): the existence check above
            # closes the common case, but two concurrent requests can both
            # pass it and both reach this INSERT -- migration 0027's
            # uq_meeting_participants_link unique constraint is the real
            # guard. Without the savepoint, catching the resulting
            # IntegrityError here would still leave the outer transaction
            # aborted for every statement after it (finding #7).
            with session.begin_nested():
                session.execute(
                    text(
                        """
                        INSERT INTO meeting_participants (
                            id, workspace_id, meeting_id, entity_id, role,
                            created_by, updated_by, created_at, updated_at, version
                        ) VALUES (
                            :id, :workspace_id, :meeting_id, :entity_id, :role,
                            :actor_id, :actor_id, :now, :now, 1
                        )
                        """
                    ),
                    {
                        "id": participant_id,
                        "workspace_id": auth.workspace_id,
                        "meeting_id": meeting_id,
                        "entity_id": payload.entity_id,
                        "role": payload.role,
                        "actor_id": auth.user_id,
                        "now": now,
                    },
                )
        except IntegrityError as exc:
            if _violated_constraint(exc) != "uq_meeting_participants_link":
                raise
            raise HTTPException(status_code=409, detail="PARTICIPANT_ALREADY_LINKED") from exc
        # Audit-only, no outbox/catalog event -- a minor sub-action, matching
        # attention_item.dismiss/defer/restore's established precedent.
        _write_event(
            session,
            auth,
            request,
            "meeting_participant.linked",
            "meeting_participant",
            meeting_id,
            participant_id,
            1,
            now,
            emit_outbox=False,
        )

        response = ParticipantResponse(
            id=participant_id,
            entity_id=payload.entity_id,
            entity_name=entity["canonical_name"],
            role=payload.role,
        )
        _store_cached(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), 201, now
        )
        return response


@router.get("/{meeting_id}/participants", response_model=ParticipantList)
def list_participants(meeting_id: UUID, auth: AuthDep, session: SessionDep) -> ParticipantList:
    if _meeting_row(session, auth, meeting_id) is None:
        raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
    rows = _fetch_participants(session, auth, meeting_id)
    return ParticipantList(
        items=[
            ParticipantResponse(
                id=p.id, entity_id=p.entity_id, entity_name=p.entity_name, role=p.role
            )
            for p in rows
        ]
    )


# ---------------------------------------------------------------------------
# Preparation pack endpoints
# ---------------------------------------------------------------------------


@router.post("/{meeting_id}/prep", response_model=MeetingPack, status_code=status.HTTP_201_CREATED)
def create_prep(
    meeting_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> MeetingPack:
    request_hash = _request_hash(f"create_prep:{meeting_id}")
    now = datetime.now(UTC)
    pack_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return MeetingPack.model_validate(cached)

        meeting_row = _meeting_row(session, auth, meeting_id)
        if meeting_row is None:
            raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")

        existing = _current_pack_row(session, auth, meeting_id)
        if existing is not None:
            if now >= existing["stale_at"]:
                raise HTTPException(status_code=409, detail="STALE_MEETING_PACK")
            raise HTTPException(status_code=409, detail="MEETING_PACK_EXISTS")

        generated = _generate_pack(session, auth, meeting_id, meeting_row)
        snapshot = _content_to_snapshot(generated.content)
        insert_params = {
            "id": pack_id,
            "workspace_id": auth.workspace_id,
            "meeting_id": meeting_id,
            "now": now,
            "stale_at": now + _STALE_AFTER,
            "source_versions": dumps(generated.fingerprint),
            "content": dumps(snapshot.model_dump(mode="json")),
            "actor_id": auth.user_id,
        }
        try:
            # A nested transaction (SAVEPOINT): the existence check above
            # closes the common case, but under true concurrency two
            # requests can both pass it and both reach this INSERT --
            # `uq_meeting_packs_active_per_meeting` (migration 0027) is
            # the real guard, and the loser must get a clean 409 instead
            # of an unhandled 500 (finding #7). A savepoint keeps that
            # failure from dooming the whole outer transaction so the
            # idempotency-record write below (a distinct concern) still
            # succeeds.
            with session.begin_nested():
                row = (
                    session.execute(
                        text(
                            f"""
                            INSERT INTO meeting_packs (
                                id, workspace_id, meeting_id, status, generated_at, stale_at,
                                source_versions, content, created_by, updated_by,
                                created_at, updated_at, version
                            ) VALUES (
                                :id, :workspace_id, :meeting_id, 'fresh', :now, :stale_at,
                                CAST(:source_versions AS jsonb), CAST(:content AS jsonb),
                                :actor_id, :actor_id, :now, :now, 1
                            )
                            RETURNING {_PACK_FIELDS}
                            """
                        ),
                        insert_params,
                    )
                    .mappings()
                    .one()
                )
        except IntegrityError as exc:
            if _violated_constraint(exc) != "uq_meeting_packs_active_per_meeting":
                raise
            raise HTTPException(status_code=409, detail="MEETING_PACK_EXISTS") from exc
        response = _pack_row_to_response(dict(row), snapshot)
        _write_event(
            session,
            auth,
            request,
            "meeting_pack.generated",
            "meeting_pack",
            meeting_id,
            pack_id,
            1,
            now,
        )
        _store_cached(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), 201, now
        )
        return response


@router.get("/{meeting_id}/prep", response_model=MeetingPack)
def get_prep(meeting_id: UUID, auth: AuthDep, session: SessionDep) -> MeetingPack:
    with session.begin():
        meeting_row = _meeting_row(session, auth, meeting_id)
        if meeting_row is None:
            raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
        pack_row = _current_pack_row(session, auth, meeting_id, for_update=True)
        if pack_row is None:
            raise HTTPException(status_code=404, detail="MEETING_PACK_NOT_FOUND")
        now = datetime.now(UTC)
        # Staleness is only ever a *status* flip -- it never regenerates or
        # overwrites the persisted `content` snapshot. A GET always returns
        # the pack exactly as it was generated (finding #6): the caller
        # sees `status: "stale"` as a signal to call .../prep/refresh, not
        # silently-updated content.
        if pack_row["status"] == "fresh" and _is_stale(
            session, auth, meeting_id, pack_row, meeting_row, now
        ):
            updated_row = (
                session.execute(
                    text(
                        f"""
                        UPDATE meeting_packs
                        SET status = 'stale', updated_by = :actor_id, updated_at = :now
                        WHERE workspace_id = :workspace_id AND id = :id
                        RETURNING {_PACK_FIELDS}
                        """
                    ),
                    {
                        "actor_id": auth.user_id,
                        "now": now,
                        "workspace_id": auth.workspace_id,
                        "id": pack_row["id"],
                    },
                )
                .mappings()
                .one()
            )
            pack_row = dict(updated_row)
        snapshot = PackContentSnapshot.model_validate(pack_row["content"])
        return _pack_row_to_response(pack_row, snapshot)


@router.post(
    "/{meeting_id}/prep/refresh", response_model=MeetingPack, status_code=status.HTTP_201_CREATED
)
def refresh_prep(
    meeting_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> MeetingPack:
    request_hash = _request_hash(f"refresh_prep:{meeting_id}")
    now = datetime.now(UTC)
    new_pack_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return MeetingPack.model_validate(cached)

        meeting_row = _meeting_row(session, auth, meeting_id)
        if meeting_row is None:
            raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")

        old = _current_pack_row(session, auth, meeting_id, for_update=True)
        if old is None:
            raise HTTPException(status_code=404, detail="MEETING_PACK_NOT_FOUND")

        # Retire the old pack *before* inserting the new one:
        # uq_meeting_packs_active_per_meeting (migration 0027) allows only
        # one fresh-or-stale row per meeting at a time, checked immediately
        # per-statement, so inserting the new 'fresh' row while the old one
        # is still 'fresh'/'stale' would violate it.
        session.execute(
            text(
                """
                UPDATE meeting_packs
                SET status = 'refreshed', updated_by = :actor_id, updated_at = :now
                WHERE workspace_id = :workspace_id AND id = :id
                """
            ),
            {
                "actor_id": auth.user_id,
                "now": now,
                "workspace_id": auth.workspace_id,
                "id": old["id"],
            },
        )

        generated = _generate_pack(session, auth, meeting_id, meeting_row)
        snapshot = _content_to_snapshot(generated.content)
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO meeting_packs (
                        id, workspace_id, meeting_id, status, generated_at, stale_at,
                        source_versions, content, created_by, updated_by,
                        created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :meeting_id, 'fresh', :now, :stale_at,
                        CAST(:source_versions AS jsonb), CAST(:content AS jsonb),
                        :actor_id, :actor_id, :now, :now, 1
                    )
                    RETURNING {_PACK_FIELDS}
                    """
                ),
                {
                    "id": new_pack_id,
                    "workspace_id": auth.workspace_id,
                    "meeting_id": meeting_id,
                    "now": now,
                    "stale_at": now + _STALE_AFTER,
                    "source_versions": dumps(generated.fingerprint),
                    "content": dumps(snapshot.model_dump(mode="json")),
                    "actor_id": auth.user_id,
                },
            )
            .mappings()
            .one()
        )
        response = _pack_row_to_response(dict(row), snapshot)
        _write_event(
            session,
            auth,
            request,
            "meeting_pack.refreshed",
            "meeting_pack",
            meeting_id,
            new_pack_id,
            1,
            now,
        )
        _store_cached(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), 201, now
        )
        return response
