---
id: PHASE-008
title: Multi-user Workspaces
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on: [PHASE-007]
contracts:
  - phase-008/DATA-MODEL.md
  - phase-008/API-SCHEMAS.md
  - phase-008/PERMISSION-CONTRACT.md
  - phase-008/DELEGATION-CONTRACT.md
  - phase-008/UX-STATES.md
  - phase-008/TEST-PLAN.md
---

# PHASE-008 — Multi-user Workspaces

## Objective

Support family and team collaboration, delegation and shared knowledge with explicit ownership, consent and least-privilege permissions.

## In scope

Workspace membership and invitations; roles and resource grants; personal/private/shared scopes; delegation and acceptance; shared tasks, commitments, knowledge and plans; activity/audit; notifications; data transfer and member removal.

## Out of scope

Enterprise SSO/compliance, public communities, covert monitoring, administrator access to private vaults, default sharing of personal domains, implicit delegation and unrestricted cross-workspace search.

## Requirements

- Personal/private data stays private unless explicitly shared.
- Access is denied by default and evaluated server-side on every read/write.
- Roles provide baselines; resource grants handle exceptions.
- Invitations expire and require verified recipient acceptance.
- Delegation requires recipient acceptance before accountability transfers.
- Revocation takes effect immediately for new access and background jobs.
- Shared recommendations identify whose authority is required.
- Audit events are visible to affected members without leaking private content.
- Member removal defines ownership transfer, export and retention outcomes.

## Exit criteria

Approved identity/permission/delegation contracts; authorization matrix and property tests; invitation/removal flows; audit and notification tests; conflict/offline behavior; backup/restore; browser acceptance; zero Critical/High/Medium findings.

## Rollback

Disable new invitations/sharing; revoke grants; preserve authoritative ownership and audit; export shared content; revert through forward fixes without exposing private records.
