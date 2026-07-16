# Executive Command Center

> A local-first AI Executive Operating System.

Executive Command Center (ECC) helps an executive manage attention, context, commitments, decisions, knowledge, meetings and execution from one trusted workspace.

The product is built around a simple rule:

> Stability is more valuable than optimization.

The repository structure and Batch 1 specification set are frozen at version 1.0. New architecture or document types require an explicit user request.

## Product principles

- Local first: user data remains locally owned and the core product works without cloud AI.
- Human authority: ECC recommends and explains; people decide.
- Explainability: consequential output must show evidence, reasoning and confidence.
- Durable memory: facts, decisions and commitments preserve provenance and history.
- Attention first: every feature must reduce cognitive overhead.
- Progressive automation: observe, recommend, assist, automate, then consider autonomy.

## Frozen project structure

```text
executive-command-center/
├── README.md
├── specification/
│   ├── rfc/
│   ├── adr/
│   ├── standards/
│   ├── phases/
│   ├── templates/
│   ├── schemas/
│   ├── prompts/
│   ├── diagrams/
│   └── wireframes/
├── backend/
├── frontend/
├── infra/
├── tools/
└── scripts/
```

Runtime manifests, lockfiles, tests and CI configuration remain at their required implementation locations. They are not specification document types.

## Frozen document types

Only four authoritative document types are permitted:

| Type | Purpose | Location |
|---|---|---|
| RFC | Product, engineering and architecture decisions | `specification/rfc/` |
| ADR | A proposed or accepted architecture decision | `specification/adr/` |
| STD | Normative engineering and repository rules | `specification/standards/` |
| SPEC | Phase or capability requirements | `specification/phases/` |

Supporting schemas, prompts, diagrams and wireframes are artifacts referenced by an RFC, ADR, STD or SPEC. They are not independent document types.

## Frozen Batch 1

Batch 1 contains exactly these eight documents:

1. [README.md](README.md)
2. [RFC-001 — Product Definition](specification/rfc/RFC-001.md)
3. [RFC-002 — Engineering Philosophy](specification/rfc/RFC-002.md)
4. [RFC-003 — Design Principles](specification/rfc/RFC-003.md)
5. [RFC-004 — System Architecture](specification/rfc/RFC-004.md)
6. [RFC-005 — Technology Registry](specification/rfc/RFC-005.md)
7. [STD-001 — Repository Standards](specification/standards/STD-001.md)
8. [RFC-000 — Document Control](specification/rfc/RFC-000.md)

Nothing is added to or removed from Batch 1 without an explicit user request.

## Frozen delivery process

Every specification document follows the same sequence:

```text
Write -> Review -> Revise -> Commit -> Next document
```

An improvement idea does not interrupt the active document. Record it later as `ADR-Proposed-XXX` and continue with the frozen specification.

## Implementation status

The backend and frontend contain an implemented Phase 1 product baseline. The frozen Batch 1 documents now govern future specification work. Existing implementation is preserved; this documentation reset does not claim that removed historical documents remain authoritative.

## Local development

Prerequisites:

- Python 3.14.6
- Node.js 22.17.0
- pnpm 10.12.4
- uv 0.7.19
- Docker Engine 28.3.2
- Docker Compose 2.38.2

Bootstrap:

```bash
cp .env.example .env
docker compose up -d postgres
uv sync --frozen --all-groups --python 3.14
set -a; source .env; set +a
uv run alembic -c backend/alembic.ini upgrade head
uv run python scripts/bootstrap_dev.py
```

Run the backend:

```bash
uv run uvicorn ecc.main:app --app-dir backend --reload --host 127.0.0.1 --port 8000
```

Run the frontend:

```bash
corepack enable
corepack prepare pnpm@10.12.4 --activate
pnpm install --frozen-lockfile
pnpm --filter @ecc/frontend dev
```

Useful endpoints:

- Frontend: `http://localhost:5173`
- Backend: `http://localhost:8000`
- OpenAPI: `http://localhost:8000/docs`
- Readiness: `http://localhost:8000/health/ready`

## Repository rule

> If a capability is not defined by an approved RFC, ADR, STD or SPEC, it does not get implemented.

No process redesign, repository restructuring or additional document type is permitted unless the user requests it explicitly.
