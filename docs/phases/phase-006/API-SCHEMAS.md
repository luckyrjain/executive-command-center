---
id: PHASE-006-API-SCHEMAS
title: Phase 6 Engineering Workspace API
status: Draft
version: 0.1.0
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

Connector creation returns required scopes and authorization state, never token values. Queries expose source coverage, freshness, definitions and evidence. Optional mutations route through approved automation policies. Signed cursors, isolation, redaction, idempotency and concurrency rules apply.
