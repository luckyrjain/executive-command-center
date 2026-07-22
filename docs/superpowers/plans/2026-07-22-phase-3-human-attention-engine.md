# Phase 3 Human Attention Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Do not start Task 0 as a code change.** This plan is queued, not authorized. Per `docs/ROADMAP.md`'s approval gates and `docs/superpowers/specs/2026-07-22-phase-3-human-attention-engine-design.md`, implementation may not begin until: Phase 3's dependency exit criteria are evidenced or the repository owner grants the same kind of parallel-start exception Phase 2 received, `docs/phases/phase-003/*.md` contracts move from Draft to Approved for Implementation, and the design doc's Open decision 1 (`attention_items` reconciliation) is resolved by the repository owner. Task 0 below is the mechanical step of applying that resolution to the contracts; everything after it assumes the design doc's recommended reconciled-schema answer (extend `attention_items` in place, no separate `attention_overrides` table) was accepted. If the owner instead directs a fork, Task 1's migration changes accordingly but nothing else in this plan's shape changes.

**Goal:** Implement all eight Phase 3 delivery slices from `docs/phases/phase-003/IMPLEMENTATION-STATUS.md` — versioned attention policy, waiting direction, risk review cadence, capacity/planning constraints, deterministic plan proposals, plan editing/acceptance/replan diff, evidence-backed meeting preparation, and the executive attention UX with two-week dogfood.

**Architecture:** Vertical, contract-preserving slices over the existing FastAPI/PostgreSQL backend and React/TanStack Query frontend, extending Phase 1's shipped `attention_items`/`attention.py` rather than forking it, and consuming Phase 2's knowledge entities/relationships/timeline for waiting counterparties and meeting participants. New backend package `backend/ecc/domains/attention/` (moved and renamed from `backend/ecc/domains/governance/attention.py`, plus `waiting.py`, `risk_reviews.py`, `capacity.py`, `planning_constraints.py`, `planning.py`, `meeting_prep.py`, `policy.py`). New frontend package `frontend/src/features/attention/` (the first dedicated attention feature folder — today attention is inline in `App.tsx`/`MorningBrief.tsx`, confirmed absent from `frontend/src/features/`).

**Tech Stack:** Same as Phase 1/2 — Python 3.14, FastAPI, SQLAlchemy Core (raw `text()` queries), PostgreSQL 18, Alembic, pytest, React 19, TypeScript 5.8, TanStack Query 5, Vitest, Playwright, pnpm. No new runtime dependency in any slice — planning/timezone arithmetic uses `zoneinfo` (already in use by `attention.py`), no scheduling/diff library.

## Global Constraints

- Preserve every frozen Phase 1/2 API and lifecycle contract; nothing in this plan modifies Phase 1/2 tables outside the additive column changes named in Task 1.
- Actor, workspace, and accountable owner remain session-derived and absent from browser payloads, matching every existing endpoint.
- Every mutation uses CSRF, a per-attempt idempotency key, and optimistic versioning (`If-Match` / `expected_version`), matching `phase-003/API-SCHEMAS.md`.
- Cross-workspace identifiers remain non-disclosing `404` responses.
- No protected characteristic, inferred emotion/personality, activity-volume, or response-speed signal is ever a scoring input — `ATTENTION-MODEL.md`'s excluded-inputs list is enforced by a static check (Task 8), not just documentation.
- Logs, metrics, test artifacts, and audit metadata never contain plan block content, meeting pack text, or waiting-link free text beyond IDs/enums — matching Phase 2's observability discipline.
- New behavior starts with a focused failing test and ends with focused plus regression proof, matching Phase 1/2's discipline.
- No new Python/JS runtime dependency in any slice.
- Every new table is workspace-scoped with composite foreign keys, matching every existing migration.
- Run security scanning for new first-party code when the configured tool is available; fix and rescan introduced findings, matching Phase 1's Task 11 discipline.

---

## Planned file structure

