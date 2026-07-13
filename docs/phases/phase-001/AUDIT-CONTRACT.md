---
id: PHASE-001-AUDIT-CONTRACT
title: Phase 1 Audit Contract
status: Approved
version: 1.0.1
owner: Lucky Jain
---

# Audit Contract

## Scope

Every successful or rejected state-changing request for tasks, commitments, notes, meetings, risks, recommendations and feedback creates an immutable audit record. Domain events and audit records are related but not interchangeable.

## Normative action mapping

| API action | Audit event type | Domain event type |
|---|---|---|
| create task | `task.created` | `task.created.v1` |
| update task | `task.updated` | `task.updated.v1` |
| complete task | `task.completed` | `task.completed.v1` |
| cancel task | `task.cancelled` | `task.cancelled.v1` |
| archive task | `task.archived` | `task.archived.v1` |
| restore task | `task.restored` | `task.restored.v1` |
| create commitment | `commitment.created` | `commitment.created.v1` |
| confirm commitment | `commitment.confirmed` | `commitment.confirmed.v1` |
| update commitment | `commitment.updated` | `commitment.updated.v1` |
| fulfil commitment | `commitment.fulfilled` | `commitment.fulfilled.v1` |
| cancel commitment | `commitment.cancelled` | `commitment.cancelled.v1` |
| archive commitment | `commitment.archived` | `commitment.archived.v1` |
| restore commitment | `commitment.restored` | `commitment.restored.v1` |
| create/update/archive/restore note | `note.created|updated|archived|restored` | matching `.v1` event |
| create/update calendar event | `calendar_event.created|updated` | `calendar_event.created.v1|changed.v1` |
| create/update meeting | `meeting.created|updated` | matching `.v1` event |
| create/update/close risk | `risk.created|updated|closed` | `risk.identified.v1|updated.v1|closed.v1` |
| request recommendation confirmation | `recommendation.confirmation_requested` | `recommendation.confirmation_requested.v1` |
| confirm and execute | `recommendation.accepted`, `recommendation.executed` | `recommendation.accepted.v1`, `recommendation.executed.v1` |
| reject/defer/pin | corresponding audit type | corresponding `.v1` event plus `feedback.recorded.v1` |
| failed confirmation attempt | `recommendation.failed` | `recommendation.failed.v1` when persisted |

Rejected authorization, validation, state and version-conflict attempts use `<aggregate>.mutation_rejected` and publish no business domain event.

## Record fields

Workspace, event type, aggregate type/id/version, actor ID, request ID, correlation ID, idempotency key hash, occurred_at, changed fields, redacted before/after snapshots, authorization result, source `user|rule|ai|system`, optional recommendation/evidence references, and failure code.

## Immutability and consistency

For normal mutations, audit, domain mutation and outbox event are written in the same transaction. Audit rows are append-only; application roles have no UPDATE or DELETE permission.

For recommendation confirmation, accepted/executed audit records, target mutation, recommendation transitions and outbox events commit atomically. Failure rolls back that transaction. A separate failure transaction may write `recommendation.failed` and its event without mutating the target.

## Redaction

Never store session tokens, passwords, secrets, connector credentials, raw authorization headers or complete private note bodies. Note auditing stores title, body checksum, body length and changed-field metadata. This is change history, not reconstructable note revision history. Proposed actions are stored only after secret-field filtering.

## Access and retention

Only the authenticated workspace owner may read audit history in Phase 1. Cross-workspace records return 404. Audit records are retained for the life of the local workspace unless the entire workspace is erased through a future approved process.

## API and tests

`GET /api/v1/audit` supports aggregate type/id, actor, event type, date range, cursor and limit. Tests prove every mapping above, transaction rollback on audit failure, append-only permissions, redaction, cross-workspace isolation, ordering, pagination and correlation propagation.
