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
- Visibility (FX1): a pack is stored once per meeting (``visibility =
  'workspace'``) and served to every reader of the meeting, so the stored
  snapshot -- and the fingerprint and AI enrichment computed from it -- is
  built only from ``workspace``-visible participants and rows. Each
  caller's response adds, per request and never stored, the private or
  explicitly-shared rows that caller may read (``_caller_view``). Not
  flag-gated: such rows exist without ``ECC_PERSONAL_DATA_ISOLATION``.
- AI enrichment (Phase 4-consuming wiring, this change): a bounded,
  fail-open ``meeting.prep_summary`` run (``ai_runtime/runtime.py``),
  gated on ``config.py``'s ``meeting_prep_ai_enrichment_enabled`` (still
  default ``False`` -- flipping *that* per deployment is a separate,
  later decision from wiring the capability itself). Computed once, at
  pack-generation time (``create_prep``/``refresh_prep``), and persisted
  into ``PackContentSnapshot.enrichment`` -- never recomputed on a later
  GET, matching every other field's frozen-snapshot discipline (finding
  #6). Any non-``completed`` run (disabled, no eligible model, timeout,
  budget exceeded, grounding failure, ...) surfaces as
  ``available=False`` with that run's own ``error_code`` -- the
  deterministic pack always generates regardless.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import execute_run, get_ollama_adapter
from ecc.domains.calendar.events import get_calendar_event_summary
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz
from ecc.platform.connector_security import personal_data_isolation_enabled
from ecc.platform.idempotency import (
    held_idempotency_lock,
    load_cached,
    lock_idempotency,
    request_hash,
    store_idempotency,
)
from ecc.platform.request_models import EmptyBody as _EmptyBody

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

    ``enrichment`` included here (not computed separately at response-
    render time, as it was before this change): AI enrichment is exactly
    as much a part of "everything computed at generation time" as any
    other field above, and computing it fresh on every GET would both
    waste a live model call per page load and could silently return a
    *different* summary across repeated GETs of the same nominally-frozen
    pack -- the same "real snapshot" property finding #6 already
    established for every other field, just not correctly applied to
    this one until now.
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
    enrichment: EnrichmentOut


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
# Audit helper. The idempotency lock/cache quartet this comment used to
# introduce (`_lock_idempotency`/`_request_hash`/`_load_cached`/
# `_store_cached`) moved to `ecc.platform.idempotency` -- see that module's
# own docstring.
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


# ---------------------------------------------------------------------------
# Fetch helpers (impure) -- one per composed domain, workspace-scoped.
# ---------------------------------------------------------------------------


def require_meeting_read(session: Session, auth: AuthContext, meeting_id: UUID) -> None:
    """Every endpoint in this module takes a meeting_id path parameter and
    treats the meeting as the authorization boundary for the child
    resources it reads or writes (participants, packs) -- the same
    parent-authorization-boundary pattern claims.py/relationships.py use
    for pkos_nodes. Collapses into the same MEETING_NOT_FOUND 404 every
    call site already raises for a nonexistent meeting_id, matching this
    router's own pre-existing convention (every endpoint here already
    404s on an unknown meeting, never a 200-empty-list) -- an
    invisible-but-real meeting must read identically to a nonexistent one.
    """
    if not authz.authorize(
        session, auth, resource_type="meetings", resource_id=meeting_id, action="read"
    ):
        raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")


def _require_meeting_write(session: Session, auth: AuthContext, meeting_id: UUID) -> None:
    if not authz.authorize(
        session, auth, resource_type="meetings", resource_id=meeting_id, action="write"
    ):
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")


def get_meeting_row(
    session: Session, auth: AuthContext, meeting_id: UUID, *, for_share: bool = False
) -> dict[str, Any] | None:
    suffix = " FOR SHARE" if for_share else ""
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


def _lock_meeting_for_write(
    session: Session, auth: AuthContext, meeting_id: UUID
) -> dict[str, Any]:
    """Lock the meeting row, then authorize the write against it.

    The meeting is the authorization boundary for every participant and
    pack write here, and `authz_grants`' ownership transfer locks that same
    row `FOR UPDATE` before rewriting `owner_id`. Authorizing first and
    reading the row unlocked afterwards let a transfer commit between the
    check and the write, so the caller wrote into a meeting it could no
    longer see. Taking the lock first makes a concurrent transfer either
    commit before this point (and the checks below see the new owner) or
    wait until this transaction ends.

    `FOR SHARE`, not `FOR UPDATE`: nothing here writes the meeting row
    itself, and a shared lock still conflicts with the transfer's (and any
    meeting edit's) `FOR UPDATE` while letting concurrent prep/participant
    writes on the same meeting proceed side by side. A missing row answers
    the same 404 an invisible one does.
    """
    meeting_row = get_meeting_row(session, auth, meeting_id, for_share=True)
    if meeting_row is None:
        raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
    require_meeting_read(session, auth, meeting_id)
    _require_meeting_write(session, auth, meeting_id)
    return meeting_row


def _meeting_input(session: Session, auth: AuthContext, row: dict[str, Any]) -> MeetingInput:
    if row["calendar_event_id"] is None:
        starts_at, ends_at, tz = (
            row["standalone_starts_at"],
            row["standalone_ends_at"],
            row["standalone_timezone"],
        )
    else:
        event = get_calendar_event_summary(session, auth, row["calendar_event_id"])
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


class _ReadFilters:
    """One request's ``authz.visible_resource_filter_sql`` (``read``, for the
    caller) fragments, memoized per resource type. Each helper call costs a
    role and an account lookup, and one prep request needs the same
    fragments twice -- for the shared snapshot and for the caller's own
    rows (``_caller_view``). Create one per transaction; never share one
    across callers."""

    def __init__(self, session: Session, auth: AuthContext) -> None:
        self._session = session
        self._auth = auth
        self._cache: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}

    def fragment(self, resource_type: str, table_alias: str) -> tuple[str, dict[str, object]]:
        key = (resource_type, table_alias)
        cached = self._cache.get(key)
        if cached is None:
            cached = authz.visible_resource_filter_sql(
                self._session,
                self._auth,
                resource_type=resource_type,
                action="read",
                table_alias=table_alias,
                param_prefix=f"{resource_type}_",
            )
            self._cache[key] = cached
        return cached


