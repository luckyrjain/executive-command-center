---
id: PHASE-001
title: Executive Dashboard MVP
status: Approved for Implementation
version: 1.0.0
owner: Lucky Jain
depends_on:
  - PHASE-000
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005@1.1.0
  - STD-001
  - DOMAIN-MODEL@1.1.0
  - API-CONTRACTS@1.1.0
  - EVENT-CATALOG@1.1.0
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

Deliver the first useful ECC experience: a local, authenticated dashboard showing today's schedule, priorities, commitments, notes and risks with traceable evidence and deterministic behavior when AI is unavailable.

## In scope

- Today dashboard and morning brief
- Local/manual calendar events and meetings; external calendar connectors are deferred
- Task, commitment, note and risk workflows
- Deterministic attention ranking
- PostgreSQL-only local search
- Recommendations with durable human confirmation
- Evidence presentation and immutable audit history
- Workspace-timezone-aware daily boundaries

## Out of scope

- Gmail, Google Calendar, GitHub, Jira or connector marketplace integration
- Autonomous or unconfirmed actions
- Semantic/vector search and dedicated graph databases
- Multi-user collaboration, cloud deployment and predictive risk modelling

## Security boundary

The server derives actor and workspace from the authenticated opaque server-side session. Browser payloads may not assert `workspace_id`, `actor_id` or ownership. All Phase 1 tables use composite workspace constraints. Cross-workspace IDs return 404 without disclosing existence.

## Functional requirements

- The dashboard shows today's local meetings, ranked priorities, overdue commitments, risks, waiting-on items and recent changes.
- Users can create, update, complete/fulfil, cancel, archive and restore supported entities according to the data contract.
- Users can create, autosave, archive and search notes locally.
- Every ranked item and recommendation exposes factors, confidence, evidence and source.
- Deterministic dashboard, brief and search remain available without AI.
- Recommendation state follows `proposed -> pending_confirmation -> accepted|rejected|expired|superseded -> executed|failed` and no proposed action mutates state before durable confirmation and target-version revalidation.
- Every listed mutation writes a redacted append-only audit record in the same transaction.
- Updates enforce optimistic concurrency and idempotency.

## Canonical API

The Phase 1 HTTP surface is frozen by `phase-001/API-SCHEMAS.md`. Canonical routes include:

```text
GET  /api/v1/dashboard/today
GET|POST /api/v1/tasks
GET|PATCH /api/v1/tasks/{id}
POST /api/v1/tasks/{id}/complete|archive
GET|POST /api/v1/commitments
GET|PATCH /api/v1/commitments/{id}
POST /api/v1/commitments/{id}/confirm|fulfil|archive
GET|POST /api/v1/notes
GET|PATCH /api/v1/notes/{id}
POST /api/v1/notes/{id}/archive
GET|POST /api/v1/calendar/events
GET|POST /api/v1/meetings
GET|POST /api/v1/risks
GET|POST /api/v1/briefs/morning
GET /api/v1/search
GET /api/v1/audit
GET /api/v1/recommendations
POST /api/v1/recommendations/{id}/confirm|reject|defer|pin
```

## Deterministic priority and brief

The exact priority weights, confidence calculation, tie-breakers, expiry and override rules are normative in `PRIORITY-MODEL.md`. Morning brief sections, limits, duplicate suppression, generation lifecycle, stale behavior and AI fallback are normative in `MORNING-BRIEF-CONTRACT.md`.

## Search

Phase 1 uses normalized exact, prefix, PostgreSQL full-text and approved trigram matching. Embeddings and external vector stores are deferred. Results include score components, timestamp/source context and evidence access state.

## UX and accessibility

All primary surfaces implement loading, empty, degraded, recoverable error, offline and conflict states. Core flows meet WCAG 2.2 AA and are keyboard operable. Version conflicts never silently overwrite.

## Feature flags

- `phase1.recommendations=false`
- `phase1.ai_brief_enrichment=false`
- `phase1.search_trigram=true`

Flags are typed server configuration, require restart in Phase 1, are exposed only through a safe capabilities response, and do not change migration requirements.

## Observability

Track dashboard/brief/search latency and error categories, recommendation feedback, audit failures and AI fallback. Metrics use bounded labels and never include entity IDs, raw content or raw search queries.

## Automated acceptance and exit gates

- Frozen contracts and migrations pass in CI.
- Dashboard p95 <2 seconds with the representative fixture.
- Search p95 <500 ms locally and <800 ms in CI.
- Deterministic ranking of 10,000 entities <500 ms.
- Core task, commitment, note, meeting, risk, search, brief and audit workflows pass API and Playwright tests.
- Every Phase 1 table has workspace-isolation tests.
- All mutations have audit coverage and recommendations require durable confirmation.
- AI-disabled and AI-unavailable tests pass.
- Backup/restore validates exact migration head, constraints and representative data.
- Accessibility core-flow tests pass.
- Zero open critical/high/medium specification or code-review findings and zero known critical/high dependency vulnerabilities.

The one-week daily-use validation remains a product outcome after engineering gates pass; it is not a substitute for automated completion criteria.

## Rollback

Migrations must have tested downgrade or documented forward-fix paths. Recommendation and AI enrichment flags may be disabled without affecting the deterministic dashboard, local search, audit records or authoritative data.