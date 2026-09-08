---
id: DOMAIN-MODEL
title: Executive Command Center Canonical Domain Model
status: Approved
version: 1.2.0
owner: Lucky Jain
related:
  - RFC-001
  - RFC-004
  - ADR-0003
  - ADR-0006
  - PHASE-001
---

# Canonical Domain Model

For a pure, code-verified glossary of terms actually in use (grouped by real bounded context, kept free of
lifecycle/implementation detail), see [`docs/domain/CONTEXT-MAP.md`](./CONTEXT-MAP.md). This document is the
fuller spec -- lifecycles, invariants, ownership -- corrected in place below where a 2026-09-08 audit found it
had drifted from what's actually implemented.

## Universal rules

Every entity has an immutable UUID, `workspace_id`, UTC timestamps, optimistic `version`, source references and provenance where derived. Soft deletion records `deleted_at` or `archived_at`. External identifiers are never primary keys. Workspace, actor and Phase 1 accountable owner are derived from the authenticated server-side session; browser payloads may not select them.

Domains may reference another domain’s entity ID but may not directly mutate another domain’s storage. Composite workspace foreign keys enforce isolation.

## Ownership map

| Domain | Owns |
|---|---|
| Identity | Workspace, User, Person, Organization |
| Planning | Task, Goal, Project, CalendarEvent, Meeting, Reminder |
| Communication | Conversation, Message, EmailThread, Commitment |
| Knowledge Platform | Note, KnowledgeItem, Document, Decision, Relationship, Evidence |
| Executive Intelligence | Risk, Brief, AttentionItem, Recommendation, RecommendationFeedback, UserFeedback |
| Audit | AuditEvent |
| AI Platform | ModelDefinition, RoutingPolicy, PromptVersion, ToolDefinition, AiRun, AiRunStep, EvaluationSet, EvaluationRun, GeneratedArtifact |
| Integration Platform | ConnectorAccount, SyncCursor, SourceRecord |

Backend package note (Phase 3, approved 2026-07-23): `AttentionItem` moves from `backend/ecc/domains/governance/attention.py` to a dedicated `backend/ecc/domains/attention/` package, which also owns Phase 3's new WaitingLink, RiskReview (history), CapacityProfile, PlanningConstraint, Plan/PlanBlock and MeetingPack records — all still conceptually within the Executive Intelligence domain above, just their own backend package since Phase 3 substantially extends and owns that surface. `Risk`'s CRUD stays in `governance/risks.py`; Phase 3 only adds the `risk_reviews` history table and a review endpoint reading/writing it.

Backend package note (architecture review, 2026-09-04, see SCR-0001): Person and Organization are
Identity-owned per the ownership map above, but `identity/person_organizations.py` has no persistence logic
of its own — `create_person`/`create_organization` build an `EntityCreate` and call
`create_entity_core` in `backend/ecc/domains/knowledge/entities.py`, which writes to Knowledge's own
`pkos_nodes` table and applies Knowledge's own read-side wiring (embeddings, retrieval, timeline). This is a
deliberate, documented tradeoff (`entities.py`'s own docstring acknowledges the tension) rather than an
accidental violation of the "domains may not directly mutate another domain's storage" rule above, but it
means all business rules, validation, and storage schema for these two "Identity-owned" concepts are
actually defined and evolve inside Knowledge — a reader relying on this table to find where Person
validation lives should look in `knowledge/entities.py`, not `identity/`.

Backend package note (Phase 4, approved 2026-07-23): the AI Platform row's entities are the concrete `phase-004/DATA-MODEL.md` records (renamed from this document's earlier placeholder names -- `PromptDefinition`/`ModelExecution`/`EvaluationResult`/`AgentRun` -- to match what `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md` actually specified), owned by a new `backend/ecc/domains/ai_runtime/` package. Domain modules never import a model-provider SDK directly (`ADR-0004`, `ADR-0007`, `ADR-0012`) or call a model outside the AI Runtime's Model Router; `attention/tools.py` and `knowledge/tools.py` add thin read-only tool-handler wrappers in their own existing packages, not new domain ownership.

