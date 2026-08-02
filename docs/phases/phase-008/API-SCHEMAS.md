---
id: PHASE-008-API-SCHEMAS
title: Phase 8 Multi-user API
status: Approved for Implementation
version: 0.3.0
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

**Shipped**, under `ecc.domains.identity.accounts` (`APIRouter(prefix="/api/v1/identity")`, alongside the existing `person_organizations` identity router): `POST /accounts`, `POST /auth/login`, `POST /auth/select-workspace`, `GET|POST /workspaces`, `GET|PATCH /workspaces/{id}`. `POST /auth/login` returns either `LoginAuthenticated` (one active membership -- session cookies set directly) or `LoginSelectWorkspace` (two or more active memberships -- a `workspace_id` list plus a short-lived, 5-minute, stateless, HMAC-signed `pending_login_token`, no server-side session-state table). `POST /auth/select-workspace` is dual-mode: a `pending_login_token` finishes an in-progress multi-membership login (no session/CSRF involved), or an already-authenticated session (CSRF-required) switches to a different workspace the same account holds an active membership in -- the two modes share one endpoint rather than being split, since both end in the identical "mint a session for `(workspace_id, users.id)`" step. `POST /accounts` was genuine open self-registration in this task's own scope (no `invitations` table existed yet); Task 2 has since tightened it to require a valid token, see that section below. Neither `POST /accounts` nor `POST /workspaces`/`PATCH /workspaces/{id}` carry `Idempotency-Key` -- see this module's own docstring for the two distinct reasons (pre-auth endpoints structurally cannot key on `(workspace_id, actor_id)`; the two workspace endpoints skip it deliberately, a mistaken second workspace being visible/correctable and a `PATCH` being naturally idempotent).

## Task 2 status

**Shipped**, under `ecc.domains.identity.invitations`: `POST|GET /workspaces/{id}/invitations` (`owner`/`admin` only for both), `POST /invitations/{id}/accept|reject|revoke`. `POST /accounts` (`accounts.py`) now requires a `token` query parameter, validated against a real, unresolved, unexpired `invitations` row, with the submitted `email` required to case-insensitively match the invitation's own recipient email -- self-registration with no invitation is closed. Two distinct acceptance paths, not one: a brand-new recipient (no account anywhere) registers, is auto-accepted into the invitation's workspace, and receives a session, all in one `POST /accounts` call, since no separate authenticated `/accept` call is reachable before that account exists; an existing account (with a session from any other workspace membership) instead calls `POST /invitations/{id}/accept` with the raw token, independently re-checked against `token_hash`/expiry/terminal-state/email-match inside one row-locked transaction, matching this document's own top-level "cannot assert recipient identity after acceptance" line. `POST /invitations/{id}/reject` is the recipient-initiated symmetric counterpart; `POST /invitations/{id}/revoke` is inviter/admin-initiated with no email-match check, since the caller's authority comes from their own workspace role, not from being the recipient.

`GET /workspaces/{id}/members`, `PATCH|DELETE /workspaces/{id}/members/{user_id}`, `GET|POST /sharing/grants`, `DELETE /sharing/grants/{id}`, `GET|POST /delegations`, `POST /delegations/{id}/accept|reject|revoke|complete`, `GET /shared/activity` are not part of Task 1 or 2 -- they land with Tasks 3/4, 5 and 6/7 respectively.

## Task 3 status

**Shipped**, under `ecc.platform.authz` (`APIRouter(prefix="/api/v1/sharing")`): `GET|POST /sharing/grants`, `DELETE /sharing/grants/{id}`. `POST /sharing/grants` first validates `resource_type` against the closed allowlist migration `0063` added `owner_id`/`visibility` to (`400 RESOURCE_TYPE_NOT_GRANTABLE` for an unrecognized type, the same code for a recognized-but-`UNGRANTABLE_RESOURCE_TYPES` Phase 7 personal-domain type), then requires the caller be either the resource's own owner or hold `owner`/`admin` role in the workspace -- a disclosed judgment call (neither the design doc nor `PERMISSION-CONTRACT.md` states who may grant explicitly), matching the same bar `create_invitation_endpoint` already holds invitation creation to. `GET /sharing/grants` is role-scoped, not just workspace-scoped: `owner`/`admin` see every grant in the workspace (the sharing-review surface Task 5 builds a UI for); every other role sees only grants naming their own account as grantee. `DELETE /sharing/grants/{id}` is idempotent-safe (`409` if already revoked, not a silent no-op) and permits either the original granter, the resource's own owner, or `owner`/`admin`.

`ecc.domains.engineering`'s 19 existing endpoints (`decisions_incidents.py`'s 6, `connector_accounts.py`'s 13) are this task's reference wiring: every mutate endpoint calls `authz.require_role_action`/`authz.authorize` before touching a resource (a two-step read-then-write check on endpoints identified by path parameter, so a resource that exists but the caller cannot see returns `404`, never `403` -- this document's own top-level "existence cannot be inferred from an authorization failure" rule, now enforced by the general mechanism rather than domain-specific `workspace_id` scoping); every list endpoint calls `authz.visible_resource_filter_sql` and embeds the returned fragment in its own `WHERE` clause, filtering server-side rather than returning then hiding rows client-side. `GET /workspaces/{id}/members`, `PATCH|DELETE /workspaces/{id}/members/{user_id}`, `GET|POST /delegations`, `POST /delegations/{id}/accept|reject|revoke|complete`, `GET /shared/activity` remain out of scope, Tasks 4/6/7's own work respectively -- Task 4 also widens the `authorize()`/`visible_resource_filter_sql` wiring pattern demonstrated here to every remaining domain's existing endpoints, mechanically, no new mechanism.
