---
id: ADR-0008
title: Authentication and Workspace Identity
status: Accepted
date: 2026-07-13
owners: [Lucky Jain]
related: [RFC-004, PHASE-000]
---

# ADR-0008 — Authentication and Workspace Identity

## Context
ECC begins as a personal local-first system but must not embed assumptions that prevent later family, team or enterprise use.

## Decision
Phase 0 uses a local workspace identity with one owner account. Authentication uses secure local sessions, password hashing and encrypted secrets. Every persisted record carries `workspace_id`; user-owned records also carry `owner_id`. Authorization is enforced at application boundaries even in single-user mode.

External OAuth credentials are connector secrets, not ECC login credentials.

## Consequences
- Single-user setup remains simple.
- Multi-user boundaries are present from the start.
- Tests must verify workspace isolation.
- Enterprise SSO is deferred behind the same identity contracts.

## Alternatives considered
No-auth local mode was rejected because it creates unsafe defaults and makes later isolation expensive.
