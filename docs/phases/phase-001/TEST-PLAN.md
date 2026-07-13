---
id: PHASE-001-TEST-PLAN
title: Phase 1 Test Plan
status: Approved
version: 1.0.1
owner: Lucky Jain
---

# Phase 1 Test Plan

## Test layers

### Domain unit tests

Cover all lifecycle transitions, including task and commitment cancellation, archive and restore; note archive and restore; separate due-date and due-time precision; priority factors; deterministic waiting-on sources; dismissal source-version behavior; confidence; tie-breakers; recommendation transition rules; brief refresh eligibility versus age staleness; duplicate suppression; meeting timing authority; and timezone boundaries.

### Database integration tests

Run against PostgreSQL 18. Prove migrations from Phase 0, downgrade or forward-fix behavior, required PKOS field migrations, indexes, version increments, pre-archive state restoration, append-only audit permissions, idempotency uniqueness, and composite workspace foreign keys for every Phase 1 table.

### API contract tests

Validate every API-SCHEMAS route, including task complete, cancel, archive and restore; commitment confirm, fulfil, cancel, archive and restore; note archive and restore; rejection of client owner fields; permitted counterparty references; linked Meeting timing restrictions; standalone Meeting timing; mutually exclusive due_date and due_at; recommendation execution only from pending_confirmation; non-execution from rejected, expired and superseded states; atomic rollback and failed-attempt recording; authentication; 404 non-disclosure; idempotency; version conflicts; cursors; archive filters; evidence states; and feature capabilities.

### Frontend and end-to-end tests

Component tests cover section rendering, empty and degraded states, explanations, evidence display, form validation, conflict handling, note autosave, confirmation preview and timezone labels.

Playwright scenarios:

1. create, edit, complete, cancel, archive and restore a task;
2. create, confirm, fulfil, cancel, archive and restore commitments;
3. create, autosave, search, archive and restore a note;
4. create a local CalendarEvent, linked Meeting and standalone Meeting, then reschedule through the authoritative record;
5. view deterministic dashboard and morning brief with AI disabled;
6. confirm, reject, defer and pin recommendations;
7. prove rejected, expired and superseded recommendations cannot execute;
8. recover from a version conflict;
9. inspect audit history and action mappings;
10. complete a keyboard-only core workflow and recover from session expiry.

### Audit, security and privacy tests

Prove every normative API-action to audit-event to domain-event mapping, rollback on audit failure, recommendation transaction atomicity, separate failed-attempt recording, audit redaction, note change-history-only semantics, session-derived identity, request protections, safe rendering of search snippets, safe evidence-link handling, filtering of sensitive proposed-action fields, dependency checks, container checks and cross-workspace isolation.

### Performance tests

Representative fixture: 10,000 tasks, commitments, risks and events; 50,000 notes; and 100,000 audit rows. Gates:

- dashboard p95 below 2 seconds;
- search p95 below 500 ms locally and 800 ms in CI;
- ranking 10,000 entities below 500 ms;
- task and commitment mutation p95 below 300 ms;
- deterministic brief generation p95 below 2 seconds;
- no query above the approved statement timeout.

### Backup and restore

Backup populated Phase 1 data, restore into a clean database, and verify the exact Alembic head, row counts, checksums, workspace constraints, audit immutability, lifecycle restoration fields, PKOS mapped columns, search rebuild and application readiness.

## CI and exit gates

CI includes frozen installs, formatting, typing, backend and frontend tests and builds, Playwright, migration checks, Docker builds, dependency and image checks, accessibility and representative-data performance smoke tests.

Phase 1 is complete only when all contracts pass; critical, high and medium findings are zero; critical and high dependency findings are zero; deterministic behavior works without AI; every mutation has mapped audit coverage; recommendation execution requires durable confirmation; workspace isolation covers every table; migration, restore and accessibility gates pass; and one-week use validation is recorded separately.
