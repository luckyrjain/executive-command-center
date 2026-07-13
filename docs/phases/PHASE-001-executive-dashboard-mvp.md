---
id: PHASE-001
title: Executive Dashboard MVP
status: Draft
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

Deliver a useful local-first daily workspace that combines priorities, calendar context, tasks and notes in one calm executive dashboard.

## User value

The user can open one application and understand what deserves attention today without manually reconstructing context.

## In scope

- Morning brief
- Daily priority list
- Calendar agenda
- Tasks and commitments
- Notes capture
- Local search
- Manual data entry and approved initial connectors
- Evidence links and recommendation explanations

## Out of scope

- Autonomous sending or scheduling
- Multi-user collaboration
- Enterprise administration
- Predictive organizational analytics
- Unapproved third-party connectors

## Functional requirements

- **FR-P1-001:** Display today's meetings, priorities, commitments and risks in one dashboard.
- **FR-P1-002:** Generate a morning brief from available local context.
- **FR-P1-003:** Allow users to create, update, complete and defer tasks.
- **FR-P1-004:** Link tasks and notes to people, projects and meetings.
- **FR-P1-005:** Prepare a meeting context view using available evidence.
- **FR-P1-006:** Provide local full-text search across supported entities.
- **FR-P1-007:** Explain each AI-generated priority or summary with evidence and confidence.
- **FR-P1-008:** Require confirmation before any external state-changing action.
- **FR-P1-009:** Preserve original source references for derived information.
- **FR-P1-010:** Remain usable when external systems are temporarily unavailable.

## Architecture impact

Uses the Executive Experience, Human Attention Engine, Knowledge Platform and Integration Platform boundaries defined by RFC-004. All model calls go through the AI Runtime.

## Data model changes

Initial entities:

- Person
- Project
- Meeting
- Task
- Commitment
- Note
- SourceRecord
- Recommendation
- Evidence

Schema changes require migrations and traceability to the requirements above.

## APIs and contracts

Versioned APIs for dashboard, agenda, tasks, notes, search, meeting context and recommendation feedback. Contracts must be documented before implementation.

## Frontend surfaces

- Executive dashboard
- Morning brief
- Meeting context drawer or page
- Task and commitment workspace
- Notes capture
- Search results
- Settings and connector status

## Security and privacy

- Local storage by default
- Explicit connector authorization
- Least-privilege scopes
- Encryption for sensitive local data where applicable
- No prompt context in logs
- Confirmation gates for external mutations

## Observability

Track synchronization health, dashboard latency, search latency, recommendation acceptance, extraction accuracy and connector failures without collecting private content unnecessarily.

## Testing strategy

- Unit and integration tests for each domain
- Contract tests for connectors
- End-to-end tests for morning planning and meeting preparation
- Golden datasets for extraction and recommendation quality
- Offline and degraded-mode tests
- Security tests for authorization and prompt injection boundaries

## Acceptance criteria

- **AT-P1-001:** The dashboard loads locally within the performance target defined by STD-001.
- **AT-P1-002:** The morning brief contains traceable evidence for every derived item.
- **AT-P1-003:** Tasks and commitments persist correctly across restarts.
- **AT-P1-004:** Meeting preparation displays linked people, history, tasks and source records.
- **AT-P1-005:** Local search returns relevant supported entities within the target latency.
- **AT-P1-006:** External mutations cannot occur without explicit confirmation.
- **AT-P1-007:** The core dashboard remains usable during connector outages.
- **AT-P1-008:** AI evaluation thresholds are documented and pass the approved golden dataset.

## Rollout and rollback

Release behind local feature flags. Connector integrations can be disabled independently. Database migrations must include tested rollback or forward-recovery procedures.

## Risks and mitigations

- Excessive scope: limit Phase 1 to daily planning workflows.
- Low trust: expose evidence, confidence and feedback controls.
- Connector instability: use durable sync state and degraded operation.
- Privacy leakage: minimize collected data and keep local execution as default.

## Deferred backlog

Engineering workspace, advanced relationship intelligence, delegation, autonomous workflows, multi-user and enterprise controls.

## Exit review

Phase 1 is complete when all acceptance criteria pass, daily usage provides clear value, and no critical privacy, security or explainability gaps remain.
