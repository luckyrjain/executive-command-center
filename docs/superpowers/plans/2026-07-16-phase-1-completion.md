# Phase 1 Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete every remaining Phase 1 browser workflow, acceptance gate, production-hardening control, recovery proof, and evidence-driven status update.

**Architecture:** Deliver contract-preserving vertical slices over the existing FastAPI/PostgreSQL backend and React/TanStack Query frontend. Centralize only cross-cutting transport and production controls, keep entity forms feature-owned, and prove each behavior through red-green-refactor before broad regression checks.

**Tech Stack:** Python 3.14, FastAPI, SQLAlchemy, PostgreSQL 18, Alembic, pytest, React 19, TypeScript 5.8, TanStack Query 5, Vitest, Playwright, pnpm, Docker, GitHub Actions.

## Global Constraints

- Preserve the frozen Phase 1 API and lifecycle contracts.
- Actor, workspace, and accountable owner remain session-derived and absent from browser payloads.
- Every mutation uses CSRF, a per-attempt idempotency key, and optimistic versioning where contracted.
- Cross-workspace identifiers remain non-disclosing `404` responses.
- Logs, metrics, test artifacts, and audit metadata contain no note bodies, tokens, cookies, CSRF values, or evidence payloads.
- New behavior starts with a focused failing test and ends with focused plus regression proof.
- The seven-day daily-use record is prepared but cannot be marked complete before seven recorded days.
- Run Snyk Code scanning for new first-party supported code when the configured tool is available; fix and rescan introduced findings.

---

## Planned file structure

- `frontend/src/api/client.ts`: shared typed HTTP, CSRF, idempotency, offline, and conflict boundary.
- `frontend/src/api/types.ts`: shared API envelopes and entity types.
- `frontend/src/navigation/WorkspaceNavigation.tsx`: semantic Phase 1 surface navigation.
- `frontend/src/features/tasks/TaskWorkspace.tsx`: task list, form, lifecycle, and conflict UI.
- `frontend/src/features/commitments/CommitmentWorkspace.tsx`: commitment workflows.
- `frontend/src/features/notes/NoteWorkspace.tsx`: note editing, autosave, archive, restore, and search.
- `frontend/src/features/schedule/ScheduleWorkspace.tsx`: calendar-event and meeting workflows.
- `frontend/src/features/risks/RiskWorkspace.tsx`: risk workflows.
- `frontend/e2e/fixtures.mjs`: deterministic route fixtures and mutable fake API state.
- `frontend/e2e/scenarios/*.mjs`: one normative browser scenario per file.
- `backend/ecc/http_security.py`: security headers, request-size enforcement, and bounded rate limiting.
- `backend/ecc/observability.py`: structured request events and bounded in-process metrics exposition.
- `scripts/seed_phase1_acceptance.py`: deterministic populated recovery dataset.
- `scripts/phase1_evidence.py`: timestamped acceptance evidence report.
- `docs/runbooks/PHASE-1-DEPLOYMENT.md`: deployment, smoke, rollback, and migration limits.
- `docs/runbooks/PHASE-1-DAILY-USE.md`: seven-day human validation record.

### Task 1: Shared frontend transport and navigation

