---
id: PHASE-007-UX-STATES
title: Phase 7 Personal Intelligence UX States
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
---

# Phase 7 UX States

Surfaces begin with domain enablement, data/consent explanation and retention choices. Always show active domains and cross-domain grants. Required states: disabled, no data, import pending, incomplete data, consent expired/revoked, sensitive insight, export running and deletion pending/completed.

Use calm, non-judgmental language; no shame, addiction loops or false urgency. Health and finance surfaces show boundaries near the insight. Accessibility meets WCAG 2.2 AA.

## Task 1 status

Ships the `habits` reference domain's own surface: domain enablement/consent/retention explanation, record capture, streak/gap deterministic insights, export and deletion. No cross-domain-grants UI yet (no second domain to grant into); no sensitive-insight boundary UI yet (`habits` is `standard` classification) -- both land with the tasks that introduce them (`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`, Decision 1).

## Task 8 status: complete

**No actual frontend code existed for any personal domain before this task** -- Tasks 1-7 were backend-only (`IMPLEMENTATION-STATUS.md`'s own summary table: "UX, safety review and dogfood | Not started -- Task 8"); the "Task 1 status" note above describes what the backend made possible, not anything a user could see. `frontend/src/features/personal/` implements one tabbed `PersonalWorkspace` (mirrors `EngineeringWorkspace.tsx`'s own roving-tabindex shell): `DomainsPanel`, `RecordsPanel`, `InsightsPanel`, `GrantsPanel`, `ExportDeletePanel`.

**Deliberately domain-generic, not one bespoke surface per domain.** Every backend endpoint here already takes a `domain_key` parameter rather than exposing a per-domain route (`API-SCHEMAS.md`'s own "a caller names a `domain_key`, never a domain-specific code path" convention) -- one domain-aware UI covers all six domains with zero per-domain duplication. `habits`' own `goals`/`routines`/`check_ins` extras are intentionally out of this task's scope, not named in the implementation plan's own Task 8 coverage list ("enable -> capture -> grant/revoke -> inspect/dismiss insight -> export -> delete").

Every required state is covered, or its absence is disclosed:

- **Disabled**: `DomainsPanel` renders all six domains unconditionally (a static, closed `DOMAIN_KEYS` list, since a domain with no `personal_domains` row at all -- never enabled -- has nothing for `GET /personal/domains` to return); a domain absent from the response, or present with `enabled: false`, renders identically: "Not enabled -- no data is captured for this domain yet."
- **No data**: `RecordsPanel`/`InsightsPanel`/`GrantsPanel` each show an explicit empty-state sentence (not a blank list) when their respective collection is empty for the selected domain/workspace.
- **Import pending**: **not implemented, deliberately.** This activation has no real import path at all (`IMPLEMENTATION-STATUS.md`: "`imported_file` source type has no real import path yet -- manual entry only"; `domain_sources.source_type` accepts the value but no endpoint ever creates one). Building UI for a state with no backend capability behind it would be misleading, not merely incomplete -- disclosed here rather than faked.
- **Incomplete data**: `InsightsPanel` renders a non-null `missing_data` field as its own "Incomplete data: ..." notice on the insight it belongs to (both deterministic and AI-generated insights carry this field).
- **Consent expired/revoked**: `GrantsPanel` computes an `active`/`expired`/`revoked` badge per cross-domain grant from `expires_at`/`revoked_at` -- `domain_consents` itself has no `expires_at` (only `revoked_at`, folded into `DomainsPanel`'s own enabled/disabled toggle, since this backend treats granting a domain's consent and enabling it as one transition -- `API-SCHEMAS.md`'s Task 5 part 1 section), so "expired" specifically is a grants-only state, matching the schema it is derived from.
- **Sensitive insight**: `InsightsPanel` renders a non-null `professional_referral_note` as a boundary callout directly under the insight it belongs to, styled distinctly (`inline-status degraded-panel`, this feature's cautionary, non-error visual language) -- non-empty specifically when a source is `high_stakes` (`INSIGHT-CONTRACT.md`'s safety rubric, proven generically against real `health`/`finance` data in Tasks 6/7).
- **Export running** / **deletion pending/completed**: `ExportDeletePanel`'s own mutation-pending state ("Export running…" / "Deletion pending…") while the request is in flight, since both backend endpoints respond synchronously with no separate job/polling model -- `DomainDeletionResponse.status` (`"completed"` in every real response this activation produces) is shown once the response returns. Disclosed as a client-side-only "in flight" signal, not a real backend job-status field, the same honesty this document's Phase 6 sibling applies to its own client-derived "stale connector" heuristic.

Calm, factual language throughout (`DomainsPanel`'s classification note names only what this activation actually enforces server-side -- encryption, per-record retention acknowledgement -- never a nudge/reminder mechanism with no backend counterpart); no ranking, streak-shaming, or urgency language anywhere in this feature.

**Tests.** 35 component-test cases across 5 files (`DomainsPanel.test.tsx`, `RecordsPanel.test.tsx`, `InsightsPanel.test.tsx`, `GrantsPanel.test.tsx`, `ExportDeletePanel.test.tsx`), covering every state above plus every mutation (enable/disable, record capture, grant create/revoke, insight dismiss/feedback/generate, export/delete) and its own failure path. One Playwright browser-acceptance scenario, `personal-domain-lifecycle.mjs` -- enable -> capture -> grant create/revoke -> inspect/dismiss insight (plus a pre-seeded `high_stakes` insight and a pre-seeded expired grant, the two states a live flow cannot itself produce without either real clock control or a real model call, mirroring `engineering-connector-states.mjs`'s own "distinct pre-seeded row" precedent) -> export -> delete, end to end -- with `@axe-core/playwright` accessibility checks after every tab switch, asserting zero serious/critical WCAG violations.
