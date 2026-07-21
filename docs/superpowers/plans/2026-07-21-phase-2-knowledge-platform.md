# Phase 2 Knowledge Platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Do not start Task 0 as a code change.** This plan is queued, not authorized. Per `docs/ROADMAP.md`'s approval gates and `docs/superpowers/specs/2026-07-21-phase-2-knowledge-platform-design.md`, implementation may not begin until: Phase 1's daily-use validation and human change-review sign-off both close, `docs/phases/phase-002/*.md` contracts move from Draft to Approved for Implementation, and the design doc's Open decision 1 (PKOS reconciliation) is resolved by the repository owner. Task 0 below is the mechanical step of applying that resolution to the contracts; everything after it assumes the design doc's recommended reconciled-schema answer was accepted. If the owner instead chooses the fork option (retire PKOS, adopt `DATA-MODEL.md`'s independent table names as-is), Task 1's migration changes accordingly but nothing else in this plan's shape changes.

**Goal:** Implement all eight Phase 2 delivery slices from `docs/phases/phase-002/IMPLEMENTATION-STATUS.md` — knowledge entities/aliases/claims, typed relationships, timeline projection, entity resolution with human review, reversible merge/split, lexical retrieval, optional hybrid retrieval (separately gated, see design doc's Open decision 2), and the executive knowledge UX.

**Architecture:** Vertical, contract-preserving slices over the existing FastAPI/PostgreSQL backend and React/TanStack Query frontend, extending the Phase 0 PKOS foundation rather than forking it. New backend package `backend/ecc/domains/knowledge/` (entities, aliases, claims, relationships, timeline, resolution, entity_operations, retrieval) plus a new `backend/ecc/domains/identity/` package for Person/Organization per `docs/domain/DOMAIN-MODEL.md`'s ownership map. New frontend package `frontend/src/features/knowledge/`.

**Tech Stack:** Same as Phase 1 — Python 3.14, FastAPI, SQLAlchemy Core (raw `text()` queries, matching every existing domain module — no ORM layer introduced), PostgreSQL 18, Alembic, pytest, React 19, TypeScript 5.8, TanStack Query 5, Vitest, Playwright, pnpm. No new runtime dependency in any slice through Slice 6; Slice 7 requires an RFC-005 amendment before it can add `pgvector` (see design doc).

## Global Constraints

- Preserve every frozen Phase 1 API and lifecycle contract; nothing in this plan modifies Phase 1 tables outside the additive column changes named in Task 1.
- Actor, workspace, and accountable owner remain session-derived and absent from browser payloads, matching every Phase 1 endpoint.
- Every mutation uses CSRF, a per-attempt idempotency key, and optimistic versioning (`If-Match` / `expected_version`), matching `phase-002/API-SCHEMAS.md`.
- Cross-workspace identifiers remain non-disclosing `404` responses.
- Logs, metrics, test artifacts, and audit metadata never contain claim bodies, relationship attribute payloads, snippets, or embedding vectors — `phase-002/PHASE-002-knowledge-platform.md`'s Observability section is explicit about this.
- New behavior starts with a focused failing test and ends with focused plus regression proof, matching Phase 1's discipline.
- No graph database, vector database, or new Python/JS dependency is added in Slices 1-6. Slice 7 is blocked on a separate RFC-005 amendment (design doc, Open decision 2) and is not scheduled against any date here.
- Every new table is workspace-scoped with composite foreign keys, matching every existing Phase 0/1 migration.
- Run security scanning for new first-party code when the configured tool is available; fix and rescan introduced findings, matching Phase 1's Task 11 discipline.

---

## Planned file structure

- `backend/migrations/versions/0010_phase2_pkos_reconciliation.py`: add the Phase-1-deferred columns to `pkos_nodes`/`pkos_edges`/`pkos_evidence` (design doc's Open decision 1 recommendation).
- `backend/migrations/versions/0011_phase2_knowledge_entities.py`: `entity_aliases`, `knowledge_claims`.
- `backend/migrations/versions/0012_phase2_timeline.py`: `timeline_entries`.
- `backend/migrations/versions/0013_phase2_resolution.py`: `resolution_candidates`, `entity_operations`.
- `backend/migrations/versions/0014_phase2_retrieval.py`: `retrieval_documents`.
- `backend/migrations/versions/0015_phase2_embeddings.py`: `embedding_projections` — created only when Slice 7 actually starts (schema-only migration ships with that slice's PR, not earlier, so an unused table doesn't sit ahead of its RFC approval).

Each migration is created and applied by exactly one task below, never reopened by a later one — an already-applied Alembic migration is never edited after the fact (standard Alembic hygiene: amending a migration another task's regression pass already ran against a real database breaks reproducibility for anyone who ran it first). Task 1's `0010` therefore adds the reconciliation columns to all three PKOS tables (`pkos_nodes`, `pkos_edges`, `pkos_evidence`) in one pass up front, even though Task 2 is what actually starts reading/writing the edge columns — not a second, later edit to `0010`. Migration file numbers match actual implementation/chain order, not task numbers in this plan -- Task 3 (timeline) was implemented before Task 4 (resolution), so `timeline_entries` took the next open slot (`0012`) and `resolution_candidates`/`entity_operations` took `0013`, swapped from this document's original task-number-matched assignment.
- `backend/ecc/domains/identity/__init__.py`, `person_organizations.py`: Person/Organization CRUD, queries + mutations split.
- `backend/ecc/domains/knowledge/entities.py`, `entities_mutations.py`: `knowledge_entities` (extended `pkos_nodes`) queries/mutations, aliases.
- `backend/ecc/domains/knowledge/claims.py`: claim record/supersede.
- `backend/ecc/domains/knowledge/relationships.py`, `relationships_mutations.py`: `relationships` (extended `pkos_edges`) queries/mutations.
- `backend/ecc/domains/knowledge/timeline.py`: timeline projection query + rebuild.
- `backend/ecc/domains/knowledge/resolution.py`: candidate scoring (pure function), review queries, confirm/reject mutations.
- `backend/ecc/domains/knowledge/entity_operations.py`: merge, reverse.
- `backend/ecc/domains/knowledge/retrieval.py`: lexical retrieval query, `degraded` hybrid stub for Slice 7 to fill in.
- `scripts/rebuild_knowledge_projections.py`: deterministic rebuild of `timeline_entries`/`retrieval_documents` from authoritative tables.
- `tests/fixtures/phase2_resolution_dataset.py`, `tests/fixtures/phase2_retrieval_benchmark.py`: versioned labelled datasets.
- `frontend/src/features/knowledge/EntityExplorer.tsx`, `EntityDetail.tsx`, `ResolutionInbox.tsx`, `MergeReview.tsx`.
- `frontend/e2e/scenarios/knowledge-entities.mjs`, `knowledge-resolution.mjs`.

### Task 0: Resolve Open decision 1 and move contracts to Approved for Implementation

**Not a code task.** Owner-only.

- [ ] **Step 1:** Repository owner reviews `docs/superpowers/specs/2026-07-21-phase-2-knowledge-platform-design.md`'s Open decision 1 and either accepts the reconciled-PKOS recommendation or directs the fork alternative.
- [ ] **Step 2:** `docs/phases/phase-002/DATA-MODEL.md` is edited to match the accepted answer (reconciled mapping table from the design doc, or confirmation the independent table names stand) and its `status` moves to `Approved for Implementation`.
- [ ] **Step 3:** The remaining Phase 2 contracts (`API-SCHEMAS.md`, `ENTITY-RESOLUTION-CONTRACT.md`, `RETRIEVAL-CONTRACT.md`, `UX-STATES.md`, `TEST-PLAN.md`) are reviewed against the accepted data model and also move to Approved for Implementation.
- [ ] **Step 4:** Confirm Phase 1 exit gates (`docs/runbooks/PHASE-1-DAILY-USE.md` seven days, human change-review sign-off) are closed.
- [ ] **Step 5:** `docs/ROADMAP.md`'s Phase 2 status line is updated from Draft/Planned to Approved for Implementation.

### Task 1: PKOS reconciliation migration and extended entity/relationship model

**Files:**
- Create: `backend/migrations/versions/0010_phase2_pkos_reconciliation.py` (adds the deferred columns to `pkos_nodes` AND `pkos_edges` AND `pkos_evidence` together — Task 2 and later tasks read/write the edge and evidence columns this creates, but never re-edit this migration file)
- Create: `backend/ecc/domains/knowledge/entities.py`
- Create: `tests/test_knowledge_entities_postgres.py`
- Create: `backend/ecc/domains/knowledge/entities_mutations.py`
- Create: `backend/migrations/versions/0011_phase2_knowledge_entities.py` (`entity_aliases`, `knowledge_claims`)
- Create: `backend/ecc/domains/knowledge/claims.py`
- Create: `tests/test_knowledge_claims_postgres.py`
- Create: `backend/ecc/domains/identity/person_organizations.py`
- Create: `tests/test_identity_person_organizations_postgres.py`
- Modify: `backend/ecc/main.py` (register new routers)
- Modify: `docs/domain/EVENT-CATALOG.md` (add Phase 2 entity/claim events)

**Interfaces:**
- Produces: `POST /api/v1/knowledge/entities`, `GET /api/v1/knowledge/entities`, `GET|PATCH /api/v1/knowledge/entities/{id}`, `POST /api/v1/knowledge/entities/{id}/archive|restore`.
- Produces: `GET|POST /api/v1/knowledge/entities/{id}/aliases`, `GET|POST /api/v1/knowledge/entities/{id}/claims`.
- Produces: `POST /api/v1/identity/people`, `POST /api/v1/identity/organizations` (thin wrappers that create a `pkos_nodes` row with `node_type='person'|'organization'` — Person/Organization are Identity-owned per `DOMAIN-MODEL.md` but physically the same extended `pkos_nodes` table Knowledge Platform entities use, since PKOS is the shared canonical store).
- Emits: `knowledge_entity.created.v1`, `knowledge_entity.claim_recorded.v1`, `knowledge_entity.archived.v1`, `knowledge_entity.restored.v1`.

- [ ] **Step 1: Write failing migration-shape tests** in `tests/test_knowledge_entities_postgres.py` asserting `pkos_nodes` gains `entity_id`, `status`, `confidence`, `version` and `pkos_edges` gains `confidence`, `evidence_id`, `valid_from`, `valid_to`, `status` (Task 2's columns, added here so `0010` is never reopened later) with the constraints DATA-MODEL.md's invariants require (confidence in [0,1], status enum).
- [ ] **Step 2: Run red:** `uv run pytest tests/test_knowledge_entities_postgres.py`; expect column-not-found failures.
- [ ] **Step 3: Write migration 0010** adding the reconciliation columns to `pkos_nodes`, `pkos_edges`, and `pkos_evidence` together via `op.add_column`, with a server default backfilling existing seeded rows to `status='active'`, `version=1`, `confidence=1.0`.
- [ ] **Step 4: Run `alembic upgrade head`** against local PostgreSQL; confirm `scripts/seed_phase1_acceptance.py`'s existing `pkos_nodes`/`pkos_edges` seed rows still round-trip (backup/restore evidence must not regress).
- [ ] **Step 5: Write failing entity CRUD tests** covering create (kind + canonical_name required), list/filter by kind and status, patch (workspace/id/kind immutable, version-conflict on stale `If-Match`), archive/restore lifecycle, and cross-workspace 404.
- [ ] **Step 6: Implement `entities.py`/`entities_mutations.py`** following the exact query/mutation router split `backend/ecc/domains/governance/risks.py`/`risk_mutations.py` already establishes.
- [ ] **Step 7: Write migration 0011** (`entity_aliases`, `knowledge_claims`) and their failing tests (alias uniqueness per workspace+normalized_value+entity kind; claim requires ≥1 source reference per DATA-MODEL.md's invariant; valid_from/valid_to cannot invert).
- [ ] **Step 8: Implement `claims.py`** (record, supersede — never destructive overwrite, matching DATA-MODEL.md's lifecycle rule).
- [ ] **Step 9: Implement `identity/person_organizations.py`** as thin create wrappers over `entities.py`'s shared creation path, scoped to `kind IN ('person','organization')`.
- [ ] **Step 10: Add the four new event types to `EVENT-CATALOG.md`** in the same PR, matching how Phase 1 events were documented alongside the code that emits them.
- [ ] **Step 11: Run full regression:** `uv run ruff check backend tests && uv run ruff format --check backend tests && uv run mypy backend && uv run pytest`.
- [ ] **Step 12: Commit:** `git commit -m "feat(knowledge): extend PKOS with entity/alias/claim lifecycle"`.

### Task 2: Typed relationships and entity detail

**Files:**
- Create: `backend/ecc/domains/knowledge/relationships.py`, `relationships_mutations.py` (the `pkos_edges` columns this task reads/writes were already added by Task 1's `0010` migration — no migration file in this task)
- Create: `tests/test_knowledge_relationships_postgres.py`
- Modify: `backend/ecc/main.py`
- Modify: `docs/domain/EVENT-CATALOG.md`

**Interfaces:**
- Produces: `GET|POST /api/v1/knowledge/entities/{id}/relationships`.
- Emits: `relationship.created.v1`, `relationship.invalidated.v1`.

- [ ] **Step 1: Write failing tests** for relationship creation (typed vocabulary from `PKOS-SCHEMA.md`'s Phase 1 list, extendable — self-relationship rejected unless explicitly permitted per DATA-MODEL.md invariant), listing from either direction, invalidation (supersede, not delete), and confidence/evidence_id/validity-interval fields added by the Task 1 migration.
- [ ] **Step 2: Implement `relationships.py`/`relationships_mutations.py`.**
- [ ] **Step 3: Extend `EntityDetail` contract** (backend response shape) to include relationships grouped by direction, matching `UX-STATES.md`'s entity-detail requirements.
- [ ] **Step 4: Run regression and commit:** `git commit -m "feat(knowledge): typed relationships over extended pkos_edges"`.

### Task 3: Timeline projection and deterministic rebuild

**Files:**
- Create: `backend/migrations/versions/0012_phase2_timeline.py`
- Create: `backend/ecc/domains/knowledge/timeline.py`
- Create: `scripts/rebuild_knowledge_projections.py` (timeline half)
- Create: `tests/test_knowledge_timeline_postgres.py`
- Create: `tests/test_rebuild_knowledge_projections.py`

**Interfaces:**
- Produces: `GET /api/v1/knowledge/entities/{id}/timeline` (signed-cursor paginated).
- Produces: `rebuild_timeline(session, workspace_id) -> RebuildReport` (importable, used by both the CLI script and tests).

- [ ] **Step 1: Write failing ordering tests** proving deterministic order by `(effective_at, recorded_at, id)` per DATA-MODEL.md, including a 10,000-entry dataset performance test asserting the <500ms p95 non-functional requirement from `PHASE-002-knowledge-platform.md`.
- [ ] **Step 2: Write failing rebuild-determinism test:** delete and rebuild `timeline_entries` for a workspace, assert byte-identical projection to the original.
- [ ] **Step 3: Implement `timeline.py`** (query) and `rebuild_knowledge_projections.py`'s timeline half (derives entries from `knowledge_claims`/`relationships`/`entity_operations` history).
- [ ] **Step 4: Run regression and commit:** `git commit -m "feat(knowledge): timeline projection and deterministic rebuild"`.

### Task 4: Resolution candidates and human review

**Files:**
- Create: `backend/migrations/versions/0013_phase2_resolution.py` (both `resolution_candidates` and `entity_operations` — Task 5's merge/reverse work reads/writes `entity_operations` but needs no migration of its own, since this one already created it)
- Create: `backend/ecc/domains/knowledge/resolution.py`
- Create: `tests/test_knowledge_resolution_postgres.py`
- Create: `tests/fixtures/phase2_resolution_dataset.py`
- Create: `tests/test_resolution_scoring_dataset.py`
- Create: `frontend/src/features/knowledge/ResolutionInbox.tsx`, `ResolutionInbox.test.tsx`
- Modify: `docs/domain/EVENT-CATALOG.md`

**Interfaces:**
- Produces: `score_candidate(left, right, context) -> ScoreResult` (pure function, no I/O — unit-testable per the design doc's separation-of-concerns approach).
- Produces: `POST /api/v1/knowledge/resolution/candidates`, `GET /api/v1/knowledge/resolution/candidates`, `POST /api/v1/knowledge/resolution/candidates/{id}/confirm|reject`.
- Emits: `resolution_candidate.created.v1`, `resolution_candidate.confirmed.v1`, `resolution_candidate.rejected.v1`.

- [ ] **Step 1: Write failing unit tests for `score_candidate`** covering each factor (normalized-name trigram, alias overlap, shared-neighbor count, temporal compatibility) in isolation with hand-crafted inputs — no database needed for these.
- [ ] **Step 2: Implement `score_candidate`** as a pure function returning `factors_json`-shaped output plus a `resolver_version` constant.
- [ ] **Step 3: Write the versioned labelled dataset** `tests/fixtures/phase2_resolution_dataset.py` (representative person/organization pairs, some true matches, some deliberate near-miss non-matches) and a test asserting precision/recall/false-merge-rate thresholds from `ENTITY-RESOLUTION-CONTRACT.md`'s quality-metrics section.
- [ ] **Step 4: Write failing integration tests** for candidate creation, idempotent confirm/reject, rejection preventing the same unchanged pair from being re-proposed (contract requirement), and that Levels 1-4 never create a candidate row (deterministic attach only).
- [ ] **Step 5: Implement `resolution.py`'s mutation/query layer** using `score_candidate` and threshold config (typed dataclass, not inline literals).
- [ ] **Step 6: Write failing `ResolutionInbox.tsx` tests** for side-by-side comparison, factor display, confirm/reject/defer actions, and keyboard/screen-reader equivalence per `UX-STATES.md`.
- [ ] **Step 7: Implement `ResolutionInbox.tsx`.**
- [ ] **Step 8: Run full regression (backend + frontend) and commit:** `git commit -m "feat(knowledge): entity resolution scoring and human review"`.

### Task 5: Reversible merge/split lineage

**Files:**
- Create: `backend/ecc/domains/knowledge/entity_operations.py`
- Create: `tests/test_knowledge_entity_operations_postgres.py`
- Create: `frontend/src/features/knowledge/MergeReview.tsx`, `MergeReview.test.tsx`
- Create: `frontend/e2e/scenarios/knowledge-resolution.mjs`
- Modify: `docs/domain/EVENT-CATALOG.md`

**Interfaces:**
- Produces: `POST /api/v1/knowledge/entities/merge`, `POST /api/v1/knowledge/entity-operations/{id}/reverse`.
- Emits: `entity_operation.merged.v1`, `entity_operation.reversed.v1`.

- [ ] **Step 1: Write failing atomicity tests** for merge: target selection, source redirect to `status='redirected'`, alias/edge rehoming, duplicate-edge deduplication after rehome, single-transaction all-or-nothing behavior on injected failure.
- [ ] **Step 2: Write failing concurrent-merge test** (two merges racing on overlapping source entities) proving no double-redirect and a clean `version_conflict` on the loser, matching the optimistic-concurrency pattern `tests/test_task_postgres.py::test_concurrent_updates_with_same_expected_version_do_not_both_succeed` already established in Phase 1.
- [ ] **Step 3: Implement merge in `entity_operations.py`**, requiring the merge originate from a `confirmed` resolution candidate per `API-SCHEMAS.md`'s mutation rules.
- [ ] **Step 4: Write failing reversal tests**: safe reversal restores prior identities exactly; reversal is rejected with `unsafe_reversal` when a dependent operation (e.g. a new claim recorded post-merge with no clear source attribution) exists.
- [ ] **Step 5: Implement reversal.**
- [ ] **Step 6: Write failing `MergeReview.tsx` tests** and the `knowledge-resolution.mjs` Playwright scenario: review a candidate, merge, verify redirect, reverse safely, verify restoration, with an accessibility check per Phase 1's `assertNoSeriousAccessibilityViolations` pattern.
- [ ] **Step 7: Implement `MergeReview.tsx` and the e2e scenario.**
- [ ] **Step 8: Run full regression and commit:** `git commit -m "feat(knowledge): reversible entity merge and split lineage"`.

### Task 6: Lexical retrieval and explanations

**Files:**
- Create: `backend/migrations/versions/0014_phase2_retrieval.py`
- Create: `backend/ecc/domains/knowledge/retrieval.py`
- Modify: `scripts/rebuild_knowledge_projections.py` (add the retrieval half — Task 3 already created this file for the timeline half; unlike a migration, extending a plain script across tasks is normal)
- Create: `tests/test_knowledge_retrieval_postgres.py`
- Create: `tests/fixtures/phase2_retrieval_benchmark.py`
- Create: `frontend/src/features/knowledge/EntityExplorer.tsx`, `EntityExplorer.test.tsx`
- Create: `frontend/e2e/scenarios/knowledge-entities.mjs`

**Interfaces:**
- Produces: `GET /api/v1/knowledge/retrieve?q=&kind=&time_range=&source_type=&limit=&cursor=&mode=lexical`.
- Every result includes entity type/id, title, snippet, score, matching mode, factor summary, evidence state, source freshness per `RETRIEVAL-CONTRACT.md`.

- [ ] **Step 1: Write failing projection tests:** `retrieval_documents` populated via the deferred-until-commit pattern (`backend/ecc/observability.py`'s existing helpers) from entity/claim/relationship writes, never left stale after a committed mutation.
- [ ] **Step 2: Write the versioned benchmark** `tests/fixtures/phase2_retrieval_benchmark.py` (representative person/project/decision/topic queries with relevance judgements) and a test asserting precision@5/recall@10 thresholds from `TEST-PLAN.md`.
- [ ] **Step 3: Write failing pipeline tests:** exact identifier/alias match ranks above lexical relevance; workspace/permission/lifecycle/time filters; signed-cursor pagination and tamper rejection (Phase 1 has no shared cursor-signing module — every paginated domain, e.g. `backend/ecc/search.py`'s `_sign_cursor`/`_decode_cursor` and `backend/ecc/domains/planning/tasks.py`'s `_encode_cursor`/`_decode_cursor`, implements its own private pair; follow that established per-module convention rather than inventing a shared utility this plan would be the first to introduce); `degraded=false` when embeddings aren't requested (mode is always `lexical` until Slice 7 exists).
- [ ] **Step 4: Implement `retrieval.py`** and the rebuild script's retrieval half.
- [ ] **Step 5: Write failing 10,000-document performance test** asserting the <500ms lexical p95 non-functional requirement, following the same p95-measurement pattern as `tests/test_search_performance_postgres.py`.
- [ ] **Step 6: Write failing `EntityExplorer.tsx` tests** and the `knowledge-entities.mjs` Playwright scenario: create an entity, add evidence, retrieve it by lexical query, inspect match explanation, with an accessibility check.
- [ ] **Step 7: Implement `EntityExplorer.tsx` and the e2e scenario.**
- [ ] **Step 8: Run full regression and commit:** `git commit -m "feat(knowledge): lexical retrieval and match explanations"`.

### Task 7: Optional embeddings and hybrid fusion — separately gated, not scheduled here

**Not started by this plan.** Blocked on the design doc's Open decision 2: an RFC-005 amendment approving `pgvector` (or another local-first embedding store) with a benchmark run against Slices 1-6's real data and an ADR, per `RFC-005.md`'s explicit "Retrieval benchmark and ADR" activation requirement. When that approval lands, this task gets its own files (`backend/migrations/versions/0015_phase2_embeddings.py`, `backend/ecc/domains/knowledge/embeddings.py`) and TDD steps mirroring Task 6's shape, with the mandatory `degraded=true` fallback-to-lexical path tested first, before the happy path — per `RETRIEVAL-CONTRACT.md`'s degradation rule and `chapter-04-knowledge-platform.md`'s "if embeddings fail, graph traversal continues" principle.

### Task 8: Executive knowledge UX polish and full browser acceptance

**Files:**
- Modify: `frontend/src/App.tsx`, `frontend/src/navigation/WorkspaceNavigation.tsx` (add Knowledge surface)
- Modify: `frontend/e2e/run.mjs` (register the two new scenarios)
- Modify: `docs/phases/phase-002/IMPLEMENTATION-STATUS.md` (evidence links, per-slice status)
- Create: `docs/runbooks/PHASE-2-DEPLOYMENT.md` (only the delta from `PHASE-1-DEPLOYMENT.md` — new env vars if any, new migration steps)

**Interfaces:**
- Wires the Knowledge surface into the existing navigation shell without changing Phase 1 surfaces, matching how Phase 1's Task 1 wired navigation before any entity workflow existed.

- [ ] **Step 1: Write failing navigation tests** for the new Knowledge surface entry, arrow-key focus, and `<main>` target — same pattern as Phase 1's Task 1.
- [ ] **Step 2: Wire `EntityExplorer`/`EntityDetail`/`ResolutionInbox`/`MergeReview` into navigation.**
- [ ] **Step 3: Run the full Playwright suite** including both new scenarios: `pnpm --filter @ecc/frontend exec playwright install --with-deps chromium && pnpm --filter @ecc/frontend test:e2e`.
- [ ] **Step 4: Run full backend + frontend regression**, matching Phase 1's Task 12 proof discipline: ruff/format/mypy/alembic/pytest, typecheck/unit/build/e2e, `pnpm audit`, `pip-audit`.
- [ ] **Step 5: Update `docs/phases/phase-002/IMPLEMENTATION-STATUS.md`** with evidence links for each slice, matching `docs/phases/phase-001/IMPLEMENTATION-STATUS.md`'s citation format.
- [ ] **Step 6: Commit:** `git commit -m "feat(phase-2): wire knowledge platform into executive frontend"`.

---

## Completion checks

- All migrations apply cleanly from a fresh Phase 1 database and are reversible (`alembic downgrade` tested for each).
- `scripts/rebuild_knowledge_projections.py` reproduces `timeline_entries`/`retrieval_documents` byte-identically from authoritative tables on a representative dataset.
- Entity-resolution and retrieval benchmarks meet the thresholds in `ENTITY-RESOLUTION-CONTRACT.md`/`TEST-PLAN.md`, with results attached to `phase-002/IMPLEMENTATION-STATUS.md`.
- Every new table's workspace isolation is covered by an adversarial cross-workspace test, matching Phase 1's isolation test convention.
- Zero Critical, High, or Medium findings, matching every prior phase's exit bar.
- Task 7 (embeddings) remains explicitly "Not started, gated on RFC-005 amendment" in the status doc — its absence is not a defect in this plan's completion, per the design doc's Open decision 2.
