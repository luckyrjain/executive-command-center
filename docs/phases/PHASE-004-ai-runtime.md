---
id: PHASE-004
title: AI Runtime
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on: [PHASE-003]
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

Provide a local-first, replaceable and observable AI runtime for bounded generation, extraction and reasoning while preserving deterministic product behavior and human authority.

## In scope

Model registry and routing; local and optional remote providers; prompt/tool versioning; structured outputs; retrieval-context boundaries; budgets and timeouts; evaluation datasets; quality and safety gates; trace capture; graceful fallback; user-visible provenance and confidence.

## Out of scope

Autonomous action, unsupervised self-modification, background automation, mandatory cloud AI, training on workspace content, cross-workspace learning and provider-specific product coupling.

## Requirements

- Every invocation declares task type, permitted providers, data classification, timeout, budget and fallback.
- Structured tasks validate against a versioned schema; invalid output never mutates authoritative state.
- Remote providers are opt-in and blocked for disallowed data classes.
- Prompts, tools, routing policies, model versions and evaluations are immutable/versioned.
- Deterministic Phase 1–3 features remain usable when AI is disabled.
- Generated claims cite authorized evidence and remain proposals until accepted.
- Logs exclude secrets and raw sensitive prompts unless explicitly enabled for local debugging.
- Circuit breakers, cancellation and bounded retries prevent runaway execution.

## Non-functional requirements

Local routing overhead p95 <50 ms. All invocations have cost/token/time limits. Evaluation and safety gates run before a routing-policy promotion. Provider failure degrades without corrupting authoritative data.

## Security

Treat retrieved text and tool output as untrusted data. Enforce prompt-injection boundaries, tool allowlists, output validation, redaction and provider egress policy. No browser-supplied actor/workspace identity.

## Exit criteria

Approved contracts; local and remote-provider adapters; structured-output and fallback tests; evaluation baseline; injection/redaction tests; cost controls; observability; backup/restore; zero Critical/High/Medium findings; AI-disabled acceptance preserved.

## Rollback

Disable individual providers, tasks or enrichment flags. Retain deterministic product behavior and prior approved policy versions. Derived AI artifacts may be regenerated from authoritative sources.
