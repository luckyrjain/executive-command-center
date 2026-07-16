---
id: PHASE-004-DATA-MODEL
title: Phase 4 AI Runtime Data Model
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 4 Data Model

| Record | Purpose | Key fields |
|---|---|---|
| model_definitions | Approved model/provider capability | provider, model_id, deployment, data_classes, status |
| routing_policies | Versioned task routing | task_type, candidates, constraints, fallback, version |
| prompt_versions | Immutable prompt contract | prompt_id, version, template_hash, input/output_schema |
| tool_definitions | Allowlisted tool contract | name, version, scopes, input/output_schema |
| ai_runs | Invocation envelope | task, policy_version, model, status, timing, token/cost totals |
| ai_run_steps | Bounded model/tool steps | run_id, sequence, kind, status, redacted trace |
| evaluation_sets | Versioned labelled examples | task_type, version, classification |
| evaluation_runs | Comparable results | policy/model versions, metrics, failures |
| generated_artifacts | Derived proposed output | source_versions, schema_version, evidence, status |

All records are workspace scoped where user data is present. Prompts and policies are immutable after activation. Raw sensitive content is not stored by default. Generated artifacts never become authoritative without a domain-specific confirmation.
