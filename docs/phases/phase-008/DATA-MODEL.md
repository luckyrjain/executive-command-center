---
id: PHASE-008-DATA-MODEL
title: Phase 8 Multi-user Data Model
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 8 Data Model

Core records: `accounts` (new, workspace-independent identity: `id`, `email` globally unique, `password_hash`, `display_name`, `created_at`, `disabled_at`), `users` (existing, unchanged FK role -- gains `account_id`, loses `email`/`password_hash`), `workspace_memberships` (new: `id`, `workspace_id`, `account_id`, `users_id`, `role`, `status`, `invited_by`, `created_at`, `updated_at`, `removed_at`), `invitations`, `resource_grants`, `delegations`, `delegation_events`, `member_notifications` and `ownership_transfers`. `roles` is a closed application-level enum (`owner|admin|member|viewer`), not a database table -- see `docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md` Decision 2 for why a bounded, enumerable role set is a requirement, not an implementation shortcut.

Resources retain workspace and accountable owner. Visibility is `private|shared_explicitly|workspace`; every Phase 7 personal-domain table is hardcoded, structurally ungrantable `private` -- not merely defaulted private, but rejected at `resource_grants` write time by an explicit `_UNGRANTABLE_RESOURCE_TYPES` check, so no role or grant can ever make one workspace-visible. Every other existing table (Phase 1's four `owner_id` tables plus the ~60 tables that previously had no `owner_id` at all) defaults `visibility = 'workspace'`, backfilled `owner_id` = the workspace's original sole `users` row. Grants name subject, resource/scope, actions and expiry. Delegation history is append-only. Membership removal never deletes the `users` FK anchor (so historical `owner_id`/`created_by` attribution survives), only sets `workspace_memberships.status = 'removed'` -- the split between `users` (stable FK anchor) and `workspace_memberships` (mutable role/status) is what makes removal-without-orphaning possible; see the design doc's Decision 1 for the full reasoning.

## Task 1 status

**Shipped.** Migration `0061_phase8_accounts_memberships.py` creates `accounts` and `workspace_memberships` exactly as described above and backfills every pre-existing `users` row: one new `accounts` row per `users` row (not deduplicated by email -- collapsing two distinct `users` rows that happen to share an email string would silently merge two identities, a real security bug), `users.account_id` populated and made `NOT NULL`, `users.email`/`users.password_hash` dropped (moved to `accounts`, case-folded), and one `active`/`owner`-role `workspace_memberships` row per `users` row (self-invited, matching how `scripts/bootstrap_dev.py`'s dev-only flow already treated a workspace's sole user as its de facto owner). Verified against real local Postgres: full `upgrade head` / `downgrade -1` / `upgrade head` round-trip, schema and backfilled data confirmed correct by direct inspection, and all ~14 pre-existing composite `owner_id`-style FKs to `users.(workspace_id, id)` confirmed completely unaffected.

`invitations`, `resource_grants`, `delegations`, `delegation_events`, `member_notifications` and `ownership_transfers` are not part of this migration -- Task 1's scope is the identity/membership/session framework only, matching the implementation plan's "framework first, one reference slice, then breadth" sequencing (Phase 7 Task 1's identical precedent). Every other existing table's `visibility`/`owner_id` migration is Task 3/4 scope, also not part of this migration.

## Task 2 status

**Shipped.** Migration `0062_phase8_invitations.py` creates `invitations` (`id`, `workspace_id`, `email`, `role`, `token_hash` unique, `invited_by`, `expires_at`, `accepted_at`, `rejected_at`, `revoked_at`, `created_at`). `accepted_at`/`rejected_at`/`revoked_at` are three separate nullable columns, not one `status` enum, with a `CHECK` constraint enforcing at most one is ever set -- verified directly against real Postgres (setting two simultaneously raises `ck_invitations_terminal_states_mutually_exclusive`). No partial unique index enforces "at most one pending invitation per `(workspace_id, email)`" -- the design doc's own "pending" definition includes `expires_at > now()`, which cannot be an immutable partial-index predicate; `ecc.domains.identity.invitations.create_invitation_endpoint` enforces it procedurally instead, via `pg_advisory_xact_lock` on the `(workspace_id, email)` pair itself (not `SELECT ... FOR UPDATE` alone, which locks only *existing* rows and would serialize nothing for a brand-new recipient -- an adversarial review round on this PR caught that gap before merge) -- verified directly against real Postgres with two concurrent transactions racing for the same lock (exactly one proceeds, confirmed by direct threading test, not merely asserted).

`resource_grants`, `delegations`, `delegation_events`, `member_notifications` and `ownership_transfers` remain out of scope, Task 3/6/7/8's own work respectively.
