# Executive Command Center

> A local-first AI Executive Operating System.

Executive Command Center (ECC) is a specification-driven platform for helping executives manage attention, context, commitments, decisions, knowledge, meetings and execution.

## Current status

<!-- BEGIN GENERATED PHASE STATUS -->
**Current engineering phase:** Phase 10 — Gmail Connector (Engineering complete; Tasks 1-8 delivered).

| Phase | Name | Engineering | Validation | Promotion |
|---:|---|---|---|---|
| 0 | Repository Foundation | Engineering complete | Passed | Promoted |
| 1 | Executive Dashboard MVP | Engineering complete | In progress | Blocked |
| 2 | Knowledge Platform | Engineering complete | Not started | Blocked |
| 3 | Human Attention Engine | Engineering complete | In progress | Blocked |
| 4 | AI Runtime | Engineering complete | Accepted with limitations | Blocked |
| 5 | Automation | Engineering complete | In progress | Blocked |
| 6 | Engineering Workspace | Engineering complete | In progress | Blocked |
| 7 | Personal Intelligence | Engineering complete | Not started | Blocked |
| 8 | Multi-user Workspaces | Engineering complete | Not started | Blocked |
| 9 | Enterprise | Not started | Not started | Not promoted |
| 10 | Gmail Connector | Engineering complete | Not started | Blocked |

**Open gates:**

- Phase 1: seven-day daily-use validation; human change-review sign-off.
- Phase 2: product validation; human change review; Phase 1 predecessor exit accepted only by parallel-start exception.
- Phase 3: two-week dogfood validation; human change review; predecessor exits accepted only by parallel-start exception.
- Phase 4: real-model re-verification of recorded limitations; repository-owner independent review; promotion decision.
- Phase 5: 14-day staged dogfood record; human change review; promotion decision.
- Phase 6: real connector-account recovery evidence; production-readiness review; independent change review and promotion decision.
- Phase 7: personal-data export deletion and restore evidence; encryption-key rotation decision; independent change review and promotion decision.
- Phase 8: first production owner provisioning; account recovery and MFA or step-up decision; independent change review and promotion decision.
- Phase 9: specification approval; predecessor production-readiness and promotion decisions.
- Phase 10: real Gmail account verification; backup and restore verification; independent change review.

Source: [`docs/phases/status.json`](docs/phases/status.json). Specification approval, engineering completion, validation, and promotion are independent states.
<!-- END GENERATED PHASE STATUS -->

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

Open the one-time URL printed by `scripts/bootstrap_dev.py`. The backend exchanges the code for an `HttpOnly` seven-day session cookie and redirects to `http://localhost:5173`.

For prerequisites, Docker usage, testing, troubleshooting, reset instructions, and first-use guidance, read [Setup and Usage](docs/SETUP.md).

Useful endpoints:

- frontend: `http://localhost:5173`
- backend: `http://localhost:8000`
- API docs: `http://localhost:8000/docs`
- readiness: `http://localhost:8000/health/ready`

## Start here

Read the documents in this order:

1. [Document Control](docs/00-document-control.md)
2. [Setup and Usage](docs/SETUP.md)
3. [SPEC-000 — Constitution](docs/specifications/SPEC-000.md)
4. [RFC-000 — Specification Governance](docs/RFC-000.md)
5. [RFC-001 — Product Definition](docs/RFC-001.md)
6. [RFC-002 — Engineering Philosophy](docs/RFC-002.md)
7. [RFC-003 — Design Principles](docs/RFC-003.md)
8. [RFC-004 — System Architecture](docs/RFC-004.md)
9. [RFC-005 — Approved Technology Registry](docs/RFC-005.md)
10. [STD-001 — Repository Standards](docs/standards/STD-001.md)
11. [Canonical Domain Model](docs/domain/DOMAIN-MODEL.md)
12. [PKOS Schema](docs/domain/PKOS-SCHEMA.md)
13. [Domain Event Catalog](docs/domain/EVENT-CATALOG.md)
14. [Domain API Contracts](docs/domain/API-CONTRACTS.md)
15. [Phase 0 Security Baseline](docs/security/PHASE-0-SECURITY-BASELINE.md)
16. [Phase 0 Backup and Restore](docs/operations/PHASE-0-BACKUP-RESTORE.md)
17. [PHASE-000 — Repository Foundation](docs/phases/PHASE-000-repository-foundation.md)
18. [PHASE-001 — Executive Dashboard MVP](docs/phases/PHASE-001-executive-dashboard-mvp.md)
19. [Phase 1 Production Release Gate](docs/runbooks/PHASE-1-RELEASE-GATE.md)
20. [Phase 1 Deployment Runbook](docs/runbooks/PHASE-1-DEPLOYMENT.md)
21. [Phase 1 Daily-Use Validation Record](docs/runbooks/PHASE-1-DAILY-USE.md)
22. [Phase Documentation Index](docs/phases/README.md)
23. [Phase 0–9 Documentation Review](docs/phases/PHASE-REVIEW.md)
24. [Roadmap](docs/ROADMAP.md)
25. [Production Readiness and Blocker Register](docs/operations/PRODUCTION-READINESS.md)
26. [Product KPI Contract](docs/product/KPI-CONTRACT.md)
27. [Contributing](docs/CONTRIBUTING.md)

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
- ADR-0011 — Hybrid Retrieval Embeddings
- ADR-0012 — Ollama Local Inference
- ADR-0013 — Durable Workflow Execution

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
- [PHASE-002 — Knowledge Platform](docs/phases/PHASE-002-knowledge-platform.md)
- [PHASE-003 — Human Attention Engine](docs/phases/PHASE-003-human-attention-engine.md)
- [PHASE-004 — AI Runtime](docs/phases/PHASE-004-ai-runtime.md)
- [PHASE-005 — Automation](docs/phases/PHASE-005-automation.md)
- [PHASE-006 — Engineering Workspace](docs/phases/PHASE-006-engineering-workspace.md)
- [PHASE-007 — Personal Intelligence](docs/phases/PHASE-007-personal-intelligence.md)
- [PHASE-008 — Multi-user Workspaces](docs/phases/PHASE-008-multi-user.md)
- [PHASE-009 — Enterprise](docs/phases/PHASE-009-enterprise.md)
- [PHASE-010 — Gmail Connector](docs/phases/PHASE-010-gmail-connector.md)
- [Canonical Phase Index and Supporting Contracts](docs/phases/README.md)

## Operations and measurement

- [Production readiness and blocker register](docs/operations/PRODUCTION-READINESS.md)
- [Product KPI contract](docs/product/KPI-CONTRACT.md)
- [Phase 6 connector recovery](docs/runbooks/PHASE-6-CONNECTOR-RECOVERY.md)
- [Phase 7 personal-data recovery](docs/runbooks/PHASE-7-PERSONAL-DATA-RECOVERY.md)
- [Phase 8 identity recovery](docs/runbooks/PHASE-8-IDENTITY-RECOVERY.md)
- [Phase 10 Gmail recovery](docs/runbooks/PHASE-10-GMAIL-RECOVERY.md)

## Repository rule

> If a capability is not documented in the current phase specification, it does not get implemented.

This is the Golden Rule defined in [Document Control](docs/00-document-control.md#golden-rule).

Every behavior-changing change must include its specification, implementation, tests and documentation in the same pull request.
