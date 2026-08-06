---
id: PHASE-010-UX-STATES
title: Phase 10 Gmail UX States
status: Approved for Implementation
version: 1.0.0
owner: Lucky Jain
depends_on:
  - PHASE-010
  - PHASE-010-API-SCHEMAS
---

# Phase 10 Gmail UX States

## Delivery boundary

No Gmail-specific frontend is shipped in Tasks 1-2. The current states are API
and connector-account states visible through generic engineering surfaces.
The planned `GmailPanel` must implement every state below before Task 8 can be
called complete.

| State | Current (Tasks 1-2) | Planned UX behavior (Tasks 3-8) |
|---|---|---|
| Disconnected | Connector absent or `disconnected` | Explain data retained/deleted state and offer reconnect only when consent permits |
| Consent missing/expired | Sync fails closed before fetch/write | Link to explicit email-domain consent action |
| OAuth pending | Authorization URL returned; state valid 10 minutes | Disable duplicate starts and show return-to-app guidance |
| OAuth error | Structured 403/422 error | Actionable retry without exposing provider detail/token |
| Syncing | Generic `sync_runs.status=running` | Show start time, type, and safe refresh; prevent duplicate run |
| Partial/rate-limited | `partial` plus redacted summary | Explain resumability and retry timing without claiming caught-up state |
| Permission lost | Connector can become `permission_lost` | Stop sync/body actions and require reauthorization |
| Empty | Successful zero-message window | Distinguish empty mailbox/window from failed sync |
| Stale | Cursor exists but no recent successful run | Show last success and manual retry |
| Body unavailable | Body is null in Tasks 1-2 | Explain not fetched, permission lost, deleted, or provider unavailable |
| Deletion pending | Not implemented | Block reads, show deletion-job progress and retry/escalation |
| Public rollout unsupported | Internal allowlist rejects account | State internal-only boundary; do not offer bypass instructions |

## Accessibility and safety

Every state needs a named heading/status region, keyboard-reachable action,
visible focus, non-color-only severity, and no email body in toast/log/error
copy. Loading and retry actions must not create duplicate syncs.

## Changelog

| Version | Date | Summary | Author |
|---|---|---|---|
| 1.0.0 | 2026-08-06 | Defined current API states and Task 8 Gmail panel requirements | Lucky Jain |
