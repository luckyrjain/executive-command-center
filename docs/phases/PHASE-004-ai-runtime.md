---
id: PHASE-004
title: AI Runtime
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
depends_on:
  - PHASE-003
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005
  - STD-001
contracts:
  - phase-004/DATA-MODEL.md
  - phase-004/API-SCHEMAS.md
  - phase-004/MODEL-ROUTING-CONTRACT.md
  - phase-004/EVALUATION-CONTRACT.md
  - phase-004/UX-STATES.md
  - phase-004/TEST-PLAN.md
---

# PHASE-004 — AI Runtime

## Objective

Provide a local-first, replaceable and observable runtime for bounded generation, extraction and reasoning while preserving deterministic behavior and human authority.

## User value

The user receives higher-quality summaries and suggestions with visible evidence and predictable privacy, while core ECC workflows continue when models are unavailable.

## In scope

Model/provider registry; local and optional remote adapters; deterministic routing; prompt/tool/policy versioning; structured output; bounded tool runtime; context and data-class policy; timeout/token/cost budgets; cancellation, fallback and circuit breakers; evaluation datasets and promotion gates; redacted traces.

## Out of scope

Autonomous action, unsupervised self-modification, open-ended background agents, mandatory cloud AI, training on workspace content, cross-workspace learning, arbitrary browser-selected models/tools and provider-specific product coupling.

## Functional requirements

- Each run declares task, data class, eligible providers, schema, timeout, budget and fallback.
- Product modules call typed task ports; callers cannot choose arbitrary models or tools.
- Output validates against an immutable schema before use.
- Generated claims cite authorized evidence and remain proposals.
- Remote providers are opt-in and blocked for disallowed data classes.
- Prompts, tools, routing policies, models and evaluations are versioned.
- Tool calls use allowlisted typed contracts and bounded steps.
- Cancellation, bounded retry and circuit breaking stop runaway execution.
- AI failure never corrupts authoritative records or disables deterministic Phase 1–3 flows.

## Non-functional requirements

Routing overhead p95 <50 ms. Every run has hard time/token/cost/step limits. Trace persistence is bounded. Provider failure returns a classified degraded/failed result. Evaluation runs are reproducible from stored versions/hashes. Core product remains usable with AI globally disabled.

## Architecture impact

Add an AI-runtime module behind typed application ports. Provider adapters are infrastructure dependencies; domain modules do not import provider SDKs. PostgreSQL stores policies, versions, run metadata and evaluation results. Long-running execution remains bounded and does not introduce Phase 5 automation semantics.

## Data changes

Add model definitions, routing policies, prompt/tool versions, AI runs/steps, evaluation sets/runs and generated artifacts defined in `phase-004/DATA-MODEL.md`. Raw sensitive prompt/output retention is off by default.

## API changes

Add run, cancel, model/policy inspection and evaluation endpoints in `phase-004/API-SCHEMAS.md`. Product task execution remains through typed internal ports. Administration requires explicit local-owner authority.

## Frontend changes

Add generated-content provenance, evidence, correction/discard, retry/cancel and degraded-state components. Deterministic content is visually distinct. Provider/data-egress settings explain exactly what may leave the device.

## Security and privacy

Treat prompts, retrieved content and tool output as untrusted data. Enforce data classification, redaction, prompt-injection boundaries, tool allowlists, output validation, secret exclusion and provider egress policy. Browser payloads cannot assert actor/workspace or bypass routing.

## Observability

Record run/task/status, policy/prompt/model versions, latency, tokens/cost, retries/fallback, cancellation, schema failures, circuit state and evaluation metrics. Default traces exclude raw sensitive content and secrets. Correlation connects AI runs to source requests and derived artifacts.

## Test strategy

Adapter contracts; deterministic routing; budgets/timeouts/cancel; structured validation; tool allowlists; fallback/circuit breakers; version reproducibility; task evaluation; prompt injection/exfiltration; redaction/isolation; performance; AI-disabled and browser acceptance.

## Acceptance criteria

- Local provider and policy routing pass task contracts.
- Structured invalid output cannot reach domain mutations.
- Remote egress is denied unless explicitly allowed.
- Budget, cancel, retry, fallback and circuit tests pass.
- Evaluation quality/safety floors and reproducibility pass.
- AI-disabled Phase 1–3 acceptance remains green.
- UI clearly shows generated provenance and degraded state.

## Exit criteria

- Contracts and any technology additions explicitly approved.
- Evaluation baseline and promotion/rollback process established.
- Security review covers injection, egress, tools and trace retention.
- Backup/restore and operational runbook complete.
- Zero open Critical, High or Medium findings.
- Phase 5 receives a stable bounded invocation/tool contract.

### Approved models, providers and evaluation floors (approved 2026-07-23)

Resolving `docs/phases/PHASE-REVIEW.md:135`'s four named approval-gate items for this first activation, per `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md` and `docs/adr/ADR-0012-ollama-local-inference.md`:

- **Approved local/remote models and providers:** exactly one -- Ollama, `qwen2.5:1.5b-instruct-q4_K_M`, local only. No remote provider is approved in this activation.
- **Data-class egress matrix:** four data classes (`public`, `internal`, `sensitive`, `restricted`), all local-only-eligible, zero remote-eligible.
- **Evaluation floors:** `phase-004/EVALUATION-CONTRACT.md`'s table for `attention.explain_item` -- 100% schema validity, 100% grounding, 0 tolerated prohibited-fact occurrences, <20s p95 latency.
- **Trace retention:** raw prompt/output retention off by default; redacted structured metadata retained; verbose trace retention is an explicit, time-bound, admin-only opt-in, never a default.

This resolution covers this first, deliberately narrow activation (one local model, no remote provider, two read-only tools, one evaluated task type). A later Phase 4 slice that adds a remote provider, a second model, or a mutating tool reopens the relevant part of this gate rather than inheriting it silently.

## Dependency exit posture (approved 2026-07-23)

Phase 4 design and contract-approval work proceeds now, in parallel with Phase 3's own still-open exit gate (the two-week dogfood window, `docs/runbooks/PHASE-3-DOGFOOD.md`, at 0/14 days recorded), under the same kind of parallel-start exception the repository owner granted Phase 2 and Phase 3 (`docs/ROADMAP.md`'s Phase 2 and Phase 3 status notes) -- not gated on Phase 3's dogfood closing first. This authorization covers proceeding with implementation of the first activation slice; it does not itself claim Phase 3 has exited, and Phase 4's own exit criteria above (including "zero open Critical, High or Medium findings" and the evaluation baseline) still apply in full.

## Rollback plan

Disable providers, tasks or enrichment flags independently. Roll routing policy to a prior approved version. Preserve deterministic functionality. Derived artifacts can be discarded/regenerated; authoritative domain data is never rolled back from AI output.

## Deferred backlog

Unbounded agents, online learning, model fine-tuning on user data, cross-workspace evaluation data, autonomous action and distributed AI execution.
