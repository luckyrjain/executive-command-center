---
id: PHASE-001-UX-STATES
title: Phase 1 UX States
status: Approved
version: 1.0.2
owner: Lucky Jain
---

# UX States

## Global rules

All pages provide loading, success, empty, degraded, recoverable error, fatal error, and offline states. Keyboard navigation, visible focus, semantic landmarks, screen-reader labels, reduced-motion support, and WCAG 2.2 AA contrast are required.

The frontend never trusts browser-provided workspace identity. Authentication expiry redirects to sign-in while preserving unsaved local form state where safe.

## Dashboard

- Loading: skeletons matching final layout; no indefinite spinner.
- Empty: explain how to add a task, meeting, commitment, note, or risk.
- Degraded AI: deterministic dashboard remains complete; show a non-blocking “AI enrichment unavailable” status.
- Stale: show last-generated time and refresh action.
- Partial data: section-level error with retry; other sections remain usable.

## Forms and concurrency

Create forms validate inline and preserve entered data after recoverable errors. Edit screens show save state. On `409 VERSION_CONFLICT`, show the latest server version, the user's unsaved changes, and actions to reload or manually reapply; silent overwrite is prohibited.

Every successful lifecycle action returns the current entity representation and updates the UI from that response. Archive is reversible and requires confirmation only when the action removes the item from normal views.

## Recommendation publication and confirmation

A generated recommendation begins in `proposed`. The detail page offers an explicit Publish action that moves it to `pending_confirmation`. Only pending-confirmation recommendations expose Confirm. The page shows source `rule|ai`, rationale, confidence, evidence, proposed action, before/after preview, target version, expiry, and possible side effects.

State transitions are exactly:

```text
proposed -> pending_confirmation
pending_confirmation -> rejected | expired | superseded
pending_confirmation -> accepted
accepted -> executed | failed
```

Confirm is enabled only when all required evidence has access state `available`, the recommendation has not expired, and the target version still matches. Confirmation requires an explicit button; no default-focused destructive action. Failed execution remains visible with retry rules and audit link.

## Evidence

Evidence components show label, source type, captured time, excerpt when permitted, and status `available|missing|permission_denied|deleted`. Deleted evidence is shown as previously existing but no longer available. External links require an explicit user click and never auto-open.

## Notes

The editor autosaves after a debounce, displays last saved time and version, and provides a recoverable local draft when the network/database is temporarily unavailable. Archive and restore are supported; hard delete is not.

## Search

Search supports keyboard submission, filters including `calendar_event`, clear state, stable result focus, escaped highlights, loading more, empty results, malformed cursor recovery, and degraded prefix-only mode.

## Time handling

All user-facing daily boundaries use the workspace IANA timezone. All-day and cross-midnight events are labelled clearly. The current timezone is visible in settings and on the dashboard when different from the device timezone.

## Feature flags

`phase1.recommendations`, `phase1.ai_brief_enrichment`, and `phase1.search_trigram` default to false, false, and true respectively. Flags come from typed server configuration, require restart in Phase 1, are returned through a safe capabilities endpoint, and must not alter database migration requirements.

## Accessibility tests

Automated axe checks cover core pages; Playwright covers keyboard-only task creation, recommendation publication and confirmation, conflict resolution, note autosave recovery, search navigation, and focus restoration after dialogs.