def _fetch_participant_rows(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    *,
    filters: _ReadFilters | None = None,
) -> list[tuple[ParticipantRow, bool]]:
    """Every participant whose person node the caller may read, each paired
    with whether that node is ``workspace``-visible (so may enter the shared
    snapshot -- see ``generate_pack``)."""
    # Found in the fourth whole-phase review: this used to join `pkos_
    # nodes` for `canonical_name` with no visibility filter on it at all --
    # `add_participant`'s own write-time authz check (see that endpoint's
    # own fix) only stops a NEW link to an invisible entity; it does
    # nothing for a participant entity narrowed to `private`/`shared_
    # explicitly` *after* being linked, which this read path would keep
    # leaking regardless. Filtering the live `pkos_nodes` row here, the
    # same "check the live source at read time" pattern `retrieval.py`'s
    # own search fix already established, closes both cases at once.
    visibility_sql, visibility_params = (filters or _ReadFilters(session, auth)).fragment(
        "pkos_nodes", "n"
    )
    rows = (
        session.execute(
            text(
                f"""
                SELECT mp.id, mp.entity_id, mp.role, n.canonical_name, n.visibility
                FROM meeting_participants mp
                JOIN pkos_nodes n ON n.workspace_id = mp.workspace_id AND n.id = mp.entity_id
                WHERE mp.workspace_id = :workspace_id AND mp.meeting_id = :meeting_id
                  AND ({visibility_sql})
                ORDER BY mp.created_at, mp.id
                """  # noqa: S608 -- authz visibility fragment; values bound
            ),
            {"workspace_id": auth.workspace_id, "meeting_id": meeting_id, **visibility_params},
        )
        .mappings()
        .all()
    )
    return [
        (
            ParticipantRow(
                id=r["id"],
                entity_id=r["entity_id"],
                entity_name=r["canonical_name"],
                role=r["role"],
            ),
            r["visibility"] == "workspace",
        )
        for r in rows
    ]


def _fetch_participants(
    session: Session, auth: AuthContext, meeting_id: UUID
) -> list[ParticipantRow]:
    return [p for p, _shared in _fetch_participant_rows(session, auth, meeting_id)]


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


# Which slice of the caller-readable rows a fetcher returns (FX1). A pack is
# stored once per meeting and served to every reader of it, so the stored
# snapshot ("shared") holds only rows every such reader may read:
# `workspace`-visible rows (every active member reads those -- authz step 5)
# keyed to the snapshot's own `workspace`-visible participants. "private"
# is the exact complement within what *this* caller may read -- their own
# private rows, rows explicitly shared with them, and anything keyed to a
# participant node only they can see -- computed per request by
# `_caller_view` and never stored. Both slices sit on top of
# `authz.visible_resource_filter_sql` for the caller, and neither is
# flag-gated: private and `shared_explicitly` rows exist without
# ECC_PERSONAL_DATA_ISOLATION (grants with `narrow_visibility`, delegations,
# planning).
_Scope = Literal["shared", "private"]


def _scoped_visibility_sql(
    filters: _ReadFilters,
    *,
    resource_type: str,
    table_alias: str,
    scope: _Scope,
    shared_entity_ids: list[UUID] | None = None,
    shared_key_column: str | None = None,
) -> tuple[str, dict[str, object]]:
    """The caller's ``authz.visible_resource_filter_sql`` (read) fragment
    narrowed to one ``_Scope``. For a participant-keyed fetcher in the
    "private" scope, ``shared_key_column``/``shared_entity_ids`` name the
    snapshot's own participants: a ``workspace`` row keyed to a participant
    that is *not* in the snapshot (a node only the caller can see) belongs
    to the caller's slice. (In the "shared" scope the fetcher is already
    given only the snapshot's participants.) Bind names are prefixed per
    resource type, so fragments never collide with each other or with the
    fetcher's own params."""
    visible_sql, params = filters.fragment(resource_type, table_alias)
    return _scope_clause(
        visible_sql,
        params,
        table_alias=table_alias,
        scope=scope,
        param_prefix=f"{resource_type}_",
        shared_entity_ids=shared_entity_ids,
        shared_key_column=shared_key_column,
    )


def _scope_clause(
    visible_sql: str,
    params: dict[str, object],
    *,
    table_alias: str,
    scope: _Scope,
    param_prefix: str,
    shared_entity_ids: list[UUID] | None,
    shared_key_column: str | None,
) -> tuple[str, dict[str, object]]:
    """AND a caller's visibility fragment with one ``_Scope``'s split. The
    "private" clause is written as the exact logical complement of the
    "shared" one with ``IS DISTINCT FROM`` / ``<> ALL``, so a row can never
    fall out of both slices (every ``visibility`` and key column involved
    is ``NOT NULL`` today; this keeps the split total if one ever is not)."""
    if scope == "shared":
        return f"({visible_sql}) AND {table_alias}.visibility = 'workspace'", params
    not_in_snapshot = f"{table_alias}.visibility IS DISTINCT FROM 'workspace'"
    if shared_key_column is not None:
        shared_ids = f"{param_prefix}shared_entity_ids"
        not_in_snapshot = (
            f"({not_in_snapshot} OR {table_alias}.{shared_key_column} IS NULL "
            f"OR {table_alias}.{shared_key_column} <> ALL(CAST(:{shared_ids} AS uuid[])))"
        )
        params = {**params, shared_ids: shared_entity_ids or []}
    return f"({visible_sql}) AND {not_in_snapshot}", params


