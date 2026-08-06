---
id: PHASE-10-GMAIL-RECOVERY
title: Phase 10 Gmail Recovery Runbook
status: Active
version: 1.0.0
owner: Lucky Jain
updated: 2026-08-06
---

# Phase 10 Gmail Recovery Runbook

This runbook reflects Phase 10 Tasks 1–2 only. It does not describe Tasks 3–8 as available. Production recovery remains blocked by PR-004, PR-006, and PR-008 in [`../operations/PRODUCTION-READINESS.md#blockers`](../operations/PRODUCTION-READINESS.md#blockers).

## Supported operator actions

1. Confirm the user is in `ECC_GMAIL_OAUTH_ALLOWLIST`, has active email-domain consent, and the OAuth client/redirect configuration matches the deployment.
2. Start authorization with `POST /api/v1/personal/gmail/oauth/start`; complete the returned Google flow and callback. Do not log the authorization code, access token, refresh token, or callback URL.
3. Inspect the connector through `GET /api/v1/engineering/connectors` and initiate manual sync with `POST /api/v1/engineering/connectors/<id>/sync`. The current implementation backfills 30 days, then uses a history cursor for incremental metadata sync; no background schedule or push delivery exists.
4. If access should stop, call `POST /api/v1/engineering/connectors/<id>/disable`, then verify revocation in the user's Google Account. Local disable is authoritative even if Google's best-effort revoke call fails.

Only header/metadata projection is implemented. Body/snippet retrieval, attention/recommendation/AI integration, consent-revocation cascade, and frontend workflow are not current capabilities.

## Consent or sync failure

An absent or mid-sync revoked email consent stops further fetch/write work. Preserve the sync-run error and leave existing data untouched. Automatic disconnect and purge on consent revocation are **Unsupported — production blocker**; contain by disabling the connector, verifying Google-side revocation, avoiding further sync, and treating existing Gmail projections as sensitive until Phase 10 Task 7 closes PR-008. Do not delete projection rows directly.

For an expired history cursor, rate limit, provider outage, or partial sync, retain the recorded cursor and projections, correct authorization/availability, and retry through the normal manual sync endpoint. A real-account recovery exercise remains **Unsupported — production blocker** until PR-006 closes. Connector-key rotation/re-encryption is likewise unsupported; see PR-004.

## Evidence to retain

Record connector and sync-run IDs, consent state (not consent content), timestamps, safe error code, whether Google-side revocation was verified, projected row counts without message data, and reviewer. Never retain email addresses, subjects, message IDs, headers, tokens, or OAuth codes in public artifacts.
