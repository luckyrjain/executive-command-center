---
id: PHASE-008-API-SCHEMAS
title: Phase 8 Multi-user API
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 8 API Schemas

```text
GET|POST /workspaces
GET|PATCH /workspaces/{id}
GET|POST /workspaces/{id}/invitations
POST /invitations/{id}/accept|reject
GET /workspaces/{id}/members
PATCH|DELETE /workspaces/{id}/members/{user_id}
GET|POST /sharing/grants
DELETE /sharing/grants/{id}
GET|POST /delegations
POST /delegations/{id}/accept|reject|revoke|complete
GET /shared/activity
```

The authenticated user selects an allowed workspace through a server-validated session context. Invitation and delegation payloads cannot assert recipient identity after acceptance. Resource responses expose effective permissions. Sensitive private content returns 404 to unauthorized callers.
