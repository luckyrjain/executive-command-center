---
id: PHASE-010-TEST-PLAN
title: Phase 10 Gmail Test Plan
status: Approved for Implementation
version: 1.2.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-010-SYNC-CONTRACT
  - PHASE-010-PRIVACY-CONSENT-CONTRACT
---

# Phase 10 Gmail Test Plan

## Current automated evidence (Tasks 1-6)

| Area | Committed test path | Coverage |
|---|---|---|
| OAuth/config/allowlist | `tests/test_gmail_connector_postgres.py` | URL, state expiry/signature/session binding, exchange/profile failures, scope and allowlist rejection, reconnect/race/rollback safety, encrypted credential non-disclosure |
| Generic connector reuse | `tests/test_gmail_connector_postgres.py` | Gmail list and manual sync through existing connector endpoints |
| Backfill/history sync | `tests/test_gmail_connector_sync_postgres.py` | 30-day query, pagination/bounds, cursor expiry/fallback/resume, deduplication, ordering, consent rechecks, rate limits, malformed responses/headers, entity linking and concurrency |
| Migration/backup fixtures | `scripts/seed_phase1_acceptance.py` | Representative email domain/thread/message rows are included in generic restore invariants |
| Awaiting-reply attention (Task 3) | `tests/test_attention_email_awaiting_reply_postgres.py` | Positive/negative surfacing, disabled-domain and unresolved-sender exclusion, staleness aging, dismissal persistence, removed-member exclusion, casefold-divergent sender resolution |
| Create-type recommendations (Task 4) | `tests/test_recommendations_postgres.py` | Schema validation of the create-type shape, generate/publish/confirm/execute for task/commitment/risk, `target_expected_version` presence/absence enforcement, no cross-supersession |
| `email.detect_action` (Task 5) | `tests/test_email_action_tools_postgres.py`, `tests/test_gmail_action_detection_sync_postgres.py`, `tests/test_ai_runtime_email_detect_action_evaluation_postgres.py`, `tests/test_ai_runtime_runtime_postgres.py` | Workspace/owner-scoped thread-content tool (decryption, size-bounded cap, trigger-message inclusion), body fetch/consent-recheck/RecursionError-guard in the sync-pipeline hook, evaluation-harness floors and synthetic-source isolation, prompt-injection cannot dispatch an out-of-scope tool |
| On-demand thread read/forget (Task 6) | `tests/test_gmail_threads_postgres.py` | Fetch-and-cache round-trip and no-refetch-when-cached for `GET`; one message's fetch failure does not fail the whole request; a disconnected connector account skips the live-fetch attempt (no Gmail call, cached content still renders); consent rejection; 404 for nonexistent and cross-workspace threads; "forget" scoped to only the targeted thread with exactly one `deletion_jobs` row recorded; idempotency replay; 404 forgetting a nonexistent thread; reopening a forgotten thread refetches its content (proves content is nulled, not deleted) |

These are committed, rerunnable tests. They primarily use mocked Google HTTP
transport and real PostgreSQL. Their existence does not satisfy real-account,
privacy-operation, or production-recovery gates.

## Required Task 7-8 automated tests

- consent-revocation disconnect/purge ordering, retry, partial failure,
  deletion propagation, and completion evidence;
- Gmail panel loading/empty/stale/partial/error/deletion states, keyboard
  operation, responsive layout, and WCAG 2.2 AA browser checks.

Deterministic awaiting-reply attention tests, create-type recommendation
tests, and `email.detect_action`'s own prompt-injection/prohibited-action
adversarial fixtures (zero automatic writes -- `EmailDetectActionOutput`'s
fail-closed model validator plus grounding-check enforcement) and body
fetch/cache encryption/authorization/permission-loss/malformed-MIME/size-
bound tests have shipped -- see "Current automated evidence" above. On-demand
thread read/forget tests (Task 6) have likewise shipped; the item removed
here is narrower than Task 6's own "forget" -- Task 6 nulls one thread's
cached content, not the connector-wide disconnect/purge this bullet still
correctly lists as unshipped.

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
| 1.1.0 | 2026-08-06 | Task 5 review (Loop 2 round 16): recorded shipped Task 3/4/5 automated evidence, moved out of "Required"; this document had gone stale after Tasks 3-5 shipped without a contract-version update | Lucky Jain |
| 1.2.0 | 2026-08-06 | Task 6: recorded shipped on-demand thread read/forget test coverage, renamed "Required" heading from "Task 6-8" to "Task 7-8" | Lucky Jain |
