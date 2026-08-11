---
id: PHASE-010-UX-STATES
title: Phase 10 Gmail UX States
status: Approved for Implementation
version: 1.1.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-010-API-SCHEMAS
---

# Phase 10 Gmail UX States

## Delivery boundary

Task 8 shipped `GmailPanel` (inside the existing `PersonalWorkspace` shell)
implementing every state below. The "Shipped UX behavior" column describes
what `GmailPanel.tsx` actually renders, not a plan; see that file and
`GmailPanel.test.tsx`/`gmail-panel-states.mjs` (component and browser-
acceptance coverage) for the concrete implementation.

| State | Current (Tasks 1-2) | Shipped UX behavior (Task 8) |
|---|---|---|
| Disconnected | Connector absent or `disconnected` | "Connect Gmail" button (`POST oauth/start`, then a real top-level redirect to the returned `authorization_url`) shown whenever no non-`disconnected` connector exists |
| Consent missing/expired | Sync fails closed before fetch/write | A hint pointing to the Domains tab's own enable action shown whenever the `email` domain is not enabled -- the Connect action still works independently, since OAuth and domain consent are separate grants |
| OAuth pending | Authorization URL returned; state valid 10 minutes | The Connect button's own `isPending` state never resolves before the real navigation happens, which is what prevents a duplicate click -- there is no return-to-app guidance screen, since Google's own redirect target in this activation is a bare backend JSON endpoint, not a page this app renders (`API-SCHEMAS.md`) |
| OAuth error | Structured 403/422 error | `personalErrorMessage` renders `GMAIL_ACCOUNT_NOT_ALLOWLISTED`/`GMAIL_OAUTH_NOT_CONFIGURED`/`GMAIL_OAUTH_STATE_INVALID`/`GMAIL_OAUTH_FAILED` as an alert before any navigation happens, without echoing provider detail or a token |
| Syncing | Generic `sync_runs.status=running` | The latest sync run's own `status` renders inline ("Syncing since ..."); the sync button disables while `running` |
| Partial/rate-limited | `partial` plus redacted summary | A `partial` run renders its own `error_summary` in a degraded (non-error) panel; `rate_limited` connector status renders the shared `statusLabel` text |
| Permission lost | Connector can become `permission_lost` | Sync actions disable; an explicit "reconnect below" note points back at the same Connect action (OAuth reactivates a `permission_lost` account) |
| Empty | Successful zero-message window | "No messages in the synced window" once a sync has run and returned no threads -- worded distinctly from "No sync has run yet" (below) |
| Stale | Cursor exists but no recent successful run | The shared `STALE_AFTER_MS` heuristic (`ConnectorHealthPanel.tsx`'s own convention) renders a degraded panel citing the last-synced time |
| Body unavailable | Body is null in Tasks 1-2 | A thread's own detail view shows "Body not fetched -- not yet cached, permission lost, or deleted from the provider" per message with a null body; the thread list marks an unfetched thread "body not yet fetched" |
| Deletion pending | Not implemented | Not a real state in this activation -- the consent-revocation cascade (Task 7) completes synchronously within the disable request itself, so there is no async deletion job to poll; `ExportDeletePanel`'s own generic delete flow (now covering `email` too) already shows "Deletion pending..." for the request's own in-flight duration |
| Public rollout unsupported | Internal allowlist rejects account | `GMAIL_ACCOUNT_NOT_ALLOWLISTED` renders the same alert as any other OAuth error, stating only that the account is not allowlisted -- no bypass instructions |

## Accessibility and safety

Every state needs a named heading/status region, keyboard-reachable action,
visible focus, non-color-only severity, and no email body in toast/log/error
copy. Loading and retry actions must not create duplicate syncs.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Defined current API states and Task 8 Gmail panel requirements | Lucky Jain |
| 1.1.0 | 2026-08-11 | Task 8: recorded the shipped `GmailPanel` behavior for every state, replacing the "planned" column; "Deletion pending" documented as not a real state in this activation (the cascade completes synchronously) | Lucky Jain |
