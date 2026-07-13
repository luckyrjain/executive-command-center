---
id: ADR-0006
title: Storage Strategy
status: Accepted
date: 2026-07-13
owners: [Lucky Jain]
related: [RFC-004, PKOS-SCHEMA]
---

# ADR-0006 — Storage Strategy

## Context
ECC needs transactional state, durable evidence, relationships, search and embeddings while remaining local-first.

## Decision
Use PostgreSQL as the canonical transactional store in the initial implementation. Store source artifacts in content-addressed local object storage with metadata in PostgreSQL. Represent knowledge entities and relationships in relational tables first, behind PKOS repository contracts. Use PostgreSQL full-text search and pgvector for initial retrieval. Derived indexes are rebuildable.

A dedicated graph engine or search service requires a later ADR supported by measured limitations.

## Consequences
- Phase 0 has one operational database and simpler backup semantics.
- Domain ownership is logical and enforced by schemas/contracts.
- Cross-domain SQL access remains prohibited.
- Specialized stores can be added later without changing domain APIs.

## Alternatives considered
Multiple databases from day one were rejected as premature operational complexity.
