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

This dataset's floors had not been verified against a live model as part of producing this contract -- the development sandbox that authored it has no network access to `ollama.com` and cannot run the Ollama server binary. Contract-level checks (schema validity, grounding-check logic, dataset structure) are testable against a mocked model response in any environment. Real pass/fail against the floors above required a dedicated CI job provisioning an actual Ollama service, mirroring `ADR-0011`'s `embeddings-benchmark` job for the analogous `sentence-transformers`/`torch` constraint -- see the design doc's Test strategy section and `ADR-0012`'s Risks.

**Since superseded, twice.** `.github/workflows/ollama-evaluation.yml` has since run for real, multiple times, against genuine `qwen2.5:1.5b-instruct-q4_K_M` output (PR #38's review cycle), reaching schema validity 90%, grounding 90%, 0 prohibited facts, p95 latency 17.3s after two real fixes (markdown-fence stripping in `validator.py`, factor-code/source_field disambiguation in `runtime.py`'s prompt rendering). A further fix (commit `10eca69`, `fix(phase-4): make Ollama generation deterministic`) then found the true root cause of the remaining ~10% gap: `OllamaAdapter.generate()` never set `temperature`/`seed`, so every call used a non-deterministic default. Setting `temperature=0` and a fixed seed raised schema validity and grounding to 100% each (three CI runs, byte-for-byte identical). **Current status: schema validity 100%, grounding 100%, p95 latency within budget -- the prohibited-fact-count floor is the one still missed** (count 1 vs. the required 0, a stable/deterministic single known case, not further pursued after two reverted prompt-engineering attempts each regressed other metrics more than they fixed this one). No promotion decision has been made -- see `IMPLEMENTATION-STATUS.md`'s "What remains before Phase 4 itself can exit" for the full progression.

## Reflection Engine (first slice) and this evaluation's floors

Migration `0033_phase4_reflection.py` added an optional, bounded, fail-open reflection call (`runtime.py:_reflect_on_answer`), gated off by default via `routing_policies.constraints.reflection_enabled` (seeded `false`). `run_evaluation` (the 20-example floor check above) inherits whatever the active policy row says, with no override of its own -- so this evaluation currently runs, and is expected to keep running, with reflection **disabled**, exactly matching production's default.

This is deliberate, not an oversight: the base single-call pipeline still has not cleared every floor above (the prohibited-fact-count gap just described), and enabling an unproven second model call for this floor check now would confound whether a future failure is a base-pipeline defect or a reflection-layer one, at exactly the moment the base pipeline's own remaining gap is still being actively worked. Once the base pipeline clears all four floors, evaluating reflection's own effect on them is a natural, cleanly separable follow-up (flip `reflection_enabled` for one comparison run), not part of this contract's current four floors.
