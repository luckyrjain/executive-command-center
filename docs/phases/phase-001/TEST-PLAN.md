---
id: PHASE-001-TEST-PLAN
title: Phase 1 Test Plan
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Phase 1 Test Plan

## Test layers

### Domain unit tests

Cover every lifecycle transition, validation rule, priority factor, confidence calculation, tie-breaker, recommendation state transition, brief section rule, duplicate suppression, archive behavior, and timezone boundary.

### Database integration tests

Run against PostgreSQL 18. Prove migrations upgrade from Phase 0, downgrade or documented forward-fix behavior, indexes exist, optimistic versions increment, append-only audit permissions, idempotency uniqueness, and composite workspace foreign keys reject cross-workspace tasks, commitments, meetings, evidence, recommendations, and feedback.

### API contract tests

Validate every route and schema in API-SCHEMAS, authentication/session-derived workspace, 404 non-disclosure for cross-workspace IDs, idempotent replay, idempotency conflict, 409 version conflict, malformed cursors, archive filters, error envelopes, correlation IDs, evidence access states, and feature capabilities.

### Frontend component tests

Cover section rendering, empty/degraded/error states, score explanation, evidence display, form validation, conflict UI, note autosave, confirmation preview, and timezone labels.

### End-to-end tests

Playwright scenarios:

1. create, edit, complete, archive and restore a task;
2. create and fulfil a commitment with evidence;
3. create and search a note;
4. create a local calendar event and meeting;
5. view deterministic dashboard and morning brief with AI disabled;
6. confirm and reject recommendations;
7. recover from version conflict;
8. inspect audit history;
9. keyboard-only critical workflow;
10. session expiry and reauthentication.

### Security and privacy tests

Cross-workspace isolation, session-derived identity, CSRF protection, cookie attributes, authorization failures, secret and note-body audit redaction, HTML escaping in search snippets, malicious evidence URIs, recommendation payload filtering, dependency audit, container scan, and secret scan.

### Performance tests

Representative fixture: 10,000 tasks/commitments/risks/events, 50,000 notes, 100,000 audit rows. Gates:

- dashboard p95 < 2 seconds,
- search p95 < 500 ms locally and < 800 ms CI,
- priority ranking 10,000 entities < 500 ms,
- task/commitment mutation p95 < 300 ms,
- morning brief deterministic generation p95 < 2 seconds,
- no query above the approved statement timeout.

### Backup and restore

Backup populated Phase 1 data, restore to a clean database, verify exact Alembic head, row counts, checksums, workspace constraints, audit immutability, search rebuild, and application readiness.

## CI gates

Frozen dependency install, Ruff, formatting, strict mypy, backend tests, frontend typecheck/tests/build, Playwright core flow, migration tests, Docker builds, pip/pnpm audit, Gitleaks, Trivy, SBOM, accessibility scan, and representative-data performance smoke tests.

## Exit criteria

Phase 1 is approved complete only when:

- all contract and acceptance tests pass;
- zero open critical/high/medium specification or code-review findings;
- zero critical/high known dependency vulnerabilities;
- deterministic dashboard and brief work with AI disabled;
- every listed mutation produces a redacted audit event;
- no mutation executes from a recommendation without durable confirmation;
- workspace isolation tests cover every Phase 1 table;
- migrations and restore validation pass;
- accessibility core flows pass;
- one-week product-use validation is recorded as a separate product outcome, not a substitute for engineering gates.