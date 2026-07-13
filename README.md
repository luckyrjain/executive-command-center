# Executive Command Center

> A local-first AI Executive Operating System.

Executive Command Center (ECC) is a specification-driven platform for helping executives manage attention, context, commitments, decisions, knowledge, meetings and execution.

## Current status

**Foundation specification:** Draft — under review  
**Implementation:** Not started  
**Current delivery phase:** Phase 0 — Repository Foundation

## Start here

Read the documents in this order:

1. [SPEC-000 — Constitution](docs/specifications/SPEC-000.md)
2. [RFC-000 — Specification Governance](docs/RFC-000.md)
3. [RFC-001 — Product Definition](docs/RFC-001.md)
4. [RFC-002 — Engineering Philosophy](docs/RFC-002.md)
5. [RFC-003 — Design Principles](docs/RFC-003.md)
6. [RFC-004 — System Architecture](docs/RFC-004.md)
7. [RFC-005 — Approved Technology Registry](docs/RFC-005.md)
8. [STD-001 — Repository Standards](docs/standards/STD-001.md)
9. [Roadmap](docs/ROADMAP.md)
10. [Contributing](docs/CONTRIBUTING.md)

## Architecture chapters

RFC-004 is split into independently reviewable chapters under [`docs/architecture`](docs/architecture/):

- Architectural vision and system context
- Core platform and service architecture
- Runtime, deployment and operations
- AI runtime
- Knowledge platform and memory
- Human attention engine
- Connector and integration platform
- Frontend and executive experience
- Data platform
- Security, privacy and local-first architecture
- Platform operations

## Governance support

- [ADR process](docs/adr/README.md)
- [RFC template](docs/templates/RFC-TEMPLATE.md)
- [ADR template](docs/templates/ADR-TEMPLATE.md)
- [Standard template](docs/templates/STD-TEMPLATE.md)
- [Phase template](docs/templates/PHASE-TEMPLATE.md)
- [Specification change request](docs/templates/SPEC-CHANGE-REQUEST.md)

## Phase specifications

- [PHASE-000 — Repository Foundation](docs/phases/PHASE-000-repository-foundation.md)
- [PHASE-001 — Executive Dashboard MVP](docs/phases/PHASE-001-executive-dashboard-mvp.md)

## Repository rule

> If a capability is not documented in the current phase specification, it does not get implemented.

Every behavior-changing change must include its specification, implementation, tests and documentation in the same pull request.
