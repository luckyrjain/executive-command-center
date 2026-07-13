---
id: PHASE-000
title: Repository Foundation
status: Ready for Implementation
version: 1.0.0
owner: Lucky Jain
depends_on:
  - SPEC-000
  - RFC-000
  - RFC-002
  - RFC-003
  - RFC-004
  - RFC-005
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
---

# PHASE-000 — Repository Foundation

## Objective

Create a reproducible, secure and observable development foundation from which ECC can be implemented safely by humans and AI coding agents.

## User value

This phase does not add executive workflows. It ensures every later phase can be built, tested and run consistently without architectural drift.

## Accepted architecture baseline

Implementation MUST conform to:

- [Canonical Domain Model](../domain/DOMAIN-MODEL.md)
- [Domain Event Catalog](../domain/EVENT-CATALOG.md)
- [Domain API Contracts](../domain/API-CONTRACTS.md)
- [PKOS Schema](../domain/PKOS-SCHEMA.md)
- [Accepted ADRs](../adr/README.md)

Phase 0 may create skeleton implementations of these contracts but MUST NOT introduce unapproved entities, event names, domain ownership or storage technologies.

## In scope

- canonical repository layout
- backend and frontend skeletons
- one-command local startup
- environment and secret handling
- linting, formatting and type checking
- unit and integration test harnesses
- CI pipeline
- health endpoints
- structured logging and correlation IDs
- architecture and dependency checks
- initial design-system shell
- local database and migration framework
- workspace and owner identity skeleton
- event envelope and in-process durable event-bus contract
- PKOS repository interfaces and initial migrations
- API contract skeletons for Phase 1

## Out of scope

- email, calendar or task integrations
- AI recommendations
- production multi-user deployment
- autonomous actions
- executive dashboard business features
- dedicated graph database
- Kubernetes or microservice deployment

## Functional requirements

### FR-P0-001

A new contributor MUST be able to clone the repository and start the development environment using one documented command.

### FR-P0-002

The repository MUST contain backend, frontend, shared contracts, tests, scripts, infrastructure and documentation areas aligned with STD-001 and ADR-0001.

### FR-P0-003

Backend and frontend applications MUST expose health checks and a basic version endpoint.

### FR-P0-004

Database schema changes MUST use version-controlled migrations and include the workspace boundary defined in ADR-0008.

### FR-P0-005

All configuration MUST be provided through typed settings with safe development defaults and no committed secrets.

### FR-P0-006

CI MUST run formatting, linting, type checks, unit tests, integration tests and documentation-link validation.

### FR-P0-007

Architecture checks MUST prevent forbidden dependency directions, circular dependencies and cross-domain persistence access.

### FR-P0-008

Every request and event MUST carry correlation identifiers through logs and service boundaries.

### FR-P0-009

The repository MUST define typed command, query and event-envelope interfaces matching the accepted API and event catalogs.

### FR-P0-010

The initial PostgreSQL migration MUST create workspace isolation, PKOS evidence/provenance foundations and migration metadata without implementing Phase 1 business workflows.

### FR-P0-011

An in-process event bus MUST provide at-least-once delivery semantics, idempotent consumer support and a dead-letter path behind a replaceable interface.

### FR-P0-012

A local owner account and secure session mechanism MUST exist; no-auth mode is prohibited.

## Non-functional requirements

- clean checkout startup succeeds on macOS and Linux
- no production secret is required for local startup
- local startup completes without cloud dependencies
- tests are deterministic and isolated
- dependency versions are pinned according to RFC-005
- all persisted records include `workspace_id`
- source evidence checksums are verifiable
- migrations are forward-tested and rollback-documented

## Deliverables

```text
backend/
frontend/
packages/
tests/
scripts/
infrastructure/
docker/
.github/
```

Required foundation modules:

```text
backend/identity/
backend/platform/events/
backend/platform/configuration/
backend/platform/observability/
backend/knowledge/contracts/
backend/knowledge/infrastructure/
packages/contracts/
```

The exact structure remains governed by STD-001 and ADR-0001.

## Security and privacy

- `.env` files are ignored
- sample configuration contains placeholders only
- dependency and secret scanning run in CI
- local data directories are excluded from version control
- logs must not contain secrets, tokens, prompt context or PII
- passwords use an approved adaptive password hash
- connector secrets are stored separately from user authentication
- workspace isolation is tested at API and repository boundaries

## Observability

- structured JSON logging
- health, readiness and liveness endpoints
- request duration and error metrics
- startup and migration diagnostics
- event publish, consume, retry and dead-letter metrics
- correlation, causation and request IDs

## Test strategy

- backend smoke test
- frontend render smoke test
- migration test
- configuration validation test
- health endpoint test
- architecture rule test
- clean-checkout bootstrap test in CI
- workspace isolation test
- event idempotency and dead-letter tests
- PKOS evidence checksum and provenance tests
- API contract schema tests

## Acceptance criteria

- `make setup` or the documented equivalent installs required project dependencies
- `make dev` or the documented equivalent starts the local stack
- `make test` passes all tests
- `make check` passes linting, formatting, typing, architecture and documentation validation
- no secrets exist in repository history introduced by this phase
- README startup instructions are verified from a clean checkout
- initial migrations create the approved foundation schema
- event-envelope contract matches `EVENT-CATALOG.md`
- API schemas match `API-CONTRACTS.md`
- architecture tests enforce domain ownership from `DOMAIN-MODEL.md`
- backup and restore of the empty/foundation database is verified

## Exit criteria

Phase 0 is complete only when:

- the repository can be reproduced from scratch
- all CI checks pass on `main`
- the accepted ADRs are reflected in code and deployment configuration
- domain, event, API and PKOS contract tests pass
- a clean local backup and restore succeeds
- Phase 1 can begin without introducing new foundation architecture

## Rollback plan

Every tooling, schema or structural change must be independently reversible. Changes that introduce irreversible migrations, new technologies or ownership changes require an ADR.

## Deferred backlog

- production deployment
- multi-user authentication and SSO
- cloud infrastructure
- connector implementations
- AI model runtime implementation
- dedicated graph/search infrastructure
