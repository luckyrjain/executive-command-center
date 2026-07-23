---
id: PHASE-004-IMPLEMENTATION-STATUS
title: Phase 4 Implementation Status
status: Planned
version: 0.2.0
owner: Lucky Jain
updated: 2026-07-23
---

# Phase 4 Implementation Status

Phase 4 design work is complete and contracts are Approved for Implementation; code implementation is beginning. This document is informational and does not override normative contracts.

## Planning artifacts

`docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md` (design doc, resolving Ollama activation, first model choice, routing algorithm, prompt/tool versioning, structured-output validation, bounded tool runtime, budgets, evaluation harness and data-class policy) and `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md` (task-by-task implementation plan, six delivery tasks for this first activation slice). Neither document authorizes implementation by itself -- see Prerequisites below.

## Prerequisites

- Phase 3 exit gates complete, or an explicit repository-owner parallel-start authorization matching Phase 2's and Phase 3's own precedent -- **granted 2026-07-23**, same exception Phase 2 and Phase 3 received; see `docs/ROADMAP.md`'s Phase 4 status note and `PHASE-004-ai-runtime.md`'s "Dependency exit posture" section.
- Phase 4 contracts approved for implementation -- **done 2026-07-23.** The four approval gates named in `docs/phases/PHASE-REVIEW.md:135` (approved local/remote models and providers, data-class egress matrix, evaluation floors, trace retention) are resolved in `PHASE-004-ai-runtime.md`'s "Approved models, providers and evaluation floors" section and the six `phase-004/*.md` contracts, all moved to `Approved for Implementation` at version 0.2.0.
- Ollama activated as a technology -- **done 2026-07-23.** `docs/RFC-005.md` v1.3.0 and `docs/adr/ADR-0012-ollama-local-inference.md`, satisfying RFC-005's pre-registered "AI-runtime phase specification and ADR review" gate.
- Versioned evaluation dataset and promotion rubric established -- planned as `tests/fixtures/phase4_evaluation_attention_explain.py` (Task 5 of the implementation plan), not yet created.
- Ethics/safety review of the tool-allowlist and prompt-injection mitigations -- planned as Task 4's dedicated adversarial test plus Task 6's security-scanning pass, not yet created.

## Planned delivery tasks

| Task | Outcome | Status |
|---|---|---|
| 0 | Resolve open decisions and move contracts to Approved for Implementation | Done (this pass) |
| 1 | Model/provider registry and deterministic router | Not started |
| 2 | Immutable prompt/tool versioning and structured-output validation | Not started |
| 3 | Budgets, timeouts, cancellation and circuit breaker | Not started |
| 4 | Bounded tool runtime and `attention.explain_item` orchestration | Not started |
| 5 | Evaluation harness and first dataset | Not started |
| 6 | Product surface, browser acceptance and security review | Not started |

## Sandbox constraint (carried forward from the design pass)

This repository's development sandbox has no outbound network access to `ollama.com` and cannot run the Ollama server binary. The `ollama` Python client package is installable via PyPI and usable against a mocked HTTP client for every contract-level test in Tasks 1-4 and most of Task 5. Real generation, real evaluation-floor pass/fail, and real latency measurement require the dedicated `.github/workflows/ollama-evaluation.yml` CI job (Task 5, Step 6) -- not executed as part of this design pass, and not executable in this sandbox at implementation time either. This is recorded here so implementation does not silently treat a mocked evaluation result as equivalent to a real one.

## Exit evidence

Implementation PRs, real evaluation-CI results, security review, isolation matrix, performance results and backup/restore evidence will be linked here as produced.
