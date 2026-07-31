---
id: PHASE-007-INSIGHT
title: Personal Insight Contract
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Personal Insight Contract

Insights must be evidence-backed, proportionate and non-manipulative. Types are `observation|trend|correlation|reminder|planning_suggestion`. Each shows source period, missing data, confidence and limitations.

The system does not diagnose, prescribe, promise financial outcomes or infer sensitive traits. High-stakes decisions direct the user to qualified professionals. Cross-domain insight requires an active grant covering every source. Feedback cannot silently turn correlation into causation.

## Safety rubric (resolved)

`source period`/`missing data`/`confidence`/`limitations` are Pydantic-required fields on the insight output schema, not a prompt-only instruction -- an insight response omitting one fails schema validation and is never returned, mirroring `attention.explain_item`'s `ExplainItemOutput` guarantee. Before any `health`/`finance`-domain prompt version is promoted, it must clear a dedicated adversarial fixture set (diagnostic claims, prescriptive treatment language, guaranteed-return language, credit/employment/insurance-decision language, sensitive-trait inference) at the same 100%-floor rigor `EVALUATION-CONTRACT.md` holds `attention.explain_item`'s `must_not_state` probe to. No insight `kind` computes or surfaces a numeric relationship-health score, ranking or frequency leaderboard -- a hard schema-level constraint on the `kind` enum, not a UI convention. `health`/`finance`-classified insights require a non-empty `professional_referral_note` field (absent on other domains' insights). `POST .../feedback` never rewrites an insight's own `kind` -- only separate feedback metadata, so feedback cannot upgrade a `correlation` into something that reads as a settled `observation`.

## Task 1 status

Zero AI-generated insights this activation -- every `habits`-domain insight is deterministic (a direct threshold computation, e.g. a check-in gap), no model call. `trend`/`correlation` kinds (the only ones a model generates) are deferred to the task that adds `cross_domain_grants` (`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`, Decision 1, item 5), once this rubric has an approved target to build against.
