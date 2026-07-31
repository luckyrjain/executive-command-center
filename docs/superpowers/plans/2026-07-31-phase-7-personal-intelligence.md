# Phase 7 Personal Intelligence Implementation Plan

Companion to `docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md` (the decisions) -- this document is the task sequence and per-task scope, mirroring the shape of `docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`'s own task breakdown.

## Task 1 — Domain/consent/vault framework and the `habits` reference domain (this activation)

- Migration: `personal_domains`, `domain_consents`, `domain_records`, `domain_sources`, `goals`, `routines`, `check_ins`, `deletion_jobs` (`docs/phases/phase-007/DATA-MODEL.md`). `cross_domain_grants` and `personal_insights` beyond deterministic kinds are added by Task 5 below, the task that first needs them -- matching Phase 6 Task 1's identical precedent (`sync_runs`-style "not every table upfront").
- `ecc.domains.personal.crypto`: Fernet-based `encrypt_field`/`decrypt_field` over `ECC_PERSONAL_DATA_ENCRYPTION_KEY`, mirroring `ecc.domains.engineering.crypto`'s shape (dedicated key, decrypted value never logged/returned/audited) but field-level (`domain_records.payload`'s individual sensitive/high_stakes keys) rather than whole-column, since `standard`-classified fields on the same record must stay plaintext/queryable.
- `ecc.domains.personal.domains`: domain enable/disable (default `false` per domain per user), consent grant/revoke, generic `domain_records`/`domain_sources` CRUD enforcing per-field encryption by the owning domain's classification tier.
- `ecc.domains.personal.habits`: the one reference domain -- habit definitions, check-ins, deterministic streak/gap insights (no model call).
- `ecc.domains.personal.export_deletion`: export (human- and machine-readable) and deletion-job propagation, proving the mechanism every later domain reuses rather than reimplementing.
- `GET|POST /personal/domains`, `POST .../{id}/enable|disable|export|delete`, `GET|POST /personal/records`, `GET|PATCH /personal/records/{id}`, `GET|POST /personal/goals|routines|check-ins`, `GET|POST /personal/consents`, `POST /personal/consents/{id}/revoke`, `GET /personal/insights`, `POST /personal/insights/{id}/dismiss` (`.../feedback` deferred to Task 5).
- Tests: workspace/user isolation, disabled-domain-blocks-all-access, encrypted-field-never-returned-in-list-view, export completeness, deletion propagation, and the always-in-scope adversarial fixtures (no diagnostic/scoring language leaking even from a `standard` domain).

## Task 2 — `learning` domain (complete)

**Confirmed: no new mechanism needed, as this task's own plan anticipated.** `learning` is `standard`-classified, identical to `habits` -- Task 1's domain enablement and generic `domain_records` CRUD already accept any closed-enum `domain_key`. `tests/test_personal_learning_postgres.py` proves enable/list/disable, `course`/`resource` record CRUD (including a `course`'s `progress_pct` update), and whole-domain export/delete all work unmodified; `docs/phases/phase-007/DATA-MODEL.md`'s "Task 2 status" section documents the `course`/`resource` `record_type` payload conventions. No `goals`/`routines`/`check_ins` convention defined for `learning` -- course progress is better served by `domain_records.payload.progress_pct` than by forcing a recurring-habit shape onto content that isn't one; not attempted without a real usage need driving it.

## Task 3 — `travel` domain (complete)

**No new table or migration -- one small, generic API addition.** `travel` is `standard`-classified, identical to `habits`/`learning`. `GET /personal/records` gains two optional query parameters, `effective_from`/`effective_until` (inclusive on both bounds), filtering on the existing `domain_records.effective_at` column -- generic on the endpoint, not `travel`-specific. `travel`'s own `trip` `record_type` convention (`payload`: `destination`, `start_date`, `end_date` optional, `notes` optional; `effective_at` set to the trip's `start_date`) is the first domain to give this filter a real reason to exist -- structured, meaningfully time-ranged records. `tests/test_personal_travel_postgres.py` proves enable/independent-domain isolation, `trip` record CRUD, the date-range filter (range match, inclusive exact-instant boundary, from-only/until-only bounds, empty-result range), and whole-domain export/delete. `docs/phases/phase-007/DATA-MODEL.md`'s "Task 3 status" section documents the convention and clarifies retention itself is unchanged (`standard` stays retained indefinitely -- "retention-by-date-range" means the record's own date range is queryable, not that records expire on a timer). No `goals`/`routines`/`check_ins` convention defined for `travel`, same reasoning as `learning`'s own Task 2 finding.

## Task 4 — `relationships` domain

`sensitive` classification -- first domain the F-04 non-scoring invariant binds directly. No relationship-health score, no contact-frequency ranking, anywhere in this task's schema or UI.

## Task 5 — Cross-domain grants and the first AI-generated insights

`cross_domain_grants` (now that 4+ domains exist to grant between); `trend`/`correlation` insight kinds via a new Phase 4 evaluated task type, gated on Phase 4's own evaluation-floor discipline (a versioned dataset, the `INSIGHT-CONTRACT.md` safety rubric enforced as a 100%-floor adversarial fixture set) before any promotion decision, the same discipline `attention.explain_item`/`meeting.prep_summary` are already held to. `POST /personal/insights/{id}/feedback` ships here, once a `trend`/`correlation` insight exists for feedback to attach to.

## Task 6 — `health` domain

`high_stakes` classification -- the safety rubric from the design doc's Decision 4, already proven structurally on `habits`/`learning`/`travel`/`relationships`, now enforced against real diagnostic-language risk for the first time. Per-record `retention_acknowledged_at`. `professional_referral_note` required on every `health`-domain insight.

## Task 7 — `finance` domain

`high_stakes` classification, same rubric adapted for regulated-advice/guaranteed-return risk (`must_not_state` fixtures: guaranteed returns, credit/employment/insurance-decision language). No transaction execution or bank-account write access -- `PHASE-007-personal-intelligence.md`'s own out-of-scope line.

## Task 8 — Executive UX and browser acceptance

All six domains' frontend surfaces (`UX-STATES.md`'s required states: disabled, no data, import pending, incomplete data, consent expired/revoked, sensitive insight, export running, deletion pending/completed), consent dashboard, cross-domain grant management, and Playwright acceptance covering enable -> capture -> grant/revoke -> inspect/dismiss insight -> export -> delete end to end, with `@axe-core/playwright` accessibility checks, mirroring Phase 6 Task 8's own precedent.
