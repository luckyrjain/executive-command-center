---
id: PHASE-004-MODEL-ROUTING
title: Model Routing Contract
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Model Routing Contract

Routing is deterministic for a task, policy version, availability snapshot and data class. Resolved for this first activation by `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md`'s Decision 2:

## Eligibility (hard filters, evaluated in this fixed order)

A candidate failing any step is excluded, not deprioritized:

1. **Data residency/privacy.** The run's declared data class must be in the candidate's `data_classes`. Evaluated first so no later, softer step can ever override it (`ADR-0007`: sensitive requests never silently fall back to cloud).
2. **Required capability.** The task's declared capability (from its typed port) must be in the candidate's `capabilities`.
3. **Structured-output support.** If the task requires schema-validated output, the candidate must have `structured_output_supported=true`.
4. **Context limit.** `estimated_prompt_tokens + declared_max_output_tokens` must fit within 90% of the candidate's `context_window_tokens` (a 10% margin absorbs tokenizer-estimate drift).
5. **Health.** The candidate's circuit-breaker state must not be `open`.
6. **Latency.** The candidate's rolling observed p95 latency for this task type must fit within the task's declared timeout minus a fixed 500ms overhead reserve.
7. **Budget.** Remaining run/session token and time budget must be non-zero.

## Preference (only reached with more than one eligible candidate)

1. Local before remote.
2. Must meet the task's configured evaluation quality floor (`EVALUATION-CONTRACT.md`) -- a candidate that has not passed evaluation for this task type is never preferred over one that has.
3. Lower expected cost.
4. Lower observed p95 latency.
5. Deterministic final tie-break: ascending `model_id` string comparison.

The first activation registered exactly one model (`ollama` / `qwen2.5:1.5b-instruct-q4_K_M`, local, all four data classes eligible), so the preference stage was reachable only in the trivial single-candidate case -- the ordering above was specified then so a second model would not require re-deriving it. Migration `0032_phase4_second_model.py` has since registered a second candidate (`ollama` / `qwen2.5:3b-instruct-q4_K_M`, same deployment, same data classes and capabilities as the first, so eligibility filtering alone cannot decide `attention.explain_item` routing before this preference stage is reached with two live candidates -- exactly the case this ordering was written to cover). Both candidates start with no observed cost/latency history, so steps 3-4 above are ties until real usage data accumulates; step 5's ascending `model_id` comparison currently selects `qwen2.5:1.5b-instruct-q4_K_M` (`"1"` < `"3"`).

`routing_policies.candidates` documents which models are intended for a task type but is not an enforced input to `route()` in this activation -- the eligibility/preference pipeline draws its candidate pool from every `active` `model_definitions` row, not filtered by this column. Both registered models are therefore real, live candidates for `attention.explain_item` regardless of whether this column lists them (it currently lists both, kept in sync for documentation/audit accuracy).

## Performance

The eligibility/preference pipeline is pure in-memory comparison against a cached registry/circuit-state snapshot (refreshed on a short interval, never queried synchronously per request), keeping routing overhead within `PHASE-004-ai-runtime.md`'s p95 <50ms non-functional requirement.

## Retries, fallback and promotion

Retries are bounded (at most one, and only on `schema_invalid` or a transient provider error) and never repeat a non-idempotent tool -- moot in this activation since both registered tools (`attention.get_item`, `knowledge.get_entity`) are read-only, but stated now so it does not need re-deriving once a mutating tool exists. Fallback changes are recorded on the `ai_runs` row. Circuit breakers exclude unhealthy deployments: open after 3 consecutive failures within a rolling 60s window, half-open probe after 30s, one probe success closes it. A caller cannot override policy or enable an unapproved provider -- no endpoint in `API-SCHEMAS.md` accepts a caller-supplied `model_id` or `provider`. Promotion of a model/policy requires evaluation evidence (`EVALUATION-CONTRACT.md`'s floors) and a rollback pointer to the prior `active` prompt/tool version.

## Reflection Engine (first slice) -- no new routing behavior

The Reflection Engine's optional critique/revise call (`runtime.py:_reflect_on_answer`, gated by `routing_policies.constraints.reflection_enabled`) reuses the exact `decision.model_id`/`RunBudget` the primary call already routed to and was budgeted with -- it is not a second routing decision, does not consult eligibility/preference again, and does not consume a second circuit-breaker credit against that model (a reflection-layer failure never feeds `_breaker_for(...)`'s success/failure counters, so a model that is reliable at explaining but noisier at critiquing its own answer cannot have reflection failures degrade its eligibility for the primary task). No `TASK_REQUIREMENTS` entry or `RunBudget` field exists for reflection specifically; it shares the same 60s total wall-clock budget the primary call and its schema-repair retry already draw from.
