---
id: PHASE-008-DATA-MODEL
title: Phase 8 Multi-user Data Model
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 8 Data Model

Core records: `users`, `workspace_memberships`, `invitations`, `roles`, `role_bindings`, `resource_grants`, `sharing_policies`, `delegations`, `delegation_events`, `shared_collections`, `member_notifications` and `ownership_transfers`.

Resources retain workspace and accountable owner. Visibility is `private|shared_explicitly|workspace`; personal-domain records default private and cannot become workspace-visible through a broad role. Grants name subject, resource/scope, actions and expiry. Delegation history is append-only. Membership removal cannot orphan authoritative records.