**Files:**
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/api/types.ts`
- Create: `frontend/src/api/client.test.ts`
- Create: `frontend/src/navigation/WorkspaceNavigation.tsx`
- Create: `frontend/src/navigation/WorkspaceNavigation.test.tsx`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Produces: `apiRequest<T>(path: string, options?: ApiRequestOptions): Promise<T>`.
- Produces: `ApiError` with `status`, `code`, `message`, and `current` fields.
- Produces: `WorkspaceView = 'today' | 'work' | 'notes' | 'schedule' | 'risks' | 'recommendations' | 'search-audit'`.

- [ ] **Step 1: Write failing client tests** proving GET credentials, mutation CSRF/idempotency headers, offline classification, and parsed `409 VERSION_CONFLICT` current state.

```ts
it('adds mutation protection headers', async () => {
  document.cookie = 'ecc_csrf=token-1'
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}', { status: 200 })))
  await apiRequest('/api/v1/tasks', { method: 'POST', body: { title: 'Plan' } })
  expect(fetch).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({
    credentials: 'include',
    headers: expect.objectContaining({ 'X-CSRF-Token': 'token-1' }),
  }))
})
```

- [ ] **Step 2: Run red tests:** `pnpm --filter @ecc/frontend test -- src/api/client.test.ts --run`; expect module-not-found failure.
- [ ] **Step 3: Implement `apiRequest`** with JSON encoding, `crypto.randomUUID()` idempotency, cookie decoding, normalized errors, and `navigator.onLine` network classification.
- [ ] **Step 4: Write and run navigation tests** asserting named navigation, one selected surface, arrow-key focus, and a `<main>` target; observe failure before wiring.
- [ ] **Step 5: Wire navigation into `App.tsx`** without changing existing Today behavior.
- [ ] **Step 6: Run green checks:** `pnpm --filter @ecc/frontend test -- src/api/client.test.ts src/navigation/WorkspaceNavigation.test.tsx --run && pnpm --filter @ecc/frontend typecheck`.
- [ ] **Step 7: Commit:** `git commit -am "feat(frontend): add shared Phase 1 application shell"` after explicitly adding new files.

### Task 2: Task and commitment browser workflows

**Files:**
- Create: `frontend/src/features/tasks/TaskWorkspace.tsx`
- Create: `frontend/src/features/tasks/TaskWorkspace.test.tsx`
- Create: `frontend/src/features/commitments/CommitmentWorkspace.tsx`
- Create: `frontend/src/features/commitments/CommitmentWorkspace.test.tsx`
- Modify: `frontend/src/App.tsx`
- Remove after migration: `frontend/src/WorkActionCenter.tsx`
- Remove after migration: `frontend/src/WorkActionCenter.test.ts`

**Interfaces:**
- Consumes: `apiRequest`, `ApiError`.
- Produces task create/edit/complete/cancel/archive/restore UI and commitment create/edit/confirm/fulfil/cancel/archive/restore UI.

- [ ] **Step 1: Write failing task component tests** for create payload exclusion of owner fields, due-date/due-time mutual exclusion, edit version use, valid lifecycle buttons, archive/restore, preserved form input after network failure, and conflict reload/retry.
- [ ] **Step 2: Verify red:** `pnpm --filter @ecc/frontend test -- src/features/tasks/TaskWorkspace.test.tsx --run`; expect missing component failure.
- [ ] **Step 3: Implement task workspace** using TanStack Query keys `['tasks', filters]` and mutation payloads matching `API-SCHEMAS.md`.
- [ ] **Step 4: Run task tests green**, then refactor repeated form status rendering only while green.
- [ ] **Step 5: Write failing commitment component tests** for direction, counterparty, lifecycle availability, version conflicts, and mutation headers supplied by `apiRequest`.
- [ ] **Step 6: Implement commitment workspace** and wire the Work navigation surface.
- [ ] **Step 7: Run regression:** `pnpm --filter @ecc/frontend test -- --run && pnpm --filter @ecc/frontend typecheck`.
- [ ] **Step 8: Commit:** `git commit -m "feat(frontend): complete task and commitment workflows"`.

### Task 3: Note workspace with resilient autosave

**Files:**
- Create: `frontend/src/features/notes/NoteWorkspace.tsx`
- Create: `frontend/src/features/notes/NoteWorkspace.test.tsx`
- Create: `frontend/src/features/notes/autosave.ts`
- Create: `frontend/src/features/notes/autosave.test.ts`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Produces: `createAutosaveController({ delayMs, save, onStateChange })` with `update`, `flush`, and `dispose`.
- Uses note query keys `['notes', filters]` and `['note', id]`.

- [ ] **Step 1: Write failing fake-timer tests** proving a 750 ms debounce, coalesced edits, flush-on-blur, latest-version updates, and retained text after rejection.
- [ ] **Step 2: Verify red:** `pnpm --filter @ecc/frontend test -- src/features/notes/autosave.test.ts --run`.
- [ ] **Step 3: Implement the minimal autosave controller** with one timer and serialized saves.
- [ ] **Step 4: Write failing component tests** for create, accessible saving/saved/error announcements, local search, archive, restore, and conflict preservation.
- [ ] **Step 5: Implement and wire `NoteWorkspace`**, rendering body as text only.
- [ ] **Step 6: Run focused and frontend regression tests, typecheck, and build.**
- [ ] **Step 7: Commit:** `git commit -m "feat(frontend): add resilient note workspace"`.

### Task 4: Calendar event and meeting workflows

**Files:**
- Create: `frontend/src/features/schedule/ScheduleWorkspace.tsx`
- Create: `frontend/src/features/schedule/ScheduleWorkspace.test.tsx`
- Create: `frontend/src/features/schedule/scheduleTypes.ts`
- Modify: `frontend/src/App.tsx`

**Interfaces:**
- Consumes `/api/v1/calendar/events` and `/api/v1/meetings` frozen schemas.
- Enforces linked-meeting timing as display-only; standalone meeting timing remains editable.

- [ ] **Step 1: Write failing tests** for event create/edit/archive/restore, IANA timezone submission, linked meeting timing lock, standalone meeting mapping, and rescheduling through the event PATCH.
- [ ] **Step 2: Verify red** with the focused Vitest command.
- [ ] **Step 3: Implement schedule types and workspace** with separate event and meeting forms and explicit authoritative-record copy.
- [ ] **Step 4: Run focused tests and verify all request bodies against examples in `docs/phases/phase-001/API-SCHEMAS.md`.**
- [ ] **Step 5: Run frontend regression, typecheck, and build.**
- [ ] **Step 6: Commit:** `git commit -m "feat(frontend): add calendar and meeting workflows"`.

### Task 5: Risk workflows and complete recommendation/brief states

**Files:**
- Create: `frontend/src/features/risks/RiskWorkspace.tsx`
- Create: `frontend/src/features/risks/RiskWorkspace.test.tsx`
- Modify: `frontend/src/RecommendationPanel.tsx`
- Modify: `frontend/src/RecommendationPanel.test.ts`
- Modify: `frontend/src/App.tsx`
- Create: `frontend/src/MorningBrief.tsx`
- Create: `frontend/src/MorningBrief.test.tsx`
- Create: `backend/ecc/domains/knowledge/evidence.py`
- Create: `tests/test_evidence_postgres.py`
- Modify: `backend/ecc/main.py`

**Interfaces:**
- Produces risk create/edit/archive/restore and contracted state transitions available from the API.
- Recommendation previews expose action, target version, confidence, factors (risk targets only — see note), and evidence state before confirmation.
- New `GET /api/v1/evidence?id=<uuid>&id=<uuid>...`: workspace-scoped, resolves each requested evidence id against `pkos_evidence` (joined to `pkos_nodes` for a label via `canonical_name`). Returns one item per requested id: `{id, status: "available"|"missing", source_type, label, captured_at}` (`source_type`/`label`/`captured_at` null when `status: "missing"`). No `permission_denied`/`deleted` status — not reachable from current schema (no soft-delete on `pkos_evidence`; a cross-workspace id is indistinguishable from a nonexistent one, matching the non-disclosure constraint, so both resolve to `"missing"`).

**2026-07-16 scope note (user-approved amendment to the original plan text):**
- "Factors" is not a field on `RecommendationResponse` (only `RiskResponse` and `/attention` items have it). Recommendation previews show target factors **only when `target_type == "risk"`**, fetched via the existing `GET /risks/{id}`. Task/commitment targets have no reliable single-entity factors source (only the list-based `/attention` endpoint, which may not contain the item) — show the target's core fields instead, no factors.
- "Required mitigation/trigger/review fields" is a **frontend form requirement only**. The backend `RiskCreate`/`RiskPatch` models leave `mitigation`/`trigger`/`review_at` nullable and must stay that way (frozen contract) — the risk workspace's own form validation enforces non-empty values before submit.
- The evidence-resolution endpoint above is new backend surface added specifically to back "evidence state" in the recommendation/confirmation preview, which had no prior data source.

- [ ] **Step 1: Write failing risk tests** for numeric bounds, frontend-required mitigation/trigger/review fields, optimistic editing, lifecycle actions, archive, and restore.
- [ ] **Step 2: Implement and wire the risk workspace; run focused green tests.**
- [ ] **Step 3: Write failing backend tests** for the evidence-resolution endpoint (available id, missing id, mixed batch, cross-workspace id resolves as `missing` not a distinct status, empty/absent query).
- [ ] **Step 4: Implement `GET /api/v1/evidence` and register its router; run focused green tests plus Ruff/mypy/Snyk.**
- [ ] **Step 5: Write failing recommendation tests** proving publication preview, confirmation preview, all reachable evidence states (`available`/`missing`), risk-target factors display, terminal-state action suppression, and conflict-safe refetch.
- [ ] **Step 6: Implement the missing recommendation presentation and state rules**, calling the new evidence endpoint and (for risk targets) `GET /risks/{id}` for factors.
- [ ] **Step 7: Write failing Morning Brief tests** for POST refresh, stale-to-fresh replacement, AI-disabled state, and recoverable refresh failure.
- [ ] **Step 8: Extract `MorningBrief` out of `App.tsx` into its own file for testability and implement the tested behavior.**
- [ ] **Step 9: Run full frontend tests, typecheck, and production build; run full backend tests, Ruff, mypy, and Snyk.**
- [ ] **Step 10: Commit:** `git commit -m "feat(frontend): complete risks recommendations and brief"`.

### Task 6: Normative Playwright scenarios and accessibility

**Files:**
- Create: `frontend/e2e/server.mjs`
- Create: `frontend/e2e/fixtures.mjs`
- Create: `frontend/e2e/accessibility.mjs`
- Create: `frontend/e2e/scenarios/tasks.mjs`
- Create: `frontend/e2e/scenarios/commitments.mjs`
- Create: `frontend/e2e/scenarios/notes.mjs`
- Create: `frontend/e2e/scenarios/schedule.mjs`
- Create: `frontend/e2e/scenarios/search-calendar.mjs`
- Create: `frontend/e2e/scenarios/dashboard-brief.mjs`
- Create: `frontend/e2e/scenarios/recommendation-execution.mjs`
- Create: `frontend/e2e/scenarios/recommendation-decisions.mjs`
- Create: `frontend/e2e/scenarios/recommendation-terminals.mjs`
- Create: `frontend/e2e/scenarios/conflict-audit-keyboard.mjs`
- Modify: `frontend/e2e/run.mjs`
- Modify: `frontend/package.json`
- Modify: `pnpm-lock.yaml`

**Interfaces:**
- `createFixtureApi(page)` returns mutable state plus captured requests.
- `assertNoSeriousAccessibilityViolations(page)` runs `@axe-core/playwright` and fails on serious/critical results.

- [ ] **Step 1: Add `@axe-core/playwright` and write a failing accessibility scenario** that detects an intentionally unlabeled fixture control; verify the assertion fails for the violation.
- [ ] **Step 2: Implement the accessibility helper** and remove the intentional violation only after the red signal is observed.
- [ ] **Step 3: Move existing smoke routes into `fixtures.mjs`** under characterization tests so Today/Search/Audit behavior stays green.
- [ ] **Step 4: Add each of the ten scenario modules one at a time**, first asserting a missing UI behavior, running it red, implementing/fixing the UI, and rerunning green.
- [ ] **Step 5: Add explicit visible-focus, landmarks, title, keyboard-only operation, offline mutation disablement, alert/status, and evidence-state assertions.**
- [ ] **Step 6: Run:** `pnpm --filter @ecc/frontend test:e2e`; expect all ten named scenarios and accessibility checks to pass.
- [ ] **Step 7: Commit:** `git commit -m "test(phase-1): cover normative browser acceptance"`.

### Task 7: Production configuration and HTTP protections

**Files:**
- Modify: `backend/ecc/config.py`
- Create: `backend/ecc/http_security.py`
- Create: `tests/test_production_security.py`
- Modify: `backend/ecc/main.py`
- Modify: `frontend/Dockerfile`
- Create: `frontend/nginx.conf`

**Interfaces:**
- Produces: `validate_production_settings(settings) -> None` raising a startup-safe configuration error.
- Produces ASGI middleware for security headers, maximum request body, and bounded per-session route-class rate limits.

- [ ] **Step 1: Write failing configuration tests** for placeholder/short secrets, permissive origins, insecure production cookies, development bootstrap, and missing environment classification.
- [ ] **Step 2: Verify red** with `pytest tests/test_production_security.py -q`.
- [ ] **Step 3: Implement production validation** while preserving explicit development defaults.
- [ ] **Step 4: Write failing HTTP tests** for required headers, oversized `413`, mutation `429`, retry header, and unaffected health checks.
- [ ] **Step 5: Implement minimal middleware** using monotonic time, bounded buckets, and route templates; do not store request bodies.
- [ ] **Step 6: Add nginx headers and a container-level assertion** that production HTML responses contain the matching policy.
- [ ] **Step 7: Run backend focused tests, Ruff, formatting, mypy, and container build.**
- [ ] **Step 8: Commit:** `git commit -m "feat(security): harden Phase 1 production HTTP"`.

### Task 8: Structured observability and Phase 1 metrics

**Files:**
- Create: `backend/ecc/observability.py`
- Create: `tests/test_observability.py`
- Modify: `backend/ecc/main.py`
- Modify domain mutation/query modules only at explicit metric emission points.

**Interfaces:**
- Produces `request_observability_middleware` and `/metrics` text exposition.
- Produces bounded counters/histograms for the events listed in the design; labels exclude entity IDs and content.

- [ ] **Step 1: Write failing log-capture tests** for request/correlation ID, route template, status, duration, authenticated workspace, and redaction.
- [ ] **Step 2: Implement structured JSON request logging and correlation response headers.**
- [ ] **Step 3: Write failing metric tests** for request/error, database failure, outbox backlog, search, ranking, brief, recommendation, idempotency, and audit/outbox failure signals.
- [ ] **Step 4: Implement bounded metric instruments and explicit domain emission calls.**
- [ ] **Step 5: Run focused tests plus full backend static checks and tests.**
- [ ] **Step 6: Commit:** `git commit -m "feat(observability): add Phase 1 operational signals"`.

### Task 9: Populated backup/restore and evidence report

**Files:**
- Create: `scripts/seed_phase1_acceptance.py`
- Create: `tests/test_seed_phase1_acceptance.py`
- Modify: `scripts/verify_restore.sh`
- Create: `scripts/phase1_evidence.py`
- Create: `tests/test_phase1_evidence.py`
- Modify: `.github/workflows/phase1-acceptance.yml`

**Interfaces:**
- Seed command inserts deterministic representative rows in every Phase 1 table under two workspaces.
- Evidence command writes JSON and Markdown containing timestamps, revisions, counts, checksums, invariants, and elapsed seconds.

- [ ] **Step 1: Write failing seed tests** asserting every Phase 1 table is non-empty, two-workspace isolation exists, and deterministic checksums repeat.
- [ ] **Step 2: Implement the seed script using backend models/contracts and idempotent fixture identifiers.**
- [ ] **Step 3: Write failing evidence tests** for required fields and a failed invariant producing non-zero exit.
- [ ] **Step 4: Implement evidence reporting.**
- [ ] **Step 5: Extend restore verification** to check representative checksums, workspace FKs, audit immutability, lifecycle fields, PKOS mappings, search query readiness, app readiness, and the 600-second RTO.
- [ ] **Step 6: Update CI to seed before backup and upload the evidence artifact.**
- [ ] **Step 7: Run the full source-to-clean-target PostgreSQL 18 drill locally or in CI and retain its output.**
- [ ] **Step 8: Commit:** `git commit -m "test(recovery): verify populated Phase 1 restore"`.

### Task 10: Representative performance gates

**Files:**
- Create: `tests/phase1_dataset.py`
- Create: `tests/test_dashboard_performance_postgres.py`
- Modify: `tests/test_search_performance_postgres.py`
- Modify: `tests/test_risks_attention_postgres.py`
- Create: `tests/test_mutation_brief_performance_postgres.py`
- Modify: `.github/workflows/phase1-acceptance.yml`

**Interfaces:**
- Deterministic fixture generates 10,000 tasks, commitments, risks, and events; 50,000 notes; and 100,000 audit rows.
- Tests emit measured p95 and fail at the exact documented budgets.

- [ ] **Step 1: Write dataset-shape tests and observe failure before generator implementation.**
- [ ] **Step 2: Implement batched deterministic PostgreSQL fixture generation.**
- [ ] **Step 3: Add dashboard, mutation, brief, and statement-timeout tests red against deliberately reduced local thresholds to prove measurement sensitivity, then restore normative thresholds.**
- [ ] **Step 4: Ensure existing search and ranking tests use the representative fixture rather than reduced samples.**
- [ ] **Step 5: Add a dedicated CI performance job and persist timing output.**
- [ ] **Step 6: Commit:** `git commit -m "test(performance): enforce Phase 1 acceptance budgets"`.

### Task 11: Dependency, filesystem, and container security gates

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/phase1-acceptance.yml`
- Modify: `config/phase1-acceptance.json`
- Modify: `scripts/check_phase1_acceptance.py`
- Modify: `tests/test_phase1_acceptance.py`

