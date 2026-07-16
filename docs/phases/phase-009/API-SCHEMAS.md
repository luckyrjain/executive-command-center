---
id: PHASE-009-API-SCHEMAS
title: Phase 9 Enterprise API
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 9 API Schemas

```text
GET|POST /enterprise/organizations
GET|POST /enterprise/identity-providers
POST /enterprise/identity-providers/{id}/validate|activate|disable
GET|POST /enterprise/policies
POST /enterprise/policies/{id}/publish|rollback
GET|POST /enterprise/retention-policies
GET|POST /enterprise/legal-holds
POST /enterprise/legal-holds/{id}/release
GET|POST /enterprise/audit-exports
GET /enterprise/compliance/controls
GET /enterprise/quotas
POST /enterprise/break-glass/request|end
```

SCIM uses the supported standard endpoint/version and idempotent provisioning. Administrative APIs require dedicated roles and step-up authentication for high-impact actions. Policy simulation returns affected scope before publication. Tenant identity is server-derived.
