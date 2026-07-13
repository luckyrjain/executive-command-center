# Executive Command Center

> A local-first AI executive operating system.

Executive Command Center (ECC) is a specification-driven platform for consolidating executive context across communication, calendar, tasks, documents, engineering systems, and long-term memory.

ECC is designed to reduce cognitive overhead, preserve decision context, protect attention, and support human judgment with explainable AI.

## Current status

**Foundation status:** Draft — under review

Implementation should not begin until the Phase 0 specification is approved.

## Documentation map

### Constitutional and governance documents

- [SPEC-000 — Executive Command Center Constitution](docs/specifications/SPEC-000.md)
- [RFC-000 — Specification Governance & Document Control](docs/RFC-000.md)

### Product and engineering RFCs

- [RFC-001 — Product Definition](docs/RFC-001.md)
- [RFC-002 — Engineering Philosophy](docs/RFC-002.md)
- [RFC-003 — Design Principles](docs/RFC-003.md)
- [RFC-004 — System Architecture](docs/RFC-004.md)
- [RFC-005 — Approved Technology Registry](docs/RFC-005.md)

### Standards

- [STD-001 — Repository Standards](docs/standards/STD-001.md)

### Architecture chapters

- [Chapter 1 — Architectural Vision & System Context](docs/architecture/chapter-01-vision.md)
- [Chapter 2A — Core Platform & Service Architecture](docs/architecture/chapter-02a-core-platform.md)
- [Chapter 2B — Platform Runtime & Deployment Architecture](docs/architecture/chapter-02b-runtime.md)
- [Chapter 3 — AI Runtime Architecture](docs/architecture/chapter-03-ai-runtime.md)
- [Chapter 4 — Knowledge Platform & Memory Architecture](docs/architecture/chapter-04-knowledge-platform.md)
- [Chapter 5 — Human Attention Engine](docs/architecture/chapter-05-attention-engine.md)
- [Chapter 6 — Integration Platform](docs/architecture/chapter-06-integration-platform.md)
- [Chapter 7 — Frontend Architecture](docs/architecture/chapter-07-frontend.md)
- [Chapter 8 — Data Platform](docs/architecture/chapter-08-data-platform.md)
- [Chapter 9 — Security, Privacy & Local-First Architecture](docs/architecture/chapter-09-security.md)
- [Chapter 10 — Operations & Platform Engineering](docs/architecture/chapter-10-operations.md)

### Delivery documents

- [Roadmap](docs/ROADMAP.md)
- [Contributing](docs/CONTRIBUTING.md)
- [Phase 0 — Repository Foundation](docs/phases/PHASE-000-repository-foundation.md)
- [Phase 1 — Executive Dashboard MVP](docs/phases/PHASE-001-executive-dashboard-mvp.md)

### Governance support

- [ADR register](docs/adr/README.md)
- [Document templates](docs/templates/)

## Development model

```text
Specification
    ↓
Architecture
    ↓
Implementation
    ↓
Testing
    ↓
Release
```

Every externally visible behavior must be traceable to an approved requirement.

## Core principles

- Local first
- Human authority
- Explainable AI
- Permanent memory
- Attention-centric design
- Progressive automation
- Specification before implementation

## Repository rule

> If a capability is not documented in the active phase specification, it does not get implemented.

## License

See [LICENSE](LICENSE).
