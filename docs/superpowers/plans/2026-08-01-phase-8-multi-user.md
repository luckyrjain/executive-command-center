# Phase 8 Multi-user Workspaces Implementation Plan

Companion to `docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md` (the decisions) -- this document is the task sequence and per-task scope, mirroring the shape of `docs/superpowers/plans/2026-07-31-phase-7-personal-intelligence.md`'s own task breakdown.

## Task 1 — Account/membership/session framework (this activation)

- Migration: `accounts` (new: `id`, `email` globally unique, `password_hash`, `display_name`, `created_at`, `disabled_at`); `users` gains `account_id` (FK to `accounts.id`, `UNIQUE(workspace_id, account_id)`), loses `email`/`password_hash` (moved to `accounts`, not duplicated); `workspace_memberships` (new: `id`, `workspace_id`, `account_id`, `users_id`, `role`, `status`, `invited_by`, `created_at`, `updated_at`, `removed_at`). Backfill: every existing `users` row gets a matching `accounts` row (from its current `email`/`password_hash`) and an `active`/`owner` `workspace_memberships` row.
- `ecc.platform.accounts` (new `platform/` package per `STD-001`'s layout rule -- this is cross-domain infrastructure, not one domain's own concern): Argon2id hashing, `POST /accounts` (invitation-token-gated -- the token itself is Task 2's own table, so Task 1 ships the endpoint shape accepting an opaque token parameter it does not yet validate against a real `invitations` row; see Task 2), `POST /auth/login`, `POST /auth/select-workspace`, reusing `backend/ecc/auth.py`'s existing `ecc_session`/`ecc_csrf` cookie mechanism unchanged in shape.
- `GET /workspaces` (every workspace the authenticated account has an `active` membership in), `GET|PATCH /workspaces/{id}` (`PATCH` requires `owner`/`admin` -- enforced directly against `workspace_memberships.role` in this task, since Task 3's general `authz.py` does not exist yet; this direct check is replaced by a call into the general mechanism once Task 3 lands, not left as a second, divergent authorization path).
- Tests: account creation/login/logout, multi-workspace session selection (an account with two `active` memberships sees both from `GET /workspaces` and can select either), migration backfill round-trip (upgrade/downgrade against real Postgres), cross-account isolation (account A cannot select a workspace it has no active membership in).

## Task 2 — Invitations

- Migration: `invitations` (`id`, `workspace_id`, `email`, `role`, `token_hash`, `invited_by`, `expires_at`, `accepted_at`, `rejected_at`, `revoked_at`, `created_at`).
- `ecc.platform.invitations`: `POST /workspaces/{id}/invitations` (`owner`/`admin` only; rejects an already-active member or a second concurrently-pending invitation for the same email), `GET /workspaces/{id}/invitations`, `POST /invitations/{id}/accept|reject` (both independently re-verify token hash, expiry, terminal-state, and -- for accept -- the authenticated account's own email against `invitations.email`, inside one row-locked transaction), `POST /invitations/{id}/revoke` (inviter/admin). `POST /accounts` (Task 1) is wired to actually validate its token parameter against a real, unaccepted, unexpired `invitations` row now that this table exists.
- Tests: accept/reject/revoke lifecycle, duplicate-invitation-for-same-email rejection, expired-token rejection, wrong-account-email-at-accept rejection, concurrent-accept-of-same-token race (only one of two simultaneous accepts succeeds), cross-workspace isolation.

## Task 3 — Authorization engine, schema-wide visibility/owner migration, and `engineering` as the reference domain

- Migration: `roles` is a closed application-level enum (`owner|admin|member|viewer`), not a database table with rows (fixed baseline, not customizable -- see the design doc's own reasoning); `resource_grants` (`id`, `workspace_id`, `grantee_account_id`, `resource_type`, `resource_id`, `actions`, `granted_by`, `expires_at`, `revoked_at`, `created_at`). Add `owner_id`/`visibility` to every table currently missing them (~60 tables across engineering, knowledge, attention, automation, AI runtime, calendar/scheduling), backfilled `owner_id` = the workspace's original sole `users` row, `visibility = 'workspace'`. The 14 tables that already have `owner_id` (Phase 1 + Phase 7) gain `visibility` only, backfilled `'workspace'` for Phase 1's four tables and hardcoded, ungrantable `'private'` for every Phase 7 personal-domain table (see design doc Decision 2, item 3).
- `ecc.platform.authz`: `authorize(auth, resource_type, resource_id, action) -> bool`, the six-step decision the design doc's Decision 2 names exactly, including the `_UNGRANTABLE_RESOURCE_TYPES` write-time rejection for Phase 7 tables. `GET|POST /sharing/grants`, `DELETE /sharing/grants/{id}` (Task 5 is the UI/broader-product surface for this; Task 3 ships the mechanism and its own direct API only).
- Wire `authorize()` into every `engineering` domain endpoint end to end (list/get/mutate), replacing that domain's existing bare `workspace_id`-only scoping -- the one reference domain proving the mechanism before Task 4 repeats it everywhere else.
- Tests: the full role x resource x action matrix for `engineering` specifically (per `TEST-PLAN.md`'s own requirement), IDOR/confused-deputy attempts (requesting another workspace's or another account's resource by guessing an id), the revocation-propagation integration test the design doc's Decision 4 names (revoke a grant/membership, assert the very next request denies, no sleep), a background-job re-check test (revoke mid-job, assert the job's next step denies), and a dedicated test proving no `resource_grants` row can ever be created against a Phase 7 personal-domain `resource_type`.

## Task 4 — Widen authorization to every remaining domain

- Mechanical repetition of Task 3's proven `authorize()` wiring across knowledge, attention, automation, AI runtime and calendar/scheduling endpoints -- no new mechanism, matching Phase 7 Task 2/3's own "no new mechanism needed" precedent once a framework is proven.
- Tests: the same role x resource x action matrix, IDOR and revocation-propagation test shapes as Task 3, run per remaining domain (not re-designed, re-applied).

## Task 5 — Sharing

- Frontend + any remaining API surface (`GET|POST /sharing/grants` shipped in Task 3; this task is the sharing-review UX and the "resource responses expose effective permissions" requirement `API-SCHEMAS.md` names): a sharing-review screen that previews exactly what becomes visible to whom before a grant is created (`PHASE-008-multi-user.md`'s own "Sharing review" scope item and `UX-STATES.md`'s "Sharing previews exactly what becomes visible" requirement), and every shared-resource response surfaces its own effective visibility/grants so a viewer always knows why they can see it.
- Tests: sharing-review preview accuracy, grant creation/revocation through the UI, effective-permissions surfaced correctly in API responses.

## Task 6 — Delegation

- Migration: `delegations` (`id`, `workspace_id`, `delegator_account_id`, `recipient_account_id`, `obligation_type`, `obligation_resource_id`, `expected_outcome`, `due_at`, `status`, `created_at`, `updated_at`), `delegation_events` (append-only history: `id`, `delegation_id`, `event_type`, `actor_account_id`, `occurred_at`, `detail`).
- `ecc.domains.collaboration.delegations` (or equivalent): `GET|POST /delegations`, `POST /delegations/{id}/accept|reject|revoke|complete`. State machine exactly as `DELEGATION-CONTRACT.md` names: `proposed -> accepted|rejected|expired`; `accepted -> completed|revoked|cancelled`. Acceptance creates scoped, read-only `resource_grants` rows for exactly the evidence the delegation names (never broader), auto-revoked when the delegation reaches any terminal state.
- Tests: full lifecycle, evidence-grant scoping (recipient can read exactly the named evidence, nothing else, verified by attempting to read an adjacent un-named resource), auto-revocation of evidence grants on completion/revocation/cancellation, idempotent notifications.

## Task 7 — Notifications and shared activity

- Migration: `member_notifications` (`id`, `workspace_id`, `account_id`, `notification_type`, `resource_ref`, `read_at`, `created_at`).
- `GET /shared/activity` (redacted audit/activity feed per `PERMISSION-CONTRACT.md`'s "Affected members see redacted audit/activity without private-content leakage" -- built on `authz.py`'s existing visibility rules, not a separate exemption).
- Tests: notification idempotency (a duplicate underlying event does not double-notify), redaction (a `private`-visibility resource's activity never appears in another member's feed even in redacted form).

## Task 8 — Ownership transfer and member removal

- Migration: `ownership_transfers` (`id`, `workspace_id`, `resource_type`, `resource_id`, `from_account_id`, `to_account_id`, `status`, `initiated_by`, `created_at`, `completed_at`).
- Member removal flow: resolves active delegations (auto-reject/expire pending ones the removed member proposed or was recipient of), reassigns or blocks removal pending ownership resolution for records only they own, offers export before finalizing, sets `workspace_memberships.status = 'removed'` (never deletes the `users` row, per Decision 1).
- Tests: removal blocked while unresolved sole-ownership exists, removal proceeds after transfer/export, active delegations correctly resolved on removal, historical `owner_id`/`created_by` attribution survives removal unchanged.

## Task 9 — Executive UX and multi-identity browser acceptance

- Frontend: workspace switcher, members/invitations panel, sharing-review screen (Task 5's own UI, wired in here alongside the rest), delegation inbox, shared activity feed, ownership-transfer and member-removal flows -- mirroring `PersonalWorkspace`'s own domain-generic shell pattern where the underlying resource type allows it.
- Every `UX-STATES.md`-required state covered or honestly disclosed as not implemented, matching Phase 7 Task 8's own discipline.
- Playwright multi-identity acceptance: two real accounts/sessions in one test, covering invite -> accept -> share -> revoke -> delegate -> remove end to end, with `@axe-core/playwright` accessibility checks, matching `personal-domain-lifecycle.mjs`'s own shape but exercising two distinct authenticated identities instead of one.
