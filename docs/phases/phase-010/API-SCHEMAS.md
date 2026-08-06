---
id: PHASE-010-API-SCHEMAS
title: Phase 10 Gmail API Schemas
status: Approved for Implementation
version: 1.0.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-006-API-SCHEMAS
---

# Phase 10 Gmail API Schemas

## Delivery boundary

- **Current (Tasks 1-2):** OAuth start/callback plus reuse of generic
  connector list, sync, sync-run, and disable endpoints.
- **Planned (Tasks 3-8):** email attention, body-reading, consent cascade,
  recommendation-create, and Gmail panel endpoints or response extensions.

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

## Planned APIs

Tasks 3-8 must version this contract before adding attention projections,
create-type recommendations, on-demand thread/body reads, consent-revocation
cascade, or frontend-specific query surfaces. No current endpoint returns
thread subjects, participants, snippets, or bodies.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented current OAuth and generic sync surfaces | Lucky Jain |
