# Executive Command Center

> A local-first AI Executive Operating System.

Executive Command Center (ECC) is a specification-driven platform for helping executives manage attention, context, commitments, decisions, knowledge, meetings and execution.

## Current status

**Foundation specification:** Phase 0 baseline approved  
**Implementation:** Phase 1 backend vertical slices implemented through Recommendations and confirmation execution  
**Current delivery phase:** Phase 1 — Executive Dashboard MVP

## Quick start

The recommended development workflow runs PostgreSQL in Docker and the backend/frontend locally.

```bash
git clone https://github.com/luckyrjain/executive-command-center.git
cd executive-command-center
cp .env.example .env
```

Set a random `ECC_SESSION_SECRET` of at least 32 characters in `.env`, then run:

```bash
docker compose up -d postgres
uv sync --frozen --all-groups --python 3.14
set -a; source .env; set +a
uv run alembic -c backend/alembic.ini upgrade head
uv run python scripts/bootstrap_dev.py
```

Start the backend:

```bash
uv run uvicorn ecc.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Start the frontend in another terminal:

```bash
corepack enable
corepack prepare pnpm@10.12.4 --activate
pnpm install --frozen-lockfile
pnpm --filter @ecc/frontend dev
```

Open `http://localhost:5173`, paste the two cookie commands printed by `scripts/bootstrap_dev.py` into the browser developer console, and reload.

For prerequisites, Docker usage, testing, troubleshooting, reset instructions, and first-use guidance, read [Setup and Usage](docs/SETUP.md).

Useful endpoints:

- frontend: `http://localhost:5173`
- backend: `http://localhost:8000`
- API docs: `http://localhost:8000/docs`
- readiness: `http://localhost:8000/health/ready`

## Start here

Read the documents in this order:

1. [Setup and Usage](docs/SETUP.md)
2. [SPEC-000 — Constitution](docs/specifications/SPEC-000.md)
3. [RFC-000 — Specification Governance](docs/RFC-000.md)
4. [RFC-001 — Product Definition](docs/RFC-001.md)
5. [RFC-002 — Engineering Philosophy](docs/RFC-002.md)
6. [RFC-003 — Design Principles](docs/RFC-003.md)
7. [RFC-004 — System Architecture](docs/RFC-004.md)
8. [RFC-005 — Approved Technology Registry](docs/RFC-005.md)
9. [STD-001 — Repository Standards](docs/standards/STD-001.md)
10. [Canonical Domain Model](docs/domain/DOMAIN-MODEL.md)
11. [PKOS Schema](docs/domain/PKOS-SCHEMA.md)
12. [Domain Event Catalog](docs/domain/EVENT-CATALOG.md)
13. [Domain API Contracts](docs/domain/API-CONTRACTS.md)
14. [Phase 0 Security Baseline](docs/security/PHASE-0-SECURITY-BASELINE.md)
15. [Phase 0 Backup and Restore](docs/operations/PHASE-0-BACKUP-RESTORE.md)
16. [PHASE-000 — Repository Foundation](docs/phases/PHASE-000-repository-foundation.md)
17. [PHASE-001 — Executive Dashboard MVP](docs/phases/PHASE-001-executive-dashboard-mvp.md)
18. [Roadmap](docs/ROADMAP.md)
19. [Contributing](docs/CONTRIBUTING.md)

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
