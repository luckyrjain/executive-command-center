---
id: PHASE-004-TEST-PLAN
title: Phase 4 Test Plan
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 4 Test Plan

Cover provider adapters, deterministic routing (`MODEL-ROUTING-CONTRACT.md`'s fixed eligibility/preference order), eligibility, budgets, timeout/cancel (streaming-call cancellation, per the design doc's Decision 5), circuit breakers, structured validation (`schema_invalid` blocks every domain-visible path), tool allowlists (`tool_not_allowlisted` rejected before dispatch, not after), retries and fallback. Run the versioned `attention.explain_item` evaluation (`EVALUATION-CONTRACT.md`) and compare policy candidates. Adversarial fixtures include prompt injection (a tool result or evidence snippet containing an embedded instruction, asserting the runtime never dispatches an out-of-allowlist tool as a result), data exfiltration (asserted structurally impossible in this activation -- no network-capable tool, no remote provider -- with a fitness-function-style check that no new outbound host appears in the AI Runtime's code), malicious tool output (oversized/malformed tool return, rejected by the same schema validator used for model output), schema confusion and oversized context (prompt exceeding the 90%-of-context-window margin, rejected before the model call is attempted).

## Sandbox constraint (resolved)

This repository's development sandbox has no outbound network access to `ollama.com` and cannot run the Ollama server binary (confirmed 403 from the environment's egress proxy); the `ollama` Python client package is installable via PyPI and usable against a mocked/stubbed HTTP client. Per `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md`'s Test strategy section:

- **Runs in any environment, including this sandbox:** routing eligibility/preference, budget/timeout/circuit-breaker state transitions, prompt/tool immutability and activation, schema validation accept/reject, tool allowlist enforcement, and every adversarial fixture above -- all exercised against a mocked Ollama HTTP client, none requiring a live model.
- **Requires a dedicated CI job with real network access** (mirroring `ADR-0011`'s `embeddings-benchmark` job): real generation against `qwen2.5:1.5b-instruct-q4_K_M` through an actual Ollama service, `EVALUATION-CONTRACT.md`'s floor pass/fail against real model output, and real p95 latency measurement on real CPU hardware. None of these were run in this sandbox; they must be verified in that dedicated job before any promotion decision is trusted.

Verify redacted traces (no raw prompt/output text in default logs), no secret leakage, workspace isolation, remote-provider opt-in (trivially true in this activation -- `remote_not_configured` is the only reachable outcome for any remote-shaped request), AI-disabled behavior (Phase 1-3 flows unaffected with the AI Runtime globally off), backup/restore and reproducibility (an `evaluation_runs` result reproduces from its pinned `prompt_version`/`model_id`/`tool_definitions` hashes). Benchmark routing overhead (p95 <50ms, in-sandbox, no live model needed) and local latency (requires the dedicated CI job above). Browser acceptance exercises the attention-item explanation affordance (`UX-STATES.md`): request, evidence inspection, correction/feedback, cancellation and every degraded state, run with the AI Runtime both enabled and disabled per the AI-disabled acceptance requirement every prior phase's plan has followed.
