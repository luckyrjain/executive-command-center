---
id: ADR-0003
title: Knowledge Platform and PKOS
status: Accepted
date: 2026-07-13
owners:
  - Lucky Jain
related:
  - RFC-004
  - PKOS-SCHEMA
---

# ADR-0003 — Knowledge Platform and PKOS

## Context

ECC needs durable memory across people, projects, meetings, commitments, decisions and evidence. A document store alone cannot represent the relationships required for executive reasoning.

## Decision

Adopt the Personal Knowledge Operating System (PKOS) as the canonical knowledge subsystem. PKOS stores normalized entities, typed relationships, provenance, temporal validity and retrieval indexes.

Source artifacts remain immutable evidence. Derived summaries, embeddings and inferred relationships are replaceable projections and never overwrite source evidence.

## Consequences

- Knowledge becomes relationship-oriented rather than file-oriented.
- Provenance and confidence are mandatory on derived facts.
- Entity resolution and merge history require explicit workflows.
- The graph representation may evolve without changing domain contracts.

## Alternatives considered

- Vector database as primary memory: rejected because similarity search cannot provide authoritative relationships or lifecycle rules.
- Notes-only model: rejected because it cannot reliably support cross-domain reasoning.
