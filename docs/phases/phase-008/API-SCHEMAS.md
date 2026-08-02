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

## Task 1 status

**Shipped**, under `ecc.domains.identity.accounts` (`APIRouter(prefix="/api/v1/identity")`, alongside the existing `person_organizations` identity router): `POST /accounts`, `POST /auth/login`, `POST /auth/select-workspace`, `GET|POST /workspaces`, `GET|PATCH /workspaces/{id}`. `POST /auth/login` returns either `LoginAuthenticated` (one active membership -- session cookies set directly) or `LoginSelectWorkspace` (two or more active memberships -- a `workspace_id` list plus a short-lived, 5-minute, stateless, HMAC-signed `pending_login_token`, no server-side session-state table). `POST /auth/select-workspace` is dual-mode: a `pending_login_token` finishes an in-progress multi-membership login (no session/CSRF involved), or an already-authenticated session (CSRF-required) switches to a different workspace the same account holds an active membership in -- the two modes share one endpoint rather than being split, since both end in the identical "mint a session for `(workspace_id, users.id)`" step. `POST /accounts` is genuine self-registration in this task's own scope (no `invitations` table exists yet -- Task 2) and deliberately has no `Idempotency-Key` support: `idempotency_records` is keyed on `(workspace_id, actor_id)`, which does not exist yet at account-creation time, and a "duplicate" login attempt is not a bug to guard against (multi-device login is normal) -- `accounts.email`'s own `UNIQUE` constraint is what makes retrying account creation safe.

`GET|POST /workspaces/{id}/invitations`, `POST /invitations/{id}/accept|reject|revoke`, `GET /workspaces/{id}/members`, `PATCH|DELETE /workspaces/{id}/members/{user_id}`, `GET|POST /sharing/grants`, `DELETE /sharing/grants/{id}`, `GET|POST /delegations`, `POST /delegations/{id}/accept|reject|revoke|complete`, `GET /shared/activity` are not part of Task 1 -- they land with Tasks 2, 3/4, 5 and 6/7 respectively.
