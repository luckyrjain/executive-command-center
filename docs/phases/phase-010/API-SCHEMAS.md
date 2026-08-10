---
id: PHASE-010-API-SCHEMAS
title: Phase 10 Gmail API Schemas
status: Approved for Implementation
version: 1.2.3
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-006-API-SCHEMAS
---

# Phase 10 Gmail API Schemas

## Delivery boundary

- **Current (Tasks 1-2, 6-7):** OAuth start/callback plus reuse of generic
  connector list, sync, sync-run, and disable endpoints; on-demand thread
  reading and per-thread "forget this" (Task 6, below) -- the first
  Gmail-specific *read* HTTP endpoints, and the first sub-domain-
  granularity deletion endpoint anywhere in this codebase; the consent
  revocation cascade (Task 7, below), which adds no new endpoint at all --
  it changes what three of Phase 7's own existing generic personal-domain
  endpoints do for `domain_key = "email"` specifically.
- **Planned (Task 8):** Gmail panel endpoints or response extensions.

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
| `POST /api/v1/engineering/connectors/{id}/disable` | For every other provider: revokes the token best-effort and marks the account disconnected. For `gmail` specifically: if the account is not already `disconnected`, rejected with `409 GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT` and no mutation (Loop 2 round 1 review found this generic endpoint could otherwise disconnect a `gmail` account, and revoke its live Google grant, without running the consent revocation cascade below); an already-`disconnected` `gmail` account is unaffected by this guard and still returns the same idempotent `200` no-op as every other provider (Loop 2 round 2 review). Callers must use the domain-level endpoints instead, which reach `gmail_revocation.cascade_email_revocation` |

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

## Consent revocation cascade (Task 7)

No new endpoint. `POST /api/v1/personal/domains/email/disable`, `POST
/api/v1/personal/consents/{id}/revoke`, and `POST /api/v1/personal/domains/
email/delete` -- Phase 7's own existing generic personal-domain endpoints,
documented in `docs/phases/phase-007/API-SCHEMAS.md`, unchanged in request/
response shape -- now additionally, for `domain_key = "email"` only, best-
effort revoke the owner's Gmail OAuth grant, disconnect every one of
their `gmail` connector account(s), and purge their own email-derived
records (threads, messages, `email_thread` attention items, non-
`executed` `email_action_detected` recommendations, and their evidence --
except an evidence row whose id collides with a different owner's own
message, deliberately left unpurged, see `PRIVACY-CONSENT-CONTRACT.md`)
in the same request. Every
other `domain_key` is unaffected -- disabling `habits`/`health`/`finance`/
etc. still only flips `enabled`/revokes consent, data untouched, exactly
as `docs/phases/phase-007/API-SCHEMAS.md` already documents. `POST .../
consents/{id}/revoke` additionally now requires `id` to name the
currently-active consent (`revoked_at IS NULL`); a stale/already-revoked
`id` gets the same `404 CONSENT_NOT_FOUND` every other unresolved lookup
in this contract returns, rather than resolving to `domain_key` and
re-running the cascade (Loop 2 round 8 review; see `PRIVACY-CONSENT-
CONTRACT.md`). See `PRIVACY-CONSENT-CONTRACT.md`'s own "Consent
revocation cascade (Task 7)" section for the full cascade description and
why "retry" needed no new `deletion_jobs` schema.

## Planned APIs

Task 8 must version this contract before adding Gmail panel endpoints or
response extensions.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented current OAuth and generic sync surfaces | Lucky Jain |
| 1.1.0 | 2026-08-06 | Task 6: documented `GET`/`POST .../forget` thread endpoints, correcting the "no current endpoint returns thread content" claim | Lucky Jain |
| 1.2.0 | 2026-08-10 | Task 7: documented the consent revocation cascade's behavior change to three existing Phase 7 generic endpoints (no new endpoint added) | Lucky Jain |
| 1.2.1 | 2026-08-10 | Task 7 Loop 2 round 4 review: corrected the "Current reused connector endpoints" table's `disable` row, stale since round 1 -- it still described pre-fix behavior for `gmail`, contradicting this same file's own consent revocation cascade section below it | Lucky Jain |
| 1.2.2 | 2026-08-10 | Task 7 Loop 2 round 6 review: the `disable` row missed round 2's already-disconnected idempotent-no-op carve-out; the "Consent revocation cascade" section overclaimed "every email-derived record" against round 4's own ambiguous-id carve-out | Lucky Jain |
| 1.2.3 | 2026-08-10 | Task 7 Loop 2 round 8 review: documented `POST .../consents/{id}/revoke`'s new `revoked_at IS NULL` requirement, closing a stale-consent-id replay that could re-trigger the cascade | Lucky Jain |
