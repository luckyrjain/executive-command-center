---
id: PHASE-004-MODEL-ROUTING
title: Model Routing Contract
status: Draft
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

In this first activation exactly one model is registered (`ollama` / `qwen2.5:1.5b-instruct-q4_K_M`, local, all four data classes eligible), so the preference stage is reachable only in the trivial single-candidate case; the ordering above is specified now so a second model does not require re-deriving it.

## Performance

The eligibility/preference pipeline is pure in-memory comparison against a cached registry/circuit-state snapshot (refreshed on a short interval, never queried synchronously per request), keeping routing overhead within `PHASE-004-ai-runtime.md`'s p95 <50ms non-functional requirement.

## Retries, fallback and promotion

Retries are bounded (at most one, and only on `schema_invalid` or a transient provider error) and never repeat a non-idempotent tool -- moot in this activation since both registered tools (`attention.get_item`, `knowledge.get_entity`) are read-only, but stated now so it does not need re-deriving once a mutating tool exists. Fallback changes are recorded on the `ai_runs` row. Circuit breakers exclude unhealthy deployments: open after 3 consecutive failures within a rolling 60s window, half-open probe after 30s, one probe success closes it. A caller cannot override policy or enable an unapproved provider -- no endpoint in `API-SCHEMAS.md` accepts a caller-supplied `model_id` or `provider`. Promotion of a model/policy requires evaluation evidence (`EVALUATION-CONTRACT.md`'s floors) and a rollback pointer to the prior `active` prompt/tool version.
