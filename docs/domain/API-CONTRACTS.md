---
id: API-CONTRACTS
title: Domain API Contracts
status: Accepted
version: 1.1.1
owner: Lucky Jain
related:
  - DOMAIN-MODEL
  - EVENT-CATALOG
  - RFC-004
  - PHASE-001
  - PHASE-001-API-SCHEMAS
---

# Domain API Contracts

## Contract rules

- External APIs use `/api/v1` and JSON.
- Commands may change state; queries never change state.
- Actor, workspace and Phase 1 accountable owner are derived exclusively from the authenticated opaque server-side session.
- Browser-supplied `workspace_id`, `actor_id` or accountable ownership fields are rejected.
- Mutations require `Idempotency-Key`; updates and lifecycle actions require `expected_version`.
- Repeating a mutation with the same key and request hash returns the original response; a different hash returns `409 IDEMPOTENCY_CONFLICT`.
- Dates are ISO-8601; storage is UTC; daily interpretation uses the workspace IANA timezone.
- Pagination uses opaque signed cursors.
- Cross-workspace and absent entity IDs both return 404.

Error envelope:

```json
{"error":{"code":"TASK_NOT_FOUND","message":"Task was not found","request_id":"uuid","details":{}}}
```

## Domain commands and queries

### Planning

Commands: CreateTask, UpdateTask, CompleteTask, CancelTask, ArchiveTask, RestoreTask, CreateCalendarEvent, UpdateCalendarEvent, CreateMeeting, UpdateMeeting, CreateReminder.
Queries: GetToday, GetTask, ListTasks, ListCalendarEvents, GetMeeting, ListMeetings.

### Communication

Commands: CreateCommitment, UpdateCommitment, ConfirmCommitment, FulfilCommitment, CancelCommitment, ArchiveCommitment, RestoreCommitment.
Queries: GetCommitment, ListCommitments, GetConversation, ListMessages.

### Knowledge Platform

Commands: CreateNote, UpdateNote, ArchiveNote, RestoreNote, ImportDocument, RecordDecision, CreateKnowledgeItem, CreateRelationship, InvalidateRelationship.
Queries: GetNote, ListNotes, SearchKnowledge, GetEvidence, GetDecision, GetRelated.

### Executive Intelligence

Commands: GenerateMorningBrief, CreateRisk, UpdateRisk, CloseRisk, GenerateRecommendation, ConfirmRecommendation, RejectRecommendation, DeferRecommendation, PinRecommendation.
Queries: GetDashboard, GetMorningBrief, ListAttentionItems, ListRisks, ListRecommendations.

### Audit

Query only: ListAuditEvents. Audit writes are internal transactional behavior, not a public command.

## Phase 1 HTTP surface

```text
GET    /api/v1/dashboard/today
GET    /api/v1/tasks
POST   /api/v1/tasks
GET    /api/v1/tasks/{id}
PATCH  /api/v1/tasks/{id}
POST   /api/v1/tasks/{id}/complete
POST   /api/v1/tasks/{id}/cancel
POST   /api/v1/tasks/{id}/archive
POST   /api/v1/tasks/{id}/restore
GET    /api/v1/commitments
POST   /api/v1/commitments
GET    /api/v1/commitments/{id}
PATCH  /api/v1/commitments/{id}
POST   /api/v1/commitments/{id}/confirm
POST   /api/v1/commitments/{id}/fulfil
POST   /api/v1/commitments/{id}/cancel
POST   /api/v1/commitments/{id}/archive
POST   /api/v1/commitments/{id}/restore
GET    /api/v1/notes
POST   /api/v1/notes
GET    /api/v1/notes/{id}
PATCH  /api/v1/notes/{id}
POST   /api/v1/notes/{id}/archive
POST   /api/v1/notes/{id}/restore
GET    /api/v1/calendar/events
POST   /api/v1/calendar/events
PATCH  /api/v1/calendar/events/{id}
GET    /api/v1/meetings
POST   /api/v1/meetings
GET    /api/v1/meetings/{id}
PATCH  /api/v1/meetings/{id}
GET    /api/v1/risks
POST   /api/v1/risks
GET    /api/v1/risks/{id}
PATCH  /api/v1/risks/{id}
POST   /api/v1/risks/{id}/close
GET    /api/v1/briefs/morning
POST   /api/v1/briefs/morning
GET    /api/v1/recommendations
GET    /api/v1/recommendations/{id}
POST   /api/v1/recommendations/{id}/confirm
POST   /api/v1/recommendations/{id}/reject
POST   /api/v1/recommendations/{id}/defer
POST   /api/v1/recommendations/{id}/pin
GET    /api/v1/search
GET    /api/v1/audit
```

Exact Phase 1 request, response, filter, validation and error schemas are normative in `docs/phases/phase-001/API-SCHEMAS.md`.

## Concurrency and recommendation confirmation

Stale mutations return `409 VERSION_CONFLICT` with the current version. Phase 1 recommendation execution is limited to local database mutations that can complete in one transaction:

1. lock recommendation and target;
2. revalidate recommendation state, expiry, evidence and target version;
3. transition recommendation to accepted;
4. mutate the target;
5. transition recommendation to executed;
6. write redacted audit records and outbox events;
7. commit atomically.

A failure before commit rolls back all successful-state changes. A failed-attempt audit and `recommendation.failed.v1` event may be persisted in a separate transaction with no target mutation. External or multi-step actions remain out of scope.
