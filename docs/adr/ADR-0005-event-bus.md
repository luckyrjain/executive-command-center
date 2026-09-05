---
id: ADR-0005
title: Event Bus
status: Accepted
version: 1.0.0
date: 2026-07-13
owner: Lucky Jain
related: [RFC-004, EVENT-CATALOG]
---

# ADR-0005 — Event Bus

## Context
ECC domains must remain independently evolvable while reacting to connector updates, knowledge changes, reminders and AI-derived proposals.

## Decision
Use versioned domain events for asynchronous cross-domain communication. Events are immutable facts named in past tense, include a standard envelope, and are published only after the originating transaction commits. Consumers must be idempotent. Delivery is at least once; ordering is guaranteed only within an aggregate stream.

Phase 0 may use an in-process durable implementation behind an event-bus contract. Infrastructure can later be replaced without changing event schemas.

## Consequences
- Loose coupling and replay become possible.
- Idempotency, dead-letter handling and schema compatibility are mandatory.
- Eventual consistency must be visible in UX and tests.

## Alternatives considered
Direct service-to-service calls for all workflows were rejected because they create synchronous coupling and cascading failure.

## Implementation Note (2026-09-04, see SCR-0001)
Not implemented as decided, and no superseding ADR was filed for the deviation. `backend/ecc/platform/events/bus.py`'s in-process bus (the Phase 0 fallback this ADR allows for) is defined but never instantiated or called from production code. The durable `event_outbox` table (`platform/audit_outbox.py`) is written to by several domains but never consumed by anything outside its own writer — it functions as an audit log, not a message bus. Cross-domain workflows instead use exactly the pattern this ADR's Alternatives section rejected: direct synchronous function calls (e.g. `domains/governance/recommendation_targets.py` calling into `communication`, `planning`, and `governance` mutation functions directly). This works today because the affected workflows are short, in-process, and already inside one DB transaction — but it is the opposite of this ADR's decision, undocumented as such until this note. A follow-up ADR should either formally supersede this decision (documenting direct-call as the accepted pattern for same-transaction cross-domain writes) or schedule the durable event-bus implementation this ADR calls for.
