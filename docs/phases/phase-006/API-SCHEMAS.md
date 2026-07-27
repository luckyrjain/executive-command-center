---
id: PHASE-006-API-SCHEMAS
title: Phase 6 Engineering Workspace API
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 6 API Schemas

```text
GET|POST /engineering/connectors
POST /engineering/connectors/{id}/sync|disable
GET /engineering/sync-runs
GET /engineering/overview
GET /engineering/repositories
GET /engineering/work-items
GET /engineering/changes
GET /engineering/deployments
GET /engineering/incidents
GET|POST /engineering/decisions
GET /engineering/metrics
```

**Task 1 status**: the first three routes above (`/engineering/connectors`, `/engineering/connectors/{id}/sync|disable`, `/engineering/sync-runs`) are implemented (`ecc.domains.engineering.connector_accounts`). The remaining routes do not exist yet -- each is added by the task that first needs it (`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`, Tasks 2-6).

Connector creation returns required scopes and authorization state, never token values. Queries expose source coverage, freshness, definitions and evidence. Optional mutations route through approved automation policies. Signed cursors, isolation, redaction, idempotency and concurrency rules apply.
