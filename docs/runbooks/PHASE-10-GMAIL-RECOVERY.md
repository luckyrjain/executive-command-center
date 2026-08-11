---
id: PHASE-10-GMAIL-RECOVERY
title: Phase 10 Gmail Recovery Runbook
status: Active
version: 2.0.0
owner: Lucky Jain
updated: 2026-08-11
---

# Phase 10 Gmail Recovery Runbook

This runbook reflects Phase 10's full engineering scope, Tasks 1-8 (`docs/phases/phase-010/IMPLEMENTATION-STATUS.md`): OAuth connector framework and internal allowlist, backfill/incremental sync with entity linking, deterministic awaiting-reply attention integration, the governed recommendations create path, AI-runtime action detection (`email.detect_action`), on-demand thread reading with per-thread "forget this," the consent revocation cascade, and the executive `GmailPanel` UI. Production recovery remains blocked by PR-004, PR-005, PR-006, PR-008, and PR-009 in [`../operations/PRODUCTION-READINESS.md#blockers`](../operations/PRODUCTION-READINESS.md#blockers) -- every capability below is engineering-complete and covered by automated tests against real PostgreSQL, but has not yet been exercised against a real Gmail account or received an independent promotion review.

## Supported operator actions

1. Confirm the user is in `ECC_GMAIL_OAUTH_ALLOWLIST`, has active email-domain consent, and the OAuth client/redirect configuration matches the deployment.
2. Start authorization with `POST /api/v1/personal/gmail/oauth/start`; complete the returned Google flow and callback. Do not log the authorization code, access token, refresh token, or callback URL.
3. Inspect the connector through `GET /api/v1/engineering/connectors` and initiate manual sync with `POST /api/v1/engineering/connectors/<id>/sync`. `run_type: "backfill"` accepts an optional `since` field (defaults to a 30-day window) to widen or narrow the initial/re-run backfill window; `run_type: "incremental"` resumes from the stored Gmail `historyId` cursor and ignores `since`. No background schedule or push delivery exists -- sync is always operator- or UI-triggered.
4. List a user's synced threads with `GET /api/v1/personal/gmail/threads` (metadata: subject, most recent sender/direction, message count, whether a body is cached). Read a specific thread's content on demand with `GET /api/v1/personal/gmail/threads/<id>` -- this performs a live, consent-gated Gmail fetch for any message not already cached, then serves cached content on subsequent reads. Delete just that thread's cached content, without touching the rest of the account, with `POST /api/v1/personal/gmail/threads/<id>/forget`.
5. To disconnect Gmail entirely: `POST /api/v1/personal/domains/email/disable` (or `POST /api/v1/personal/consents/<id>/revoke`, or `POST /api/v1/personal/domains/email/delete`) runs the consent revocation cascade in the same request -- it disconnects every one of the owner's `gmail`-provider connector accounts, best-effort-revokes the Google OAuth grant (deferred past the local transaction commit, so a slow or failed Google-side call never blocks the local cascade), and purges synced `email_threads`/`email_messages`, Gmail-sourced `attention_items`, and Gmail-derived `pkos_evidence` in one action -- there is no reachable path that disconnects without also purging, or purges without also disconnecting. Non-`executed` `email_action_detected` recommendations are deleted; an already-`executed` one is redacted in place, keeping its confirmed target. The generic engineering `POST /api/v1/engineering/connectors/<id>/disable` endpoint rejects an active `gmail`-provider account outright (`409 GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT`) rather than silently leaving Gmail data behind -- always use one of the three domain-level endpoints above for Gmail. Verify revocation independently in the user's Google Account regardless of the local result.

## Consent or sync failure

An absent or mid-sync revoked email consent stops further fetch/write work immediately; the sync run records `status: "partial"` with a safe error summary, and existing data is left untouched. `GmailPanel`'s AI-runtime action-detection sync hook (Task 5) re-checks consent per message, not only once at the start of a batch, so a mid-batch revocation cannot process messages after the revocation lands.

For an expired history cursor, rate limit, provider outage, or partial sync, retain the recorded cursor and projections, correct authorization/availability, and retry through the normal manual sync endpoint (`incremental_sync` resumes from the stored cursor; a genuinely expired cursor falls back to a fresh backfill, matching every other connector in this system). A real-account recovery exercise remains **Unsupported — production blocker** (PR-006) until it runs against real GitHub/GitLab/Jira/Datadog/Gmail accounts with revoke verification at each provider. Connector-key rotation/re-encryption is likewise unsupported; see PR-004 (connector credentials) and PR-005 (the personal-data key `email_threads`/`email_messages` bodies are encrypted under).

The consent revocation cascade described above (Supported operator action 5) is fully implemented and covered by extensive automated tests -- including concurrent-revoke races, cross-owner isolation, and idempotency-key replay -- but real-account end-to-end verification (Google-side revocation confirmed, backup-window evidence) remains **Unsupported — production blocker** (PR-008) until that evidence exists. Treat any Gmail data in a deployment as sensitive until PR-008 closes.

## Evidence to retain

Record connector and sync-run IDs, consent state (not consent content), timestamps, safe error code, whether Google-side revocation was verified, thread/message counts without message content, and reviewer. Never retain email addresses, subjects, message IDs, headers, tokens, or OAuth codes in public artifacts.
