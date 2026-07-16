---
id: PHASE-002
title: Knowledge Platform
status: Draft
version: 0.2.0
owner: Lucky Jain
depends_on:
  - PHASE-001
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005
  - STD-001
  - DOMAIN-MODEL
  - PKOS-SCHEMA
  - EVENT-CATALOG
contracts:
  - phase-002/DATA-MODEL.md
  - phase-002/API-SCHEMAS.md
  - phase-002/ENTITY-RESOLUTION-CONTRACT.md
  - phase-002/RETRIEVAL-CONTRACT.md
  - phase-002/UX-STATES.md
  - phase-002/TEST-PLAN.md
---

# PHASE-002 — Knowledge Platform

## Objective

Turn Phase 1 records into durable, connected executive memory preserving provenance, identity, relationships and history, retrievable without an AI model.

## User value

The user can find what is known about a person, project, decision or topic, understand connections, inspect evidence and reconstruct change over time.

## In scope

Canonical people, organizations, projects, topics, decisions and documents; typed relationships and claims; deterministic entity resolution; reversible merge/split; timeline projection; lexical retrieval; optional locally approved embedding projection; hybrid ranking; evidence/freshness; correction, archive and restore.

## Out of scope

External connectors; autonomous merges; dedicated graph database without an ADR; cross-workspace learning; background agents; predictive recommendations; multi-user collaboration; mandatory cloud vector services; generative AI claims.

## Functional requirements

- Claims and relationships identify source, effective/observed time, confidence and workspace.
- Stable canonical IDs survive alias changes and merges.
- Ambiguous identity matches require review; no inferred silent merge.
- Merge and split preserve reversible lineage.
- Timeline order is deterministic by effective time, recorded time and ID.
- Lexical retrieval is the mandatory release floor.
- Optional embeddings are derived, local-first and gated by RFC-005/ADR approval; failure falls back to lexical.
- Results expose match factors, evidence state and source freshness.
- Permission loss or source deletion removes derived content without rewriting historical lineage.

## Non-functional requirements

Entity lookup p95 <300 ms; lexical retrieval p95 <500 ms; hybrid retrieval p95 <800 ms; 10,000-entry timeline p95 <500 ms. Rebuilds are deterministic. No cross-workspace leakage. Core capture, timeline and lexical retrieval function without AI or internet.

## Architecture impact

Add knowledge, identity, relationship, timeline and retrieval modules. PostgreSQL remains authoritative. Search and embedding records are rebuildable projections. A graph/vector technology requires measured need, ADR and RFC-005 update before implementation.

## Data changes

Add the records and constraints in `phase-002/DATA-MODEL.md`: entities, aliases, claims, relationships, source references, resolution candidates, merge/split operations, timeline and retrieval projections. Migrations preserve Phase 1 ownership.

## API changes

Add the workspace-scoped entity, claim, relationship, timeline, resolution, merge/reversal and retrieval endpoints in `phase-002/API-SCHEMAS.md`. Mutations use idempotency and optimistic concurrency; signed cursors protect pagination.

## Frontend changes

Add Knowledge Explorer, entity detail, relationship/timeline views, resolution inbox, merge review and retrieval filters. Graph visualizations always have accessible list/table equivalents.

## Security and privacy

Session derives actor/workspace. Sensitive content is redacted from audit and excluded from embeddings by policy. Source permission is checked at projection, query and render time. Export/deletion propagates to snippets, indexes and embeddings. Cross-workspace IDs return 404.

## Observability

Measure projection lag/rebuild, unresolved resolution count, merge/reversal outcomes, false-merge corrections, timeline latency, retrieval latency/mode, fallback, result count, stale evidence and deletion propagation. Do not log claim bodies, snippets or vector content.

## Test strategy

Use lifecycle/contract tests, entity-resolution labelled datasets, merge/split property tests, deterministic projection rebuild, lexical/hybrid evaluation, isolation/redaction, deletion propagation, performance, accessibility, browser acceptance and backup/restore.

## Acceptance criteria

- All supporting contracts are reviewed and internally consistent.
- Entity/claim/relationship lifecycle and resolution review pass.
- Merge/reversal and deterministic timeline rebuild pass.
- Retrieval meets relevance and latency gates defined in the test plan.
- AI/embedding-disabled lexical fallback passes.
- Isolation, redaction, permission/deletion and accessibility tests pass.

## Exit criteria

- Contracts move explicitly to Approved for Implementation before coding.
- All Phase 2 implementation slices and migrations are merged.
- Benchmark reports, isolation matrix and backup/restore evidence are attached.
- Representative data retrieves people, projects, decisions and prior context successfully.
- Zero open Critical, High or Medium findings.
- Phase 3 can consume stable knowledge/timeline contracts.

## Rollback plan

Disable embedding and hybrid flags while retaining lexical retrieval. Rebuild projections from authoritative records/events. Merge operations remain reversible; unsafe automatic downgrade is prohibited. Schema changes use tested downgrade or forward fix.

## Deferred backlog

External ingestion, dedicated graph database, ontology learning, cross-workspace federation, connector-specific resolvers and AI-generated claims.