**Interfaces:**
- Acceptance checker validates recorded command results and artifact metadata, not only evidence-path existence.
- CI blocks unaccepted High/Critical Python, JavaScript, filesystem, and built-image findings.

- [ ] **Step 1: Write failing checker tests** for missing result status, stale head SHA, High vulnerability count, and absent image digest.
- [ ] **Step 2: Implement result-aware acceptance validation.**
- [ ] **Step 3: Add pnpm audit enforcement and Trivy High/Critical scans for filesystem plus both built images.**
- [ ] **Step 4: Run local acceptance checker tests and validate workflow syntax.**
- [ ] **Step 5: Run Snyk Code scan on modified first-party code if the configured tool is available; remediate and rescan introduced findings.**
- [ ] **Step 6: Commit:** `git commit -m "ci(security): enforce Phase 1 release thresholds"`.

### Task 12: Operations, status synchronization, and full proof

**Files:**
- Create: `docs/runbooks/PHASE-1-DEPLOYMENT.md`
- Create: `docs/runbooks/PHASE-1-DAILY-USE.md`
- Modify: `docs/runbooks/PHASE-1-RELEASE-GATE.md`
- Modify: `docs/phases/phase-001/IMPLEMENTATION-STATUS.md`
- Modify: `docs/phases/phase-001/FINAL-ACCEPTANCE.md`
- Modify: `docs/ROADMAP.md`
- Modify: `README.md`

