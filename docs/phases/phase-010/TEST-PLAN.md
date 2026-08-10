---
id: PHASE-010-TEST-PLAN
title: Phase 10 Gmail Test Plan
status: Approved for Implementation
version: 1.4.4
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-010-SYNC-CONTRACT
  - PHASE-010-PRIVACY-CONSENT-CONTRACT
---

# Phase 10 Gmail Test Plan

## Current automated evidence (Tasks 1-7)

| Area | Committed test path | Coverage |
|---|---|---|
| OAuth/config/allowlist | `tests/test_gmail_connector_postgres.py` | URL, state expiry/signature/session binding, exchange/profile failures, scope and allowlist rejection, reconnect/race/rollback safety, encrypted credential non-disclosure |
| Generic connector reuse | `tests/test_gmail_connector_postgres.py` | Gmail list and manual sync through existing connector endpoints |
| Backfill/history sync | `tests/test_gmail_connector_sync_postgres.py` | 30-day query, pagination/bounds, cursor expiry/fallback/resume, deduplication, ordering, consent rechecks, rate limits, malformed responses/headers, entity linking and concurrency |
| Migration/backup fixtures | `scripts/seed_phase1_acceptance.py` | Representative email domain/thread/message rows are included in generic restore invariants |
| Awaiting-reply attention (Task 3) | `tests/test_attention_email_awaiting_reply_postgres.py` | Positive/negative surfacing, disabled-domain and unresolved-sender exclusion, staleness aging, dismissal persistence, removed-member exclusion, casefold-divergent sender resolution |
| Create-type recommendations (Task 4) | `tests/test_recommendations_postgres.py` | Schema validation of the create-type shape, generate/publish/confirm/execute for task/commitment/risk, `target_expected_version` presence/absence enforcement, no cross-supersession |
| `email.detect_action` (Task 5) | `tests/test_email_action_tools_postgres.py`, `tests/test_gmail_action_detection_sync_postgres.py`, `tests/test_ai_runtime_email_detect_action_evaluation_postgres.py`, `tests/test_ai_runtime_runtime_postgres.py` | Workspace/owner-scoped thread-content tool (decryption, size-bounded cap, trigger-message inclusion), body fetch/consent-recheck/RecursionError-guard in the sync-pipeline hook, evaluation-harness floors and synthetic-source isolation, prompt-injection cannot dispatch an out-of-scope tool |
| On-demand thread read/forget (Task 6) | `tests/test_gmail_threads_postgres.py` | Fetch-and-cache round-trip and no-refetch-when-cached for `GET`; one message's fetch failure does not fail the whole request; a disconnected connector account skips the live-fetch attempt (no Gmail call; cached content, if any, is unaffected by this path); consent rejection; 404 for nonexistent and cross-workspace threads; "forget" scoped to only the targeted thread with exactly one `deletion_jobs` row recorded; idempotency replay; 404 forgetting a nonexistent thread; reopening a forgotten thread refetches its content (proves content is nulled, not deleted) |
| Consent revocation cascade (Task 7) | `tests/test_gmail_revocation_postgres.py`, `tests/test_engineering_connectors_postgres.py` | Disabling `email` disconnects the connector and purges threads/messages, `email_thread` attention items, non-`executed` recommendations, and their `gmail_sync` evidence; an already-`executed` recommendation is redacted in place, not deleted, and its confirmed `target_id` survives; `pkos_nodes` survive (only their evidence is removed); `revoke_consent_endpoint` and `delete_domain_endpoint` reach the identical cascade; disabling an already-disabled domain, and an idempotency-key replay, both no-op rather than re-running the cascade; a non-`email` domain's own disable is unaffected; an owner with two simultaneously-active `gmail` connector accounts has both disconnected by one cascade run (Loop 2 round 1 review); the generic engineering `POST /connectors/{id}/disable` endpoint rejects `gmail`-provider accounts outright (`409 GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT`) but stays an idempotent no-op for an already-disconnected one (Loop 2 rounds 1-2 review); the pool connection and cascade's own row lock are released before the blocking Google revoke call for both call sites that reach it; a second owner's own Gmail data (and connector) in the same workspace is untouched by the first owner's disable; two owners whose Gmail accounts produce a message sharing the same raw `external_message_id` do not leak `pkos_evidence` across the ownership boundary (Loop 2 round 4 review, all three); two owners disabling *sequentially* (not concurrently) for a colliding id do not leak the first owner's own preserved evidence via `email_message_id_purge_log` (migration `0076`); `revoke_consent_endpoint` rejects a `consent_id` a *later* grant has superseded rather than re-running the cascade against freshly re-granted state, while a merely-revoked `consent_id` with nothing superseding it -- including a same-`Idempotency-Key` retry of an already-succeeded revoke -- still resolves normally and is served from the idempotency cache, not re-executed (Loop 2 round 8 review, refined round 9); that re-check now genuinely serializes against a concurrent re-grant for the same domain via the shared `personal_domains` row lock (Loop 2 round 10 review, closing a TOCTOU window rounds 8-9 left open), proven end-to-end by revoking for real, holding the row lock, replaying the now-superseded `consent_id` against `revoke_consent_endpoint` itself in a background thread, regranting over the held connection, and asserting the replay rejects with `404 CONSENT_NOT_FOUND` once unblocked (Loop 2 round 11 review: the round-10 version of this test only proved generic lock contention, not `revoke_consent_endpoint`'s own behavior); `delete_domain_endpoint`'s own `for_update=True` lock (Loop 2 round 11 review) serializes its read-cascade-write sequence against a concurrent re-grant, though -- unlike `_disable_domain` -- it has no `consent_id` to reject staleness on, the same already-accepted self-race behavior `disable_domain_endpoint` has always had (Loop 2 round 12 review corrected the round-11 test-plan wording, which had overclaimed parity with `_disable_domain`'s own rejection behavior); two owners' colliding `external_message_id`s are also serialized against each other under genuine, not merely sequential, concurrency via a `pg_advisory_xact_lock` keyed on `workspace_id` (Loop 2 round 17 review, closing a TOCTOU window the round-4/round-8 fixes left open for two truly-overlapping cascades; round 19 review widened the key from one lock per candidate id to one per workspace to close a resource-exhaustion risk the original per-id keying had for large mailboxes); reusing the same `Idempotency-Key` for `disable_domain_endpoint`/`delete_domain_endpoint` across a real disable/delete followed by a genuine re-enable followed by a second disable/delete attempt is rejected with `409 IDEMPOTENCY_CONFLICT` rather than silently served the first call's stale cached response, while a same-key replay with nothing else changed in between is still served from cache correctly (Loop 2 round 21 review); the identical rejection also holds when the intervening state change is a genuine Gmail reconnect through the separate OAuth callback flow rather than a domain re-enable, since that flow never touches `personal_domains` (Loop 2 round 22 review, closing a gap round 21's `domain.enabled`-only check missed); a genuinely fresh (non-replayed) disable request against an already-disabled domain also now reaches a Gmail account reconnected in the meantime, not only a replayed-key request -- proven by asserting a thread inserted after the reconnect is purged by the fresh call (Loop 2 round 24 review, HIGH finding); the generic engineering `/disable` endpoint's own `gmail`-provider rejection is itself gated on the owner having a `personal_domains` row for `email` -- an owner with no such row (Gmail OAuth-connected but never enabled the `email` domain) falls through to the ordinary disconnect instead of being stuck behind a rejection the domain-level endpoints can't satisfy either (Loop 2 round 25 review, MEDIUM finding); that same generic `/disable` endpoint also now rejects a reused `Idempotency-Key` with `409 IDEMPOTENCY_CONFLICT` if the account is no longer `disconnected` when replayed (a real Gmail reconnect in between), the identical stale-cache-after-reconnect gap rounds 21-22 closed for the domain-level endpoints (Loop 2 round 27 review, MEDIUM-HIGH finding) |

These are committed, rerunnable tests. They primarily use mocked Google HTTP
transport and real PostgreSQL. Their existence does not satisfy real-account,
privacy-operation, or production-recovery gates.

## Required Task 8 automated tests

- Gmail panel loading/empty/stale/partial/error/deletion states, keyboard
  operation, responsive layout, and WCAG 2.2 AA browser checks.

Deterministic awaiting-reply attention tests, create-type recommendation
tests, `email.detect_action`'s own prompt-injection/prohibited-action
adversarial fixtures (zero automatic writes -- `EmailDetectActionOutput`'s
fail-closed model validator plus grounding-check enforcement), body
fetch/cache encryption/authorization/permission-loss/malformed-MIME/size-
bound tests, on-demand thread read/forget, and the consent revocation
cascade (disconnect/purge ordering, retry via the existing idempotency-key
mechanism, and derived-record propagation to attention/recommendations/
evidence -- see `PRIVACY-CONSENT-CONTRACT.md`'s own Task 7 section for why
no separate "partial failure" state was needed) have all shipped -- see
"Current automated evidence" above.

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
| 1.3.0 | 2026-08-10 | Task 7: recorded shipped consent revocation cascade test coverage, renamed "Required" heading from "Task 7-8" to "Task 8" | Lucky Jain |
| 1.3.1 | 2026-08-10 | Task 7 Loop 2 round 1-2 review: recorded the multi-connector-account and generic-disable-endpoint-rejection regression coverage, including the cross-file `test_engineering_connectors_postgres.py` addition | Lucky Jain |
| 1.3.2 | 2026-08-10 | Task 7 Loop 2 round 4 review: recorded the idempotent-no-op, pool/lock-release concurrency, cross-owner isolation, and colliding-external-message-id evidence coverage | Lucky Jain |
| 1.3.3 | 2026-08-10 | Task 7 Loop 2 round 8 review: recorded the sequential (non-concurrent) cross-owner evidence-leak regression and the stale-consent-id-replay rejection coverage | Lucky Jain |
| 1.3.4 | 2026-08-10 | Task 7 Loop 2 round 10 review: this file was never revisited after round 8's own consent-lookup coverage was refined round 9 (a merely-revoked, not-superseded `consent_id` -- e.g. an `Idempotency-Key` retry -- must still resolve normally); corrected | Lucky Jain |
| 1.3.5 | 2026-08-10 | Task 7 Loop 2 round 10 review: recorded the TOCTOU-closing concurrency coverage (the consent re-check now serializes against a concurrent re-grant via the shared row lock) | Lucky Jain |
| 1.3.6 | 2026-08-10 | Task 7 Loop 2 round 11 review: recorded the rewritten concurrency test now exercising `revoke_consent_endpoint`'s own code path end-to-end (not only generic lock contention), and `delete_domain_endpoint`'s own `for_update=True` TOCTOU fix | Lucky Jain |
| 1.3.7 | 2026-08-10 | Task 7 Loop 2 round 12 review: corrected the 1.3.6 entry's overclaim -- `delete_domain_endpoint`'s lock serializes but does not reject a losing race the way `_disable_domain`'s `consent_id` check does | Lucky Jain |
| 1.3.8 | 2026-08-10 | Task 7 Loop 2 round 17 review: recorded the new advisory-lock fix and test closing the genuine-concurrency gap in the cross-owner colliding-`external_message_id` ambiguity check | Lucky Jain |
| 1.3.9 | 2026-08-10 | Task 7 Loop 2 round 19 review: recorded the round-19 rekeying of the advisory lock from per-candidate-id to per-workspace, closing a resource-exhaustion risk for large mailboxes | Lucky Jain |
| 1.4.0 | 2026-08-10 | Task 7 Loop 2 round 21 review: recorded the new `409 IDEMPOTENCY_CONFLICT` coverage closing an idempotency-key-reuse-after-regrant gap in `disable_domain_endpoint`/`delete_domain_endpoint` (test count 18 -> 20) | Lucky Jain |
| 1.4.1 | 2026-08-10 | Task 7 Loop 2 round 22 review: recorded the widened `409 IDEMPOTENCY_CONFLICT` coverage for a genuine Gmail OAuth reconnect (not only a domain re-enable) after a real disable/delete (test count 20 -> 22) | Lucky Jain |
| 1.4.2 | 2026-08-10 | Task 7 Loop 2 round 24 review (HIGH): recorded coverage proving a genuinely fresh disable request, not only a replayed one, now reaches a Gmail account reconnected after a real disable (test count 22 -> 23) | Lucky Jain |
| 1.4.3 | 2026-08-10 | Task 7 Loop 2 round 25 review (MEDIUM): recorded coverage for the generic engineering `/disable` endpoint's `gmail`-provider rejection now falling through when the owner has no `personal_domains` row for `email` at all | Lucky Jain |
| 1.4.4 | 2026-08-10 | Task 7 Loop 2 round 27 review (MEDIUM-HIGH): recorded coverage for the generic engineering `/disable` endpoint now rejecting an `Idempotency-Key` reused after a genuine Gmail reconnect with `409 IDEMPOTENCY_CONFLICT`, instead of serving a stale cached `"disconnected"` response | Lucky Jain |
