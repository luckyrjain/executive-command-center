# Architecture Decision Records

Architecture Decision Records capture decisions that materially affect system structure, technology, data ownership, security boundaries, deployment or long-term maintainability.

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
