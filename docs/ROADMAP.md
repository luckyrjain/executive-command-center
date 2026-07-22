# Executive Command Center Roadmap

## Current status

**Foundation:** Phase 0 baseline approved and implemented  
**Current delivery:** [Phase 1 — Executive Dashboard MVP](phases/PHASE-001-executive-dashboard-mvp.md) — engineering delivery complete on `feature/phase-1-production-hardening` (Tasks 1-11 of `superpowers/plans/2026-07-16-phase-1-completion.md`, each independently reviewed with zero Critical or Important findings); Phase 1 exit remains open pending the seven-day daily-use validation gate and human change review — see [Phase 1 Implementation Status](phases/phase-001/IMPLEMENTATION-STATUS.md)  
**Future specifications:** Phase 2 Approved for Implementation and in progress (parallel to Phase 1's open exit gates); Phases 3–9 published as Draft / Planned

The [canonical phase index](phases/README.md) lists every primary specification and supporting contract. The [Phase 0–9 documentation review](phases/PHASE-REVIEW.md) records completeness findings and approval gates.

## Delivery principles

Every phase must:

- deliver independently usable value
- preserve local-first ownership and deterministic fallback
- compile, migrate, test and remain recoverable
- preserve architecture, privacy and authorization boundaries
- define measurable acceptance and exit criteria before implementation
- identify rollback and deferred scope
- receive explicit approval after all dependency exit gates pass
- avoid implementation outside the approved phase

## Delivery sequence

```text
Phase 0 — Repository Foundation         [Implemented]
  -> Phase 1 — Executive Dashboard MVP [Engineering delivery complete; exit gates open]
  -> Phase 2 — Knowledge Platform      [Approved for Implementation; in progress, parallel to open Phase 1 exit gates]
  -> Phase 3 — Human Attention Engine  [Draft]
  -> Phase 4 — AI Runtime              [Draft]
  -> Phase 5 — Automation              [Draft]
  -> Phase 6 — Engineering Workspace   [Draft]
  -> Phase 7 — Personal Intelligence   [Draft]
  -> Phase 8 — Multi-user Workspaces   [Draft]
  -> Phase 9 — Enterprise              [Draft]
```

A later phase may be designed or reviewed early, but implementation begins only after its dependencies satisfy exit criteria and its status is changed to Approved for Implementation.

## Phase 0 — Repository Foundation

**Status:** Approved baseline; implemented.

Primary outcomes:

- reproducible local development and CI
- modular-monolith architecture enforcement
- PostgreSQL persistence and migrations
- authentication, workspace isolation and security baseline
- durable event/outbox foundation
- observability, backup and restore

Specification: [PHASE-000](phases/PHASE-000-repository-foundation.md)

## Phase 1 — Executive Dashboard MVP

**Status:** Approved for Implementation; every capability below is delivered and independently reviewed on `feature/phase-1-production-hardening`. Phase 1 is not yet closed: it exits only after the seven-day daily-use validation (`docs/runbooks/PHASE-1-DAILY-USE.md`) and human change-review sign-off, both still open.

Primary outcomes:

- Today dashboard and Morning Brief
- tasks, commitments, notes, meetings and risks
- deterministic attention ranking and local search
- immutable audit
- explainable recommendations with durable human confirmation
- executive frontend and browser acceptance
- production hardening: security/config validation, structured observability, verified backup/restore, representative-scale performance gates, and dependency/container/secret scanning

Specification: [PHASE-001](phases/PHASE-001-executive-dashboard-mvp.md)  
Delivery status: [Phase 1 Implementation Status](phases/phase-001/IMPLEMENTATION-STATUS.md)  
Release gate: [Phase 1 Production Release Gate](runbooks/PHASE-1-RELEASE-GATE.md)  
Deployment runbook: [Phase 1 Deployment](runbooks/PHASE-1-DEPLOYMENT.md)  
Daily-use validation record: [Phase 1 Daily-Use Validation Record](runbooks/PHASE-1-DAILY-USE.md)

## Phase 2 — Knowledge Platform

**Status:** Approved for Implementation; contracts moved from Draft after resolving the PKOS-reconciliation decision in `docs/superpowers/specs/2026-07-21-phase-2-knowledge-platform-design.md` (extend the existing `pkos_nodes`/`pkos_edges`/`pkos_evidence` tables rather than fork independent ones). Implementation began by explicit repository-owner authorization to proceed in parallel with Phase 1's still-open exit gates (seven-day daily-use validation, human change review) — a deliberate exception to this document's own "implementation begins only after dependencies satisfy exit criteria" principle above, not a claim that Phase 1 has exited.

Tasks 1-6 and 8 (entities/claims/provenance, typed relationships, timeline, resolution, reversible merge/split, lexical retrieval, and the executive knowledge UX consuming all of it) are implemented. **Task 7 (optional embeddings and hybrid fusion), the design doc's Open decision 2, is now also authorized by the repository owner to proceed**, closing that decision's "repository owner decides embeddings are worth pursuing" precondition. This authorization covers starting the work, not the RFC-005/ADR gate itself — `RFC-005.md`'s "Retrieval benchmark and ADR" activation requirement for `pgvector` is satisfied separately, by RFC-005 v1.2.0's amendment and ADR-0011, both produced as part of this same authorization.

Persistent entities, claims, relationships, entity resolution, reversible merge/split, timelines and lexical-first hybrid retrieval, now extended with optional local embeddings for semantic recall.

Specification: [PHASE-002](phases/PHASE-002-knowledge-platform.md)  
Implementation plan: [Phase 2 Knowledge Platform Implementation Plan](superpowers/plans/2026-07-21-phase-2-knowledge-platform.md)

## Phase 3 — Human Attention Engine

**Status:** Draft / Planned.

Explainable attention, waiting direction, risk review, capacity-aware planning and evidence-backed meeting preparation.

Specification: [PHASE-003](phases/PHASE-003-human-attention-engine.md)

## Phase 4 — AI Runtime

**Status:** Draft / Planned.

Local-first model routing, typed prompts/tools, structured output, budgets, safety, evaluation and deterministic degradation.

Specification: [PHASE-004](phases/PHASE-004-ai-runtime.md)

## Phase 5 — Automation

**Status:** Draft / Planned.

Versioned workflows, simulation, explicit approval, durable execution, schedules, cancellation, compensation and kill switches.

Specification: [PHASE-005](phases/PHASE-005-automation.md)

## Phase 6 — Engineering Workspace

**Status:** Draft / Planned.

GitHub, GitLab and Jira connectors; delivery/reliability intelligence; incidents; decisions; evidence and source coverage without person scoring.

Specification: [PHASE-006](phases/PHASE-006-engineering-workspace.md)

## Phase 7 — Personal Intelligence

**Status:** Draft / Planned.

Opt-in private domains for health, finance, learning, travel, habits and relationships, with consent, bounded insights, export and deletion.

Specification: [PHASE-007](phases/PHASE-007-personal-intelligence.md)

## Phase 8 — Multi-user Workspaces

**Status:** Draft / Planned.

Membership, invitations, least-privilege permissions, explicit sharing, delegation acceptance, ownership transfer and privacy-preserving collaboration.

Specification: [PHASE-008](phases/PHASE-008-multi-user.md)

## Phase 9 — Enterprise

**Status:** Draft / Planned.

Tenant isolation, SSO/SCIM, policy administration, keys/residency, retention/legal hold, audit export, compliance evidence and disaster recovery.

Specification: [PHASE-009](phases/PHASE-009-enterprise.md)

## Approval gates

Before a Draft phase becomes Approved for Implementation:

1. dependency exit criteria are evidenced
2. phase scope and supporting contracts are reviewed
3. technology additions are approved through RFC-005 and an ADR where required
4. threat model and privacy boundaries are approved
5. measurable acceptance, performance and recovery datasets are frozen
6. rollback and operational runbooks are reviewable
7. zero Critical, High or Medium findings remain

Phase-specific decisions are recorded in [PHASE-REVIEW](phases/PHASE-REVIEW.md).

## Roadmap governance

A material phase change requires an explicit version update and reviewed pull request. Implementation status documents report evidence but never override normative contracts. No phase may silently skip dependencies or expand approved scope.

## Long-term goal

Build a local-first executive operating system trusted as the first application opened each morning for decisions, commitments, knowledge and attention.
