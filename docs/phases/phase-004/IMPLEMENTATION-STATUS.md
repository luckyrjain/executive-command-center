---
id: PHASE-004-IMPLEMENTATION-STATUS
title: Phase 4 Implementation Status
status: Implemented (Tasks 0-6 of the first activation slice)
version: 0.3.0
owner: Lucky Jain
updated: 2026-07-23
---

# Phase 4 Implementation Status

Phase 4's design work and its six-task implementation plan (`docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`) are complete: the model registry/router, prompt/tool versioning, budgets/circuit-breaker, bounded tool runtime, evaluation harness and the browser-facing product surface all landed on `feature/phase-4-ai-runtime`. This document is informational and does not override normative contracts. Phase 4's own exit criteria (`PHASE-004-ai-runtime.md`, e.g. the real `ollama-evaluation.yml` CI run against a live Ollama server) remain a separate, later milestone from this plan's code landing, matching how Phase 1's engineering delivery completed before its own validation gate did (see "What remains before Phase 4 itself can exit" below).

## Planning artifacts

`docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md` (design doc, resolving Ollama activation, first model choice, routing algorithm, prompt/tool versioning, structured-output validation, bounded tool runtime, budgets, evaluation harness and data-class policy) and `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md` (task-by-task implementation plan, six delivery tasks for this first activation slice). Neither document authorizes implementation by itself -- see Prerequisites below.

## Prerequisites

- Phase 3 exit gates complete, or an explicit repository-owner parallel-start authorization matching Phase 2's and Phase 3's own precedent -- **granted 2026-07-23**, same exception Phase 2 and Phase 3 received; see `docs/ROADMAP.md`'s Phase 4 status note and `PHASE-004-ai-runtime.md`'s "Dependency exit posture" section.
- Phase 4 contracts approved for implementation -- **done 2026-07-23.** The four approval gates named in `docs/phases/PHASE-REVIEW.md:135` (approved local/remote models and providers, data-class egress matrix, evaluation floors, trace retention) are resolved in `PHASE-004-ai-runtime.md`'s "Approved models, providers and evaluation floors" section and the six `phase-004/*.md` contracts, all moved to `Approved for Implementation` at version 0.2.0.
- Ollama activated as a technology -- **done 2026-07-23.** `docs/RFC-005.md` v1.3.0 and `docs/adr/ADR-0012-ollama-local-inference.md`, satisfying RFC-005's pre-registered "AI-runtime phase specification and ADR review" gate.
- Versioned evaluation dataset and promotion rubric established -- planned as `tests/fixtures/phase4_evaluation_attention_explain.py` (Task 5 of the implementation plan), not yet created.
- Ethics/safety review of the tool-allowlist and prompt-injection mitigations -- planned as Task 4's dedicated adversarial test plus Task 6's security-scanning pass, not yet created.

## Delivery tasks

| Task | Outcome | Status |
|---|---|---|
| 0 | Resolve open decisions and move contracts to Approved for Implementation | Done |
| 1 | Model/provider registry and deterministic router | Done -- `732b8de` |
| 2 | Immutable prompt/tool versioning and structured-output validation | Done -- `c052be9` |
| 3 | Budgets, timeouts, cancellation and circuit breaker | Done -- `db2afad` |
| 4 | Bounded tool runtime and `attention.explain_item` orchestration | Done -- `2531873` |
| 5 | Evaluation harness and first dataset | Done -- `0d08726` |
| 6 | Product surface, browser acceptance and security review | Done -- this commit |

### Task 1 evidence -- model/provider registry and router

Commit `732b8de` (`feat(ai-runtime): model registry and deterministic router`). `backend/ecc/domains/ai_runtime/registry.py`, `router.py`, `ollama_client.py`; migration `0028_phase4_model_registry.py` (seeds the sole `qwen2.5:1.5b-instruct-q4_K_M`/`ollama`/`local` row plus `routing_policies`). Tests: `tests/test_ai_runtime_routing_postgres.py` (33 tests -- the fixed seven-step eligibility pipeline and five-step preference/tie-break order, `MODEL-ROUTING-CONTRACT.md`). `GET /ai/models`/`GET /ai/policies` read endpoints.

### Task 2 evidence -- prompt/tool versioning and structured-output validation

