---
id: PHASE-008-PERMISSIONS
title: Multi-user Permission Contract
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
---

# Multi-user Permission Contract

Authorization evaluates active membership, role, resource visibility, explicit grant/deny, ownership, action and time, through one closed decision function (`ecc.platform.authz.authorize`, `docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md` Decision 2) every domain calls -- not a per-endpoint ad hoc check. Deny and privacy constraints override role grants. Workspace administrators cannot read personal/private vaults merely because they administer membership: every Phase 7 personal-domain table is hardcoded `private` visibility with no code path that ever widens it, and `resource_grants` targeting those tables are rejected at write time, not merely never issued by convention.

Checks occur in service and query boundaries, pre-query (a list endpoint filters visibility server-side in its own `WHERE` clause; a denied single-resource `GET` returns 404, never 403); UI hiding is not security. Background jobs snapshot no broader authority than the initiating policy and re-check `authorize()` immediately before each side-effecting step, not once at job-start -- a mid-run revocation takes effect on the job's next step, not its next full run. Permission changes propagate near-instantly because no permission cache exists anywhere in this system to invalidate: `workspace_memberships.status`, `resource_grants.revoked_at`/`expires_at` and `sessions.revoked_at` are read fresh from PostgreSQL on every request; membership revocation additionally revokes every live session for that membership in the same transaction as a second, independent propagation path.

**Corrected during Phase 8's final whole-phase review, not implemented as originally written here:** `authorize()`'s own six-step decision is a pure, deterministic function of live role/visibility/grant state, evaluated fresh on every request -- it has no version field, and `_ROLE_PERMISSIONS` (the role-to-action baseline) is a fixed application-level dict, not a versioned artifact. Nor does an individual `authorize()` call itself write an audit row for its own allow/deny outcome. What this system actually has, and what "redacted audit evidence" more accurately describes, is `ecc.platform.notifications`'s `GET /shared/activity` (Task 7): a redacted audit/activity feed that re-runs `authorize()` fresh per candidate row before including it, so a member only ever sees activity for resources they can currently see -- a real-time redaction check on read, not a stored, versioned audit trail of past authorization decisions.