**Backend package note (domain-modeling audit, 2026-09-08): the Ownership map above has drifted from the real
backend and is corrected here rather than rewritten in place, matching this section's own established
convention.** See `docs/domain/CONTEXT-MAP.md` and its per-context `CONTEXT.md` files for the full,
code-verified glossary this note summarizes.

- **Planning is not one owning package.** `Task` is the only entity `backend/ecc/domains/planning/` actually
  owns. `CalendarEvent` is owned by a separate real package, `backend/ecc/domains/calendar/`. `Meeting` is
  owned by another separate real package, `backend/ecc/domains/scheduling/`, with its own status lifecycle
  independent of CalendarEvent (see the Meeting entry below). `Goal` is not a Planning concept at all -- see
  its own corrected entry below. `Project` and `Reminder` do not exist anywhere in the codebase -- no table,
  class, or endpoint of any kind, despite being named in this table and given full definitions/lifecycles
  below.
- **Communication's row is wrong.** `Conversation` and `Message` do not exist anywhere in the codebase.
  `EmailThread` is real but is owned by `backend/ecc/domains/personal/` (the Gmail Connector sub-area), not
  Communication, and is gated by that context's consent mechanism, not a messaging concept of any kind.
  `Commitment` is the only entity this row actually owns, and it has no code relationship to messaging --  it
  is an obligation-tracking entity linked into the Knowledge Platform's person/evidence graph.
- **Knowledge Platform's `KnowledgeItem` does not exist.** The real, single generic node type is `Entity`
  (`knowledge/entities.py`, table `pkos_nodes`) -- see its corrected entry below.
- **Executive Intelligence's `Brief` does not exist.** The real artifact matching this description is
  `MeetingPack`, generated and owned by `attention/meeting_prep.py` -- see its corrected entry below.
  `RecommendationFeedback` and `UserFeedback` are not first-class entities either: only a write-only
  `recommendation_feedback` table exists, with no schema or response model of its own.