Commit `c052be9` (`feat(ai-runtime): immutable prompt/tool versioning and structured output validation`). `backend/ecc/domains/ai_runtime/prompts.py`, `tools.py`, `validator.py`; migration `0029_phase4_prompt_tool_versions.py` (the `trg_prompt_versions_immutability`/`trg_tool_definitions_immutability` triggers, partial-unique-active-version indexes, seeded `attention.explain_item.v1` prompt and `attention.get_item`/`knowledge.get_entity` tool contracts). Tests: `tests/test_ai_runtime_versioning_postgres.py` (27 tests, including the DB-level immutability-trigger test bypassing the application layer) and `tests/test_ai_runtime_validation_postgres.py` (17 tests -- Pydantic `TypeAdapter` strict-mode accept/reject, the `attention.explain_item` grounding check, and the one-bounded-repair-retry rule). `POST /ai/policies/{id}/activate`.

### Task 3 evidence -- budgets, timeouts, cancellation, circuit breaker

Commit `db2afad` (`feat(ai-runtime): budgets, timeouts, cancellation and circuit breaker`). `backend/ecc/domains/ai_runtime/budgets.py` (`CircuitBreaker`, `RunBudget`, `RunGuard`, `CancellationToken`). Tests: `tests/test_ai_runtime_budgets_postgres.py` (34 tests -- closed/open/half-open state transitions, 60s total/20s per-model/5s per-tool budgets, input/output token caps, cancellation closing the stream).

### Task 4 evidence -- bounded tool runtime and orchestration loop

Commit `2531873` (`feat(ai-runtime): bounded tool runtime and attention.explain_item orchestration`). `backend/ecc/domains/ai_runtime/runtime.py` (`execute_run`, the `TASK_PORTS` allowlist, `_dispatch_tool`), `backend/ecc/domains/attention/tools.py` (`get_item_tool`), `backend/ecc/domains/knowledge/tools.py` (`get_entity_tool`); migration `0030_phase4_ai_runs.py` (`ai_runs`/`ai_run_steps`, no raw-prompt/output column by default). Tests: `tests/test_ai_runtime_tools_postgres.py` (7 tests -- workspace scoping/cross-workspace 404), `tests/test_ai_runtime_runtime_postgres.py` (17 tests, including `test_execute_run_prompt_injection_in_factor_label_cannot_dispatch_out_of_scope_tool` -- the plan's own "single most important safety gate" adversarial test, verified again independently in this task's Step 4 security pass below). `POST /ai/runs`, `GET /ai/runs/{id}`, `POST /ai/runs/{id}/cancel`.

### Task 5 evidence -- evaluation harness and first dataset

