---
id: PHASE-004-DATA-MODEL
title: Phase 4 AI Runtime Data Model
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 4 Data Model

Concrete shape for this first activation, resolved by `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md` (Decisions 1-9) and `docs/adr/ADR-0012-ollama-local-inference.md`. Every table is workspace scoped where user data is present, matching every existing Phase 1-3 migration.

| Record | Purpose | Key fields | Resolved for this activation |
|---|---|---|---|
| model_definitions | Approved model/provider capability | `provider`, `model_id`, `deployment`, `data_classes`, `capabilities`, `context_window_tokens`, `structured_output_supported`, `status` | Two rows (migration `0032_phase4_second_model.py` added the second): `provider='ollama'`, `deployment='local'` for both `model_id='qwen2.5:1.5b-instruct-q4_K_M'` and `model_id='qwen2.5:3b-instruct-q4_K_M'`, both with `data_classes` = all four defined classes and `capabilities` = `{extraction, summarization, explanation}` (deliberately identical across both rows, so the router's preference stage -- not eligibility filtering -- decides between them). No remote row exists in this activation. |
| routing_policies | Versioned task routing | `task_type`, `candidates`, `constraints`, `fallback`, `version` | One policy version per `task_type`; `attention.explain_item` is the only task type registered in this activation (design doc Decision 9). `candidates` documents both registered models (kept in sync for audit accuracy) but is not an enforced input to `route()`, which reads every `active` `model_definitions` row directly -- see `MODEL-ROUTING-CONTRACT.md`. Eligibility/preference pipeline is fixed by the design doc's Decision 2, not per-policy configurable in this first cut. |
| prompt_versions | Immutable prompt contract | `prompt_id`, `version`, `template`, `template_hash`, `input_schema_ref`, `output_schema_ref`, `status` | `template_hash` is `sha256` over `{template, input_schema_ref, output_schema_ref}`. A PostgreSQL trigger rejects `UPDATE` of `template`/`template_hash` once `status <> 'draft'`. Exactly one `active` version per `prompt_id`, enforced by a partial unique index (`WHERE status = 'active'`). |
| tool_definitions | Allowlisted tool contract | `name`, `version`, `scopes`, `input_schema`, `output_schema`, `handler_ref`, `status` | Two rows in this activation: `attention.get_item` (`scopes=['read:attention']`) and `knowledge.get_entity` (`scopes=['read:knowledge']`), both read-only, both immutable under the same trigger/activation rule as `prompt_versions`. |
| ai_runs | Invocation envelope | `task`, `data_class`, `policy_version`, `model_id`, `prompt_version`, `status`, `timing`, `token/cost totals` | `status` in `completed \| degraded \| failed \| cancelled`. `cost` is always `0.0` in this activation (no metered remote provider exists); the field is populated for schema stability, not because it varies yet. |
| ai_run_steps | Bounded model/tool steps | `run_id`, `sequence`, `kind` (`model_call \| tool_call`), `status`, redacted trace | At most one `model_call` step plus one bounded schema-repair retry, and at most one `tool_call` step per run in this activation (design doc Decision 5's budget table: 60s total run budget). Trace is redacted by default -- raw prompt/output text is not stored unless a workspace has an explicit, time-bound verbose-trace opt-in. |
| evaluation_sets | Versioned labelled examples | `task_type`, `version`, `classification` | One set: `task_type='attention.explain_item'`, version 1, 20 hand-labelled examples (`tests/fixtures/phase4_evaluation_attention_explain.py`), each with `must_cite`/`must_not_state` fields (design doc Decision 9). |
| evaluation_runs | Comparable results | `policy/model/prompt versions`, `metrics`, `failures` | Metrics recorded: schema validity, grounding rate, prohibited-fact count, p95 latency -- the four floors in design doc Decision 9's table. |
| generated_artifacts | Derived proposed output | `source_versions`, `schema_version`, `evidence`, `status` | For `attention.explain_item`: `source_versions` pins the `attention_items` row's version read; `evidence` is the cited `factor` codes (which must be a subset of the source item's actual factors -- the grounding check is enforced at write time, not only at evaluation time). Never becomes authoritative; Phase 3's `attention_items` remains the source of truth. |

## Data classes (resolved)

Four classes: `public`, `internal`, `sensitive`, `restricted` -- the same granularity `docs/phases/phase-007/DOMAIN-PRIVACY-CONTRACT.md` already assumes for a later phase, adopted here rather than invented twice. Every domain record Phase 4 can currently read (Phase 1 tasks/commitments/risks, Phase 2 entities/claims, Phase 3 attention items) defaults to `sensitive` unless a future phase-specific rule reclassifies it -- a conservative default. All four classes are local-only-eligible in this activation; none is remote-eligible (no remote provider is registered at all -- design doc Decision 7).

## Immutability and versioning (resolved)

Prompts and tool definitions are immutable after activation, enforced by a database trigger (not only application code) rejecting mutation of `template`/`template_hash` or schema/scope columns once `status <> 'draft'`. Raw sensitive prompt/output content is not stored by default. Generated artifacts never become authoritative without a domain-specific confirmation -- in this activation there is no confirmation path at all, because no task writes to a domain mutation (the first two tools are read-only and `attention.explain_item`'s output is a proposal surfaced for reading, not accepted into any table other than `generated_artifacts` itself).
