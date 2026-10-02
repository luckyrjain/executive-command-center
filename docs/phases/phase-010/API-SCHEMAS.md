---
id: PHASE-010-API-SCHEMAS
title: Phase 10 Gmail API Schemas
status: Approved for Implementation
version: 1.7.0
owner: Lucky Jain
updated: 2026-10-01
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
- **Current (Task 8):** the Gmail panel's own three backend additions -- a
  thread-list endpoint and a `since` sync parameter, both documented below,
  plus a `recommendation_type` server-side filter on Phase 1's own
  `GET /api/v1/recommendations` (the embedded `RecommendationPanel`
  instance's own dependency, added by a Loop 2 round 5 review fix; this
  file's own delivery boundary is thread-list/`since` only, so see
  `docs/phases/phase-001/API-SCHEMAS.md` for the parameter's own contract).

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
validation errors. Security remediation Spec A adds the responses below.
None of them returns row data or either email address. The four refusals
(identity mismatch, owner conflict, membership inactive, insufficient role)
each write a `denied` `connector_account.enrollment_refused` audit event and
count `ecc_connector_enrollment_refused_total{provider="gmail",reason}`; the
last two items are errors, not refusals, and write neither (see
[Security remediation (Spec A)](#security-remediation-spec-a)):

- `403 GMAIL_ACCOUNT_IDENTITY_MISMATCH` -- with
  `ECC_GMAIL_REQUIRE_IDENTITY_MATCH` on, the Google account's email differs
  from the caller's ECC account email, or the caller has none (S1.1).
- `409 CONNECTOR_OWNED_BY_ANOTHER_MEMBER` -- another member of this
  workspace already has a connector row for this Google account, in any
  status; it is never returned, reactivated or overwritten (S1.2, unflagged).
- `403 MEMBERSHIP_INACTIVE` -- the caller was removed from the workspace
  while the OAuth round trip was in flight; nothing is written (S1.11).
- `403 INSUFFICIENT_ROLE` -- the caller was demoted below write while the
  round trip was in flight (ADR-0014).
- `500 CONNECTOR_ACCOUNT_PERSIST_FAILED` -- a database integrity error other
  than the duplicate-account unique violation (previously reported as a
  `409` duplicate); only SQLSTATE and constraint name are logged (S1.5/S1.6).
  No `enrollment_refused` audit event and no refusal metric.
- `409 GMAIL_ACCOUNT_ALREADY_CONNECTED` (pre-existing, not reachable in
  practice): the conflicting row could not be re-read. No `enrollment_refused`
  audit event and no refusal metric.

On every one of these responses the just-minted Google grant is revoked only if
`revoke_is_safe` allows it: under the default `ECC_GMAIL_REVOKE_SCOPE=global`,
only when no non-disconnected row in any workspace uses that Google account,
so another member's live grant is never revoked.

### `GET /api/v1/personal/gmail/oauth/complete?code=...&state=...` (later addition)

The real Google-facing OAuth redirect target -- `ECC_GMAIL_OAUTH_REDIRECT_URI`
(and the matching entry in the Google Cloud Console OAuth client) must name
this path, not `/oauth/callback` above. `/oauth/callback` stayed a plain
JSON API endpoint deliberately (its own ~30 integration tests keep testing
that exact contract); this is a thin wrapper calling it in-process, then
converting the result into a browser-usable response -- **not** JSON. A
browser landing here is redirected (`302`) to `{ECC_FRONTEND_URL}/` with a
query marker instead: `?gmail=connected` on success, `?gmail=error&code=
<the same error code /oauth/callback would have returned>` on failure (state
invalid, Google rejection, missing scope, not allowlisted, and the Spec A
refusal codes above; an unexpected exception reports `GMAIL_OAUTH_FAILED`). `GmailPanel.tsx`
reads and clears this marker on mount. Requires no new settings beyond
`ECC_FRONTEND_URL` (default `http://localhost:5173`, matching
`ECC_CORS_ORIGINS`'s own default -- a separate setting, not a reuse of that
comma-separated allow-list, since redirecting needs exactly one canonical
target; see `frontend_url`'s own field comment in `backend/ecc/config.py`.

Before this endpoint existed, Google's registered redirect target *was*
`/oauth/callback` itself -- a real, disclosed gap: the browser landed on raw
backend JSON at the API origin with no way back into the app, and a user had
to navigate back to the frontend manually.

## Current reused connector endpoints

| Endpoint | Gmail contract |
|---|---|
| `GET /api/v1/engineering/connectors` | Lists authorized visible accounts; never returns credentials. With `ECC_PERSONAL_DATA_ISOLATION` on, a `gmail` connector (and its sync runs and cursors) is written `private` to its owner, so other members, including workspace owners and admins, no longer see it here or in `GET /sync-runs` / effective permissions once the backfill has run |
| `POST /api/v1/engineering/connectors/{id}/sync` | Body `{"run_type":"backfill|incremental","resource_type":"message","since":null}`; requires `Idempotency-Key` and CSRF. `since` (Task 8, optional, defaults `null`) is `GmailAdapter.backfill`'s own "expand history" parameter (accepted since Task 1's own `connectors.py` Protocol widening -- a pure Python signature change, not a migration) finally reaching an HTTP caller -- only meaningful with `run_type: "backfill"`; `incremental_sync` has no `since` parameter at all (it resumes from `cursor` instead), so this field is silently ignored for `run_type: "incremental"` |
| `GET /api/v1/engineering/sync-runs` | Lists redacted run outcome and item count |
| `POST /api/v1/engineering/connectors/{id}/disable` | For every other provider: revokes the token best-effort and marks the account disconnected. For `gmail` specifically: if the account is not already `disconnected` **and** the owner has a `personal_domains` row for `email`, rejected with `409 GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT` and no mutation (Loop 2 round 1 review found this generic endpoint could otherwise disconnect a `gmail` account, and revoke its live Google grant, without running the consent revocation cascade below); an already-`disconnected` `gmail` account is unaffected by this guard and still returns the same idempotent `200` no-op as every other provider (Loop 2 round 2 review); an owner who completed the Gmail OAuth flow without ever calling `POST /domains`/`POST /consents` for `email` has no `personal_domains` row at all, so this guard falls through and the account is disconnected the same way any other provider's is -- there is nothing for the cascade to purge or revoke in that case, and without this carve-out such a connector had no HTTP-reachable way to disconnect it at all, since the domain-level endpoints 404 `DOMAIN_NOT_FOUND` for an owner with no domain row (Loop 2 round 25 review). Callers with an `email` domain must use the domain-level endpoints instead, which reach `gmail_revocation.cascade_email_revocation` |

With `ECC_PERSONAL_DATA_ISOLATION` on, `sync` and `disable` on another
member's `gmail` connector return `404 CONNECTOR_NOT_FOUND` (the same as a
nonexistent id) and count `ecc_connector_access_denied_total{provider,route}`.
This owner check runs before every other check, so a non-owner's `disable`
of a Gmail connector now gets `404` where it used to get `409
GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT`; the owner still gets the `409`.
With the flag off, the behaviour described in the table is unchanged.

Manual `webhook` sync is not accepted. A second running sync for the same
account returns `409 CONNECTOR_SYNC_IN_PROGRESS`. Provider errors are
sanitized before persistence or response.

Sync skips (Spec A S1.11 / FX5), each with nothing but the reserved run
written:

- `403 MEMBERSHIP_INACTIVE` -- the acting member, or the personal
  connector's owner, is no longer an active workspace member (checked in
  phase 1, and again before the outcome is recorded; a run already
  reserved is closed `failed`, "sync stopped: workspace membership is no
  longer active").
- `403 EMAIL_CONSENT_NOT_ACTIVE` -- the mailbox owner's `email` domain
  consent is not active, or the Gmail connector was disconnected, at the
  start of or at any point during the sync (including after the adapter
  returned). The run is closed `failed` with "sync stopped: email consent
  is no longer active"; the cursor is not advanced; no outcome,
  connector-status or sync audit is written and action detection does not
  run. (Previously a never-active consent produced a `201` with a `failed`
  run, a mid-sync revocation a `201` `partial` run, and a sync of a
  non-`message` resource type -- which the Gmail adapter treats as a no-op
  and never consent-checked -- a `201` `succeeded` run even with no email
  consent at all.)

## Current thread endpoints (Task 6, list added Task 8)

### `GET /api/v1/personal/gmail/threads`

Requires the same active `email` domain consent as the single-thread `GET`
below (`403 EMAIL_CONSENT_NOT_ACTIVE` otherwise) -- gated identically even
though this endpoint makes no live Gmail call itself, since it still
returns subject lines and sender addresses, the same sensitivity class as
thread content. Lists the caller's own threads, newest-`last_message_at`-
first, capped by an optional `limit` query parameter (default 50, max
200) -- no offset/cursor, matching this file's own "Current reused
connector endpoints" table (`list_sync_runs_endpoint`/`list_repositories_
endpoint`), neither of which paginate at this activation's expected data
volume. `last_sender`/`last_direction` come from the single most recent
message only; `message_count`/`body_cached` are computed over every
message in the thread (round 5 review: earlier wording claimed all four
fields were aggregated across the thread, which was true only for the
latter two).

```json
{
  "threads": [
    {
      "id": "uuid",
      "subject": "Signed contract needed by Friday",
      "last_message_at": "2026-08-06T00:00:00Z",
      "last_sender": "priya@partner-co.test",
      "last_direction": "inbound",
      "message_count": 3,
      "body_cached": true
    }
  ]
}
```

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
consents/{id}/revoke` additionally now rejects an `id` a *later* grant for
the same domain has superseded; such an `id` gets the same `404
CONSENT_NOT_FOUND` every other unresolved lookup in this contract
returns, rather than resolving to `domain_key` and re-running the cascade
(Loop 2 round 8 review, refined round 9 -- an `id` that is merely revoked,
with no later grant superseding it, still resolves normally, so a same-
`Idempotency-Key` retry of an already-succeeded revoke keeps working; see
`PRIVACY-CONSENT-CONTRACT.md`). `POST .../domains/email/disable` and
`POST .../domains/email/delete` (and, transitively, `POST .../consents/
{id}/revoke`) additionally now return `409 IDEMPOTENCY_CONFLICT` for an
`Idempotency-Key` reused after a genuine later state change -- the domain
re-enabled, or the owner's Gmail account reconnected through the separate
OAuth flow -- even though the request itself is byte-identical and would
normally hash-match the cached response (every other domain's own
disable/delete endpoints, and this framework generally, only 409 on a
request-hash *mismatch*; see `docs/domain/API-CONTRACTS.md`). This is
`email`-specific, matching this cascade's own consent/data-purge stakes
(Loop 2 rounds 21-22 review; see `PRIVACY-CONSENT-CONTRACT.md`'s own
"Reusing the same `Idempotency-Key`..." sections for why). See
`PRIVACY-CONSENT-CONTRACT.md`'s own "Consent revocation cascade (Task 7)"
section for the full cascade description and why "retry" needed no new
`deletion_jobs` schema. `POST /api/v1/engineering/connectors/{id}/disable`
(the generic, provider-agnostic endpoint documented above) has the
identical `409 IDEMPOTENCY_CONFLICT`-on-reconnect behavior for any
account whose `Idempotency-Key` is reused after it stops being
`disconnected` in the interim -- in practice reachable only via a `gmail`
account's OAuth reconnect, since no other provider has a way to leave
`disconnected` once entered (Loop 2 round 27 review).

## Security remediation (Spec A)

Connector ownership and personal-data isolation (security remediation Spec
A, plan tasks T01-T21 and fix wave FX1-FX6). Rollout and flags:
`docs/runbooks/SPEC-A-ROLLOUT.md`. "Flag" means `ECC_PERSONAL_DATA_ISOLATION`
unless stated; everything marked unflagged is live as soon as the code is
deployed.

| Endpoint | Change |
|---|---|
| Gmail `/oauth/callback`, `/oauth/complete` | `403 GMAIL_ACCOUNT_IDENTITY_MISMATCH` (`ECC_GMAIL_REQUIRE_IDENTITY_MATCH`), `409 CONNECTOR_OWNED_BY_ANOTHER_MEMBER` (unflagged), `403 MEMBERSHIP_INACTIVE`, `403 INSUFFICIENT_ROLE`, `500 CONNECTOR_ACCOUNT_PERSIST_FAILED` -- see the callback section above. With the flag on, a new connector is written `private` to the caller |
| `POST /api/v1/engineering/connectors` (reactivating a `disconnected` row) | Unflagged (S1.7): a caller who cannot read the existing row gets `404 CONNECTOR_NOT_FOUND`, one who can read but not write it `403 INSUFFICIENT_ROLE` (refusal audited as `connector_account.enrollment_refused`, reasons `not_found` / `access_denied`). An integrity error other than the duplicate-account unique violation is `500 CONNECTOR_ACCOUNT_PERSIST_FAILED` instead of a `409`. The pre-existing `409 CONNECTOR_ALREADY_CONNECTED` for an active row is unchanged |
| `GET /api/v1/engineering/connectors`, `GET /sync-runs`, effective permissions | Flag: other members' Gmail connectors, runs and cursors are hidden (private to the owner) |
| `POST .../connectors/{id}/sync`, `.../disable` | Flag: `404 CONNECTOR_NOT_FOUND` for a non-owner of a Gmail connector (see above). Sync, unflagged: `403 MEMBERSHIP_INACTIVE`, `403 EMAIL_CONSENT_NOT_ACTIVE` (see "Sync skips") |
| `POST /api/v1/sharing/grants`, grant preview, `POST /api/v1/ownership/transfers`, `POST /api/v1/delegations` | Flag: `400 RESOURCE_TYPE_NOT_GRANTABLE` for a personal row (a Gmail connector, its sync runs/cursors, an email attention item, an `email_action_detected` recommendation, an `email.*` AI run or step, Gmail evidence) or an email-derived row (a task/commitment/risk created by confirming an `email_action_detected` recommendation, feedback on an email recommendation or email attention item). Each refusal writes a `denied` `personal_data.share_refused` audit event and counts `ecc_personal_data_share_refused_total{resource_type,path}`. Delegation accept skips such items. The refusal applies to the row's own owner too |
| `DELETE /api/v1/identity/workspaces/{id}/members/{user_id}` | Flag: personal and email-derived rows no longer block removal (no `409 OWNED_RESOURCES_BLOCK_REMOVAL` for them). Removal disconnects the member's Gmail connectors (revoked at Google if safe), re-owns their Gmail-only person nodes and those nodes' Gmail-derived aliases to the earliest other active workspace owner, and keeps their other Gmail-derived rows, private to them (not purged) |
| `POST /api/v1/recommendations` | Unflagged: `422 VALIDATION_ERROR` for `recommendation_type="email_action_detected"` (reserved for the detection hook; `docs/phases/phase-001/API-SCHEMAS.md`) |
| `POST /api/v1/recommendations/{id}/confirm` (`email_action_detected`) | Unflagged: `403 EMAIL_CONSENT_NOT_ACTIVE`, `409 RECOMMENDATION_OWNER_CHANGED`. Flag: the created task/commitment/risk is owned by the recommendation's owner and `private` |
| Recommendations, AI runs, evidence (list, detail, resolve, delete) | Flag: email recommendations, `email.*` AI runs and their steps, and Gmail evidence are visible only to the mailbox owner (`404` to others) |
| Knowledge readers (entity tool, retrieval, meeting prep evidence) and evidence citations (claims, relationships, risk reviews, commitments) | Flag: other members' private evidence is excluded; citing evidence the caller cannot read is rejected |
| Global search | Unflagged: every result type is filtered by the caller's visibility |
| Meeting prep (`/api/v1/meetings/{id}/prep`) | Unflagged: the stored pack holds only workspace-visible sources, plus a per-caller live overlay (`docs/phases/phase-003/MEETING-PREP-CONTRACT.md`) |
| Member removal, role change | Unflagged: can return a retryable `409 MEMBERSHIP_CHANGE_BUSY` (ADR-0014) while a sync holds the membership lock during a token refresh |

Unchanged on purpose: person entities and their aliases stay workspace
knowledge (DS3), so `GET /entities` and `/entities/{id}/aliases` (which
returns `source_id`, not content) still list Gmail correspondents.

## Planned APIs

None. Task 8 -- the plan's final task -- is documented above; this section
is now closed out.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Documented current OAuth and generic sync surfaces | Lucky Jain |
| 1.1.0 | 2026-08-06 | Task 6: documented `GET`/`POST .../forget` thread endpoints, correcting the "no current endpoint returns thread content" claim | Lucky Jain |
| 1.2.0 | 2026-08-10 | Task 7: documented the consent revocation cascade's behavior change to three existing Phase 7 generic endpoints (no new endpoint added) | Lucky Jain |
| 1.2.1 | 2026-08-10 | Task 7 Loop 2 round 4 review: corrected the "Current reused connector endpoints" table's `disable` row, stale since round 1 -- it still described pre-fix behavior for `gmail`, contradicting this same file's own consent revocation cascade section below it | Lucky Jain |
| 1.2.2 | 2026-08-10 | Task 7 Loop 2 round 6 review: the `disable` row missed round 2's already-disconnected idempotent-no-op carve-out; the "Consent revocation cascade" section overclaimed "every email-derived record" against round 4's own ambiguous-id carve-out | Lucky Jain |
| 1.2.3 | 2026-08-10 | Task 7 Loop 2 round 8 review: documented `POST .../consents/{id}/revoke`'s new `revoked_at IS NULL` requirement, closing a stale-consent-id replay that could re-trigger the cascade | Lucky Jain |
| 1.2.4 | 2026-08-10 | Task 7 Loop 2 round 9 review: corrected the round-8 revoke row -- the plain `revoked_at IS NULL` check broke legitimate `Idempotency-Key` retries; replaced with a check for a later superseding grant | Lucky Jain |
| 1.2.5 | 2026-08-10 | Task 7 Loop 2 round 23 review: documented the new `409 IDEMPOTENCY_CONFLICT` behavior (rounds 21-22) for `disable`/`delete`/`revoke` on a same-hash `Idempotency-Key` reused after a genuine domain re-enable or Gmail OAuth reconnect -- this file had gone stale since round 9, never updated for either round | Lucky Jain |
| 1.2.6 | 2026-08-10 | Task 7 Loop 2 round 25 review (MEDIUM): documented that the generic `/disable` endpoint's `gmail`-provider rejection now also requires the owner to have a `personal_domains` row for `email`, falling through to the ordinary disconnect otherwise | Lucky Jain |
| 1.2.7 | 2026-08-10 | Task 7 Loop 2 round 27 review (MEDIUM-HIGH): documented that the generic `/disable` endpoint also now returns `409 IDEMPOTENCY_CONFLICT` for a reused `Idempotency-Key` if the account is no longer `disconnected` when replayed -- reachable only via a `gmail` OAuth reconnect, since no other provider has a way to leave `disconnected` | Lucky Jain |
| 1.3.0 | 2026-08-11 | Task 8: documented `GET /api/v1/personal/gmail/threads` (list) and the `since` field on `SyncRequest`, the plan's own two backend-shaped Task 8 gaps; closed out "Planned APIs" now that the plan's final task has shipped | Lucky Jain |
| 1.4.0 | 2026-08-11 | Task 8 Loop 2 round 5 review: corrected the "computed live from every message" overclaim -- `last_sender`/`last_direction` come from the single most recent message only, `message_count`/`body_cached` are the true aggregates | Lucky Jain |
| 1.4.1 | 2026-08-11 | Task 8 Loop 2 round 8 review: "Current (Task 8)" named only two of Task 8's three real backend additions, omitting the `recommendation_type` server-side filter round 5 added to `GET /api/v1/recommendations` -- round 5's own IMPLEMENTATION-STATUS.md evidence had also incorrectly claimed this file was updated for that parameter when it never was; added a cross-reference bullet pointing at `docs/phases/phase-001/API-SCHEMAS.md`, this parameter's own owning doc | Lucky Jain |
| 1.4.2 | 2026-08-11 | Task 8 Loop 2 round 10 review: the `sync` endpoint row credited `since`'s acceptance to "migration `0069`'s Task 1 Protocol widening" -- migration `0069` is a database schema migration; the `since` parameter is a pure Python `Protocol` signature change with no accompanying migration at all. Corrected to credit Task 1's `connectors.py` widening directly, matching how this same fact is stated correctly elsewhere in this PR | Lucky Jain |
| 1.5.0 | 2026-08-11 | Later addition: documented `GET /api/v1/personal/gmail/oauth/complete`, the real Google-facing redirect target that closes the "browser stranded on raw backend JSON" gap `/oauth/callback`'s own section already disclosed | Lucky Jain |
| 1.6.0 | 2026-09-28 | Security Remediation FX5: documented the sync endpoint's `403 MEMBERSHIP_INACTIVE` and `403 EMAIL_CONSENT_NOT_ACTIVE` skips (run closed `failed`, cursor not advanced, no outcome/audit writes), replacing the previous `201` failed/partial/succeeded outcomes | Lucky Jain |
| 1.7.0 | 2026-10-01 | Security Remediation Spec A (T19; review fix: only the four refusals write `enrollment_refused`, not `CONNECTOR_ACCOUNT_PERSIST_FAILED` / `GMAIL_ACCOUNT_ALREADY_CONNECTED`): documented the callback refusals (`GMAIL_ACCOUNT_IDENTITY_MISMATCH`, `CONNECTOR_OWNED_BY_ANOTHER_MEMBER`, `MEMBERSHIP_INACTIVE`, `INSUFFICIENT_ROLE`, `CONNECTOR_ACCOUNT_PERSIST_FAILED`), engineering reactivation `404`/`403`, the non-owner Gmail `sync`/`disable` `404` (was `409` for `disable`), and a Spec A summary table across sharing, removal, recommendations, readers, search and meeting prep | Lucky Jain |
