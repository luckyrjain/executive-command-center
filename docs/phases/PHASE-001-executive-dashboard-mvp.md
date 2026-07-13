---
id: PHASE-001
title: Executive Dashboard MVP
status: Approved for Implementation
version: 1.0.2
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

## In scope

- Today dashboard and morning brief.
- Local/manual calendar events and meetings; external connectors deferred.
- Task, commitment, note and risk workflows.
- Complete, fulfil, cancel, archive and restore actions where defined by the API contract.
- Deterministic attention ranking and PostgreSQL-only search, including CalendarEvent results.
- Recommendations with explicit publication and durable human confirmation.
- Evidence presentation using `available|missing|permission_denied|deleted`.
- Immutable audit history and workspace-timezone-aware daily boundaries.

## Out of scope

Gmail, Google Calendar, GitHub, Jira and connector marketplace integration; autonomous actions; semantic/vector search; dedicated graph databases; multi-user collaboration; cloud deployment; predictive risk modelling; external or multi-step recommendation execution.

## Security boundary

The server derives actor, workspace and Phase 1 accountable owner from the authenticated opaque server-side session. Browser payloads may not assert workspace, actor or owner. Counterparty/related-person references are allowed only where explicitly contracted. All Phase 1 tables use composite workspace constraints. Cross-workspace IDs return 404.

## Functional requirements

- Dashboard shows today's local meetings, ranked priorities, overdue commitments, risks, waiting-on items and recent changes.
- Users can create, update, complete/fulfil, cancel, archive and restore supported entities.
- Users can create, autosave, archive, restore and search notes locally.
- Every ranked item and recommendation exposes factors, confidence, evidence and source.
- Deterministic dashboard, brief and search remain available without AI.
- Recommendation transitions are exactly: `proposed -> pending_confirmation`; `pending_confirmation -> rejected|expired|superseded|accepted`; `accepted -> executed|failed`.
- `GenerateRecommendation` creates proposed; `/publish` is the only transition to pending_confirmation; `/confirm` is available only from pending_confirmation and atomically performs accepted transition, local target mutation, executed transition, audit and outbox writes.
- Every listed mutation writes a redacted append-only audit record.
- Updates enforce optimistic concurrency and idempotency.
- Every successful lifecycle action returns `200` with the current entity representation.

## Canonical API

The frozen HTTP surface is defined by `phase-001/API-SCHEMAS.md` and includes:

```text
GET /api/v1/dashboard/today
GET|POST /api/v1/tasks
GET|PATCH /api/v1/tasks/{id}
POST /api/v1/tasks/{id}/complete|cancel|archive|restore
GET|POST /api/v1/commitments
GET|PATCH /api/v1/commitments/{id}
POST /api/v1/commitments/{id}/confirm|fulfil|cancel|archive|restore
GET|POST /api/v1/notes
GET|PATCH /api/v1/notes/{id}
POST /api/v1/notes/{id}/archive|restore
GET|POST /api/v1/calendar/events
GET|POST /api/v1/meetings
GET|POST /api/v1/risks
GET|POST /api/v1/briefs/morning
GET /api/v1/search
GET /api/v1/audit
GET /api/v1/recommendations
POST /api/v1/recommendations/{id}/publish|confirm|reject|defer|pin
```

## Deterministic behavior

Priority weights, waiting-on signals, dismissal versioning, tie-breakers and confidence are normative in `PRIORITY-MODEL.md`. Brief sections, refresh eligibility, stale-by-age rules, duplicate suppression and AI fallback are normative in `MORNING-BRIEF-CONTRACT.md`. Date-only and datetime due precision are separate fields.

## Search, UX and feature flags

Search uses normalized exact, prefix, PostgreSQL full-text and approved trigram matching for task, commitment, note, meeting, calendar_event and risk. Embeddings and external vectors are deferred. Primary surfaces implement loading, empty, degraded, recoverable error, offline and version-conflict states, with WCAG 2.2 AA core flows.

Feature flags: `phase1.recommendations=false`, `phase1.ai_brief_enrichment=false`, `phase1.search_trigram=true`. Flags are typed restart-required server configuration and do not change migration requirements.

## Automated acceptance and exit gates

- Frozen contracts and migrations pass CI.
- Dashboard p95 <2 seconds; search p95 <500 ms locally and <800 ms CI; ranking 10,000 entities <500 ms.
- Core CRUD/lifecycle, search, brief, recommendation publication/confirmation, audit and Playwright tests pass.
- Every Phase 1 table has workspace-isolation tests.
- Every mutation has audit coverage and recommendation execution requires publication plus durable confirmation.
- AI-disabled/unavailable, backup/restore and accessibility tests pass.
- Zero open critical/high/medium specification or code-review findings and zero known critical/high dependency vulnerabilities.

The one-week daily-use validation remains a separate product outcome.

## Rollback

Migrations must have tested downgrade or documented forward-fix paths. Recommendation and AI enrichment flags may be disabled without affecting deterministic dashboard, local search, audit or authoritative data.
