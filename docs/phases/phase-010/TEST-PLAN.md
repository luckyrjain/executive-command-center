---
id: PHASE-010-TEST-PLAN
title: Phase 10 Gmail Test Plan
status: Approved for Implementation
version: 1.0.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-010-SYNC-CONTRACT
  - PHASE-010-PRIVACY-CONSENT-CONTRACT
---

# Phase 10 Gmail Test Plan

## Current automated evidence (Tasks 1-2)

| Area | Committed test path | Coverage |
|---|---|---|
| OAuth/config/allowlist | `tests/test_gmail_connector_postgres.py` | URL, state expiry/signature/session binding, exchange/profile failures, scope and allowlist rejection, reconnect/race/rollback safety, encrypted credential non-disclosure |
| Generic connector reuse | `tests/test_gmail_connector_postgres.py` | Gmail list and manual sync through existing connector endpoints |
| Backfill/history sync | `tests/test_gmail_connector_sync_postgres.py` | 30-day query, pagination/bounds, cursor expiry/fallback/resume, deduplication, ordering, consent rechecks, rate limits, malformed responses/headers, entity linking and concurrency |
| Migration/backup fixtures | `scripts/seed_phase1_acceptance.py` | Representative email domain/thread/message rows are included in generic restore invariants |

These are committed, rerunnable tests. They primarily use mocked Google HTTP
transport and real PostgreSQL. Their existence does not satisfy real-account,
privacy-operation, or production-recovery gates.

## Required Task 3-8 automated tests

- deterministic awaiting-reply attention positives, negatives, replay, and
  stale/removal behavior;
- create-type task/commitment/risk recommendation schema, evidence grounding,
  authorization, idempotency, confirmation, audit, and rollback;
- prompt-injection and prohibited-action adversarial fixtures with zero
  automatic writes;
- body fetch/cache encryption, authorization, expiry, permission loss,
  malformed MIME/size bounds, and never-in-list/log assertions;
- consent-revocation disconnect/purge ordering, retry, partial failure,
  deletion propagation, and completion evidence;
- Gmail panel loading/empty/stale/partial/error/deletion states, keyboard
  operation, responsive layout, and WCAG 2.2 AA browser checks.

## Required non-mocked evidence before promotion

| Gate | Required record |
|---|---|
| Real Gmail account | Internal allowlisted test account completes OAuth, 30-day backfill, incremental sync, reconnect, permission loss, and revoke |
| Backup/restore | Encrypted thread/message fixtures restore with checksums, owner isolation, indexes, cursors, and application readiness |
| Privacy | Export/delete/revoke test proves zero readable content and derived-record propagation |
| Performance | Representative mailbox records p50/p95, quota calls, DB time, memory, and bounded partial-resume behavior |
| Security/adversarial | Independent review plus malicious headers/MIME, OAuth state, token leakage, cross-owner/workspace, SSRF/redirect, and prompt injection tests |
| Accessibility | Browser report covering every UX state in `UX-STATES.md` |
| Recovery | Operator executes `PHASE-10-GMAIL-RECOVERY.md` scenarios and commits sanitized results |

No gate is satisfied by an unchecked box or an uncommitted local report.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Recorded current Task 1-2 coverage and required promotion evidence | Lucky Jain |
