---
id: PHASE-007-API-SCHEMAS
title: Phase 7 Personal Intelligence API
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 7 API Schemas

```text
GET|POST /personal/domains
POST /personal/domains/{id}/enable|disable|export|delete
GET|POST /personal/records
GET|PATCH /personal/records/{id}
GET|POST /personal/goals
GET|POST /personal/routines
GET|POST /personal/check-ins
GET|POST /personal/consents
POST /personal/consents/{id}/revoke
GET /personal/insights
POST /personal/insights/{id}/dismiss|feedback
```

Requests declare domain and purpose. APIs enforce consent and field policy server-side. Insight responses include kind, evidence, confidence, limitations, freshness and policy version. Health/finance suggestions never use diagnostic or guaranteed-return language.
