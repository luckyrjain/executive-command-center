---
id: PHASE-004-API-SCHEMAS
title: Phase 4 AI Runtime API
status: Draft
version: 0.2.0
owner: Lucky Jain
---

# Phase 4 API Schemas

Resolved administrative/runtime surface for this first activation (`docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md`):

```text
GET /ai/models
GET /ai/policies
POST /ai/policies/{prompt_id_or_tool_name}/activate
POST /ai/runs
GET /ai/runs/{id}
POST /ai/runs/{id}/cancel
GET /ai/evaluations
POST /ai/evaluations/runs
GET /ai/evaluations/runs/{id}
```

Product modules invoke the runtime through typed internal ports; they may not select arbitrary models or tools. `POST /ai/runs` in this activation accepts exactly one `task` value, `attention.explain_item` (design doc Decision 9) -- any other `task` value is rejected at the port boundary before routing is attempted, not silently ignored. Run requests declare task, schema version, authorized source refs (`attention_item_id`), data class and budget; budget defaults match design doc Decision 5's table (20s model-call timeout, 512 max output tokens) and may be tightened but never loosened by the caller.

Responses distinguish `completed|degraded|failed|cancelled`, include policy/model/prompt versions, evidence (the source item's cited factor codes), usage (tokens; `cost=0.0` in this activation) and validated output. `POST /ai/runs/{id}/cancel` closes the underlying Ollama streaming call (design doc Decision 5) rather than merely marking the row cancelled after the fact -- a run already past its final schema-validation step cannot be cancelled, only a new run started.

`POST /ai/policies/{prompt_id_or_tool_name}/activate` is the explicit administrative action that flips which `prompt_versions`/`tool_definitions` row is `active`; it requires local-owner authority (matching `PHASE-004-ai-runtime.md`'s "Administration requires explicit local-owner authority") and writes an audit event. It never mutates an already-`active` or `retired` row -- activation always points the "current" pointer at an existing immutable version, never edits one.

Idempotency, session-derived identity, audit redaction and 404 isolation apply, matching every existing Phase 1-3 endpoint convention. No endpoint in this activation accepts a caller-supplied `model_id`, `provider`, or `prompt_version` -- all three are resolved server-side by the router (design doc Decision 2), never by the browser payload.

## Errors

Required codes: `schema_invalid` (design doc Decision 4 -- validation failure, output never reaches the caller), `tool_not_allowlisted` (a task attempted to use a tool outside its declared `eligible_tools`), `budget_exceeded`, `timeout`, `circuit_open` (design doc Decision 5), `feature_disabled` (a task/tool not yet registered in this activation, e.g. any `task` other than `attention.explain_item`), `remote_not_configured` (any attempt to route to a data class/task combination that would require a remote provider -- always returned in this activation, since none is registered).
