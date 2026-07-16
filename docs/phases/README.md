# Executive Command Center — Phase Documentation

This is the canonical index for phase-wise product, architecture, API, data, UX, security, testing and implementation documentation.

- [Phase 0–9 documentation completeness review](./PHASE-REVIEW.md)

## Governance

- A top-level phase specification defines objective, scope, dependencies, boundaries and exit criteria.
- Supporting documents are normative only after the phase is approved for implementation.
- Draft contracts are planning artifacts and do not authorize implementation.
- Approved behavior changes require review and explicit version updates.
- Implementation status reports delivery evidence and never overrides a contract.

## Status

| Phase | Name | Specification | Delivery status |
|---:|---|---|---|
| 0 | Repository Foundation | Approved baseline | Implemented |
| 1 | Executive Dashboard MVP | Approved for implementation | In progress |
| 2 | Knowledge Platform | Draft | Not started |
| 3 | Human Attention Engine | Draft | Not started |
| 4 | AI Runtime | Draft | Not started |
| 5 | Automation | Draft | Not started |
| 6 | Engineering Workspace | Draft | Not started |
| 7 | Personal Intelligence | Draft | Not started |
| 8 | Multi-user Workspaces | Draft | Not started |
| 9 | Enterprise | Draft | Not started |

## Phase 0 — Repository Foundation

- [Primary phase specification](./PHASE-000-repository-foundation.md)


## Phase 1 — Executive Dashboard MVP

- [Primary phase specification](./PHASE-001-executive-dashboard-mvp.md)
- [DATA MODEL](./phase-001/DATA-MODEL.md)
- [API SCHEMAS](./phase-001/API-SCHEMAS.md)
- [PRIORITY MODEL](./phase-001/PRIORITY-MODEL.md)
- [MORNING BRIEF CONTRACT](./phase-001/MORNING-BRIEF-CONTRACT.md)
- [AUDIT CONTRACT](./phase-001/AUDIT-CONTRACT.md)
- [SEARCH CONTRACT](./phase-001/SEARCH-CONTRACT.md)
- [UX STATES](./phase-001/UX-STATES.md)
- [TEST PLAN](./phase-001/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-001/IMPLEMENTATION-STATUS.md)

## Phase 2 — Knowledge Platform

- [Primary phase specification](./PHASE-002-knowledge-platform.md)
- [DATA MODEL](./phase-002/DATA-MODEL.md)
- [API SCHEMAS](./phase-002/API-SCHEMAS.md)
- [ENTITY RESOLUTION CONTRACT](./phase-002/ENTITY-RESOLUTION-CONTRACT.md)
- [RETRIEVAL CONTRACT](./phase-002/RETRIEVAL-CONTRACT.md)
- [UX STATES](./phase-002/UX-STATES.md)
- [TEST PLAN](./phase-002/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-002/IMPLEMENTATION-STATUS.md)

## Phase 3 — Human Attention Engine

- [Primary phase specification](./PHASE-003-human-attention-engine.md)
- [DATA MODEL](./phase-003/DATA-MODEL.md)
- [API SCHEMAS](./phase-003/API-SCHEMAS.md)
- [ATTENTION MODEL](./phase-003/ATTENTION-MODEL.md)
- [PLANNING CONTRACT](./phase-003/PLANNING-CONTRACT.md)
- [MEETING PREP CONTRACT](./phase-003/MEETING-PREP-CONTRACT.md)
- [UX STATES](./phase-003/UX-STATES.md)
- [TEST PLAN](./phase-003/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-003/IMPLEMENTATION-STATUS.md)

## Phase 4 — AI Runtime

- [Primary phase specification](./PHASE-004-ai-runtime.md)
- [DATA MODEL](./phase-004/DATA-MODEL.md)
- [API SCHEMAS](./phase-004/API-SCHEMAS.md)
- [MODEL ROUTING CONTRACT](./phase-004/MODEL-ROUTING-CONTRACT.md)
- [EVALUATION CONTRACT](./phase-004/EVALUATION-CONTRACT.md)
- [UX STATES](./phase-004/UX-STATES.md)
- [TEST PLAN](./phase-004/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-004/IMPLEMENTATION-STATUS.md)