def _fetch_timeline(
    session: Session,
    auth: AuthContext,
    entity_ids: list[UUID],
    *,
    scope: _Scope = "shared",
    shared_entity_ids: list[UUID] | None = None,
    filters: _ReadFilters | None = None,
) -> list[TimelineRow]:
    if not entity_ids:
        return []
    scope_sql, scope_params = _scoped_visibility_sql(
        filters or _ReadFilters(session, auth),
        resource_type="timeline_entries",
        table_alias="t",
        scope=scope,
        shared_entity_ids=shared_entity_ids,
        shared_key_column="entity_id",
    )
    rows = (
        session.execute(
            text(
                f"""
                SELECT t.id, t.entity_id, t.effective_at, t.event_type, t.summary
                FROM timeline_entries t
                WHERE t.workspace_id = :workspace_id AND t.entity_id = ANY(:entity_ids)
                  AND {scope_sql}
                ORDER BY t.effective_at DESC, t.id DESC
                LIMIT :limit
                """  # noqa: S608 -- authz visibility fragment; values bound
            ),
            {
                "workspace_id": auth.workspace_id,
                "entity_ids": entity_ids,
                "limit": _MAX_TIMELINE_ENTRIES,
                **scope_params,
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
    session: Session,
    auth: AuthContext,
    participant_entity_ids: list[UUID],
    *,
    scope: _Scope = "shared",
    shared_entity_ids: list[UUID] | None = None,
    filters: _ReadFilters | None = None,
) -> list[CommitmentRow]:
    if not participant_entity_ids:
        return []
    scope_sql, scope_params = _scoped_visibility_sql(
        filters or _ReadFilters(session, auth),
        resource_type="commitments",
        table_alias="c",
        scope=scope,
        shared_entity_ids=shared_entity_ids,
        shared_key_column="counterparty_person_id",
    )
    rows = (
        session.execute(
            text(
                f"""
                SELECT c.id, c.direction, c.summary, c.status, c.due_at, c.counterparty_name
                FROM commitments c
                WHERE c.workspace_id = :workspace_id
                  AND c.counterparty_person_id = ANY(:entity_ids)
                  AND c.status IN ('confirmed', 'active')
                  AND c.archived_at IS NULL
                  AND {scope_sql}
                ORDER BY c.due_at NULLS LAST, c.id
                LIMIT :limit
                """  # noqa: S608 -- authz visibility fragment; values bound
            ),
            {
                "workspace_id": auth.workspace_id,
                "entity_ids": participant_entity_ids,
                "limit": _MAX_COMMITMENTS,
                **scope_params,
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


def _fetch_notes(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    *,
    scope: _Scope = "shared",
    filters: _ReadFilters | None = None,
) -> list[NoteRow]:
    # Restricted notes are excluded here, in the SQL WHERE clause, *before*
    # LIMIT is applied -- not filtered out afterward in Python (build_pack's
    # filter stays as a defense-in-depth belt-and-suspenders check, but
    # must never be the *only* place this happens). Filtering post-fetch
    # let LIMIT :limit count restricted rows against the cap, so a meeting
    # with _MAX_NOTES-or-more restricted notes could return fewer than
    # _MAX_NOTES visible notes even when more visible ones existed beyond
    # the truncated window (finding #8). The visibility scope sits in the
    # same WHERE clause for the same reason.
    scope_sql, scope_params = _scoped_visibility_sql(
        filters or _ReadFilters(session, auth), resource_type="notes", table_alias="nt", scope=scope
    )
    rows = (
        session.execute(
            text(
                f"""
                SELECT nt.id, nt.title, nt.body, nt.note_type, nt.restricted, nt.created_at
                FROM notes nt
                WHERE nt.workspace_id = :workspace_id AND nt.meeting_id = :meeting_id
                  AND nt.archived_at IS NULL AND nt.restricted = false
                  AND {scope_sql}
                ORDER BY nt.created_at DESC, nt.id
                LIMIT :limit
                """  # noqa: S608 -- authz visibility fragment; values bound
            ),
            {
                "workspace_id": auth.workspace_id,
                "meeting_id": meeting_id,
                "limit": _MAX_NOTES,
                **scope_params,
            },
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


def _fetch_risks(
    session: Session,
    auth: AuthContext,
    *,
    scope: _Scope = "shared",
    filters: _ReadFilters | None = None,
) -> list[RiskRow]:
    # Workspace-wide, not participant-scoped -- see module docstring.
    scope_sql, scope_params = _scoped_visibility_sql(
        filters or _ReadFilters(session, auth), resource_type="risks", table_alias="r", scope=scope
    )
    rows = (
        session.execute(
            text(
                f"""
                SELECT r.id, r.description, r.status, r.probability, r.impact, r.review_at
                FROM risks r
                WHERE r.workspace_id = :workspace_id AND r.status <> 'closed'
                  AND r.archived_at IS NULL
                  AND {scope_sql}
                ORDER BY r.review_at NULLS LAST, r.id
                LIMIT :limit
                """  # noqa: S608 -- authz visibility fragment; values bound
            ),
            {"workspace_id": auth.workspace_id, "limit": _MAX_RISKS, **scope_params},
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
    session: Session,
    auth: AuthContext,
    participant_entity_ids: list[UUID],
    *,
    scope: _Scope = "shared",
    shared_entity_ids: list[UUID] | None = None,
    filters: _ReadFilters | None = None,
) -> list[DependencyRow]:
    if not participant_entity_ids:
        return []
    scope_sql, scope_params = _scoped_visibility_sql(
        filters or _ReadFilters(session, auth),
        resource_type="waiting_links",
        table_alias="w",
        scope=scope,
        shared_entity_ids=shared_entity_ids,
        shared_key_column="counterparty_entity_id",
    )
    rows = (
        session.execute(
            text(
                f"""
                SELECT w.id, w.direction, w.note, w.expected_at
                FROM waiting_links w
                WHERE w.workspace_id = :workspace_id
                  AND w.status = 'open'
                  AND w.counterparty_entity_id = ANY(:entity_ids)
                  AND {scope_sql}
                ORDER BY w.expected_at NULLS LAST, w.id
                LIMIT :limit
                """  # noqa: S608 -- authz visibility fragment; values bound
            ),
            {
                "workspace_id": auth.workspace_id,
                "entity_ids": participant_entity_ids,
                "limit": _MAX_DEPENDENCIES,
                **scope_params,
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


def _fetch_evidence(
    session: Session,
    auth: AuthContext,
    node_ids: list[UUID],
    *,
    scope: _Scope = "shared",
    shared_entity_ids: list[UUID] | None = None,
) -> list[EvidenceRow]:
    if not node_ids:
        return []
    # Spec A S1.14: only evidence the caller may read (flag-gated). Flag
    # on, the shared/private split applies like every other fetcher's, so
    # the snapshot and its fingerprint are the same for every caller and
    # no reader's GET can flip the pack stale over their own evidence.
    # Flag off, evidence rows are unfiltered (pre-flag behaviour) and split
    # only by participant: every row on a snapshot participant is shared;
    # the "private" slice is every row on a participant node only the
    # caller can see (so its owner still gets its gaps, as before FX1).
    if personal_data_isolation_enabled():
        visibility_sql, visibility_params = _scope_clause(
            *authz.evidence_visibility_filter_sql(session, auth, table_alias="pkos_evidence"),
            table_alias="pkos_evidence",
            scope=scope,
            param_prefix="evidence_",
            shared_entity_ids=shared_entity_ids,
            shared_key_column="node_id",
        )
    elif scope == "private":
        visibility_sql = "pkos_evidence.node_id <> ALL(CAST(:evidence_shared_entity_ids AS uuid[]))"
        visibility_params = {"evidence_shared_entity_ids": shared_entity_ids or []}
    else:
        visibility_sql, visibility_params = "TRUE", {}
    rows = (
        session.execute(
            text(
                f"""
                SELECT id, source_type, evidence_state
                FROM pkos_evidence
                WHERE workspace_id = :workspace_id AND node_id = ANY(:node_ids)
                  AND ({visibility_sql})
                ORDER BY id
                LIMIT :limit
                """  # noqa: S608 -- authz visibility fragment; values bound
            ),
            {
                "workspace_id": auth.workspace_id,
                "node_ids": node_ids,
                "limit": _MAX_EVIDENCE,
                **visibility_params,
            },
        )
        .mappings()
        .all()
    )
    return [
        EvidenceRow(id=r["id"], source_type=r["source_type"], evidence_state=r["evidence_state"])
        for r in rows
    ]


def _resolve_ollama_adapter(request: Request) -> OllamaAdapter:
    """Resolves the same provider `OllamaAdapterDep` (`Depends(get_
    ollama_adapter)`) would -- including honoring `app.dependency_
    overrides[get_ollama_adapter]`, exactly how tests inject a mocked
    adapter -- but called explicitly, only from inside `create_prep`/
    `refresh_prep`'s enrichment-enabled branch, instead of declared as an
    eagerly-resolved route parameter.

    This is deliberate, not a style preference: unlike `ai_runtime/
    runtime.py:create_run` (where the adapter is unconditionally needed,
    so declaring `adapter: OllamaAdapterDep` and letting FastAPI resolve
    it upfront is correct), `create_prep`/`refresh_prep` only need a real
    adapter when `meeting_prep_ai_enrichment_enabled` is on -- the
    minority case. `get_ollama_adapter`'s default provider constructs a
    fresh `OllamaAdapter`/`ollama.Client`/`httpx.Client` per call, which
    is not free (this module's own p95 pack-generation budget test caught
    it: constructing one unconditionally on every request, including the
    far more common enrichment-disabled path where it is never used, was
    a measurable, real per-request cost). Resolving it lazily here means
    the disabled path -- `_compute_enrichment`'s own guaranteed no-op --
    never pays for it at all.
    """
    override = request.app.dependency_overrides.get(get_ollama_adapter)
    return override() if override is not None else get_ollama_adapter()


def _compute_enrichment(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    *,
    ollama_adapter: OllamaAdapter | None = None,
) -> EnrichmentOut:
    """MEETING-PREP-CONTRACT.md's "Optional enrichment": AI may summarize
    retrieved authorized evidence behind a feature flag. Called once, at
    pack-generation time (``create_prep``/``refresh_prep``), and its
    result persisted into ``PackContentSnapshot.enrichment`` -- see that
    class's docstring for why this must never be recomputed at GET time.

    Fail-open by construction, mirroring ``ai_runtime/runtime.py:execute_
    run``'s own fail-open discipline: any non-``completed`` run (feature
    disabled, no eligible model, timeout, budget exceeded, a grounding
    failure, ...) surfaces as ``available=False`` carrying that run's own
    ``error_code`` -- this function never raises, and the deterministic
    pack always still generates regardless of what happens here.

    Runs synchronously inside the caller's transaction, exactly like
    ``ai_runtime/runtime.py``'s own ``POST /ai/runs`` endpoint calls
    ``execute_run`` synchronously within its own request -- not a new
    pattern introduced here. Bounded by that run's own budget (60s total
    wall clock, ``budgets.py``), and a no-op entirely (an immediate
    ``feature_disabled`` return, no model call attempted) whenever the
    flag is off, which is this activation's default.

    ``ollama_adapter`` mirrors ``execute_run``'s own optional parameter --
    ``None`` in production (`execute_run` falls back to a real
    ``OllamaAdapter()``), a test-supplied mocked transport in tests,
    threaded from ``create_prep``/``refresh_prep``'s own
    ``_resolve_ollama_adapter(request)`` call -- see that helper's
    docstring for why it is resolved lazily there rather than declared as
    an eagerly-resolved ``OllamaAdapterDep`` route parameter like
    ``ai_runtime/runtime.py:create_run`` uses for ``POST /ai/runs``.
    """
    if not get_settings().meeting_prep_ai_enrichment_enabled:
        return EnrichmentOut(available=False, summary=None, error_code="feature_disabled")

    run = execute_run(
        "meeting.prep_summary",
        "sensitive",
        {"meeting_id": str(meeting_id)},
        session=session,
        auth=auth,
        ollama_adapter=ollama_adapter,
    )
    if run.status != "completed" or run.output is None:
        return EnrichmentOut(available=False, summary=None, error_code=run.error_code)
    return EnrichmentOut(available=True, summary=run.output["summary_text"], error_code=None)


@dataclass(frozen=True)
class _GeneratedPack:
    content: PackContent
    fingerprint: dict[str, str]


def generate_pack(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    meeting_row: dict[str, Any],
    *,
    filters: _ReadFilters | None = None,
) -> _GeneratedPack:
    """The shared snapshot: what gets stored in ``meeting_packs`` and is
    served to every reader of the meeting, fingerprinted for staleness and
    handed to the ``meeting.prep_summary`` enrichment run. Built only from
    ``workspace``-visible participants and rows (``_Scope`` "shared"), so it
    never depends on who generated it and never copies anyone's private or
    explicitly-shared rows (FX1). Each caller's own extra rows are added
    per request by ``_caller_view``, never stored.

    Evidence follows the same split, but only with the Spec A flag on
    (T21's evidence filter is flag-gated; flag off, every evidence row is
    shared exactly as before)."""
    meeting = _meeting_input(session, auth, meeting_row)
    filters = filters or _ReadFilters(session, auth)
    participants = [
        p
        for p, shared in _fetch_participant_rows(session, auth, meeting_id, filters=filters)
        if shared
    ]
    participant_entity_ids = [p.entity_id for p in participants]
    timeline = _fetch_timeline(session, auth, participant_entity_ids, filters=filters)
    commitments = _fetch_commitments(session, auth, participant_entity_ids, filters=filters)
    notes = _fetch_notes(session, auth, meeting_id, filters=filters)
    risks = _fetch_risks(session, auth, filters=filters)
    dependencies = _fetch_dependencies(session, auth, participant_entity_ids, filters=filters)
    evidence = _fetch_evidence(session, auth, participant_entity_ids)
    content = build_pack(
        meeting, participants, timeline, commitments, notes, risks, dependencies, evidence
    )
    fingerprint = _source_fingerprint(
        meeting, participants, timeline, commitments, notes, risks, dependencies, evidence
    )
    return _GeneratedPack(content=content, fingerprint=fingerprint)


_LATEST = datetime.max.replace(tzinfo=UTC)


class _HasId(Protocol):
    @property
    def id(self) -> UUID: ...


def _merge_rows[T: _HasId](
    base: list[T], extra: list[T], *, key: Callable[[T], Any], limit: int, reverse: bool = False
) -> list[T]:
    """``base`` plus ``extra`` in the fetcher's own SQL order, capped at its
    own limit: the top-N of two top-N lists is the top-N of their union, so
    the result equals one caller-filtered query. An id in both keeps the
    live (``extra``) row."""
    if not extra:
        return base
    extra_ids = {row.id for row in extra}
    merged = [row for row in base if row.id not in extra_ids] + extra
    return sorted(merged, key=key, reverse=reverse)[:limit]


def _caller_view(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    snapshot: PackContentSnapshot,
    *,
    filters: _ReadFilters | None = None,
) -> PackContentSnapshot:
    """The stored shared snapshot plus the rows only *this* caller may read
    (``_Scope`` "private": their own private rows, rows explicitly shared
    with them, and rows about participant nodes only they can see).
    Computed per request and returned, never persisted and never part of
    the fingerprint -- so one member's private rows neither reach another
    member nor flip the shared pack's stale flag. With no such rows (every
    workspace-only pack) the snapshot is returned unchanged."""
    filters = filters or _ReadFilters(session, auth)
    participant_rows = _fetch_participant_rows(session, auth, meeting_id, filters=filters)
    shared_ids = [p.entity_id for p, shared in participant_rows if shared]
    private_participants = [p for p, shared in participant_rows if not shared]
    all_ids = [p.entity_id for p, _shared in participant_rows]
    timeline = _fetch_timeline(
        session, auth, all_ids, scope="private", shared_entity_ids=shared_ids, filters=filters
    )
    commitments = _fetch_commitments(
        session, auth, all_ids, scope="private", shared_entity_ids=shared_ids, filters=filters
    )
    notes = _fetch_notes(session, auth, meeting_id, scope="private", filters=filters)
    risks = _fetch_risks(session, auth, scope="private", filters=filters)
    dependencies = _fetch_dependencies(
        session, auth, all_ids, scope="private", shared_entity_ids=shared_ids, filters=filters
    )
    evidence = _fetch_evidence(
        session, auth, all_ids, scope="private", shared_entity_ids=shared_ids
    )
    if not (
        private_participants or timeline or commitments or notes or risks or dependencies
    ) and all(e.evidence_state == "available" for e in evidence):
        return snapshot

    meeting = MeetingInput(
        id=meeting_id,
        title=snapshot.objective,
        agenda=None,
        starts_at=snapshot.starts_at,
        ends_at=snapshot.ends_at,
        timezone=snapshot.timezone,
    )
    extra = _content_to_snapshot(
        build_pack(
            meeting,
            private_participants,
            timeline,
            commitments,
            notes,
            risks,
            dependencies,
            evidence,
        ),
        snapshot.enrichment,
    )

    extra_participant_ids = {p.id for p in extra.participants}
    participant_order = {p.id: index for index, (p, _shared) in enumerate(participant_rows)}

    def _nullable_last(value: datetime | None) -> tuple[bool, datetime]:
        return (value is None, value or _LATEST)

    # Notes share one fetch (and one limit) across decisions and general
    # notes: `created_at DESC, id` -- id ascending within a timestamp, so
    # sort by id first and then (stably) by created_at descending.
    decisions, general_notes = snapshot.decisions, snapshot.notes
    if extra.decisions or extra.notes:
        by_id = _merge_rows(
            [*snapshot.decisions, *snapshot.notes],
            [*extra.decisions, *extra.notes],
            key=lambda n: n.id,
            limit=2 * _MAX_NOTES,  # both inputs are already capped at _MAX_NOTES
        )
        all_notes = sorted(by_id, key=lambda n: n.created_at, reverse=True)[:_MAX_NOTES]
        decisions = [n for n in all_notes if n.note_type == "decision"]
        general_notes = [n for n in all_notes if n.note_type != "decision"]
    return snapshot.model_copy(
        update={
            # One list in the fetch's own `mp.created_at, mp.id` order --
            # `participant_rows` is exactly that order; a snapshot
            # participant since unlinked (absent from it) keeps its place
            # at the end.
            "participants": sorted(
                [
                    *(p for p in snapshot.participants if p.id not in extra_participant_ids),
                    *extra.participants,
                ],
                key=lambda p: participant_order.get(p.id, len(participant_order)),
            ),
            "timeline": _merge_rows(
                snapshot.timeline,
                extra.timeline,
                key=lambda t: (t.effective_at, t.id),
                limit=_MAX_TIMELINE_ENTRIES,
                reverse=True,
            ),
            "commitments": _merge_rows(
                snapshot.commitments,
                extra.commitments,
                key=lambda c: (*_nullable_last(c.due_at), c.id),
                limit=_MAX_COMMITMENTS,
            ),
            "decisions": decisions,
            "notes": general_notes,
            "risks": _merge_rows(
                snapshot.risks,
                extra.risks,
                key=lambda r: (*_nullable_last(r.review_at), r.id),
                limit=_MAX_RISKS,
            ),
            "dependencies": _merge_rows(
                snapshot.dependencies,
                extra.dependencies,
                key=lambda d: (*_nullable_last(d.expected_at), d.id),
                limit=_MAX_DEPENDENCIES,
            ),
            # Accepted approximation: each slice caps *evidence rows* at
            # _MAX_EVIDENCE before non-available ones become gaps, so near
            # that cap this merge can show more gaps than one capped query
            # would. It only ever shows rows the caller may read.
            "evidence_gaps": _merge_rows(
                snapshot.evidence_gaps,
                extra.evidence_gaps,
                key=lambda e: e.id,
                limit=_MAX_EVIDENCE,
            ),
        }
    )


def _content_to_snapshot(content: PackContent, enrichment: EnrichmentOut) -> PackContentSnapshot:
    """Render a freshly-generated ``PackContent`` into the exact JSON-able
    shape persisted as ``meeting_packs.content`` (finding #6). Called once,
    at generation time (``create_prep``/``refresh_prep``) -- never at GET
    time, which loads the already-persisted snapshot instead of calling
    this again. ``enrichment`` is computed by the caller (``_compute_
    enrichment``) rather than here, matching this function's existing
    "pure rendering, no I/O" shape -- computing it would need `session`/
    `auth` and a live model call, neither of which any other field this
    function renders needs.
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
        enrichment=enrichment,
    )


def _pack_row_to_response(row: dict[str, Any], snapshot: PackContentSnapshot) -> MeetingPack:
    """Merge a ``meeting_packs`` row's own columns with its persisted
    content snapshot. ``snapshot`` always comes from ``row["content"]``
    (the frozen body stored at generation time) -- never from a fresh
    ``generate_pack``/``_compute_enrichment`` call, which would defeat
    the point of storing it. ``snapshot.model_dump()`` already supplies
    ``enrichment`` (part of ``PackContentSnapshot`` -- see its docstring),
    so this never recomputes it.
    """
    return MeetingPack(
        id=row["id"],
        meeting_id=row["meeting_id"],
        status=row["status"],
        generated_at=row["generated_at"],
        stale_at=row["stale_at"],
        source_versions=dict(row["source_versions"]),
        **snapshot.model_dump(),
    )


def _is_stale(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    pack_row: dict[str, Any],
    meeting_row: dict[str, Any],
    now: datetime,
    *,
    filters: _ReadFilters | None = None,
) -> bool:
    if now >= pack_row["stale_at"]:
        return True
    generated = generate_pack(session, auth, meeting_id, meeting_row, filters=filters)
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
    req_hash = request_hash(payload, f"add_participant:{meeting_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="meeting_prep")
        if cached is not None:
            return ParticipantResponse.model_validate(cached)

        _lock_meeting_for_write(session, auth, meeting_id)
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
        # Found in the fourth whole-phase review: the existence check above
        # let any caller who can write to the *meeting* link an arbitrary
        # `pkos_nodes` id into it regardless of that entity's own
        # visibility, immediately disclosing its `canonical_name` in this
        # endpoint's own 201 response and (until `_fetch_participants`'
        # sibling fix) every later read of the meeting's participants and
        # generated prep packs. Same existence-then-authorize pattern
        # `waiting.py`'s own precedent already established, folded into the
        # same 404 a nonexistent entity returns.
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=payload.entity_id, action="read"
        ):
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
                            created_by, updated_by, created_at, updated_at, version,
                            owner_id, visibility
                        ) VALUES (
                            :id, :workspace_id, :meeting_id, :entity_id, :role,
                            :actor_id, :actor_id, :now, :now, 1,
                            :actor_id, 'workspace'
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
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="meeting_participant.linked",
            aggregate_type="meeting_participant",
            aggregate_id=participant_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"meeting_id": str(meeting_id)},
            now=now,
            domain="meeting_prep",
            emit_outbox=False,
        )
        queue_lifecycle_event(
            session, "meeting_participant", "meeting_participant.linked", "allowed"
        )

        response = ParticipantResponse(
            id=participant_id,
            entity_id=payload.entity_id,
            entity_name=entity["canonical_name"],
            role=payload.role,
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now, 201
        )
        return response


@router.get("/{meeting_id}/participants", response_model=ParticipantList)
def list_participants(meeting_id: UUID, auth: AuthDep, session: SessionDep) -> ParticipantList:
    require_meeting_read(session, auth, meeting_id)
    rows = _fetch_participants(session, auth, meeting_id)
    session.rollback()
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
    """Two paths, chosen once up front by the enrichment flag -- not a
    stylistic split, a measured one: the p95 pack-generation budget test
    (`test_build_meeting_pack_p95_under_budget`) caught the fast path
    below regressing to ~3x its budget when this endpoint unconditionally
    paid for three separate committed transactions per call, including on
    the far more common enrichment-disabled request.

    When the flag is off, `_compute_enrichment` is a guaranteed no-op (it
    returns `feature_disabled` immediately, before ever touching the
    session or calling `execute_run`) -- so there is no reason to split
    the transaction at all, and this path is exactly the pre-Phase-4
    shape: one `session.begin()`, the lighter transaction-scoped
    `lock_idempotency` every other endpoint in this module uses.

    When the flag is on, `_compute_enrichment` really does call
    `execute_run`, which unconditionally commits partway through
    (`_persist_terminal`'s own docstring) -- calling it from inside a
    still-open `session.begin()` block closes that transaction out from
    under the caller (confirmed against a real Postgres instance while
    building this: `session.begin_nested()` afterward raised `Can't
    operate on closed transaction`). So only this path pays for splitting
    the body into several short transactions with `_compute_enrichment`
    called bare between them, exactly like `ai_runtime/runtime.py:
    create_run`'s own three-phase shape, with `held_idempotency_lock`
    (a session-scoped lock, not tied to any one transaction) as what
    keeps two concurrent requests carrying the same Idempotency-Key from
    both reaching `execute_run` and each triggering a real model call.
    """
    req_hash = request_hash(_EmptyBody(), f"create_prep:{meeting_id}")
    now = datetime.now(UTC)
    pack_id = uuid4()

    def _insert_pack(
        generated: _GeneratedPack,
        enrichment: EnrichmentOut,
        filters: _ReadFilters | None = None,
    ) -> MeetingPack:
        snapshot = _content_to_snapshot(generated.content, enrichment)
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
                                created_at, updated_at, version, owner_id, visibility
                            ) VALUES (
                                :id, :workspace_id, :meeting_id, 'fresh', :now, :stale_at,
                                CAST(:source_versions AS jsonb), CAST(:content AS jsonb),
                                :actor_id, :actor_id, :now, :now, 1, :actor_id, 'workspace'
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
        # The stored row holds only the shared snapshot; the response (and
        # the actor-scoped idempotency replay below) is this caller's view.
        response = _pack_row_to_response(
            dict(row), _caller_view(session, auth, meeting_id, snapshot, filters=filters)
        )
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="meeting_pack.generated",
            aggregate_type="meeting_pack",
            aggregate_id=pack_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"meeting_id": str(meeting_id)},
            now=now,
            domain="meeting_prep",
        )
        queue_lifecycle_event(session, "meeting_pack", "meeting_pack.generated", "allowed")
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            201,
        )
        return response

    if not get_settings().meeting_prep_ai_enrichment_enabled:
        with session.begin():
            lock_idempotency(session, auth, idempotency_key)
            cached = load_cached(session, auth, idempotency_key, req_hash, domain="meeting_prep")
            if cached is not None:
                return MeetingPack.model_validate(cached)

            meeting_row = _lock_meeting_for_write(session, auth, meeting_id)

            existing = _current_pack_row(session, auth, meeting_id)
            if existing is not None:
                if now >= existing["stale_at"]:
                    raise HTTPException(status_code=409, detail="STALE_MEETING_PACK")
                raise HTTPException(status_code=409, detail="MEETING_PACK_EXISTS")

            filters = _ReadFilters(session, auth)
            generated = generate_pack(session, auth, meeting_id, meeting_row, filters=filters)
            enrichment = EnrichmentOut(available=False, summary=None, error_code="feature_disabled")
            return _insert_pack(generated, enrichment, filters)

    with held_idempotency_lock(auth, idempotency_key):
        with session.begin():
            cached = load_cached(session, auth, idempotency_key, req_hash, domain="meeting_prep")
        if cached is not None:
            return MeetingPack.model_validate(cached)

        with session.begin():
            meeting_row = _lock_meeting_for_write(session, auth, meeting_id)

            existing = _current_pack_row(session, auth, meeting_id)
            if existing is not None:
                if now >= existing["stale_at"]:
                    raise HTTPException(status_code=409, detail="STALE_MEETING_PACK")
                raise HTTPException(status_code=409, detail="MEETING_PACK_EXISTS")

            generated = generate_pack(session, auth, meeting_id, meeting_row)

        enrichment = _compute_enrichment(
            session, auth, meeting_id, ollama_adapter=_resolve_ollama_adapter(request)
        )

        # Enrichment ran outside any transaction, so the authorization above
        # is seconds old by now: re-lock the meeting and re-check before the
        # pack lands.
        with session.begin():
            _lock_meeting_for_write(session, auth, meeting_id)
            return _insert_pack(generated, enrichment)


@router.get("/{meeting_id}/prep", response_model=MeetingPack)
def get_prep(meeting_id: UUID, auth: AuthDep, session: SessionDep) -> MeetingPack:
    with session.begin():
        require_meeting_read(session, auth, meeting_id)
        meeting_row = get_meeting_row(session, auth, meeting_id)
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
        filters = _ReadFilters(session, auth)
        if pack_row["status"] == "fresh" and _is_stale(
            session, auth, meeting_id, pack_row, meeting_row, now, filters=filters
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
        return _pack_row_to_response(
            pack_row, _caller_view(session, auth, meeting_id, snapshot, filters=filters)
        )


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
    """Two paths, chosen once up front by the enrichment flag -- see
    `create_prep`'s identical docstring for why this split exists at all
    (a real, measured p95 regression from unconditionally paying for
    three committed transactions per call, including on the far more
    common enrichment-disabled request) and why the fast path below is
    safe: `_compute_enrichment` is a guaranteed no-op when the flag is
    off, so `execute_run` (whose internal commit is the only reason the
    slow path below needs multiple transactions) is never called.

    Slow path (flag on): retiring `old` and inserting the new row are
    kept together in the *final* transaction (after `_compute_enrichment`
    has already run), not split across the transaction that runs before
    it. An earlier version of this restructuring retired `old` in the
    pre-enrichment transaction; a real bug was found while reviewing that
    shape: if `_compute_enrichment`/`execute_run` ever raised for a
    reason other than the model call itself failing (`execute_run`'s own
    terminal persist can re-raise a genuine `SQLAlchemyError` -- see
    `ai_runtime/runtime.py:_persist_terminal`'s outbox-write path), the
    already-committed retirement of `old` would survive while the new
    row's `INSERT` never ran, leaving the meeting with zero active
    packs. Retiring and inserting together here means either both
    happen or neither does -- if anything above this point raises, `old`
    is untouched.

    This still trades away one property the original single-transaction
    version had: `old`'s `FOR UPDATE` lock (taken below, before
    `_compute_enrichment` runs) is released at that transaction's commit
    and not held across the AI call, so a second `refresh_prep` call
    landing in that exact gap can also pass the `old is not None` check
    and reach this same final transaction. Guarded the same way
    `create_prep`'s single-attempt-per-request `INSERT` already is: the
    loser's `INSERT` (or its `UPDATE ... WHERE status IN (...)` matching
    zero rows because the winner already retired `old`) is caught below
    and turned into a clean `409 MEETING_PACK_EXISTS` rather than an
    unhandled 500 or a second active pack, matching `ai_runtime/
    runtime.py:create_run`'s own established precedent for this same
    "must call execute_run, cannot hold one transaction across it" shape
    (that endpoint's own idempotency-replay path is what makes a
    same-key retry safe either way).
    """
    req_hash = request_hash(_EmptyBody(), f"refresh_prep:{meeting_id}")
    now = datetime.now(UTC)
    new_pack_id = uuid4()

    def _retire_and_insert(
        old_id: UUID,
        generated: _GeneratedPack,
        enrichment: EnrichmentOut,
        filters: _ReadFilters | None = None,
    ) -> MeetingPack:
        # Retire the old pack *before* inserting the new one, both in this
        # one transaction: uq_meeting_packs_active_per_meeting (migration
        # 0027) allows only one fresh-or-stale row per meeting at a time,
        # checked immediately per-statement, so inserting the new 'fresh'
        # row while the old one is still 'fresh'/'stale' would violate it.
        session.execute(
            text(
                """
                UPDATE meeting_packs
                SET status = 'refreshed', updated_by = :actor_id, updated_at = :now
                WHERE workspace_id = :workspace_id AND id = :id
                    AND status IN ('fresh', 'stale')
                """
            ),
            {
                "actor_id": auth.user_id,
                "now": now,
                "workspace_id": auth.workspace_id,
                "id": old_id,
            },
        )
        snapshot = _content_to_snapshot(generated.content, enrichment)
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO meeting_packs (
                        id, workspace_id, meeting_id, status, generated_at, stale_at,
                        source_versions, content, created_by, updated_by,
                        created_at, updated_at, version, owner_id, visibility
                    ) VALUES (
                        :id, :workspace_id, :meeting_id, 'fresh', :now, :stale_at,
                        CAST(:source_versions AS jsonb), CAST(:content AS jsonb),
                        :actor_id, :actor_id, :now, :now, 1, :actor_id, 'workspace'
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
        response = _pack_row_to_response(
            dict(row), _caller_view(session, auth, meeting_id, snapshot, filters=filters)
        )
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="meeting_pack.refreshed",
            aggregate_type="meeting_pack",
            aggregate_id=new_pack_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"meeting_id": str(meeting_id)},
            now=now,
            domain="meeting_prep",
        )
        queue_lifecycle_event(session, "meeting_pack", "meeting_pack.refreshed", "allowed")
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            201,
        )
        return response

    if not get_settings().meeting_prep_ai_enrichment_enabled:
        try:
            with session.begin():
                lock_idempotency(session, auth, idempotency_key)
                cached = load_cached(
                    session, auth, idempotency_key, req_hash, domain="meeting_prep"
                )
                if cached is not None:
                    return MeetingPack.model_validate(cached)

                meeting_row = _lock_meeting_for_write(session, auth, meeting_id)

                old = _current_pack_row(session, auth, meeting_id, for_update=True)
                if old is None:
                    raise HTTPException(status_code=404, detail="MEETING_PACK_NOT_FOUND")

                filters = _ReadFilters(session, auth)
                generated = generate_pack(session, auth, meeting_id, meeting_row, filters=filters)
                enrichment = EnrichmentOut(
                    available=False, summary=None, error_code="feature_disabled"
                )
                return _retire_and_insert(old["id"], generated, enrichment, filters)
        except IntegrityError as exc:
            if _violated_constraint(exc) != "uq_meeting_packs_active_per_meeting":
                raise
            raise HTTPException(status_code=409, detail="MEETING_PACK_EXISTS") from exc

    with held_idempotency_lock(auth, idempotency_key):
        with session.begin():
            cached = load_cached(session, auth, idempotency_key, req_hash, domain="meeting_prep")
        if cached is not None:
            return MeetingPack.model_validate(cached)

        with session.begin():
            meeting_row = _lock_meeting_for_write(session, auth, meeting_id)

            old = _current_pack_row(session, auth, meeting_id, for_update=True)
            if old is None:
                raise HTTPException(status_code=404, detail="MEETING_PACK_NOT_FOUND")

            generated = generate_pack(session, auth, meeting_id, meeting_row)

        enrichment = _compute_enrichment(
            session, auth, meeting_id, ollama_adapter=_resolve_ollama_adapter(request)
        )

        try:
            # Same re-lock and re-check as `create_prep`'s enrichment path.
            with session.begin():
                _lock_meeting_for_write(session, auth, meeting_id)
                return _retire_and_insert(old["id"], generated, enrichment)
        except IntegrityError as exc:
            if _violated_constraint(exc) != "uq_meeting_packs_active_per_meeting":
                raise
            raise HTTPException(status_code=409, detail="MEETING_PACK_EXISTS") from exc