Commit `0d08726` (`feat(ai-runtime): evaluation harness and attention.explain_item dataset`). `backend/ecc/domains/ai_runtime/evaluation.py`; `tests/fixtures/phase4_evaluation_attention_explain.py` (20 hand-labelled examples, 3-4 per Phase 3 entity type); migration `0031_phase4_evaluation.py` (`evaluation_sets`/`evaluation_runs`/`generated_artifacts`). Tests: `tests/test_ai_runtime_evaluation_postgres.py` (26 tests -- schema-validity/grounding/prohibited-fact/latency scoring, `check_promotion_floors`). `GET /ai/evaluations`, `POST /ai/evaluations/runs`, `GET /ai/evaluations/runs/{id}`. `.github/workflows/ollama-evaluation.yml` is authored but **not executed** -- see "What remains before Phase 4 itself can exit" below; `tests/test_ai_runtime_evaluation_live_ollama.py` is the one test in this suite that requires a live Ollama server and is correspondingly skipped in this sandbox (confirmed in this task's own regression run, "Sandbox constraint" section below).

### Task 6 evidence -- product surface, browser acceptance and security review (this commit)

**Frontend component.** `frontend/src/features/attention/AttentionExplanation.tsx` (+ `AttentionExplanation.test.tsx`, 19 tests) -- the optional, discardable "Explain with AI" affordance wired to `POST /api/v1/ai/runs`/`GET /api/v1/ai/runs/{id}`/`POST /api/v1/ai/runs/{id}/cancel`, covering every `UX-STATES.md`-required state (AI disabled, local model unavailable/`circuit_open`, remote not permitted/`remote_not_configured`, budget exceeded, timed out, cancelled -- both client-side abort and server-reported, invalid output/`schema_invalid` never showing raw model output, degraded fallback, stale result). A real bounded progress indicator (`role="progressbar"`, `aria-valuemax=20`, tracking real elapsed time against Decision 5's 20s per-model-call budget, not an indefinite spinner). Wired into `frontend/src/features/attention/AttentionQueue.tsx` as a supplement to (never a replacement of) the existing deterministic factor list -- the queue does not mount the component at all when AI explanations are globally disabled (`window.__ECC_AI_EXPLANATIONS_ENABLED__` at runtime for e2e, `VITE_AI_EXPLANATIONS_ENABLED=0` at real deploy time), which is how the pixel/behavioral-parity requirement below is satisfied.

**Browser acceptance.** `frontend/e2e/scenarios/attention-explanation.mjs`, registered in `frontend/e2e/run.mjs`, run twice against the full 17-scenario suite (mocked backend fixtures added to `frontend/e2e/fixtures.mjs`'s `makeAiRuntimeApi`, no live model/backend):
- `AI_EXPLANATIONS_ENABLED` unset (on): all 17 scenarios pass, including `attention-explanation (AI runtime on)` -- explanation request, progress indicator, cited-factor cross-reference, model/prompt version display, discard action.
- `AI_EXPLANATIONS_ENABLED=0` (off): all 17 scenarios pass, including `attention-explanation (AI runtime off)`, which asserts the "Explain with AI" affordance never mounts, zero `/api/v1/ai/*` requests are ever made, and the pre-existing deterministic queue flow (score, evidence, dismiss/restore) is unaffected -- satisfying Task 6 Step 3's "existing Attention Queue is pixel-for-pixel/behaviorally unaffected in the disabled case" requirement.

Both runs include this scenario's own axe-core accessibility scan (zero serious/critical violations), matching every other scenario in this suite.

**Security review (Step 4).** A close adversarial read of `backend/ecc/domains/ai_runtime/` (`runtime.py`, `validator.py`, `prompts.py`, `tools.py`, `router.py`, `budgets.py`, `ollama_client.py`, `registry.py`, `evaluation.py`) and the two tool handlers (`backend/ecc/domains/attention/tools.py`, `backend/ecc/domains/knowledge/tools.py`) against the design doc's Threat model section:
- *Prompt injection / tool allowlist.* `_dispatch_tool`'s allowlist check runs before any `tool_definitions` row is read or handler resolved (`runtime.py`); re-traced the exact code path an injected `{"tool_call": ...}` response takes, including the case where the requested tool is the *in-scope* `attention.get_item` itself with attacker-chosen arguments -- confirmed this still cannot reach the caller, because any `tool_call`-shaped response makes the run fail immediately (`error_code` set, `output` never populated) rather than continuing to a response. `tests/test_ai_runtime_runtime_postgres.py::test_execute_run_prompt_injection_in_factor_label_cannot_dispatch_out_of_scope_tool` independently re-verified passing.
- *Tool-output trust boundary.* Confirmed a successful tool dispatch's actual output is never written into `ai_run_steps.trace` (only `{"tool_name": ...}` is) -- consistent with `DATA-MODEL.md`'s "raw sensitive content is not stored by default" and the design doc's redaction rule.
- *Handler-ref trust.* `_resolve_handler` does `import_module`/`getattr` on `tool_definitions.handler_ref` -- confirmed this column is only ever written by the migration seed (no `POST` endpoint accepts a caller-supplied `handler_ref`; `POST /ai/policies/{name}/activate` only ever flips `status`, never schema/handler columns, enforced additionally by the DB-level immutability trigger), so this is not a dynamic-code-execution injection vector.
- *SQL injection.* Every dynamic-looking `f"SELECT {_X_FIELDS} FROM ..."` string interpolates only a module-level constant column list, never request-derived data; all actual values are bound parameters (`:name` placeholders). No string-built `WHERE`/`VALUES` clause with request data anywhere in the package.
- *Cross-workspace isolation.* `attention/tools.py:get_item_tool`/`knowledge/tools.py:get_entity_tool` both scope by `workspace_id = :workspace_id AND id = :id`, collapsing "not found" and "wrong workspace" into one outcome, matching every Phase 1-3 read endpoint; `POST /ai/runs` 404s on a cross-workspace `attention_item_id` before a run is even created.
- *Data exfiltration.* Both registered tools remain read-only; no remote provider is registered (`model_definitions` has exactly the one local `ollama` row); the only network hop in the whole request path is `backend` to local Ollama.

**Finding: none.** No new Critical/High/Medium finding. This is a genuine negative result, not an unexamined one -- the reasoning trail above is the adversarial re-check the plan's Step 4 asks for (a real re-trace of `_dispatch_tool`/`_try_parse_tool_call_request` against a same-tool-different-arguments variant of the injection this task's own Step 5 already covers), not a re-confirmation that existing tests still pass.

**Full regression (this task).**
- Backend: `.venv/bin/python -m pytest tests/ -q` -- **650 passed, 1 failed, 5 skipped** in an isolated run (`0:02:55`, no concurrent load). The one failure, `tests/test_risks_attention_postgres.py::test_ranking_10000_eligible_entities_under_budget`, is a pre-existing Phase 1 performance gate (10,000-entity ranking p95 < 500ms locally) unrelated to this task: Task 6 makes zero functional backend changes (confirmed by diffing the working tree against `HEAD` after the Python-3.14-shim workaround was stripped -- the diff is empty), and the test's own p95 samples in this sandbox cluster tightly around 800-1150ms across two independent runs (not spiky/one-off), consistent with this shared sandbox's underlying CPU/Postgres throughput being slower than the ~350-400ms baseline the 500ms budget was calibrated against, not a code regression. Not fixed here (out of this task's scope -- a Phase 1 performance budget, not an `ai_runtime` file).
- `.venv/bin/python -m ruff check backend tests` -- **all checks passed**.
- `.venv/bin/python -m ruff format --check backend tests` -- **169 files already formatted**.
- `.venv/bin/python -m mypy backend` -- **17 pre-existing errors in 7 files**, all in Task 1-2 code (`ai_runtime/registry.py`, `router.py`, `prompts.py`, `tools.py`, `runtime.py`) and Phase 3's `attention/planning.py`, confirmed identical with and without this task's changes (bisected via `git stash`) -- pre-existing gaps from Tasks 1-5, not introduced or fixed by Task 6 per this task's explicit "do not modify Task 1-5 files" scope.
- `pnpm --filter @ecc/frontend typecheck` -- clean.
- `pnpm --filter @ecc/frontend test -- --run` -- **160/160 tests passed** (22 files, including the new 19-test `AttentionExplanation.test.tsx`).
- `pnpm --filter @ecc/frontend build` -- clean.
- `pnpm --filter @ecc/frontend test:e2e` (`node e2e/run.mjs`) -- **17/17 scenarios passed**, run twice (AI Runtime on/off, see above).
- `pip-audit` -- no known vulnerabilities found.
- `pnpm audit --audit-level=high` -- no known vulnerabilities found.

## Sandbox constraint (carried forward from the design pass)

This repository's development sandbox has no outbound network access to `ollama.com` and cannot run the Ollama server binary. The `ollama` Python client package is installable via PyPI and usable against a mocked HTTP client for every contract-level test in Tasks 1-4 and most of Task 5. Real generation, real evaluation-floor pass/fail, and real latency measurement require the dedicated `.github/workflows/ollama-evaluation.yml` CI job (Task 5, Step 6) -- authored but not executed anywhere in this sandbox at implementation time. This is recorded here so implementation does not silently treat a mocked evaluation result as equivalent to a real one. This sandbox also has no live Docker/Ollama service and no dedicated performance-calibrated hardware, per Task 6's regression evidence above.

## What remains before Phase 4 itself can exit

- `.github/workflows/ollama-evaluation.yml`'s first real run, against a real `ollama/ollama` container and `qwen2.5:1.5b-instruct-q4_K_M`, with `EVALUATION-CONTRACT.md`'s four floors actually passing against genuine model output -- an unverified floor is treated as unresolved, not passing by assumption (design doc's Test strategy section).
- The independent full-repo re-verification the repository owner intends to perform before this phase is considered shippable (this task's own instructions).
- No push, no PR: this commit lands on `feature/phase-4-ai-runtime` only; the repository owner handles final wrap-up.

## Exit evidence

Implementation commits are linked above per task. Real evaluation-CI results (`ollama-evaluation.yml`'s first live run) and any resulting promotion decision remain outstanding and will be linked here once produced, per the design doc's Test strategy section.