- `backend/migrations/versions/0016_phase3_attention_policy.py`: extend `attention_items` (`policy_version SMALLINT NOT NULL DEFAULT 1`, `override_reason TEXT NULL`) and create `attention_feedback`; widen the `EntityType` literal in application code (no DB `CHECK` to change — verified `entity_type` is an unconstrained `String(32)`).
- `backend/migrations/versions/0017_phase3_waiting.py`: `waiting_links`.
- `backend/migrations/versions/0018_phase3_risk_reviews.py`: `risk_reviews`.
- `backend/migrations/versions/0019_phase3_capacity_planning.py`: `capacity_profiles`, `planning_constraints`.
- `backend/migrations/versions/0020_phase3_plans.py`: `plans`, `plan_blocks`.
- `backend/migrations/versions/0021_phase3_meetings.py`: `meeting_participants` (design doc's Open decision 2 — new join table, `calendar_events` × Phase 2 `pkos_nodes`), `meeting_packs`.

Each migration is created and applied by exactly one task below, never reopened by a later one, matching Phase 2's established Alembic hygiene rule. Migration file numbers match actual implementation/chain order, not necessarily task numbers, if slice order shifts during execution — same allowance Phase 2's plan documented and used.

- `backend/ecc/domains/attention/__init__.py`, `attention.py` (moved from `backend/ecc/domains/governance/attention.py`), `policy.py` (Task 1).
- `backend/ecc/domains/attention/waiting.py` (Task 2).
- `backend/ecc/domains/attention/risk_reviews.py` (Task 3).
- `backend/ecc/domains/attention/capacity.py`, `planning_constraints.py` (Task 4).
- `backend/ecc/domains/attention/planning.py` (Tasks 5-6).
- `backend/ecc/domains/attention/meeting_prep.py` (Task 7).
- `scripts/rebuild_attention_projections.py`: thin CLI wrapper over `attention.py`'s existing `regenerate_attention` logic, extended to cover the new `entity_type`s.
- `tests/fixtures/phase3_attention_scenarios.py`, `tests/fixtures/phase3_planning_scenarios.py`, `tests/fixtures/phase3_meeting_scenarios.py`: versioned labelled datasets.
- `frontend/src/features/attention/AttentionQueue.tsx`, `WaitingView.tsx`, `RiskReviewQueue.tsx`, `Planner.tsx`, `MeetingPrep.tsx`.
- `frontend/e2e/scenarios/attention-queue.mjs`, `attention-planning.mjs`, `attention-meeting-prep.mjs`.

### Task 0: Resolve Open decisions and move contracts to Approved for Implementation

**Not a code task.** Owner-only.

- [ ] **Step 1:** Repository owner reviews `docs/superpowers/specs/2026-07-22-phase-3-human-attention-engine-design.md`'s Open decision 1 and either accepts the reconciled-`attention_items` recommendation or directs a fork.
- [ ] **Step 2:** Repository owner resolves the three named approval gates (`docs/phases/PHASE-REVIEW.md:128`): attention policy weights/caps, critical-item definition, dogfood success thresholds — accepting this document's proposed starting values or setting different ones.
- [ ] **Step 3:** `docs/phases/phase-003/DATA-MODEL.md` and `ATTENTION-MODEL.md` are edited to match the accepted answers (reconciled `attention_items` mapping table, named policy-v1 weights, critical-item definition) and their `status` moves to `Approved for Implementation`.
- [ ] **Step 4:** The remaining Phase 3 contracts (`API-SCHEMAS.md`, `PLANNING-CONTRACT.md`, `MEETING-PREP-CONTRACT.md`, `UX-STATES.md`, `TEST-PLAN.md`) are reviewed against the accepted decisions and also move to Approved for Implementation.
- [ ] **Step 5:** Confirm the dependency exit posture: either Phase 1/2 exit gates are closed, or the repository owner grants an explicit parallel-start exception matching Phase 2's precedent (`docs/ROADMAP.md`'s Phase 2 status note).
- [ ] **Step 6:** `docs/ROADMAP.md`'s Phase 3 status line is updated from Draft/Planned to Approved for Implementation.
- [ ] **Step 7:** `docs/domain/DOMAIN-MODEL.md`'s ownership map is updated to list the new `Attention` domain package (design doc's Architecture impact section) alongside the existing `governance/risks.py` → `risks` ownership.

### Task 1: Versioned attention policy over the extended `attention_items`

**Files:**
- Create: `backend/migrations/versions/0016_phase3_attention_policy.py`
- Move: `backend/ecc/domains/governance/attention.py` → `backend/ecc/domains/attention/attention.py`
- Create: `backend/ecc/domains/attention/policy.py`
- Create: `tests/fixtures/phase3_attention_scenarios.py`
- Modify: `tests/test_attention_postgres.py` (locate existing Phase 1 test file; extend, do not fork a parallel test file)
- Modify: `backend/ecc/main.py` (router import path)
- Modify: `docs/domain/EVENT-CATALOG.md` (no new events — `attention_item.created.v1`/`updated.v1` are reused as-is)

