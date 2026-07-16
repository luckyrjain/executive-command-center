# Executive Command Center — Phase Documentation

This directory is the canonical index for phase-wise product, architecture, API, data, UX, security, testing, and implementation documentation.

## Documentation rules

- The top-level phase document defines scope, dependencies, exit criteria, and the frozen API surface.
- Supporting documents under the matching phase directory are normative contracts for implementation.
- Approved contracts are changed only through a reviewed pull request with an explicit version bump.
- Implementation status documents describe delivery progress; they do not override approved contracts.
- Repository code, migrations, tests, and CI must remain consistent with the approved phase documents.

## Phase 0 — Foundation

Phase 0 establishes the repository, engineering standards, architecture decisions, core domain model, API conventions, event contracts, security boundaries, and PKOS foundation used by later phases.

Canonical foundation documents are maintained under:

- `docs/architecture/`
- `docs/domain/`
- `docs/rfcs/`
- `docs/standards/`

## Phase 1 — Executive Dashboard MVP

Primary phase document:

- [`PHASE-001-executive-dashboard-mvp.md`](./PHASE-001-executive-dashboard-mvp.md)

Normative supporting contracts:

- [`phase-001/DATA-MODEL.md`](./phase-001/DATA-MODEL.md)
- [`phase-001/API-SCHEMAS.md`](./phase-001/API-SCHEMAS.md)
- [`phase-001/PRIORITY-MODEL.md`](./phase-001/PRIORITY-MODEL.md)
- [`phase-001/MORNING-BRIEF-CONTRACT.md`](./phase-001/MORNING-BRIEF-CONTRACT.md)
- [`phase-001/AUDIT-CONTRACT.md`](./phase-001/AUDIT-CONTRACT.md)
- [`phase-001/SEARCH-CONTRACT.md`](./phase-001/SEARCH-CONTRACT.md)
- [`phase-001/UX-STATES.md`](./phase-001/UX-STATES.md)
- [`phase-001/TEST-PLAN.md`](./phase-001/TEST-PLAN.md)

Delivery and traceability:

- [`phase-001/IMPLEMENTATION-STATUS.md`](./phase-001/IMPLEMENTATION-STATUS.md)

## Phase 1 capability map

| Capability | Primary contract |
|---|---|
| Tasks and commitments | `API-SCHEMAS.md`, `DATA-MODEL.md` |
| Notes and local knowledge | `API-SCHEMAS.md`, `DATA-MODEL.md`, `SEARCH-CONTRACT.md` |
| Calendar events and meetings | `API-SCHEMAS.md`, `DATA-MODEL.md`, `UX-STATES.md` |
| Risks and attention ranking | `PRIORITY-MODEL.md`, `DATA-MODEL.md` |
| Global search | `SEARCH-CONTRACT.md` |
| Immutable audit history | `AUDIT-CONTRACT.md` |
| Today dashboard | `PHASE-001-executive-dashboard-mvp.md`, `UX-STATES.md` |
| Morning Brief | `MORNING-BRIEF-CONTRACT.md` |
| Recommendations and confirmation | `API-SCHEMAS.md`, `DATA-MODEL.md`, `AUDIT-CONTRACT.md` |
| Acceptance and performance | `TEST-PLAN.md` |

## Future phases

New phases should follow this layout:

```text
docs/phases/
  PHASE-00N-short-name.md
  phase-00N/
    DATA-MODEL.md
    API-SCHEMAS.md
    UX-STATES.md
    TEST-PLAN.md
    IMPLEMENTATION-STATUS.md
```

Only documents relevant to the phase should be added; not every phase requires every contract type.
