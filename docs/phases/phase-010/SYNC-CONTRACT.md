---
id: PHASE-010-SYNC-CONTRACT
title: Phase 10 Gmail Sync Contract
status: Approved for Implementation
version: 1.2.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-006-CONNECTOR-CONTRACT
---

# Phase 10 Gmail Sync Contract

## Current behavior (Tasks 2-5)

Gmail sync is pull-based and manually invoked through the existing connector
sync endpoint. No scheduler or Pub/Sub push consumer is shipped.

Task 3 adds a deterministic "awaiting reply" attention projection computed
from already-synced thread/message data (`attention.py:regenerate_
attention`'s own `email_thread` branch), not a sync-path change of its own.
Task 5 adds controlled body retrieval: `gmail_adapter.py:detect_actions_
since`, called from the sync pipeline's own success path, fetches one newly-
eligible message's full body (`gmail.readonly`, `format=full`), stores it
encrypted, and (via `email.detect_action`) may create a `source="ai"`
recommendation plus its own evidence row -- never a direct `tasks`/
`commitments`/`risks` write. Feature-flagged off by default (`ECC_EMAIL_
ACTION_DETECTION_ENABLED`). See `IMPLEMENTATION-STATUS.md`'s own Task 3/5
evidence sections for the full detail this summary intentionally omits.

### Backfill

- Resource type is `message`; other types complete with zero items.
- Default lower bound is now minus 30 days.
- An explicit `since` value narrows or expands the Gmail `after:<epoch>` query.
- `messages.list` pages contain at most 50 IDs; one invocation fetches at most
  200 messages, then returns `partial` without claiming it is caught up.
- A fully completed pass returns the highest observed `historyId`; an empty
  window falls back to `users.getProfile.historyId`.

### Incremental sync

- A missing cursor behaves as a fresh 30-day backfill.
- A plain Gmail `historyId` resumes `history.list`.
- A compound `historyId:recordId:skipCount` cursor permits progress through
  one oversized history record without replay livelock.
- Gmail `404` for an expired history cursor falls back to backfill.
- A cursor advances only past work durably processed; partial backfill does
  not invent a caught-up cursor.

### Idempotency and ordering

Threads upsert on `(workspace_id, connector_account_id,
external_thread_id)`. Messages insert on `(workspace_id, thread_id,
external_message_id)` with conflict-as-no-op. Thread `last_message_at` uses
`GREATEST`; out-of-order messages cannot move it backward. Entity aliases
converge through their unique normalized-email constraint.

### Consent and permissions

An active `email` domain consent is checked before external fetch and again
before each message write. Revocation during a call stops further writes.
OAuth refresh requires both `gmail.metadata` and `gmail.readonly`; missing
scope returns `permission_lost`. **Current limitation:** there is no scheduled
permission reconciliation or automatic consent-revocation disconnect/purge.

### Rate limits and malformed input

HTTP `429` and Gmail quota-specific `403` reasons get one bounded retry when
the wait is at most five seconds. Continued throttling returns `partial` with
a redacted error summary. Other provider failures fail the run. Header/address
lengths, recipient count, parsing complexity, invalid timestamps, duplicate
IDs, and response shapes are bounded or rejected so one malformed email does
not wedge the account cursor.

## Planned behavior (Tasks 7-8)

- consent-revocation disconnect and purge;
- executive sync state and retry UI.

Deterministic awaiting-reply attention projection (Task 3), controlled body
retrieval using `gmail.readonly` (Task 5), and recommendation/evidence
creation (Task 5) have shipped -- see "Current behavior" above.

Task 6 shipped on-demand human-facing thread reading and a per-thread
"forget this" action (`GET`/`POST .../forget`, `API-SCHEMAS.md`'s own
"Current thread endpoints" section), but neither is a sync-pipeline change:
the `GET` endpoint fetches an already-known thread's not-yet-cached
message bodies synchronously on request, outside backfill/incremental
sync's own cursor-driven flow, and "forget" only nulls cached content for
one thread, not the connector-wide "disconnect and purge" this section's
own bullet still correctly lists as unshipped. The "Current limitation" the
Consent and permissions subsection above already names -- no scheduled
permission reconciliation or automatic consent-revocation disconnect/purge
-- remains accurate after Task 6.

## Polling and push decision

The current activation is manual pull only. Periodic polling requires an
approved cadence, ownership, quota budget, and recovery SLO. Gmail watch/Cloud
Pub/Sub push is explicitly deferred and `handle_webhook` remains a no-op.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented Task 2 backfill and history-cursor behavior | Lucky Jain |
| 1.1.0 | 2026-08-06 | Task 5 review (Loop 2 round 16): moved shipped Task 3/5 behavior (attention projection, body retrieval, recommendation/evidence creation) from "Planned" to "Current"; this document had gone stale after Tasks 3-5 shipped without a contract-version update | Lucky Jain |
| 1.2.0 | 2026-08-06 | Task 6: clarified that the new on-demand thread read/forget HTTP surface is not a sync-pipeline change, and renamed the "Planned" heading from "Tasks 6-8" to "Tasks 7-8" now that Task 6 has shipped | Lucky Jain |
