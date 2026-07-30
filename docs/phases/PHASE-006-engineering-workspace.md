---
id: PHASE-006
title: Engineering Workspace
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
depends_on:
  - PHASE-005
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005
  - STD-001
contracts:
  - phase-006/DATA-MODEL.md
  - phase-006/API-SCHEMAS.md
  - phase-006/CONNECTOR-CONTRACT.md
  - phase-006/DELIVERY-INTELLIGENCE-CONTRACT.md
  - phase-006/UX-STATES.md
  - phase-006/TEST-PLAN.md
---

# PHASE-006 — Engineering Workspace

## Approved decisions

Contracts moved from Draft after resolving `docs/phases/PHASE-REVIEW.md:137`'s three named approval-gate items (provider scopes and retention; connector release set; metric definitions and source-coverage thresholds) in `docs/superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md`, per that document's own Decision 1-3 sections. In summary: GitHub ships first as the reference connector, with GitLab and Jira explicitly sequenced next against the same `ConnectorAdapter` contract (not descoped); connector credentials are encrypted at rest (Fernet, RFC-005 v1.4.0) with least-privilege read-only scopes by default, no raw provider payload retention, and disconnect/delete kept as two distinct operator actions; the seven `DELIVERY-INTELLIGENCE-CONTRACT.md` metrics are made concrete with windows, population and a 95%/50% coverage-threshold policy, aggregated at system/team/workstream level only, never per person. Implementation begins with Task 1 (connector framework and source projections) by the same kind of repository-owner parallel-start exception Phases 2-5 each received, proceeding alongside Phase 5's own still-open exit gate (`docs/runbooks/PHASE-5-DOGFOOD.md`, 0 of 14 days logged) -- this does not itself close that gate. Implementation plan: `docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`. Delivery status: `docs/phases/phase-006/IMPLEMENTATION-STATUS.md`.

## Objective

Provide an evidence-backed executive view of engineering delivery, reliability, architecture and decisions without replacing source systems or scoring individuals.

## User value

An engineering leader sees delivery and reliability risk, retrieves decision/incident context and prepares reviews without manually correlating multiple engineering systems.

## In scope

Read-first GitHub, GitLab and Jira adapters; connector health/backfill/incremental sync/webhooks; normalized repositories, work items, changes, reviews, deployments, incidents and decisions; Phase 2 identity linking; delivery/reliability metrics; evidence-backed summaries; approved Phase 5 write actions.

## Out of scope

Employee productivity scores; surveillance; mandatory cloud sync; replacing source control/issues; autonomous mutation; code-generation platform; compensation/performance decisions; Slack/email/calendar connectors; unrestricted raw provider data retention.

## Functional requirements

- Source systems remain authoritative; projections retain URL, provider ID, freshness and permission state.
- Sync is incremental, idempotent, resumable, rate-limited and deletion aware.
- Poll/webhook overlap deduplicates deterministically.
- Ambiguous person/project identity uses Phase 2 review.
- Every metric publishes definition/version, window, numerator, denominator, population and coverage.
- Partial coverage is visible and never presented as complete.
- Delivery/risk signals cite source evidence and confidence.
- No individual composite score, leaderboard or performance inference.
- Write actions require Phase 5 policy and connector scope.

## Non-functional requirements

Incremental sync reaches 95% of new events within five minutes when provider limits permit. Backfill resumes without duplicate projections. Overview p95 <2 seconds and metric queries p95 <1 second on acceptance data. Connector failure does not block local records. No source token or private payload appears in logs.

## Architecture impact

Add connector platform adapters and engineering-domain projections. Source adapters depend on provider interfaces, not domain modules. PostgreSQL stores normalized projections/cursors. Phase 2 provides knowledge identity, Phase 3 attention consumes risk, Phase 5 governs writes.

## Data changes

Add accounts, cursors/runs, repositories, work items, changes, reviews, deployments, incidents, decisions, service links, metric snapshots and tombstones in `phase-006/DATA-MODEL.md`.

## API changes

Add connector lifecycle/sync health and engineering overview/query endpoints in `phase-006/API-SCHEMAS.md`. Connector secrets never return. Write operations are exposed only through approved automation.

## Frontend changes

Add Engineering Overview, Delivery, Reliability, Repository, Incident, Decision, Connector Health and Coverage views. Charts provide accessible tables, definitions, windows, coverage and evidence drill-down.

## Security and privacy

OAuth/tokens use least scope, encrypted secret storage and revocation. Webhooks verify signatures/replay windows. Provider payloads are untrusted. Permission loss removes unauthorized derived content. Identity mapping and metrics cannot be used for person surveillance.

## Observability

Measure auth/scope health, sync lag/cursor age, backfill, rate limiting, webhook verification/dedupe, projection errors, permission/deletion propagation, source coverage, metric version and API latency. Avoid high-cardinality user/source content and token logging.

## Test strategy

Provider contract fixtures, sandbox adapters, backfill/resume, webhook/poll dedupe, rate limits, rename/delete/access loss, metric goldens, identity ambiguity, isolation/redaction, malicious payloads, non-surveillance checks, performance, browser acceptance and backup/restore.

## Acceptance criteria

- All three read connectors pass the common contract or are explicitly descoped before approval.
- Backfill/incremental/webhook flows meet durability and freshness gates.
- Permission/deletion propagation passes.
- Metric golden datasets match hand-calculated results.
- Partial source coverage and definitions are visible.
- No person ranking fields/routes/UI exist.
- Approved write actions cannot bypass Phase 5.

## Exit criteria

- Connector contracts, provider scopes and technology additions approved.
- Production-like sandbox sync, restore and operational runbooks pass.
- Delivery/reliability metrics are signed off against source evidence.
- Non-surveillance review and browser acceptance complete.
- Zero open Critical, High or Medium findings.
- Phase 7 can consume stable optional context without importing engineering permissions.

## Rollback plan

Disable/revoke connectors independently and stop new sync. Preserve last-known projections with freshness state or delete per policy. Rebuild from source. Disable writes without affecting read views. Cursor/schema changes use forward fix when downgrade risks duplication.

## Deferred backlog

Additional providers, code intelligence, incident automation, team benchmarking, organization-wide engineering analytics, team integration outside engineering and autonomous repository mutations.
