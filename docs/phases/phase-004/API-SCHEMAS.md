---
id: PHASE-004-API-SCHEMAS
title: Phase 4 AI Runtime API
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 4 API Schemas

Proposed administrative/runtime surface:

```text
GET /ai/models
GET /ai/policies
POST /ai/runs
GET /ai/runs/{id}
POST /ai/runs/{id}/cancel
GET /ai/evaluations
POST /ai/evaluations/runs
GET /ai/evaluations/runs/{id}
```

Product modules invoke the runtime through typed internal ports; they may not select arbitrary models or tools. Run requests declare task, schema version, authorized source refs, data class and budget. Responses distinguish `completed|degraded|failed|cancelled`, include policy/model/prompt versions, evidence, usage and validated output. Idempotency, session-derived identity, audit redaction and 404 isolation apply.
