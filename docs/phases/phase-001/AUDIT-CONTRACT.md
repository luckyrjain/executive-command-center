---
id: PHASE-001-AUDIT-CONTRACT
title: Phase 1 Audit Contract
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Audit Contract

## Scope

Every successful or rejected state-changing request for tasks, commitments, notes, meetings, risks, recommendations, and feedback creates an immutable audit record. Domain events and audit records are related but not interchangeable: domain events communicate business changes; audit records preserve actor, request, before/after state, and authorization context.

## Required event vocabulary

`task.created`, `task.updated`, `task.completed`, `task.cancelled`, `task.archived`, `commitment.created`, `commitment.confirmed`, `commitment.updated`, `commitment.fulfilled`, `commitment.cancelled`, `commitment.archived`, `note.created`, `note.updated`, `note.archived`, `calendar_event.created`, `calendar_event.updated`, `meeting.created`, `meeting.updated`, `risk.created`, `risk.updated`, `risk.closed`, `recommendation.generated`, `recommendation.confirmed`, `recommendation.rejected`, `recommendation.deferred`, `recommendation.pinned`, `recommendation.executed`, `recommendation.failed`.

## Record fields

Workspace, event type, aggregate type/id/version, actor ID, request ID, correlation ID, idempotency key hash, occurred_at, changed fields, redacted before/after snapshots, authorization result, source `user|rule|ai|system`, optional recommendation/evidence references, and failure code for rejected mutations.

## Immutability and consistency

Audit inserts occur in the same database transaction as the domain mutation and outbox event. Audit rows are append-only; database roles used by the application have no UPDATE or DELETE permission on audit tables. Failed authorization and version conflicts are recorded without private body content.

## Redaction

Never store session tokens, passwords, secrets, connector credentials, raw authorization headers, or complete private note bodies. Note auditing stores title, body checksum, body length, and changed-field metadata. Recommendation proposed actions may be stored only after secret-field filtering.

## Access and retention

Only the authenticated workspace owner may read audit history in Phase 1. Cross-workspace records return 404. Audit records are retained for the life of the local workspace unless the entire workspace is cryptographically erased through a future approved process.

## API

`GET /api/v1/audit` supports aggregate type/id, actor, event types, date range, cursor, and limit. Results are newest first and expose redacted snapshots, request/correlation IDs, source, and evidence links.

## Tests

Contract tests prove audit creation for every listed mutation, transaction rollback on audit failure, append-only permissions, redaction, cross-workspace isolation, ordering, pagination, and correlation propagation.