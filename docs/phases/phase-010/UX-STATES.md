---
id: PHASE-010-UX-STATES
title: Phase 10 Gmail UX States
status: Approved for Implementation
version: 1.3.0
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
| Disconnected | Connector absent or `disconnected` | A two-step wizard (later addition, real-user setup feedback: "should be more like a wizard, natural click, click" -- see the connector-setup-wizard design mockup this session also produced), shown whenever no non-`disconnected` connector exists: Step 1 states what Gmail access actually gives (metadata always, bodies only once the `email` domain is enabled, internal-allowlist-only) with a Continue action; Step 2 is the real "Connect Gmail" button (`POST oauth/start`, then a real top-level redirect to the returned `authorization_url`), with a Back action returning to Step 1. No third "connected" step is modeled -- the real post-connect experience is the existing connector-status panel below, reached via the real OAuth redirect-and-return, which already shows far more than a generic confirmation screen would |
| Consent missing/expired | Sync fails closed before fetch/write | A hint pointing to the Domains tab's own enable action shown whenever the `email` domain is not enabled -- the Connect action still works independently, since OAuth and domain consent are separate grants |
| OAuth pending | Authorization URL returned; state valid 10 minutes | The Connect button's own `isPending` state never resolves before the real navigation happens, which is what prevents a duplicate click |
| OAuth error | Structured 403/422 error | `personalErrorMessage` renders `GMAIL_ACCOUNT_NOT_ALLOWLISTED`/`GMAIL_OAUTH_NOT_CONFIGURED`/`GMAIL_OAUTH_STATE_INVALID`/`GMAIL_OAUTH_FAILED` as an alert before any navigation happens, without echoing provider detail or a token |
| OAuth return (later addition) | `GET /oauth/complete` redirects the browser back to `{ECC_FRONTEND_URL}/?gmail=connected\|error&code=...` (`API-SCHEMAS.md`) | `GmailPanel.tsx` reads the `gmail`/`code` query params on mount, shows a one-line "Google account connected." status or the same `personalErrorMessage` mapping as the "OAuth error" row above, then strips both params via `history.replaceState` so a page refresh doesn't keep re-showing a stale result. Before this, Google's own redirect target was a bare backend JSON endpoint, not a page this app renders -- a user had to navigate back to the app manually |
| Syncing | Generic `sync_runs.status=running` | The latest sync run's own `status` renders inline ("Syncing since ..."); the sync button disables while `running` |
| Partial/rate-limited | `partial` plus redacted summary | A `partial` run renders its own `error_summary` in a degraded (non-error) panel; `rate_limited` connector status renders the shared `statusLabel` text |
| Permission lost | Connector can become `permission_lost` | Sync actions disable; an explicit "reconnect below" note points back at the same Connect action (OAuth reactivates a `permission_lost` account) |
| Empty | Successful zero-message window | "No messages in the synced window" once a sync has run and returned no threads -- worded distinctly from "No sync has run yet" (below) |
| Stale | Cursor exists but no recent successful run | The shared `STALE_AFTER_MS` heuristic (`ConnectorHealthPanel.tsx`'s own convention) renders a degraded panel citing the last-synced time |
| Body unavailable | Body is null in Tasks 1-2 | The thread list marks a thread whose `body_cached` is false "body not yet fetched" -- the one genuine "not fetched" signal this activation surfaces. A thread's own detail view only ever returns messages `get_thread_content_tool` has already fetched (its own SQL filters `body IS NOT NULL`), so a message reaching that view is never "not yet fetched"; an HTML-only or otherwise unextractable message is stored and returned as a genuinely empty `body: ""` (`gmail_adapter.py`'s documented sentinel) and renders "(no text content in this message)" -- Loop 2 round 7 review found the prior "Body not fetched -- not yet cached, permission lost, or deleted from the provider" copy misrepresented this real, reachable case as a fetch failure |
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
| 1.1.1 | 2026-08-11 | Task 8 Loop 2 round 8 review: this file was never revisited after round 7 changed the "Body unavailable" copy and, per round 7's own finding, disclosed that a null-body detail-view message can never actually occur -- "Body unavailable" row corrected to describe the real `body_cached`/empty-string-sentinel behavior instead of the deleted UI copy | Lucky Jain |
| 1.2.0 | 2026-08-11 | Later addition: `GET /oauth/complete` now redirects the browser back to the frontend with a `gmail=connected\|error` marker instead of stranding it on raw backend JSON -- new "OAuth return" row; "OAuth pending" row's own stale "no return-to-app guidance screen" claim removed now that one exists | Lucky Jain |
| 1.3.0 | 2026-08-23 | Later addition, real-user setup feedback: the single "Connect Gmail" button in the "Disconnected" row is now a two-step wizard (what's shared, then sign in) instead of one bare button with no context -- "Disconnected" row rewritten; no backend change, `GmailPanel.test.tsx` grows from 18 to 19 cases (the split step-1/step-2 assertions plus a new Back-then-Continue case) | Lucky Jain |
