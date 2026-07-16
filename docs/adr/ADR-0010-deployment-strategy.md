---
id: ADR-0010
title: Deployment Strategy
status: Accepted
date: 2026-07-13
owners: [Lucky Jain]
related: [RFC-004, PHASE-000]
---

# ADR-0010 — Deployment Strategy

## Context
ECC must run reliably on a developer laptop first while preserving a path to hosted and enterprise deployments.

## Decision
Phase 0 ships as a containerized modular monolith with separate frontend, backend, worker, PostgreSQL and optional Ollama processes orchestrated through Docker Compose. Domain boundaries remain internal contracts, not network boundaries.

A production deployment may scale processes independently. Microservice extraction requires measured need and a new ADR.

The system must support one-command startup, health checks, migrations, backup and restore.

## Consequences
- Local setup and debugging remain simple.
- Operational overhead is minimized.
- Domain boundaries must be enforced through code and tests rather than network separation.
- Deployment infrastructure can evolve without rewriting domain logic.

## Alternatives considered
Kubernetes and independently deployed microservices in Phase 0 were rejected as premature complexity.