- **Automation is missing from this table entirely.** `backend/ecc/domains/automation/` is a substantial,
  independently-designed domain (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`) owning
  `AutomationPolicy`, `WorkflowVersion`, `WorkflowRun`, `WorkflowRunStep`, `ApprovalRequest`, `KillSwitch` --
  comparable in scope to every other row in this table, with no row of its own.
- **Integration Platform's `SourceRecord` does not exist.** No class, table, or migration of that name exists
  anywhere -- synced provider data lives in named per-provider tables instead (`repositories`, `changes`,
  `reviews`, `engineering_work_items`, Datadog's own monitor/service/dashboard tables). `SyncCursor` has a real
  table but no dedicated class -- it is read and written as raw rows, not a first-class object the way
  `ConnectorAccount` is; listing it here as an entity on par with `ConnectorAccount` overstates its current
  shape.

## Core entities

### Workspace
Top-level authorization and data boundary. Lifecycle: `active -> suspended -> archived`. Includes an IANA timezone used for daily boundaries.

### User
Authenticated actor within a workspace. Phase 1 supports one owner user while retaining multi-user identifiers.

### Person and Organization
People and groups known to ECC. Non-deterministic high-impact merges require human confirmation and retain reversible merge records.

### Project and Goal
**Not built (domain-modeling audit, 2026-09-08).** `Project`, as described below, does not exist anywhere in
the codebase -- no table, class, or endpoint. Projects are bounded outcomes; goals are measurable outcomes.
Project lifecycle: `proposed -> active -> blocked|on_hold -> completed|cancelled -> archived`.

A `Goal` does exist in code, but not as this section describes it: `backend/ecc/domains/personal/habits.py`'s
`goals` table is a per-owner, private habit-tracking construct (`target_count`, `target_at`, scoped to one of
six personal domains: habits, learning, travel, relationships, health, finance) -- unrelated to Task or to any
business "measurable outcome" paired with Project. See `docs/domain/personal/CONTEXT.md`.

### Task
Discrete work with owner, status, manual priority, due date and source. In Phase 1 `owner_id` is always the authenticated user. Lifecycle: `captured -> planned -> in_progress -> blocked -> completed|cancelled -> archived`, with `archived -> previous_terminal_or_active_state` on restore.

### Commitment
A promise made by or to a person, preserving parties and evidence. In Phase 1 `owner_id` is always the authenticated user; counterparty identity remains user-selectable. Lifecycle: `detected -> confirmed -> active -> fulfilled|broken|cancelled -> archived`, with restore returning to the state held before archive. AI-detected commitments remain detected until confirmed.

### CalendarEvent
A scheduled interval. In Phase 1 only local/manual events are supported; external calendar authority is deferred to connector phases. Owned by its own real package, `backend/ecc/domains/calendar/`, not Planning.

### Meeting
Semantic meeting view with agenda, preparation, notes, decisions and follow-ups. It may reference zero or one CalendarEvent. A linked Meeting projects timing from its CalendarEvent and cannot edit timing independently; a standalone Meeting owns its timing. Linking a standalone Meeting adopts CalendarEvent timing while preserving Meeting identity. Owned by its own real package, `backend/ecc/domains/scheduling/`, not Planning -- it is a first-class, independently-lifecycled record, not merely "a semantic layer over CalendarEvent" as the ownership map's grouping might suggest.

### Conversation and Message
**Not built (domain-modeling audit, 2026-09-08).** Neither exists anywhere in the codebase -- no table, class,
or endpoint. A conversation is an ordered communication thread. Messages are immutable artifacts; source
content is preserved as Evidence. The real Communication-adjacent record in code is `Commitment` (see above);
the real Personal-domain email record is `EmailThread`/`EmailMessage` (see `docs/domain/personal/CONTEXT.md`)
-- neither is an ordered communication thread in this sense.

### Note
A user-authored Knowledge Platform item with title, body, type, optional meeting link, searchable projection and archival state. Audit preserves change history, checksum and field metadata, not reconstructable body revisions. Hard delete is not exposed in Phase 1.

### Document, Decision, KnowledgeItem and Relationship
Documents are versioned source artifacts. Decisions preserve context, alternatives and rationale. KnowledgeItems are normalized assertions with confidence and provenance. Relationships are typed, directed, temporal and evidence-backed.

**Correction (domain-modeling audit, 2026-09-08): `KnowledgeItem` does not exist.** The real, single generic
node type in the knowledge graph is `Entity` (`knowledge/entities.py`, table `pkos_nodes`) -- a person,
organization, project, topic, decision, document, or team node. What this section calls a "normalized
assertion with confidence and provenance" is closer to `Claim` (`knowledge/claims.py`), a distinct, real
concept: a time-bounded, sourced assertion about an Entity. Separately, **"Decision" is overloaded three ways
with no shared meaning**, not previously disclosed: an `Entity` kind (a generic graph node), a `Note` type
(what a MeetingPack's own "decisions" section actually returns -- a list of Notes), and a wholly separate
`engineering_decisions` record owned by the Engineering domain (Phase 6, human-authored, unrelated to the
entity graph). This section's single "Decision" entry conflates all three.

### Evidence
Immutable source pointer with location, checksum, capture time, excerpt boundaries and access status `available|missing|permission_denied|deleted`. Derived knowledge is never source evidence.

### Risk
Uncertain condition with probability, impact, owner, mitigation, trigger and review date. In Phase 1 `owner_id` is the authenticated user. Lifecycle: `identified -> assessed -> monitoring|mitigating -> materialized|closed`.

### AttentionItem
Deterministic ranked projection referencing an underlying entity and exposing score factors, explanation, confidence and expiry. It is not a recommendation.

### Recommendation
A rule- or AI-generated proposed action. It never mutates domain state directly. Explicit transitions are:

```text
proposed -> pending_confirmation
pending_confirmation -> rejected | expired | superseded
pending_confirmation -> accepted
accepted -> executed | failed
```

`GenerateRecommendation` creates `proposed`. `PublishRecommendation` is the only transition to `pending_confirmation`. `ConfirmRecommendation` is valid only from `pending_confirmation`, transitions to accepted and attempts the local transactional execution. Publication and confirmation each record actor, target version, evidence and audit metadata.

### RecommendationFeedback and UserFeedback
**Not built as first-class entities (domain-modeling audit, 2026-09-08).** Append-only user responses such as
dismiss, defer, pin, accept or reject. Feedback does not silently mutate the underlying entity. In code, only
a write-only `recommendation_feedback` table exists (via `governance/recommendation_events.py::record_feedback`),
with no schema or response model of its own -- not an exposed, queryable resource. `AttentionFeedback`
(Attention domain) and `personal/habits.py`'s own feedback concept are separate, unrelated mechanisms that
happen to share the word "feedback" -- neither is this.

### Brief
**Not built under this name (domain-modeling audit, 2026-09-08).** Persisted daily snapshot containing deterministic sections, source entity versions, evidence and generation metadata. AI enrichment is optional and may not alter inclusion or ranking. The real artifact matching this description is `MeetingPack`
(`attention/meeting_prep.py`, table `meeting_packs`) -- no class or table named `Brief` exists anywhere in the
codebase.

### AuditEvent
Immutable, redacted record of mutation attempts and successful state transitions with actor, request, correlation, aggregate version and before/after metadata. AuditEvent is separate from domain events.

### Reminder
**Not built (domain-modeling audit, 2026-09-08).** No table, class, or endpoint of any kind exists. Request to surface an entity at a time or condition remains only this document's stated intent.

## Source-of-truth rules

- ECC is authoritative for locally created tasks, commitments, notes, meetings, risks, recommendations and feedback.
- External systems remain authoritative for future imported messages, calendar events and source-control artifacts.
- Derived knowledge, attention projections and summaries are rebuildable and never replace evidence.

## Important relationships

`Person OWNS Task`, `Person MAKES Commitment`, `Person PARTICIPATES_IN Meeting`, `Commitment RELATES_TO Project`, `Meeting PRODUCES Task|Decision`, `Note ABOUT Meeting|Project`, `Evidence SUPPORTS KnowledgeItem|Recommendation`, `Risk THREATENS Project`, `AttentionItem HIGHLIGHTS Entity`, `Recommendation PROPOSES_ACTION_ON Entity`.

**Correction (domain-modeling audit, 2026-09-08):** every relationship above naming `Project` or `KnowledgeItem`
is unimplemented -- neither entity exists in code (see their corrected entries above). `Decision` in
`Meeting PRODUCES Task|Decision` and `Evidence SUPPORTS KnowledgeItem|Recommendation` refers to whichever of
this document's three overloaded "Decision" meanings applies in context (see the Document/Decision section
above) -- not a single, unambiguous entity.

## Phase 1 freeze

The Phase 1 entities, ownership map, lifecycle states and Task/Commitment/Note/Meeting/Risk/AttentionItem/Recommendation distinctions are frozen for implementation. Changes require an ADR plus synchronized data, API, event and test updates.

**Note (domain-modeling audit, 2026-09-08): the freeze was never actually violated.** Every entity found
unimplemented above (Project, Reminder, Brief, KnowledgeItem, Conversation, Message, SourceRecord) and every
entity found meaning something different than described (Goal) falls outside this clause's own explicit
frozen list -- the freeze only ever named Task, Commitment, Note, Meeting, Risk, AttentionItem, and
Recommendation, all seven of which this audit confirmed real and accurate. The drift was in this document's
broader ownership map and entity-definition prose presenting a wider wishlist as already built, not in the
frozen core itself.
