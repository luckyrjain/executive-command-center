# Architecture Decision Records

Architecture Decision Records capture decisions that materially affect system structure, technology, data ownership, security boundaries, deployment or long-term maintainability.

## Accepted Phase 0 decisions

1. [ADR-0001 — Repository Layout](ADR-0001-repository-layout.md)
2. [ADR-0002 — Local-First Architecture](ADR-0002-local-first-architecture.md)
3. [ADR-0003 — Knowledge Platform and PKOS](ADR-0003-knowledge-platform-pkos.md)
4. [ADR-0004 — AI Runtime](ADR-0004-ai-runtime.md)
5. [ADR-0005 — Event Bus](ADR-0005-event-bus.md)
6. [ADR-0006 — Storage Strategy](ADR-0006-storage-strategy.md)
7. [ADR-0007 — Model Router](ADR-0007-model-router.md)
8. [ADR-0008 — Authentication and Workspace Identity](ADR-0008-authentication.md)
9. [ADR-0009 — Connector Synchronization](ADR-0009-synchronization.md)
10. [ADR-0010 — Deployment Strategy](ADR-0010-deployment-strategy.md)

## Naming

```text
ADR-0001-short-kebab-case-title.md
```

Numbers are sequential and never reused.

## Status

- Proposed
- Accepted
- Superseded
- Rejected
- Deprecated

## When an ADR is required

Create an ADR when a change:

- introduces or replaces a technology
- changes domain or data ownership
- changes an architectural boundary
- creates a new top-level repository directory
- changes a security or privacy boundary
- introduces an irreversible migration
- intentionally deviates from an RFC or standard

## Process

1. Copy [`ADR-TEMPLATE.md`](../templates/ADR-TEMPLATE.md).
2. Describe the context, constraints and alternatives.
3. Record the decision and consequences.
4. Link the affected RFCs, standards and phase specifications.
5. Obtain approval before implementation.
6. Update the status after review.

ADRs refine RFCs. They must not silently contradict them.
