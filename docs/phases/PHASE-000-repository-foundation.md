---
id: PHASE-000
title: Repository Foundation
status: Approved for Implementation
version: 1.1.0
owner: Lucky Jain
depends_on:
  - SPEC-000
  - RFC-000
  - RFC-002
  - RFC-003
  - RFC-004
  - RFC-005@1.1.0
  - STD-001
  - ADR-0001
  - ADR-0002
  - ADR-0003
  - ADR-0004
  - ADR-0005
  - ADR-0006
  - ADR-0007
  - ADR-0008
  - ADR-0009
  - ADR-0010
  - DOMAIN-MODEL
  - EVENT-CATALOG
  - API-CONTRACTS
  - PKOS-SCHEMA
  - PHASE-0-SECURITY-BASELINE
  - PHASE-0-BACKUP-RESTORE
---

# PHASE-000 — Repository Foundation

## Objective

Create a reproducible, secure and observable development foundation from which ECC can be implemented safely by humans and AI coding agents.

## User value

This phase does not add executive workflows. It ensures every later phase can be built, tested and run consistently without architectural drift.

## Accepted baseline

Implementation MUST conform to:

- [Approved Technology Registry](../RFC-005.md)
- [Accepted ADRs](../adr/README.md)
- [Canonical Domain Model](../domain/DOMAIN-MODEL.md)
- [Domain Event Catalog](../domain/EVENT-CATALOG.md)
- [Domain API Contracts](../domain/API-CONTRACTS.md)
- [PKOS Schema](../domain/PKOS-SCHEMA.md)
- [Phase 0 Security Baseline](../security/PHASE-0-SECURITY-BASELINE.md)
- [Phase 0 Backup and Restore](../operations/PHASE-0-BACKUP-RESTORE.md)

Phase 0 may create skeleton implementations of these contracts but MUST NOT introduce unapproved entities, event names, domain ownership, storage technologies or external services.

## In scope

- canonical repository layout
- backend and frontend skeletons
- one-command local startup
- exact runtime and dependency pins with lockfiles
- environment and secret handling
- formatting, linting and type checking
- unit, integration, contract and architecture test harnesses
- CI pipeline with supply-chain controls
- health, readiness, liveness and version endpoints
- structured logging and correlation identifiers
- architecture and dependency checks
- initial design-system shell
- PostgreSQL 18 and migration framework
- local owner identity and workspace isolation
- opaque server-side browser sessions
- durable in-process event-bus contract backed by PostgreSQL outbox/inbox
- PKOS repository interfaces and foundation migrations
- API contract skeletons for Phase 1
- logical backup, clean restore and integrity verification

## Out of scope

- email, calendar or task integrations
- AI recommendations or Ollama runtime
- production multi-user deployment
- autonomous actions
- executive dashboard business features
- Neo4j, Qdrant, pgvector or Redis
- NATS, Kafka or Temporal
- Kubernetes or microservice deployment
- cloud-managed services

## Functional requirements

### FR-P0-001 — Bootstrap

A new contributor MUST be able to clone the repository and start the development environment using one documented command.

### FR-P0-002 — Repository structure

The repository MUST contain backend, frontend, shared contracts, tests, scripts, infrastructure and documentation areas aligned with STD-001 and ADR-0001.

### FR-P0-003 — Health and version

Backend and frontend applications MUST expose health checks and a basic version endpoint.

### FR-P0-004 — Migrations

Database changes MUST use version-controlled migrations and enforce the workspace boundary defined in ADR-0008.

### FR-P0-005 — Configuration

All configuration MUST use typed settings with safe local defaults and no committed secrets.

### FR-P0-006 — CI quality gate

CI MUST run formatting, linting, type checks, unit tests, integration tests, architecture checks, API/event contract validation and documentation-link validation.

### FR-P0-007 — Architecture enforcement

Automated checks MUST prevent forbidden dependency directions, circular dependencies, cross-domain persistence access and unapproved technologies.

### FR-P0-008 — Correlation

Every request and event MUST carry correlation identifiers through logs and service boundaries.

### FR-P0-009 — Typed contracts

The repository MUST define typed command, query and event-envelope interfaces matching the accepted catalogs.

### FR-P0-010 — Foundation schema

The initial PostgreSQL migration MUST create workspace isolation, local identity, sessions, PKOS evidence/provenance, durable event state and migration metadata without implementing Phase 1 workflows.

