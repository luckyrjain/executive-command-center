---
id: DOMAIN-MODEL
title: Executive Command Center Canonical Domain Model
status: Accepted
version: 1.1.2
owner: Lucky Jain
related:
  - RFC-001
  - RFC-004
  - ADR-0003
  - ADR-0006
  - PHASE-001
---

# Canonical Domain Model

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
| AI Platform | PromptDefinition, ModelExecution, EvaluationResult, AgentRun |
| Integration Platform | ConnectorAccount, SyncCursor, SourceRecord |

Backend package note (Phase 3, approved 2026-07-23): `AttentionItem` moves from `backend/ecc/domains/governance/attention.py` to a dedicated `backend/ecc/domains/attention/` package, which also owns Phase 3's new WaitingLink, RiskReview (history), CapacityProfile, PlanningConstraint, Plan/PlanBlock and MeetingPack records — all still conceptually within the Executive Intelligence domain above, just their own backend package since Phase 3 substantially extends and owns that surface. `Risk`'s CRUD stays in `governance/risks.py`; Phase 3 only adds the `risk_reviews` history table and a review endpoint reading/writing it.

## Core entities

### Workspace
Top-level authorization and data boundary. Lifecycle: `active -> suspended -> archived`. Includes an IANA timezone used for daily boundaries.

### User
Authenticated actor within a workspace. Phase 1 supports one owner user while retaining multi-user identifiers.

### Person and Organization
People and groups known to ECC. Non-deterministic high-impact merges require human confirmation and retain reversible merge records.

### Project and Goal
Projects are bounded outcomes; goals are measurable outcomes. Project lifecycle: `proposed -> active -> blocked|on_hold -> completed|cancelled -> archived`.

### Task
Discrete work with owner, status, manual priority, due date and source. In Phase 1 `owner_id` is always the authenticated user. Lifecycle: `captured -> planned -> in_progress -> blocked -> completed|cancelled -> archived`, with `archived -> previous_terminal_or_active_state` on restore.

### Commitment
A promise made by or to a person, preserving parties and evidence. In Phase 1 `owner_id` is always the authenticated user; counterparty identity remains user-selectable. Lifecycle: `detected -> confirmed -> active -> fulfilled|broken|cancelled -> archived`, with restore returning to the state held before archive. AI-detected commitments remain detected until confirmed.

### CalendarEvent
A scheduled interval. In Phase 1 only local/manual events are supported; external calendar authority is deferred to connector phases.

### Meeting
Semantic meeting view with agenda, preparation, notes, decisions and follow-ups. It may reference zero or one CalendarEvent. A linked Meeting projects timing from its CalendarEvent and cannot edit timing independently; a standalone Meeting owns its timing. Linking a standalone Meeting adopts CalendarEvent timing while preserving Meeting identity.

### Conversation and Message
A conversation is an ordered communication thread. Messages are immutable artifacts; source content is preserved as Evidence.

### Note
A user-authored Knowledge Platform item with title, body, type, optional meeting link, searchable projection and archival state. Audit preserves change history, checksum and field metadata, not reconstructable body revisions. Hard delete is not exposed in Phase 1.

### Document, Decision, KnowledgeItem and Relationship
Documents are versioned source artifacts. Decisions preserve context, alternatives and rationale. KnowledgeItems are normalized assertions with confidence and provenance. Relationships are typed, directed, temporal and evidence-backed.

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
Append-only user responses such as dismiss, defer, pin, accept or reject. Feedback does not silently mutate the underlying entity.

### Brief
Persisted daily snapshot containing deterministic sections, source entity versions, evidence and generation metadata. AI enrichment is optional and may not alter inclusion or ranking.

### AuditEvent
Immutable, redacted record of mutation attempts and successful state transitions with actor, request, correlation, aggregate version and before/after metadata. AuditEvent is separate from domain events.

### Reminder
Request to surface an entity at a time or condition.

## Source-of-truth rules

- ECC is authoritative for locally created tasks, commitments, notes, meetings, risks, recommendations and feedback.
- External systems remain authoritative for future imported messages, calendar events and source-control artifacts.
- Derived knowledge, attention projections and summaries are rebuildable and never replace evidence.

## Important relationships

`Person OWNS Task`, `Person MAKES Commitment`, `Person PARTICIPATES_IN Meeting`, `Commitment RELATES_TO Project`, `Meeting PRODUCES Task|Decision`, `Note ABOUT Meeting|Project`, `Evidence SUPPORTS KnowledgeItem|Recommendation`, `Risk THREATENS Project`, `AttentionItem HIGHLIGHTS Entity`, `Recommendation PROPOSES_ACTION_ON Entity`.

## Phase 1 freeze

The Phase 1 entities, ownership map, lifecycle states and Task/Commitment/Note/Meeting/Risk/AttentionItem/Recommendation distinctions are frozen for implementation. Changes require an ADR plus synchronized data, API, event and test updates.
