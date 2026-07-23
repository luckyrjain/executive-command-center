---
id: PHASE-004-EVALUATION
title: AI Evaluation Contract
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# AI Evaluation Contract

Each AI task has a versioned dataset, rubric, required quality floor and prohibited outcomes. Resolved first cut, per `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md`'s Decision 9:

## First task and dataset

**Task type: `attention.explain_item`.** Given an `attention_item_id`, produce `{explanation_text: str (<=60 words), cited_factor_codes: list[str]}` -- a plain-language explanation of an already-computed, already-deterministic attention score, grounded entirely in the item's own `factors` (read via the `attention.get_item` tool). Chosen as the first evaluated task because grounding is structurally checkable: every `cited_factor_codes` entry must appear in the source item's actual `factors` list, not a fuzzy similarity judgment.

**Dataset:** `evaluation_sets`, `task_type='attention.explain_item'`, version 1 -- 20 hand-labelled examples, `tests/fixtures/phase4_evaluation_attention_explain.py` (versioned, checked in, mirroring `tests/fixtures/phase3_*`'s convention), 3-4 examples per Phase 3 entity type (`task`, `commitment`, `risk`, `waiting_link`, `risk_review`, `meeting`). Each example specifies the input item's factors, a `must_cite` set, a `must_not_state` set (a hallucination probe -- facts that do not exist on the item), and a reference explanation for readability comparison only (not exact-match scored).

## Metrics and promotion floors

| Metric | Floor | Consequence of miss |
|---|---|---|
| Schema validity | 100% | Any invalid output blocks promotion. |
| Grounding (cited factors exist on the item) | 100% | A citation to a nonexistent factor is a hallucination, zero tolerance. |
| Prohibited-fact rate (`must_not_state` violations) | 0 occurrences | Any occurrence blocks promotion -- stated as a count, not a percentage. |
| Latency (p95, full run including tool call) | <20s | Matches the per-model-call timeout budget. |

Human-labelled examples (the 20 above) are strictly separated from any development examples used while iterating on the prompt template before evaluation -- development examples are never reused as evaluation data.

## Promotion and reproducibility

Policy/model/prompt changes for `attention.explain_item` re-run the full 20-example set and require every floor above to pass before the new `prompt_versions`/`routing_policies` version can become `active`. Any privacy leak, unsupported consequential claim, unauthorized tool request or critical regression blocks promotion. Online feedback is labelled evidence, not an automatic policy update. Evaluation results, environment and artifact hashes (`prompt_versions.template_hash`, `tool_definitions` hashes, `model_id`) are retained for reproducibility.

## Sandbox constraint

This dataset's floors have not been verified against a live model as part of producing this contract -- this development sandbox has no network access to `ollama.com` and cannot run the Ollama server binary. Contract-level checks (schema validity, grounding-check logic, dataset structure) are testable against a mocked model response in any environment. Real pass/fail against the floors above requires a dedicated CI job provisioning an actual Ollama service, mirroring `ADR-0011`'s `embeddings-benchmark` job for the analogous `sentence-transformers`/`torch` constraint -- see the design doc's Test strategy section and `ADR-0012`'s Risks.
