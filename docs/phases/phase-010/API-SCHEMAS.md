---
id: PHASE-010-API-SCHEMAS
title: Phase 10 Gmail API Schemas
status: Approved for Implementation
version: 1.1.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-006-API-SCHEMAS
---

# Phase 10 Gmail API Schemas

## Delivery boundary

- **Current (Tasks 1-2, 6):** OAuth start/callback plus reuse of generic
  connector list, sync, sync-run, and disable endpoints; on-demand thread
  reading and per-thread "forget this" (Task 6, below) -- the first
  Gmail-specific *read* HTTP endpoints, and the first sub-domain-
  granularity deletion endpoint anywhere in this codebase.
- **Planned (Tasks 7-8):** consent-revocation cascade, and Gmail panel
  endpoints or response extensions.

## Current OAuth endpoints

### `POST /api/v1/personal/gmail/oauth/start`

Requires an authenticated workspace member with write permission and a valid
CSRF token. The caller's ECC account email must be in
`ECC_GMAIL_OAUTH_ALLOWLIST`.

```json
{"authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?..."}
```

Errors: `403 GMAIL_ACCOUNT_NOT_ALLOWLISTED`, `403` CSRF/authorization errors,
or `422 GMAIL_OAUTH_NOT_CONFIGURED`. The returned state is HMAC-signed,
session-bound, and expires after 600 seconds.

### `GET /api/v1/personal/gmail/oauth/callback?code=...&state=...`

Requires the same authenticated workspace session. It verifies state,
exchanges the code, verifies both required Gmail scopes, fetches the Google
account email, rechecks the allowlist, and creates or safely reactivates a
connector account.

Response is the existing `ConnectorAccountResponse`:

```json
{
  "id": "uuid",
  "provider": "gmail",
  "external_account_id": "owner@example.com",
  "display_name": "owner@example.com",
  "granted_scopes": [
    "https://www.googleapis.com/auth/gmail.metadata",
    "https://www.googleapis.com/auth/gmail.readonly"
  ],
  "status": "active",
  "status_detail": null,
  "last_synced_at": null,
  "last_error": null,
  "disconnected_at": null,
  "version": 1,
  "created_at": "2026-08-06T00:00:00Z",
  "updated_at": "2026-08-06T00:00:00Z"
}
```

Errors: `403 GMAIL_OAUTH_STATE_INVALID`, `422` with `{code:
GMAIL_OAUTH_FAILED, error: <sanitized>}`, authorization errors, or standard
validation errors.

## Current reused connector endpoints

| Endpoint | Gmail contract |
|---|---|
| `GET /api/v1/engineering/connectors` | Lists authorized visible accounts; never returns credentials |
| `POST /api/v1/engineering/connectors/{id}/sync` | Body `{"run_type":"backfill|incremental","resource_type":"message"}`; requires `Idempotency-Key` and CSRF |
| `GET /api/v1/engineering/sync-runs` | Lists redacted run outcome and item count |
| `POST /api/v1/engineering/connectors/{id}/disable` | Revokes the Google token best-effort and marks the account disconnected |

Manual `webhook` sync is not accepted. A second running sync for the same
account returns `409 CONNECTOR_SYNC_IN_PROGRESS`. Provider errors are
sanitized before persistence or response.

## Current thread endpoints (Task 6)

### `GET /api/v1/personal/gmail/threads/{thread_id}`

Requires an authenticated workspace session with an active `email` domain
consent (`403 EMAIL_CONSENT_NOT_ACTIVE` otherwise). Fetches any not-yet-
cached message body in the thread from Gmail (`gmail.readonly`) before
responding -- the first endpoint anywhere in this codebase that returns
thread subjects, senders, or bodies (correcting this document's own prior
"no current endpoint" claim, now stale). The response carries each
message's `sender` only, not a full participant/recipient list, and no
`snippet` field -- matching the worked example below.

```json
{
  "subject": "Signed contract needed by Friday",
  "messages": [
    {
      "id": "uuid",
      "sender": "priya@partner-co.test",
      "sent_at": "2026-08-06T00:00:00Z",
      "direction": "inbound",
      "body": "Hi -- could you please sign and send back the attached contract..."
    }
  ]
}
```

Errors: `404 THREAD_NOT_FOUND` (does not exist, or belongs to a different
workspace/owner -- non-disclosing, matching every other read endpoint in
this codebase).

### `POST /api/v1/personal/gmail/threads/{thread_id}/forget`

Requires the same authenticated session, CSRF token, and `Idempotency-Key`
every other mutating personal-domain endpoint requires. Nulls the cached
`snippet`/`body`/`body_fetched_at` for every message in the targeted
thread only -- does not delete the thread or its messages' own structural
rows (see `PRIVACY-CONSENT-CONTRACT.md`'s own entry for why).

```json
{
  "id": "uuid",
  "thread_id": "uuid",
  "status": "completed",
  "requested_at": "2026-08-06T00:00:00Z",
  "completed_at": "2026-08-06T00:00:00Z"
}
```

Errors: `404 THREAD_NOT_FOUND`.

## Planned APIs

Tasks 7-8 must version this contract before adding consent-revocation
cascade or frontend-specific query surfaces.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented current OAuth and generic sync surfaces | Lucky Jain |
| 1.1.0 | 2026-08-06 | Task 6: documented `GET`/`POST .../forget` thread endpoints, correcting the "no current endpoint returns thread content" claim | Lucky Jain |