**Interfaces:**
- Produces: `policy.py:get_active_policy(version) -> AttentionPolicy` (typed weights/caps dataclass).
- Modifies: `attention.py`'s existing `_score_task`/`_score_commitment`/`_score_risk` to read weights from the active policy instead of inline literals.
- Produces: `GET /api/v1/attention/{id}` (new — does not exist in Phase 1, confirmed by reading `attention.py`'s router; only list/regenerate/dismiss/defer/restore exist today).
- Produces: `POST /api/v1/attention/{id}/feedback` (new — `attention_feedback` table, moved here from the ambiguous placement in this plan's first draft: it belongs with the core attention surface and has no dependency on Tasks 2-7).
- Modifies: `POST /api/v1/attention/{id}/dismiss|defer` (existing, gains optional `reason`, persisted to the new `override_reason` column).

- [ ] **Step 1: Write a failing regression-equivalence test** asserting policy-v1's `get_active_policy(1)` weights, applied through the refactored scorers, produce byte-identical scores/factors to the current pre-refactor `_score_task`/`_score_commitment`/`_score_risk` for `tests/fixtures/phase3_attention_scenarios.py`'s frozen representative dataset. This is the safety net for the whole refactor — it must exist and pass before any new factor is added.
- [ ] **Step 2: Run red:** confirm the fixture's expected scores were captured from the pre-refactor code (golden values), not invented.
- [ ] **Step 3: Write migration 0016** adding `policy_version SMALLINT NOT NULL DEFAULT 1` and `override_reason TEXT NULL` to `attention_items`, plus the `attention_feedback` table (`target_type/id`, `label`, `reason`, `actor_id`, `policy_version` per `DATA-MODEL.md`) in the same migration — both belong to the core attention surface this task owns, so one migration file covers them instead of the first draft's orphaned `0022`. Run `alembic upgrade head` and confirm `scripts/seed_phase1_acceptance.py`'s existing rows still round-trip.
- [ ] **Step 4: Implement `policy.py`** with policy-v1's weights matching Phase 1's exact current values (Task 0's accepted decision), then refactor `_score_task`/`_score_commitment`/`_score_risk` to consume it. Run Step 1's equivalence test green before proceeding.
- [ ] **Step 5: Write failing tests for the new factors** — dependency/blocked-by, meeting proximity, user-set importance, bounded recency, bounded deferral penalty — added additively to policy-v1 per Task 0's decision.
- [ ] **Step 6: Implement the new factors** in `policy.py` and wire into the scorers.
- [ ] **Step 7: Write failing tests for `override_reason`** on dismiss/defer (optional field, stored, returned in `AttentionItem`), `GET /{id}` (single-item fetch, workspace/cross-workspace 404), and `POST /{id}/feedback` (label + reason, writes `attention_feedback`, idempotent per `actor_id`+`target`+generation).
- [ ] **Step 8: Implement `GET /{id}` and `POST /{id}/feedback`.**
- [ ] **Step 9: Re-run and, if needed, extend the existing Phase 1 attention performance test** (`tests/test_risks_attention_postgres.py::test_ranking_10000_eligible_entities_under_budget`, currently asserting p95 <800ms for 10,000 task/commitment/risk rows) against the widened `EntityType` set from Step 10 below, confirming the policy refactor and new columns don't regress it. Called out explicitly because this exact test's CI behavior was measured as sensitive to unrelated changes to its runner/job (checkpoint I/O, dependency footprint) during the Task 7 PR (#33) — a scoring-pipeline change is a more direct risk to this budget than either of those were, so it needs its own explicit check here rather than an assumption it'll still pass.
- [ ] **Step 10: Move `attention.py`** to `backend/ecc/domains/attention/`, update `main.py`'s router import, widen the `EntityType` literal to include `waiting_link`, `risk_review`, `meeting` (used starting Task 2/3/7; harmless to widen now).
- [ ] **Step 11: Run full regression:** `uv run ruff check backend tests && uv run ruff format --check backend tests && uv run mypy backend && uv run alembic -c backend/alembic.ini upgrade head && uv run pytest`.
- [ ] **Step 12: Commit:** `git commit -m "feat(attention): versioned policy over the extended attention_items"`.

### Task 2: Waiting direction and dependency lifecycle

**Files:**
- Create: `backend/migrations/versions/0017_phase3_waiting.py`
- Create: `backend/ecc/domains/attention/waiting.py`
- Create: `tests/test_attention_waiting_postgres.py`
- Modify: `backend/ecc/main.py`
- Modify: `docs/domain/EVENT-CATALOG.md` (`waiting_link.opened.v1`, `waiting_link.fulfilled.v1`, `waiting_link.cancelled.v1`)

**Interfaces:**
- Produces: `GET|POST /api/v1/waiting` (signed-cursor paginated per `API-SCHEMAS.md:34`'s mandatory convention, matching `backend/ecc/search.py`'s/`tasks.py`'s per-module `_sign_cursor`/`_decode_cursor` pattern), `PATCH /api/v1/waiting/{id}`, `POST /api/v1/waiting/{id}/fulfil|cancel`.
- Waiting items surface in the reconciled `attention_items` as `entity_type='waiting_link'`, scored by Task 1's `dependency` factor.

- [ ] **Step 1: Write failing tests** for waiting-link creation (`subject_type/id`, `counterparty_entity_id` FK into Phase 2's `pkos_nodes`, `direction` enum), direction-change-creates-history (supersede, not overwrite — mirrors Phase 2's claim-supersede pattern), fulfil/cancel lifecycle, signed-cursor pagination and tamper rejection, and cross-workspace 404.
- [ ] **Step 2: Write a failing circular-dependency test**: a `blocked_by` chain that cycles back to its own subject (A blocked_by B, B blocked_by A) is rejected at creation with `invalid_waiting_direction` rather than accepted and left to loop forever in any downstream traversal — `TEST-PLAN.md`'s "Determinism and property tests" names circular dependencies explicitly as required coverage.
- [ ] **Step 3: Implement `waiting.py`**, including the cycle check (a bounded graph walk over existing `blocked_by` links for the workspace before insert — cheap at Phase 3's target scale, matching Phase 2's resolution-neighborhood query pattern rather than a new graph library).
- [ ] **Step 4: Wire waiting links into `attention.py`'s regenerate/list pipeline** as a fourth scored `entity_type`.
- [ ] **Step 5: Write failing waiting-ageing test** (item open beyond a threshold surfaces a staleness factor, matching `ATTENTION-MODEL.md`'s evaluation requirements).
- [ ] **Step 6: Run full regression and commit:** `git commit -m "feat(attention): waiting direction and dependency lifecycle"`.

### Task 3: Risk review queue and cadence

**Files:**
- Create: `backend/migrations/versions/0018_phase3_risk_reviews.py`
- Create: `backend/ecc/domains/attention/risk_reviews.py`
- Create: `tests/test_attention_risk_reviews_postgres.py`
- Modify: `backend/ecc/main.py`
- Modify: `docs/domain/EVENT-CATALOG.md` (`risk_review.recorded.v1`)

**Interfaces:**
- Produces: `GET /api/v1/risks/review-queue`, `POST /api/v1/risks/{id}/review`.
- Extends `governance/risks.py`'s existing `review_at` field (unchanged) with a `risk_reviews` history table; risks' existing `_score_risk` factor logic (`review_overdue`/`review_due_soon`, already live) is unmodified by this task.

- [ ] **Step 1: Write failing tests** for review recording (outcome, `next_review_at`, `evidence_refs`), review-queue listing ordered by cadence urgency, and that recording a review updates `risks.review_at` transactionally with the `risk_reviews` insert (single transaction, matching every existing dual-write pattern in this codebase).
- [ ] **Step 2: Implement `risk_reviews.py`.**
- [ ] **Step 3: Run full regression and commit:** `git commit -m "feat(attention): risk review queue and cadence history"`.

### Task 4: Capacity profiles and planning constraints

**Files:**
- Create: `backend/migrations/versions/0019_phase3_capacity_planning.py`
- Create: `backend/ecc/domains/attention/capacity.py`, `planning_constraints.py`
- Create: `tests/test_attention_capacity_postgres.py`
- Modify: `backend/ecc/main.py`

**Interfaces:**
- Produces: `GET|PUT /api/v1/planning/capacity` (per-weekday available/focus minutes, timezone, versioned).
- Produces: constraint CRUD consumed internally by Task 5's planner (no dedicated public endpoint beyond what planning surfaces need — confirm against `API-SCHEMAS.md` during implementation; the published surface only lists `/planning/capacity`, so `planning_constraints` may be plan-scoped input rather than independently CRUD-exposed).

- [ ] **Step 1: Write failing tests** for capacity profile validation (available_minutes ≥ focus_minutes, timezone valid, per-weekday), versioning on update, and planning-constraint kinds (fixed time, deadline, preference) with hardness/priority.
- [ ] **Step 2: Implement `capacity.py`/`planning_constraints.py`.**
- [ ] **Step 3: Run full regression and commit:** `git commit -m "feat(attention): capacity profiles and planning constraints"`.

### Task 5: Deterministic plan proposals and conflicts

**Files:**
- Create: `backend/migrations/versions/0020_phase3_plans.py`
- Create: `backend/ecc/domains/attention/planning.py`
- Create: `tests/test_attention_planning_postgres.py`
- Create: `tests/fixtures/phase3_planning_scenarios.py`
- Modify: `backend/ecc/main.py`
- Modify: `docs/domain/EVENT-CATALOG.md` (`plan.proposed.v1`)

**Interfaces:**
- Produces: `planning.py:propose_plan(...) -> PlanProposal` (pure function per the design doc's pure/impure split).
- Produces: `GET|POST /api/v1/plans` (signed-cursor paginated list, matching Task 2's convention), `GET /api/v1/plans/{id}`.

- [ ] **Step 1: Write failing scenario tests** for `PLANNING-CONTRACT.md`'s seven-step deterministic order: full calendars, no capacity, timezone/DST boundaries, overdue work, equal scores (stable tie-break), missing effort estimates (default bucket, lower confidence per the design doc's "no new effort field" decision), fixed meetings, stale sources.
- [ ] **Step 2: Implement `propose_plan`** as a pure function over capacity/constraints/calendar/attention-ranking inputs.
- [ ] **Step 3: Write failing conflict-transparency tests:** over-capacity (`capacity_exceeded`), missed-deadline, and hard-constraint (`constraint_conflict`) conflicts are always returned explicitly, never silently dropped, using the exact error codes `API-SCHEMAS.md`'s Errors section names.
- [ ] **Step 4: Implement the `POST /plans` mutation** wrapping `propose_plan`, persisting `plans`/`plan_blocks` in `draft`/`proposed` status.
- [ ] **Step 5: Write a failing performance test** asserting the <1 second p95 deterministic-daily-plan budget (`PHASE-003-human-attention-engine.md`'s Non-functional requirements) for a dense weekly plan against a representative dataset (`tests/fixtures/phase3_planning_scenarios.py`), following the same p95-measurement pattern as `tests/test_search_performance_postgres.py` — `TEST-PLAN.md`'s Performance section names this explicitly and the first draft of this plan omitted it.
- [ ] **Step 6: Run full regression and commit:** `git commit -m "feat(attention): deterministic plan proposals and conflict detection"`.

### Task 6: Plan editing, acceptance and replan diff

**Files:**
- Modify: `backend/ecc/domains/attention/planning.py` (accept/supersede/edit)
- Modify: `tests/test_attention_planning_postgres.py`
- Modify: `docs/domain/EVENT-CATALOG.md` (`plan.accepted.v1`, `plan.superseded.v1`)

**Interfaces:**
- Produces: `POST /api/v1/plans/{id}/propose|accept|supersede`, `POST /api/v1/plans/{id}/blocks/{block_id}/move|remove`.

- [ ] **Step 1: Write failing tests** for block move/remove (produces a new plan version, matching `PLANNING-CONTRACT.md`'s "moving or removing blocks produces a new version" rule), accept (idempotent, audited, durable human confirmation, does not write external calendars), and `stale_plan`/`version_conflict` error codes on concurrent edits.
- [ ] **Step 2: Implement accept/edit mutations.**
- [ ] **Step 3: Write failing replan-diff tests:** source change marks a proposal stale; replanning produces added/removed/moved/unchanged/newly-conflicted blocks against the prior accepted version; accepted plans are never silently rewritten.
- [ ] **Step 4: Implement replan and the diff computation** (plain per-block-id comparison, no new dependency).
- [ ] **Step 5: Run full regression and commit:** `git commit -m "feat(attention): plan acceptance and replan diff"`.

### Task 7: Evidence-backed meeting preparation

**Files:**
- Create: `backend/migrations/versions/0021_phase3_meetings.py` (`meeting_participants`, `meeting_packs` — design doc's Open decision 2)
- Create: `backend/ecc/domains/attention/meeting_prep.py`
- Create: `tests/test_attention_meeting_prep_postgres.py`
- Create: `tests/fixtures/phase3_meeting_scenarios.py`
- Modify: `backend/ecc/main.py`
- Modify: `docs/domain/EVENT-CATALOG.md` (`meeting_pack.generated.v1`, `meeting_pack.refreshed.v1`)

**Interfaces:**
- Produces: `GET|POST /api/v1/meetings/{id}/prep`, `POST /api/v1/meetings/{id}/prep/refresh`.
- Composes existing Phase 1/2 queries (timeline, commitments, relationships, risks, evidence) per the design doc's Meeting preparation approach — no new source-of-truth tables beyond `meeting_participants`/`meeting_packs`.

- [ ] **Step 1: Write failing tests for `meeting_participants`**: linking a `calendar_events` row to Phase 2 `pkos_nodes` person entities with a role, workspace-scoped, cross-workspace 404.
- [ ] **Step 2: Implement the participant linkage.**
- [ ] **Step 3: Write failing pack-composition tests:** every required deterministic section present (objective/timing, participants, timeline, open commitments by direction, prior decisions, unresolved questions, active risks, evidence gaps), citations traceable to source IDs, permission-denied/deleted evidence shown only as an availability state (`evidence_unavailable`), no uncited facts.
- [ ] **Step 4: Write a failing restricted-note-exclusion test:** a private/restricted note linked to a meeting's participants or timeline is excluded from the generated pack unless explicitly authorized for that surface — `TEST-PLAN.md`'s Security section names this explicitly and the first draft of this plan folded it into general evidence-permission handling without a dedicated test; this is a distinct, named requirement and gets a distinct test.
- [ ] **Step 5: Write a failing `feature_disabled` test:** requesting optional AI enrichment while the feature flag is off (the default, per the design doc's Meeting preparation approach — Phase 4 doesn't exist yet to serve it) returns the deterministic pack plus the documented `feature_disabled` error code for the enrichment section specifically, never a failed request for the deterministic sections.
- [ ] **Step 6: Implement `build_pack`** composing the existing domain queries and writing a `meeting_packs` snapshot with `source_versions`.
- [ ] **Step 7: Write failing staleness tests:** a material change to a cited source marks the pack stale; refresh creates a new snapshot; prior snapshots remain available.
- [ ] **Step 8: Implement staleness detection and refresh.**
- [ ] **Step 9: Write failing prompt-injection fixture tests** confirming source content is never treated as instruction (contract's "Safety" section) — reuse Phase 1/2's existing prompt-injection test pattern if one exists; if not, this is the first one and should be named for reuse by Phase 4.
- [ ] **Step 10: Write a failing performance test** asserting the <2 second p95 meeting-pack budget (excluding optional enrichment) against a large meeting history dataset (`tests/fixtures/phase3_meeting_scenarios.py`) — `TEST-PLAN.md`'s Performance section names this explicitly and the first draft of this plan omitted it.
- [ ] **Step 11: Run full regression and commit:** `git commit -m "feat(attention): evidence-backed meeting preparation"`.

### Task 8: Executive attention UX, browser acceptance, and two-week dogfood

**Files:**
- Create: `frontend/src/features/attention/AttentionQueue.tsx`, `WaitingView.tsx`, `RiskReviewQueue.tsx`, `Planner.tsx`, `MeetingPrep.tsx` (+ `.test.tsx` per component)
- Modify: `frontend/src/App.tsx`, navigation shell (add/replace the inline Today attention section with the dedicated surface)
- Create: `frontend/e2e/scenarios/attention-queue.mjs`, `attention-planning.mjs`, `attention-meeting-prep.mjs`
- Modify: `frontend/e2e/run.mjs`
- Create: `backend/scripts/check_phase3_prohibited_signals.py` (static check enforcing `ATTENTION-MODEL.md`'s excluded-inputs list — no protected characteristic, inferred emotion, activity-volume, or response-speed field ever reaches a scoring function; mirrors `scripts/check_phase1_acceptance.py`'s pattern of a scriptable, CI-runnable gate rather than a manual review step)
- Modify: `docs/phases/phase-003/IMPLEMENTATION-STATUS.md` (evidence links, per-slice status)
- Create: `docs/runbooks/PHASE-3-DOGFOOD.md` (two-week daily-use log template, mirroring `docs/runbooks/PHASE-1-DAILY-USE.md`'s structure)

**Interfaces:**
- Wires the Attention/Planner/Meeting-Prep surfaces into the existing navigation shell, replacing the inline `MorningBrief.tsx` attention section with links into the dedicated feature.

- [ ] **Step 1: Write failing navigation tests** for the new Attention surface entries, arrow-key focus, `<main>` targets — same pattern as Phase 1/2's navigation tasks.
- [ ] **Step 2: Implement all five components**, wired to Tasks 1-7's endpoints, following `UX-STATES.md`'s required states (loading/empty/no-capacity/over-capacity/conflict/stale/degraded/offline/permission-denied/error/version-conflict) and accessibility rules (no red/green-only, no urgency animation, no shame language, WCAG 2.2 AA).
- [ ] **Step 3: Implement `check_phase3_prohibited_signals.py`** and wire into CI, matching `PHASE-REVIEW.md`'s cross-phase "work not people" invariant as an automated, not just documented, gate.
- [ ] **Step 4: Run the full Playwright suite** including all three new scenarios, each run twice: once with Phase 3's AI-enrichment flag on and once off (`TEST-PLAN.md`'s Browser acceptance section requires core flows working "in AI-disabled mode" explicitly, not just the deterministic-pack unit tests from Task 7), plus a keyboard-only pass per `UX-STATES.md`.
- [ ] **Step 5: Run full backend + frontend regression**: ruff/format/mypy/alembic/pytest, typecheck/unit/build/e2e, `pnpm audit`, `pip-audit`.
- [ ] **Step 6: Begin the two-week dogfood** using `docs/runbooks/PHASE-3-DOGFOOD.md`, logging top-five usefulness, missed critical items, false urgency, plan acceptance/churn, and meeting-pack corrections daily.
- [ ] **Step 7: Update `docs/phases/phase-003/IMPLEMENTATION-STATUS.md`** with evidence links for each slice, matching Phase 1/2's citation format.
- [ ] **Step 8: Commit:** `git commit -m "feat(phase-3): wire attention engine into executive frontend"`.

---

## Completion checks

- All migrations apply cleanly from a fresh Phase 2 database and are reversible (`alembic downgrade` tested for each).
- Task 1's regression-equivalence test proves policy-v1 reproduces Phase 1's exact pre-refactor scores before any new factor is added, **and** Task 1 Step 9's re-run of the existing 10,000-item attention performance test confirms the widened `entity_type` set doesn't regress the p95<800ms budget — together the single most important correctness gate in this plan, since it's the one place an unnoticed behavior or performance change would silently alter what every existing user already sees ranked today.
- `scripts/rebuild_attention_projections.py` reproduces `attention_items` scores deterministically from authoritative sources on a representative dataset.
- Attention (existing, re-validated), planning (<1s p95, Task 5 Step 5), and meeting-prep (<2s p95, Task 7 Step 10) performance budgets all have a dedicated benchmark test with results attached to `phase-003/IMPLEMENTATION-STATUS.md` — not left as an unverified non-functional requirement the way this plan's first draft left planning and meeting-prep.
- Every new table's workspace isolation is covered by an adversarial cross-workspace test, matching Phase 1/2's isolation test convention.
- Every `API-SCHEMAS.md` endpoint (including `GET /attention/{id}` and `POST /attention/{id}/feedback`, missing from this plan's first draft) and every named error code (`version_conflict`, `constraint_conflict`, `capacity_exceeded`, `invalid_waiting_direction`, `stale_plan`, `stale_meeting_pack`, `evidence_unavailable`, `feature_disabled`, `cursor_invalid`) has at least one test exercising it.
- `check_phase3_prohibited_signals.py` passes in CI on every PR touching `backend/ecc/domains/attention/`.
- Zero Critical, High, or Medium findings, matching every prior phase's exit bar.
- Two-week dogfood (Task 8, Step 6) records against Task 0's accepted success thresholds before Phase 3 exit is claimed — this plan's completion is Task 8's code landing; Phase 3's *exit* is the dogfood result, and the two are not the same milestone (matching how Phase 1's engineering delivery completed before its own seven-day validation gate did).
