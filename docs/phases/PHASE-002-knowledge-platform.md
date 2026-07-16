---
id: PHASE-002
title: Knowledge Platform
status: Draft
version: 0.1.0
owner: Lucky Jain
depends_on:
  - PHASE-001
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

Turn Phase 1 records into durable, connected executive memory that preserves provenance, identity, relationships and history, and can be retrieved without depending on an AI model.

## User value

The user can find what is known about a person, project, decision or topic; understand how facts are connected; inspect the supporting evidence; and reconstruct what changed over time.

## In scope

- Canonical knowledge items for people, organizations, projects, topics, decisions and documents.
- Typed relationships with provenance, validity intervals and confidence.
- Deterministic entity resolution with reviewable merge candidates.
- Reversible merge and split operations.
- Append-only timeline projection across Phase 1 and Phase 2 entities.
- PostgreSQL lexical retrieval plus optional embedding retrieval behind a feature flag.
- Hybrid ranking, evidence presentation and source freshness.
- Knowledge capture, correction, archive, restore and conflict handling.
- Workspace isolation, redacted audit records and outbox events.

## Out of scope

External connectors; autonomous entity merges; dedicated graph databases; cross-workspace learning; background agent reasoning; predictive recommendations; organization-wide collaboration; cloud-only vector services.

## Functional requirements

- Every knowledge claim identifies its source, observed time, confidence and accountable workspace.
- Canonical entities use stable IDs; aliases never replace identifiers.
- Exact identifiers and user-confirmed mappings outrank inferred matches.
- Ambiguous resolution creates a review candidate and never silently merges.
- Merge and split preserve complete lineage and are reversible.
- Timeline results are deterministic and ordered by effective time, recorded time and ID.
- Retrieval supports lexical-only operation when embeddings or model services are unavailable.
- Every result explains why it matched and exposes evidence availability.
- Deleting or losing source access changes evidence state without rewriting historical lineage.

## Non-functional requirements

- Entity lookup p95 <300 ms for the local acceptance dataset.
- Hybrid retrieval p95 <800 ms locally; lexical fallback p95 <500 ms.
- Timeline query p95 <500 ms for a 10,000-event entity history.
- No cross-workspace identifiers or content may be observable.
- Rebuilding projections produces equivalent results.

## Architecture impact

Extend the modular monolith with knowledge, identity, relationship, timeline and retrieval modules. PostgreSQL remains authoritative. Embeddings are derived projections, not source-of-truth records. A dedicated graph or vector database requires a later ADR and measured need.

## Security and privacy

The authenticated session derives actor and workspace. Sensitive content is redacted from audit payloads and embeddings. Export, deletion and source-permission changes must propagate to projections. Cross-workspace IDs return 404.

## Acceptance and exit criteria

- All normative contracts are approved and versioned.
- Create, correct, merge, split, relationship, timeline and retrieval flows pass.
- Deterministic rebuild and AI-disabled tests pass.
- Isolation, redaction, deletion propagation and backup/restore tests pass.
- Zero open Critical, High or Medium review findings.
- A representative personal dataset demonstrates successful retrieval of people, projects, decisions and prior context.

## Rollback

Disable embedding and hybrid-ranking flags to retain lexical retrieval. New projections must be rebuildable from authoritative records and events. Schema downgrades require tested downgrade or documented forward-fix paths.

## Deferred backlog

External ingestion, graph database, advanced ontology learning, cross-workspace federation, connector-specific identity resolvers and AI-generated claims.
