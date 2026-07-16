# Executive Command Center — Phase Documentation

This directory is the canonical index for phase-wise product, architecture, API, data, UX, security, testing and implementation documentation.

## Documentation rules

- The top-level phase document defines scope, dependencies and exit criteria.
- Supporting documents under the matching phase directory are normative contracts for implementation.
- Draft contracts are planning artifacts and are not approved implementation authority.
- Approved contracts change only through a reviewed pull request with an explicit version bump.
- Implementation status documents report delivery progress; they do not override contracts.
- Code, migrations, tests and CI must remain consistent with the approved phase documents.

## Phase 0 — Repository Foundation

Phase 0 establishes the repository, engineering standards, architecture decisions, core domain model, API conventions, event contracts, security boundaries and PKOS foundation.

- [PHASE-000 — Repository Foundation](./PHASE-000-repository-foundation.md)

Canonical foundation material is also maintained under `docs/architecture/`, `docs/domain/`, `docs/adr/`, `docs/security/` and `docs/standards/`.

## Phase 1 — Executive Dashboard MVP

Status: approved for implementation; delivery in progress.

Primary phase document:

- [PHASE-001 — Executive Dashboard MVP](./PHASE-001-executive-dashboard-mvp.md)

Normative supporting contracts:

- [Data model](./phase-001/DATA-MODEL.md)
- [API schemas](./phase-001/API-SCHEMAS.md)
- [Priority model](./phase-001/PRIORITY-MODEL.md)
- [Morning Brief contract](./phase-001/MORNING-BRIEF-CONTRACT.md)
- [Audit contract](./phase-001/AUDIT-CONTRACT.md)
- [Search contract](./phase-001/SEARCH-CONTRACT.md)
- [UX states](./phase-001/UX-STATES.md)
- [Test plan](./phase-001/TEST-PLAN.md)
- [Implementation status](./phase-001/IMPLEMENTATION-STATUS.md)

## Phase 2 — Knowledge Platform

Status: draft; implementation has not started.

Primary phase document:

- [PHASE-002 — Knowledge Platform](./PHASE-002-knowledge-platform.md)

Draft supporting contracts:

- [Data model](./phase-002/DATA-MODEL.md)
- [API schemas](./phase-002/API-SCHEMAS.md)
- [Entity resolution contract](./phase-002/ENTITY-RESOLUTION-CONTRACT.md)
- [Hybrid retrieval contract](./phase-002/RETRIEVAL-CONTRACT.md)
- [UX states](./phase-002/UX-STATES.md)
- [Test plan](./phase-002/TEST-PLAN.md)
- [Implementation status](./phase-002/IMPLEMENTATION-STATUS.md)

## Phase 3 — Human Attention Engine

Status: draft; implementation has not started.

Primary phase document:

- [PHASE-003 — Human Attention Engine](./PHASE-003-human-attention-engine.md)

Draft supporting contracts:

- [Data model](./phase-003/DATA-MODEL.md)
- [API schemas](./phase-003/API-SCHEMAS.md)
- [Attention model](./phase-003/ATTENTION-MODEL.md)
- [Planning contract](./phase-003/PLANNING-CONTRACT.md)
- [Meeting preparation contract](./phase-003/MEETING-PREP-CONTRACT.md)
- [UX states](./phase-003/UX-STATES.md)
- [Test plan](./phase-003/TEST-PLAN.md)
- [Implementation status](./phase-003/IMPLEMENTATION-STATUS.md)

## Capability ownership

| Capability | Phase | Primary contract |
|---|---:|---|
| Tasks, commitments, notes and local calendar | 1 | Phase 1 API and data model |
| Today dashboard and Morning Brief | 1 | Phase 1 phase and brief contracts |
| Local search and audit | 1 | Phase 1 search and audit contracts |
| Persistent entities, claims and relationships | 2 | Phase 2 data model |
| Identity matching, merge and split | 2 | Entity resolution contract |
| Timeline and hybrid retrieval | 2 | Retrieval contract and data model |
| Explainable executive attention | 3 | Attention model |
| Waiting direction and risk review | 3 | Phase 3 phase, API and data model |
| Daily and weekly planning | 3 | Planning contract |
| Evidence-backed meeting preparation | 3 | Meeting preparation contract |

## Standard layout

```text
docs/phases/
  PHASE-00N-short-name.md
  phase-00N/
    DATA-MODEL.md
    API-SCHEMAS.md
    <CAPABILITY-CONTRACT>.md
    UX-STATES.md
    TEST-PLAN.md
    IMPLEMENTATION-STATUS.md
```

Only relevant contracts should be added. A phase remains Draft until its dependencies, architecture impact, security boundary, acceptance criteria and rollback plan have completed review.