### FR-P0-011 — Event delivery

The in-process event bus MUST provide at-least-once delivery, idempotent consumer support, retry state and a dead-letter path behind a replaceable interface.

### FR-P0-012 — Authentication

A local owner account and secure opaque session mechanism MUST exist. No-auth mode and browser JWT sessions are prohibited.

### FR-P0-013 — Supply-chain security

CI MUST run Gitleaks, Trivy, Syft, pip-audit and `pnpm audit` using the versions and policies in RFC-005.

### FR-P0-014 — Backup and restore

The repository MUST provide non-interactive logical backup, restore and integrity-verification commands conforming to the Phase 0 backup specification.

## Non-functional requirements

- clean-checkout startup succeeds on macOS and Linux
- startup completes without cloud dependencies
- only PostgreSQL is required as shared Phase 0 infrastructure
- direct dependencies and tool versions match RFC-005 exactly
- `uv.lock` and `pnpm-lock.yaml` are committed
- Docker images and GitHub Actions are immutable-pinned
- tests are deterministic and isolated
- all persisted records include `workspace_id`
- source evidence checksums are verifiable
- migrations are forward-tested and rollback-documented
- foundation restore completes within 10 minutes on a supported developer machine

## Required deliverables

```text
backend/
  identity/
  platform/events/
  platform/configuration/
  platform/observability/
  knowledge/contracts/
  knowledge/infrastructure/
frontend/
packages/contracts/
tests/
scripts/
infrastructure/
docker/
.github/
```

Required repository artifacts include:

- `.python-version`
- `.node-version`
- `uv.lock`
- `pnpm-lock.yaml`
- pinned Docker Compose configuration
- migration configuration
- CI workflows
- generated OpenAPI schema
- generated SBOM artifact
- backup and restore scripts

## Security and privacy

All Phase 0 security controls are governed by the [Phase 0 Security Baseline](../security/PHASE-0-SECURITY-BASELINE.md). In particular:

- Argon2id password hashing
- opaque server-side sessions
- CSRF protection
- secure cookie attributes
- workspace isolation tests
- secret and vulnerability scanning
- no sensitive logging

## Observability

- structured JSON logging
- health, readiness and liveness endpoints
- request duration and error metrics
- startup and migration diagnostics
- event publish, consume, retry and dead-letter metrics
- correlation, causation, request and event identifiers

## Test strategy

- backend and frontend smoke tests
- migration and rollback-documentation tests
- typed configuration tests
- health and version endpoint tests
- architecture and technology allowlist tests
- clean-checkout bootstrap test in CI
- password, session, CSRF and cookie tests
- workspace-isolation tests
- event idempotency, retry and dead-letter tests
- PKOS checksum and provenance tests
- API and event schema tests
- secret-scanner fixture test
- vulnerability and SBOM workflow tests
- backup, clean restore and integrity tests

## Acceptance criteria

- the documented setup command installs exact approved dependencies
- the documented development command starts the local stack
- PostgreSQL is the only required shared infrastructure service
- all quality, test, architecture, security and documentation checks pass
- manifests contain no floating versions
- lockfiles reproduce on macOS and Linux
- no unapproved Phase 0 technology appears in manifests or Compose
- initial migrations create the approved foundation schema
- authentication behavior matches ADR-0008
- event-envelope and API schemas match their catalogs
- architecture tests enforce domain and workspace ownership
- Gitleaks, Trivy, Syft, pip-audit and `pnpm audit` complete successfully
- a logical backup is checksummed, restored into a clean PostgreSQL 18 database and verified
- README startup instructions are verified from a clean checkout

## Exit criteria

Phase 0 is complete only when:

- the repository can be reproduced from scratch
- all required CI checks pass on `main`
- accepted ADRs are reflected in code and configuration
- domain, event, API and PKOS contract tests pass
- security baseline evidence is attached to the exit review
- SBOM and vulnerability scan artifacts are available
- clean backup and restore succeeds within the documented target
- Phase 1 can start without introducing new foundation architecture

## Rollback plan

Every tooling, schema or structural change must be independently reversible. Irreversible migrations, new technologies or ownership changes require an ADR before implementation.

## Deferred backlog

- production deployment
- multi-user authentication and SSO
- cloud infrastructure
- connector implementations
- AI model runtime
- dedicated graph, vector, cache or distributed messaging infrastructure
