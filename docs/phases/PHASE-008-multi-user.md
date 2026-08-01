---
id: PHASE-008
title: Multi-user Workspaces
status: Approved for Implementation
version: 0.3.0
owner: Lucky Jain
depends_on:
  - PHASE-007
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005
  - STD-001
contracts:
  - phase-008/DATA-MODEL.md
  - phase-008/API-SCHEMAS.md
  - phase-008/PERMISSION-CONTRACT.md
  - phase-008/DELEGATION-CONTRACT.md
  - phase-008/UX-STATES.md
  - phase-008/TEST-PLAN.md
---

# PHASE-008 — Multi-user Workspaces

## Approved decisions

Contracts moved from Draft after resolving `docs/phases/PHASE-REVIEW.md:139`'s four named approval-gate items (identity migration; complete authorization matrix; invitation verification; revocation propagation SLO) in `docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md`, per that document's own Decision 1-4 sections. In summary: a new workspace-independent `accounts` table becomes the thing a person authenticates as; the existing `users` table keeps its exact current role as the composite `(workspace_id, id)` FK anchor every `owner_id` column across ~14 existing tables already points to (zero disruption to those FKs), gaining only an `account_id` column, with a new `workspace_memberships` table carrying the mutable role/status a removed member's historical ownership must survive losing; a single closed `authorize()` function with a bounded `owner|admin|member|viewer` role enum plus resource/action/time-scoped `resource_grants` is the one authorization decision point every domain calls, with every Phase 7 personal-domain table hardcoded, structurally ungrantable `private` visibility (F-04's "Phase 8 roles do not grant private-vault access" made impossible, not merely undefaulted); invitations are recipient-bound by email, single-use, expiring, and re-verified against the accepting account's own email inside one row-locked transaction; revocation propagates near-instantly by construction, since no permission cache exists anywhere in this design to invalidate, measured directly in Task 3's own integration tests against the 60-second ceiling rather than merely argued to meet it. Implementation begins with Task 1 (account/membership/session framework) once Phase 7's own whole-phase review round converged with zero open findings (PR #99, merged) -- the same kind of repository-owner-directed sequencing every phase 2-7 transition has used. Implementation plan: `docs/superpowers/plans/2026-08-01-phase-8-multi-user.md`. Delivery status: `docs/phases/phase-008/IMPLEMENTATION-STATUS.md`.

## Objective

Support family and team collaboration, delegation and shared knowledge with explicit ownership, consent and least-privilege permissions.

## User value

Users coordinate shared commitments and context while knowing who owns each item, exactly what others can see and when accountability has been accepted.

## In scope

User identity expansion; workspace membership/invitations; baseline roles and resource grants; private/shared/workspace visibility; sharing review; delegation proposal/acceptance/revocation/completion; shared tasks, commitments, knowledge and plans; member notifications/activity; ownership transfer and removal.

## Out of scope

Enterprise SSO/SCIM/compliance; public communities; covert monitoring; admin access to private vaults; default sharing of personal domains; implicit delegation; unrestricted cross-workspace search; external guest federation; minors/dependents.

## Functional requirements

- Access is denied by default and evaluated server-side for every action.
- Private/personal data remains private until explicitly shared.
- Roles provide bounded baselines; explicit grants are resource/action/time scoped.
- Invitations are recipient-bound, single-use, expiring and verified at acceptance.
- Delegation transfers accountability only after recipient acceptance.
- Revocation blocks new access and background jobs promptly.
- Shared recommendations identify each required authority.
- Affected members see redacted audit/activity without private-content leakage.
- Member removal resolves ownership, active delegations, export and retention before completion.
- Concurrent shared edits use optimistic concurrency and conflict UX.

## Non-functional requirements

Authorization evaluation p95 <50 ms excluding resource query. Revocation propagates to sessions/caches/background work within 60 seconds. Invitation/delegation notification is idempotent. Multi-user core flows meet WCAG 2.2 AA. Isolation tests cover every resource/action combination.

## Architecture impact

Replace single-owner assumptions with authenticated users and workspace memberships while preserving workspace boundaries. Add centralized authorization ports used by queries, mutations, search, AI context, connectors and background jobs. Private vaults remain separate compartments.

## Data changes

Add users, memberships, invitations, roles/bindings, grants, sharing policies, delegations/events, shared collections, notifications and ownership transfers defined in `phase-008/DATA-MODEL.md`. Existing records receive explicit accountable owner/visibility migrations.

## API changes

Add workspace, invitation, membership, grant, delegation and shared-activity endpoints in `phase-008/API-SCHEMAS.md`. Workspace selection is session validated; unauthorized resource existence is hidden.

## Frontend changes

Add workspace switcher, members/invitations, permission/sharing review, delegation inbox, shared activity, ownership-transfer and member-removal flows. Every shared item shows workspace, visibility and accountable owner.

## Security and privacy

Authorization uses membership, role, explicit grant/deny, ownership, action, purpose and time. Deny/privacy rules override roles. UI hiding is not security. Background jobs re-check current authority. Admin membership management does not grant private-vault read access. Security-sensitive changes require step-up confirmation where configured.

## Observability

Measure invitation/delegation lifecycle, authorization denies by reason code, grant/revocation propagation, membership/session invalidation, ownership conflicts and background authorization failures. Never log private content, invitation secrets or broad resource IDs in unauthorized traces.

## Test strategy

Full role/resource/action matrix; authorization property tests; invitation token/replay; workspace switching; sharing/revocation; delegation lifecycle; concurrent edits; ownership transfer/removal; confused-deputy/IDOR/cache/background-job attacks; multiple-identity browser acceptance; backup/restore.

## Acceptance criteria

- Deny-by-default matrix passes every resource/action.
- Private/personal data never becomes visible through broad roles.
- Revocation meets propagation target.
- Invitation and delegation identity/replay tests pass.
- Removal cannot orphan resources or active accountability.
- AI/search/connectors/background jobs honor the same authorization.
- Multi-identity browser acceptance passes.

## Exit criteria

- Identity migration and permission contracts approved.
- Threat model and independent authorization review complete.
- All existing Phase 1–7 resources have explicit visibility/owner policy.
- Backup/restore preserves membership, grants, ownership and audit.
- Zero open Critical, High or Medium findings.
- Phase 9 can layer enterprise identity/policy without bypassing resource authorization.

## Rollback plan

Disable new invitations/sharing/delegation; revoke grants; invalidate affected sessions; preserve ownership/audit; export or transfer shared records. Authorization schema changes use forward fixes to avoid accidental widening.

## Deferred backlog

Enterprise SSO/SCIM, public/external federation, minors/dependents, cross-tenant sharing, public communities and administrator private-content access.
