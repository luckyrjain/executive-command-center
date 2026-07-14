# Executive Command Center

> A local-first AI Executive Operating System.

Executive Command Center (ECC) is a specification-driven platform for helping executives manage attention, context, commitments, decisions, knowledge, meetings and execution.

## Current status

**Foundation specification:** Phase 0 baseline approved  
**Implementation:** Phase 1 task and commitment vertical slices implemented  
**Current delivery phase:** Phase 1 — Executive Dashboard MVP

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
9. [Canonical Domain Model](docs/domain/DOMAIN-MODEL.md)
10. [PKOS Schema](docs/domain/PKOS-SCHEMA.md)
11. [Domain Event Catalog](docs/domain/EVENT-CATALOG.md)
12. [Domain API Contracts](docs/domain/API-CONTRACTS.md)
13. [Phase 0 Security Baseline](docs/security/PHASE-0-SECURITY-BASELINE.md)
14. [Phase 0 Backup and Restore](docs/operations/PHASE-0-BACKUP-RESTORE.md)
15. [PHASE-000 — Repository Foundation](docs/phases/PHASE-000-repository-foundation.md)
16. [PHASE-001 — Executive Dashboard MVP](docs/phases/PHASE-001-executive-dashboard-mvp.md)
17. [Roadmap](docs/ROADMAP.md)
18. [Contributing](docs/CONTRIBUTING.md)

## Architecture decisions

The accepted Phase 0 decisions are recorded under [`docs/adr`](docs/adr/):

- ADR-0001 — Repository Layout
- ADR-0002 — Local-First Architecture
- ADR-0003 — Knowledge Platform and PKOS
- ADR-0004 — AI Runtime
- ADR-0005 — Event Bus
- ADR-0006 — Storage Strategy
- ADR-0007 — Model Router
- ADR-0008 — Authentication and Workspace Identity
- ADR-0009 — Connector Synchronization
- ADR-0010 — Deployment Strategy

## Phase 0 technology boundary

Phase 0 uses a React web application, a FastAPI modular monolith and PostgreSQL 18. Neo4j, Qdrant, Redis, distributed messaging, Kubernetes, cloud services and the AI model runtime are explicitly deferred.

All direct dependency, runtime, scanner and container versions are pinned in [RFC-005](docs/RFC-005.md). Lockfiles remain the source of truth for transitive dependency resolution.

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
