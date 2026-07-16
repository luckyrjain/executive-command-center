---
id: PHASE-001
title: Executive Dashboard MVP
status: Approved for Implementation
version: 1.0.3
owner: Lucky Jain
depends_on:
  - PHASE-000
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005@1.1.0
  - STD-001
  - DOMAIN-MODEL@1.1.2
  - API-CONTRACTS@1.1.2
  - EVENT-CATALOG@1.1.2
  - PKOS-SCHEMA@1.1.1
contracts:
  - phase-001/DATA-MODEL.md
  - phase-001/API-SCHEMAS.md
  - phase-001/PRIORITY-MODEL.md
  - phase-001/MORNING-BRIEF-CONTRACT.md
  - phase-001/AUDIT-CONTRACT.md
  - phase-001/SEARCH-CONTRACT.md
  - phase-001/UX-STATES.md
  - phase-001/TEST-PLAN.md
---

# PHASE-001 — Executive Dashboard MVP

## Objective

Deliver a local, authenticated dashboard showing today's schedule, priorities, commitments, notes and risks with traceable evidence and deterministic behavior when AI is unavailable.

## User value

The user opens one application each morning, understands what requires attention, captures or updates executive work, retrieves recent context and confirms recommendations without depending on cloud services.

## In scope

- Today dashboard and Morning Brief.
- Local/manual calendar events and meetings; external connectors deferred.
- Task, commitment, note and risk workflows.
- Complete, fulfil, cancel, archive and restore actions defined by the API contract.
- Deterministic attention ranking and PostgreSQL search, including CalendarEvent results.
- Recommendations with explicit publication and durable human confirmation.
- Evidence states: `available|missing|permission_denied|deleted`.
- Immutable audit history and workspace-timezone-aware daily boundaries.

## Out of scope

External connectors; autonomous actions; semantic/vector search; dedicated graph databases; multi-user collaboration; cloud deployment; predictive risk modelling; external or multi-step recommendation execution.

## Functional requirements

- Dashboard shows today's meetings, priorities, overdue commitments, risks, waiting-on items and recent changes.
- Users can create, update and perform contracted lifecycle actions on supported entities.
- Notes support autosave, archive, restore and local search.
- Ranked items and recommendations expose factors, confidence, evidence and source.
- Dashboard, brief and search remain available without AI.
- Recommendation transitions are exactly: `proposed -> pending_confirmation`; `pending_confirmation -> rejected|expired|superseded|accepted`; `accepted -> executed|failed`.
- `/publish` is the only transition to pending confirmation; `/confirm` atomically accepts, applies the local mutation, records execution, audit and outbox.
- Every listed mutation writes a redacted append-only audit record.
- Updates enforce optimistic concurrency and idempotency.

## Non-functional requirements

- Dashboard p95 <2 seconds on the acceptance dataset.
- Search p95 <500 ms locally and <800 ms in CI.
- Ranking 10,000 eligible records completes within 500 ms.
- Core flows work offline after application load and without AI.
- Primary flows meet WCAG 2.2 AA.
- No cross-workspace identifiers or content are observable.

## Architecture impact

Phase 1 adds task, commitment, note, calendar, meeting, risk, attention, brief, search, audit and recommendation modules to the Phase 0 modular monolith. PostgreSQL remains authoritative; outbox events remain durable; no new infrastructure technology is introduced.

## Data changes

Normative tables, fields, constraints, lifecycle states, indexes and migration rules are defined in `phase-001/DATA-MODEL.md`. Every table is workspace scoped with composite workspace constraints. Date-only and datetime due precision remain separate.

## API changes

The frozen HTTP surface is defined in `phase-001/API-SCHEMAS.md` and covers dashboard, tasks, commitments, notes, calendar events, meetings, risks, Morning Brief, search, audit and recommendations. Server-side sessions derive actor, workspace and accountable owner.

## Frontend changes

Add Today, Morning Brief, Work Actions, Recommendations, Search and Audit surfaces. Required loading, empty, degraded, offline, recoverable error and version-conflict behavior is defined in `UX-STATES.md`. Feature flags are typed, restart-required configuration.

## Security and privacy

Browser payloads may not assert workspace, actor or owner. Cross-workspace IDs return 404. Every mutation is audited with redaction. Search snippets and evidence respect lifecycle and permission state. Recommendation execution requires durable confirmation.

## Observability

Record request latency/error rate, lifecycle mutation outcomes, search latency/result counts, ranking duration/input size, brief generation/fallback/staleness, recommendation transitions, idempotency conflicts, audit/outbox failures and correlation IDs. Metrics contain no note bodies or sensitive content.

## Test strategy

Normative coverage is in `TEST-PLAN.md`: CRUD/lifecycle, isolation, audit, ranking, search, brief, recommendation, migration, backup/restore, AI-disabled, accessibility and Chromium acceptance.

## Acceptance criteria

- Frozen schemas and migrations pass CI.
- All contracted entity and lifecycle flows pass.
- Performance thresholds pass on the documented dataset.
- Every table has workspace-isolation coverage and every mutation has audit coverage.
- AI-disabled, offline/degraded, accessibility and backup/restore tests pass.
- Frontend typecheck, unit, production build and Chromium acceptance pass.

## Exit criteria

- All Phase 1 delivery slices are merged.
- Required CI checks pass on the final head.
- Zero open Critical, High or Medium specification/code findings.
- Zero known Critical or High dependency vulnerabilities without accepted exception.
- Clean backup/restore succeeds.
- One-week daily-use validation is completed and recorded.

## Rollback plan

Migrations have tested downgrade or documented forward-fix paths. Recommendation and AI-enrichment flags may be disabled without affecting deterministic dashboard, local search, audit or authoritative data. Failed projections can be rebuilt from authoritative records.

## Deferred backlog

External connectors, semantic/vector search, autonomous workflows, multi-user collaboration, cloud deployment, predictive models and external recommendation execution.

> Version 1.0.3 is a documentation-completeness clarification. It does not change the previously frozen Phase 1 runtime behavior.