**Interfaces:**
- Deployment runbook names environment variables, owner, deploy, migration, smoke, rollback, and restore commands.
- Daily-use record contains seven dated rows and remains incomplete until evidence exists.

- [ ] **Step 1: Write documentation assertions** in `tests/test_phase1_acceptance.py` that reject contradictory status values, unchecked automated gates with passing evidence, and a prematurely completed daily-use gate.
- [ ] **Step 2: Run the tests red against current contradictory documents.**
- [ ] **Step 3: Write deployment/rollback and daily-use runbooks with exact commands and safety guards.**
- [ ] **Step 4: Update status documents only to the level supported by fresh evidence; leave the seven-day gate open.**
- [ ] **Step 5: Run full proof:** backend Ruff/format/mypy/pytest/pip-audit; frontend tests/typecheck/build/Playwright; acceptance checker; PostgreSQL recovery; performance jobs; Docker builds and image scans.
- [ ] **Step 6: Review `git diff --check`, changed-file scope, generated artifacts, and release-gate evidence.**
- [ ] **Step 7: Commit:** `git commit -m "docs(phase-1): synchronize completion evidence"`.
- [ ] **Step 8: Hand off to change review** with the branch, exact commands/results, unresolved seven-day validation, and rollback guidance; do not mark Phase 1 complete until that human gate is satisfied.

## Completion checks

- Every design section maps to Tasks 1–12.
- Every behavior task includes an observed red test before production changes.
- Every task produces a reviewable commit and focused proof.
- Full acceptance is evidence-driven; documents cannot manufacture completion.
- The branch is ready for review only after automated gates pass; Phase 1 closure still requires seven recorded days and explicit approval.
