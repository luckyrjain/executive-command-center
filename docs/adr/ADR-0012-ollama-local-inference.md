---
id: ADR-0012
title: Ollama Local Inference
status: Accepted
date: 2026-07-23
owners:
  - Lucky Jain
related:
  - RFC-005
  - ADR-0004
  - ADR-0007
  - PHASE-004-AI-RUNTIME
---

# ADR-0012 — Ollama Local Inference

## Context

`RFC-005.md`'s "Approved later" table pre-registered Ollama in Phase 0 as the intended local model-inference technology, gated behind exactly two things: "AI-runtime phase specification and ADR review." `docs/phases/PHASE-004-ai-runtime.md` is that phase specification, and `ADR-0004-ai-runtime.md`/`ADR-0007-model-router.md` (both already Accepted) already commit this repository to a single AI Runtime behind a provider-neutral Model Router, local preferred for sensitive content, and no silent cloud fallback — this ADR does not redecide any of that; it activates the specific local-inference technology those already-Accepted ADRs assumed would eventually exist, and names the concrete first model.

The repository owner has authorized proceeding (see `docs/ROADMAP.md`'s Phase 4 status note for how this authorization is recorded, mirroring the precedent set for Phase 2's and Phase 3's own parallel-start authorizations — Phase 4 formally depends on Phase 3 per `PHASE-004-ai-runtime.md`'s frontmatter, and Phase 3's two-week dogfood exit gate is still open). This ADR is the other half of RFC-005's Ollama activation gate alongside the RFC-005 v1.3.0 amendment and `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md` (the design doc this ADR summarizes the decision from).

## Decision

Activate **Ollama** as the local model-inference runtime, accessed exclusively through the `ollama` Python client package (the only new runtime dependency this ADR introduces), itself accessed exclusively through the AI Runtime's Model Router (`ADR-0007`) — no domain module imports the `ollama` package directly, matching `chapter-03-ai-runtime.md`'s `AFF-AI-001` fitness function ("No direct Ollama calls outside Model Router") even though that chapter remains Draft.

Register exactly one model for this first activation: **`qwen2.5:1.5b-instruct-q4_K_M`** (Qwen2.5, 1.5B parameters, Q4_K_M quantization). At this quantization the model needs roughly 1-1.5 GB of resident memory and produces usable tokens/second on CPU-only hardware, making "runs on the executive's own local machine, no GPU required" a fact rather than an aspiration for this first activation — consistent with `ADR-0002`'s local-first architecture decision. Qwen2.5's instruct checkpoints are specifically tuned for structured (JSON/tool-call) output adherence, which reduces (but does not replace — see the design doc's Decision 4) the rate of schema-validation failures the runtime's Pydantic-based validator has to reject and retry. The choice is also consistent with `chapter-03-ai-runtime.md`'s own draft model-selection table, which already names "Qwen Instruct" for Planning-shaped tasks (line 206) — this activation picks a concrete, small member of that same model family rather than an unrelated one.

Every model call goes through Ollama's **streaming** generate endpoint, not the non-streaming one, specifically so that a budget/operator-triggered cancellation can close the stream mid-generation instead of waiting on a non-preemptible blocking call.

No remote provider is registered or wired up in this activation. `model_definitions` (`phase-004/DATA-MODEL.md`) contains exactly one row, `deployment=local`, eligible for every data class this activation defines. This keeps the first AI Runtime activation's network footprint to a single new hop — `backend` to a local Ollama process, both inside the trust boundary Docker Compose already establishes — with zero new external egress destination, mirroring `ADR-0011`'s equivalent choice to keep Phase 2's embeddings activation fully local with no external embedding API.

## Consequences

### Positive

- Executive-facing AI features (starting with the design doc's `attention.explain_item` task) can run entirely on the user's own machine with no cloud dependency, no per-call cost, and no new data leaving the device — directly serving `PHASE-004-ai-runtime.md`'s "predictable privacy" user-value statement.
- Ollama's HTTP API is a stable, well-documented surface (list/pull/generate/chat, streaming and non-streaming) that the Model Router can wrap once and reuse for every future local model this repository registers — adding a second Ollama-served model later is a `model_definitions` row, not new integration code.
- The `ollama` Python client is a small, pure-Python package (thin HTTP wrapper) with no GPU/native-build requirement of its own — unlike `ADR-0011`'s `sentence-transformers`/`torch` addition, this dependency carries no Alpine/musl packaging problem, because the actual model inference happens inside the separate Ollama server process, not inside `backend`'s own Python process.

### Negative

- Ollama itself is a separate operating process/service (already anticipated by `chapter-02b-runtime.md`'s Docker Compose diagrams, which show an `Ollama` box alongside `backend`/`storage` since early architecture drafts) — this is a second long-running local process Phase 0-3 did not require, though still within a single Docker Compose deployment, not a new distributed system.
- First-call model load/pull costs a one-time multi-hundred-MB download (`qwen2.5:1.5b-instruct-q4_K_M`'s quantized weights) and a several-second warm-up; mitigated the same way `ADR-0011` mitigated `sentence-transformers`' first-call cost — lazy loading, and the feature stays behind the AI Runtime's own request path rather than blocking any Phase 1-3 deterministic flow, so `Ollama offline` degrades AI features specifically (`chapter-02b-runtime.md`: "Ollama Offline → Recommendations unavailable → Knowledge still searchable") and nothing else.
- A single 1.5B model is deliberately not enough model for every future Phase 4 task (longer-context reasoning, coding-adjacent tasks a later phase might add) — accepted as the correct size for this first, deliberately narrow activation; a second, larger model is an explicit near-term follow-up (design doc Decision 1's "Alternatives considered and deferred"), not a limitation this ADR treats as permanent.

### Risks

- **Sandbox/CI testing constraint.** This development sandbox has no outbound network access to `ollama.com` (confirmed: `curl https://ollama.com` returns a 403 from the environment's egress proxy) and cannot install or run the actual Ollama server binary — real end-to-end testing against a live local Ollama server is not possible here, only in a real deployment or a dedicated CI job with genuine network access (mirroring `ADR-0011`'s `embeddings-benchmark` CI job for the analogous `torch`-on-Alpine constraint). The `ollama` Python client package itself is installable via `pip` (PyPI access confirmed working), so contract-level tests (routing, budgets, validation, tool allowlisting) can and do run against a mocked/stubbed HTTP client in every environment including this one; only real generation quality, real evaluation-floor pass/fail (design doc Decision 9), and real CPU latency numbers require that dedicated CI job with an actual Ollama service, and none of those were exercised as part of producing this ADR.
- CPU-only inference latency is sensitive to host hardware in a way GPU-backed inference is not — the budget numbers in the design doc's Decision 5 are conservative first-cut estimates, not measurements, and must be re-validated against the dedicated CI job's real numbers before being treated as a committed SLA.
- A poorly-tuned system prompt or an under-specified `eligible_tools` allowlist could either make the model unable to complete legitimate tasks (over-restriction) or leave more injection surface than intended (under-restriction); the design doc's Threat model section names the concrete mitigations in place for this activation's specific two-read-only-tool, no-remote-provider scope, and states explicitly that those mitigations do not automatically extend to a future slice that adds mutating tools or remote providers.

## Alternatives considered

- **llama.cpp directly** (the inference engine Ollama itself wraps): rejected for this activation. Using `llama.cpp`'s own server/bindings directly would mean this repository owns model lifecycle management (GGUF acquisition/verification, context management, request queuing, quantization selection) that Ollama already provides as a stable, versioned HTTP API — reimplementing that management layer is effort spent on infrastructure this phase's actual scope (routing, versioning, structured output, bounded tools, evaluation — `PHASE-004-ai-runtime.md`'s "In scope") does not need to own. `chapter-03-ai-runtime.md`'s existing draft architecture also already assumes an Ollama-shaped boundary (`AFF-AI-001`), so choosing llama.cpp directly would mean rewriting that chapter's direction rather than implementing it. Revisit only if Ollama's API genuinely cannot express a future requirement (e.g. fine-grained KV-cache control this phase does not need).
- **ONNX Runtime**: rejected for this activation. ONNX Runtime is a general inference engine, not a model-serving system — it would still require this repository to build the model download/verification, request queuing, and HTTP-serving layer Ollama already provides, and ONNX-format LLM checkpoints are a smaller, less actively maintained ecosystem for instruction-tuned chat models than the GGUF format Ollama's model library standardizes on, meaning less choice for Decision 1's model selection and more manual conversion work to keep a model current. ONNX Runtime remains a reasonable choice for other future inference workloads (e.g. a small classifier that isn't a chat/instruct LLM) but is not the right fit for this activation's chat-completion-shaped tasks.
- **A hosted/remote inference API** (e.g. a cloud LLM endpoint) as the *first* provider instead of, or alongside, a local one: rejected for this activation, for the same reason `ADR-0011` rejected an external embedding API for Phase 2 — it would mean prompt/context content leaves the local device on every call from day one, contradicting `ADR-0002`'s local-first principle and `ADR-0007`'s "local preferred for sensitive content" before any local option has even been proven end to end. The design doc's Decision 7 defers remote providers entirely, not permanently — a later Phase 4 slice can add one once the local path, allowlist and validation mechanisms this ADR activates are proven.
- **Do nothing (leave Phase 4 undesigned until a later date)**: available, since nothing forces this activation now — Phase 3's own exit gate (two-week dogfood) is still open, the same situation Phase 2 and Phase 3 were each in when their own parallel-start exceptions were granted. Rejected now that the repository owner has explicitly authorized proceeding in parallel, recorded here rather than silently declined so the reasoning for either choice stays visible, matching `ADR-0011`'s own handling of this same class of alternative.
