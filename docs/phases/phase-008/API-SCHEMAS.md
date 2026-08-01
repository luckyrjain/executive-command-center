---
id: PHASE-008-API-SCHEMAS
title: Phase 8 Multi-user API
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 8 API Schemas

```text
POST /accounts
POST /auth/login
POST /auth/select-workspace
GET|POST /workspaces
GET|PATCH /workspaces/{id}
GET|POST /workspaces/{id}/invitations
POST /invitations/{id}/accept|reject|revoke
GET /workspaces/{id}/members
PATCH|DELETE /workspaces/{id}/members/{user_id}
GET|POST /sharing/grants
DELETE /sharing/grants/{id}
GET|POST /delegations
POST /delegations/{id}/accept|reject|revoke|complete
GET /shared/activity
```

The authenticated user selects an allowed workspace through a server-validated session context (`POST /auth/select-workspace`, Task 1) -- a session is scoped to exactly one workspace at a time; switching workspaces creates a new session for a different `(workspace_id, users.id)` pair under the same `account_id`, it never lets one session span two workspaces. Invitation and delegation payloads cannot assert recipient identity after acceptance -- `POST /invitations/{id}/accept` independently re-verifies the authenticated account's own email against the invitation's recipient email inside one row-locked transaction (`docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md` Decision 3), not merely trusting whichever account presents a valid token. Resource responses expose effective permissions (which visibility tier and, where relevant, which grant is why the caller can see this resource). Sensitive private content returns 404, never 403, to unauthorized callers, so existence cannot be inferred from an authorization failure.
