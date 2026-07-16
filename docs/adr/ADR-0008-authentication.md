---
id: ADR-0008
title: Authentication and Workspace Identity
status: Accepted
date: 2026-07-13
owners: [Lucky Jain]
related: [RFC-004, RFC-005, PHASE-000]
---

# ADR-0008 — Authentication and Workspace Identity

## Context

ECC begins as a personal local-first system but must not embed assumptions that prevent later family, team or enterprise use. A no-auth local mode would create unsafe defaults, weaken testing and make later isolation expensive.

## Decision

### Identity

- Phase 0 creates one local owner account per installation.
- Every persisted record carries `workspace_id`.
- User-owned records additionally carry `owner_id`.
- Authorization is enforced at API, application and repository boundaries even in single-user mode.

### Passwords

- Passwords are hashed with Argon2id through the approved `argon2-cffi` dependency.
- Password hashes, recovery material and connector credentials are never logged.
- Authentication responses do not reveal whether an account exists.

### Browser sessions

- ECC uses opaque, random, server-side sessions persisted in PostgreSQL.
- JWT is not used for Phase 0 browser authentication.
- Session identifiers are stored only in cookies with `HttpOnly` and `SameSite=Lax`.
- `Secure` is mandatory outside explicit localhost HTTP development.
- State-changing browser requests require CSRF validation.
- Sessions expire after 12 hours of inactivity and after 7 days absolutely.
- Session identifiers rotate after login and privilege-sensitive operations.
- Logout revokes the server-side session immediately.

### External authorization

OAuth2 is reserved for connector authorization. External OAuth credentials are connector secrets and are never used as ECC login credentials.

## Consequences

- Single-user setup remains simple without weakening future isolation.
- Local sessions can be revoked centrally.
- Tests must verify workspace isolation, CSRF behavior, cookie flags, session rotation and expiration.
- Enterprise SSO can later be introduced behind the same identity and authorization contracts.

## Rejected alternatives

- **No-auth local mode:** rejected because it creates an unsafe default.
- **JWT browser sessions:** rejected because revocation and rotation add unnecessary complexity for a local modular monolith.
- **OAuth-only login:** rejected because ECC must remain usable without an external identity provider.
