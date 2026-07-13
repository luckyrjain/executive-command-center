# Executive Command Center Roadmap

## Current status

**Foundation specification:** Draft — under review  
**Current delivery phase:** [PHASE-000 — Repository Foundation](phases/PHASE-000-repository-foundation.md)  
**Next delivery phase:** [PHASE-001 — Executive Dashboard MVP](phases/PHASE-001-executive-dashboard-mvp.md)

The foundation documents exist, but the specification is not considered approved until the normative RFCs and standards complete review and their statuses are updated consistently.

## Delivery principles

Every phase must:

- deliver usable value
- compile and pass tests
- remain locally deployable
- preserve architecture and privacy boundaries
- define acceptance and exit criteria before implementation
- avoid speculative work outside the approved phase

## Delivery sequence

```text
Foundation specification review
        ↓
Phase 0 — Repository Foundation
        ↓
Phase 1 — Executive Dashboard MVP
        ↓
Phase 2 — Knowledge Platform
        ↓
Phase 3 — Human Attention Engine
        ↓
Phase 4 — AI Runtime
        ↓
Phase 5 — Automation
        ↓
Phase 6 — Engineering Workspace
        ↓
Phase 7 — Personal Intelligence
        ↓
Phase 8 — Multi-user
        ↓
Phase 9 — Enterprise
```

## Phase 0 — Repository Foundation

**Status:** Specification drafted; implementation not started.

Primary outcomes:

- canonical repository layout
- reproducible local development
- CI, testing and architecture enforcement
- typed configuration and secret handling
- observability foundation
- backend, frontend and design-system skeletons

Exit criteria are defined in [PHASE-000](phases/PHASE-000-repository-foundation.md).

## Phase 1 — Executive Dashboard MVP

**Status:** Specification drafted; blocked on Phase 0.

Primary outcomes:

- morning brief
- today's dashboard
- tasks and commitments
- notes
- meetings
- local search
- explainable recommendations
- human confirmation for state-changing actions

Exit criteria are defined in [PHASE-001](phases/PHASE-001-executive-dashboard-mvp.md).

## Later phases

### Phase 2 — Knowledge Platform

Persistent memory, entity resolution, relationships, timeline and hybrid retrieval.

### Phase 3 — Human Attention Engine

Priority, waiting-on, waiting-for, risk, planning and meeting preparation.

### Phase 4 — AI Runtime

Model routing, tool runtime, prompt versioning, evaluation, reflection and reliability controls.

### Phase 5 — Automation

Approval-based workflows, scheduling, background execution and automation policies.

### Phase 6 — Engineering Workspace

GitHub, GitLab, Jira, delivery intelligence, architecture decisions and engineering health.

### Phase 7 — Personal Intelligence

Health, finance, learning, travel, habits and personal relationships.

### Phase 8 — Multi-user

Family and team workspaces, delegation, shared knowledge and permissions.

### Phase 9 — Enterprise

SSO, audit, compliance, multi-tenancy and policy enforcement.

## Roadmap governance

A new phase or material phase change requires:

1. an approved phase specification
2. linked RFC and standard references
3. architecture review
4. acceptance criteria
5. rollback strategy
6. exit review

No implementation may silently skip or expand an approved phase.

## Long-term goal

Build a local-first executive operating system that users trust enough to open every morning and rely on for decisions, commitments and attention management.
