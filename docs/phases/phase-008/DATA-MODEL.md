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
