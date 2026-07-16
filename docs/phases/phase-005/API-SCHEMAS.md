---
id: PHASE-005-API-SCHEMAS
title: Phase 5 Automation API
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 5 API Schemas

```text
GET|POST /automations/workflows
GET /automations/workflows/{id}
POST /automations/workflows/{id}/publish|disable|simulate
GET|POST /automations/policies
POST /automations/policies/{id}/revoke
GET|POST /automations/runs
GET /automations/runs/{id}
POST /automations/runs/{id}/pause|resume|cancel
GET /automations/approvals
POST /automations/approvals/{id}/approve|reject
```

Simulation returns predicted steps, requested permissions, possible side effects and approval points without executing. Approval responses require current version and exact action digest. Run views expose step state and redacted evidence. Standard idempotency, concurrency, audit and isolation rules apply.
