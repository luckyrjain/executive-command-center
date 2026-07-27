---
id: PHASE-006-UX-STATES
title: Phase 6 Engineering UX States
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
---

# Phase 6 UX States

**Task 1 status**: no frontend surface existed yet as of Task 1 -- this document's states were the approved target for Task 8. Task 1 delivered only the backend connector platform these surfaces consume.

Surfaces: engineering overview, delivery, reliability, repositories, incidents, decisions, connector health and source coverage. Required states include first sync, backfill, partial permissions, stale connector, rate limited, disconnected, provider unavailable and conflicting identities.

Charts always show definition, window, coverage and evidence drill-down. Never display person rankings or shame language. Accessible tables accompany visualizations; core workflows meet WCAG 2.2 AA.

**Task 8 status: complete.** `frontend/src/features/engineering/` implements all eight named surfaces as one tabbed `EngineeringWorkspace` (mirrors `AutomationWorkspace.tsx`'s own roving-tabindex shell): `EngineeringOverview`, `DeliveryPanel`, `ReliabilityPanel`, `RepositoriesPanel`, `IncidentsPanel`, `DecisionsPanel`, `ConnectorHealthPanel`, `CoveragePanel`. Every required state is covered:

- **First sync** / **backfill**: `ConnectorHealthPanel` renders `pending`-status connectors with zero sync-run history distinctly from a connector whose first backfill is already `running` (its own sync-run row shown as "in progress") -- these are two genuinely different states, not one collapsed into the other (a real bug the browser-acceptance run itself caught and closed: a `pending` connector with an already-running backfill must not also claim "no sync has ever run").
- **Partial permissions**: `permission_lost` at the connector-account level (`ConnectorHealthPanel`) and `permission_state` at the per-repository/per-work-item level (`RepositoriesPanel`) are both surfaced -- distinct scopes, since one connector account can have some repositories with lost permission and others still fully active.
- **Stale connector**: derived client-side from `last_synced_at`'s own age (`ConnectorHealthPanel`, a disclosed 24-hour heuristic since this activation has no periodic freshness monitor) and from the per-row `freshness_state` column (`RepositoriesPanel`).
- **Rate limited** / **disconnected** / **provider unavailable**: map directly onto `ConnectorAccountResponse.status`'s `rate_limited`/`disconnected`/`error` values (`ConnectorHealthPanel`), the last paired with the real `last_error` detail.
- **Conflicting identities**: `CoveragePanel` discloses unresolved `reporter_external_id`/`assignee_external_id` values as a sample list with an explicit note that no identity-resolution mechanism exists for engineering data in this activation (unlike Phase 2's `resolution_candidates`) -- a disclosure, not an interactive merge flow, since no backend resolution machinery exists to back one.

**Two disclosed additive query endpoints** (`GET /engineering/repositories`, `GET /engineering/work-items`) were added as part of this task -- see `API-SCHEMAS.md`'s own "Task 8 status" note. **Charts always show definition/window/coverage/evidence**: `MetricCard.tsx` (shared by Delivery and Reliability) renders all four together for every metric snapshot, including the `insufficient_coverage` case, which shows "not yet available" rather than a misleading number. **No person rankings**: verified structurally -- no metric card, panel, or endpoint in this feature carries a `person_id`/engineer-scoped field at all.

Verified via component tests (55 cases across the 8 panels/shell, covering every state above) and two Playwright browser-acceptance scenarios (`engineering-connector-states.mjs`, `engineering-lifecycle.mjs`) with `@axe-core/playwright` accessibility checks after every tab switch, asserting zero serious/critical WCAG violations.
