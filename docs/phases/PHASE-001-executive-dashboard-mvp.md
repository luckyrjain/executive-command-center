---
id: PHASE-001
title: Executive Dashboard MVP
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on:
  - PHASE-000
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005
  - STD-001
---

# PHASE-001 — Executive Dashboard MVP

## Objective

Deliver the first useful ECC experience: a local dashboard that shows today's schedule, priorities, commitments, notes and risks with traceable source evidence.

## User value

A user can begin the day in one application and understand what deserves attention without manually checking multiple systems.

## In scope

- executive dashboard shell
- today's calendar view
- manually managed tasks and commitments
- notes capture
- morning brief assembled from local data
- source evidence links
- simple priority model
- local search across Phase 1 data
- human confirmation before state-changing actions

## Out of scope

- autonomous email sending
- broad connector marketplace
- organizational knowledge graph
- predictive risk analysis
- multi-user collaboration
- enterprise deployment

## Functional requirements

### FR-P1-001

The dashboard MUST show today's meetings, active priorities, overdue commitments and identified risks.

### FR-P1-002

Users MUST be able to create, update, complete and archive tasks and commitments.

### FR-P1-003

Users MUST be able to create and search notes locally.

### FR-P1-004

The morning brief MUST explain why each item appears and identify its source evidence.

### FR-P1-005

The priority model MUST expose its factors and confidence. It MUST NOT rely on opaque model output alone.

### FR-P1-006

Users MUST be able to dismiss, defer or pin recommendations, and the system MUST record that feedback.

### FR-P1-007

The dashboard MUST remain usable when AI services are unavailable.

### FR-P1-008

All state-changing AI-assisted actions MUST require explicit confirmation.

### FR-P1-009

Search MUST return tasks, commitments, notes and meetings with source and timestamp context.

### FR-P1-010

The application MUST provide an audit history for task, commitment and recommendation state changes.

## Data entities

- Task
- Commitment
- Note
- Meeting
- Recommendation
- Evidence
- UserFeedback
- AuditEvent

Each entity requires a stable identifier, created and updated timestamps, source metadata and archival state.

## API surface

Initial API groups:

```text
/api/v1/dashboard
/api/v1/tasks
/api/v1/commitments
/api/v1/notes
/api/v1/meetings
/api/v1/recommendations
/api/v1/search
/api/v1/audit
```

Exact schemas require contract definitions before implementation.

## Frontend surfaces

- Today dashboard
- Morning brief
- Task and commitment detail
- Note editor
- Search results
- Recommendation explanation
- Settings and data-source status

## Security and privacy

- all Phase 1 data remains local by default
- no raw private content is sent to a cloud model without explicit configuration and consent
- audit events exclude secrets and unnecessary sensitive content
- source evidence access follows the permission of the original source

## Observability

Track:

- dashboard load latency
- search latency
- recommendation generation latency
- recommendation acceptance and dismissal
- failed data reads and writes
- AI unavailable fallbacks

## Test strategy

- domain unit tests
- API contract tests
- local database integration tests
- dashboard end-to-end test
- recommendation explanation test
- AI-unavailable fallback test
- audit-history test
- privacy boundary test

## Acceptance criteria

- dashboard loads in under two seconds on the supported local development machine with representative Phase 1 data
- all displayed recommendations include evidence and rationale
- core task, commitment and note workflows work without an AI model
- search returns relevant local entities with context
- no state-changing recommendation executes without confirmation
- all acceptance tests are automated and pass in CI

## Exit criteria

Phase 1 is complete when the user can use ECC as a reliable daily planning surface for at least one week without relying on another task-management application for the included workflows.

## Rollback plan

Schema migrations must support tested rollback or forward-fix procedures. Recommendation features must be feature-flagged so the deterministic dashboard remains usable if AI capabilities regress.

## Deferred backlog

- Gmail and external calendar write actions
- GitHub and Jira connectors
- advanced knowledge graph
- relationship intelligence
- delegated workflows
- autonomous execution