## Phase 5 — Automation

- [Primary phase specification](./PHASE-005-automation.md)
- [DATA MODEL](./phase-005/DATA-MODEL.md)
- [API SCHEMAS](./phase-005/API-SCHEMAS.md)
- [EXECUTION CONTRACT](./phase-005/EXECUTION-CONTRACT.md)
- [APPROVAL POLICY](./phase-005/APPROVAL-POLICY.md)
- [UX STATES](./phase-005/UX-STATES.md)
- [TEST PLAN](./phase-005/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-005/IMPLEMENTATION-STATUS.md)

## Phase 6 — Engineering Workspace

- [Primary phase specification](./PHASE-006-engineering-workspace.md)
- [DATA MODEL](./phase-006/DATA-MODEL.md)
- [API SCHEMAS](./phase-006/API-SCHEMAS.md)
- [CONNECTOR CONTRACT](./phase-006/CONNECTOR-CONTRACT.md)
- [DELIVERY INTELLIGENCE CONTRACT](./phase-006/DELIVERY-INTELLIGENCE-CONTRACT.md)
- [UX STATES](./phase-006/UX-STATES.md)
- [TEST PLAN](./phase-006/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-006/IMPLEMENTATION-STATUS.md)

## Phase 7 — Personal Intelligence

- [Primary phase specification](./PHASE-007-personal-intelligence.md)
- [DATA MODEL](./phase-007/DATA-MODEL.md)
- [API SCHEMAS](./phase-007/API-SCHEMAS.md)
- [DOMAIN PRIVACY CONTRACT](./phase-007/DOMAIN-PRIVACY-CONTRACT.md)
- [INSIGHT CONTRACT](./phase-007/INSIGHT-CONTRACT.md)
- [UX STATES](./phase-007/UX-STATES.md)
- [TEST PLAN](./phase-007/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-007/IMPLEMENTATION-STATUS.md)

## Phase 8 — Multi-user Workspaces

- [Primary phase specification](./PHASE-008-multi-user.md)
- [DATA MODEL](./phase-008/DATA-MODEL.md)
- [API SCHEMAS](./phase-008/API-SCHEMAS.md)
- [PERMISSION CONTRACT](./phase-008/PERMISSION-CONTRACT.md)
- [DELEGATION CONTRACT](./phase-008/DELEGATION-CONTRACT.md)
- [UX STATES](./phase-008/UX-STATES.md)
- [TEST PLAN](./phase-008/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-008/IMPLEMENTATION-STATUS.md)

## Phase 9 — Enterprise

- [Primary phase specification](./PHASE-009-enterprise.md)
- [DATA MODEL](./phase-009/DATA-MODEL.md)
- [API SCHEMAS](./phase-009/API-SCHEMAS.md)
- [TENANCY CONTRACT](./phase-009/TENANCY-CONTRACT.md)
- [COMPLIANCE CONTRACT](./phase-009/COMPLIANCE-CONTRACT.md)
- [UX STATES](./phase-009/UX-STATES.md)
- [TEST PLAN](./phase-009/TEST-PLAN.md)
- [IMPLEMENTATION STATUS](./phase-009/IMPLEMENTATION-STATUS.md)

## Dependency sequence

```text
Phase 0 Foundation
  -> Phase 1 Dashboard
  -> Phase 2 Knowledge
  -> Phase 3 Attention
  -> Phase 4 AI Runtime
  -> Phase 5 Automation
  -> Phase 6 Engineering Workspace
  -> Phase 7 Personal Intelligence
  -> Phase 8 Multi-user
  -> Phase 9 Enterprise
```

Implementation begins only after dependencies meet exit criteria and the phase is explicitly Approved for Implementation.

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
