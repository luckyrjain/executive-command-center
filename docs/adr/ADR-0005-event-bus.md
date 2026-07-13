---
id: ADR-0005
title: Event Bus
status: Accepted
date: 2026-07-13
owners: [Lucky Jain]
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
