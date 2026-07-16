---
id: PHASE-002-API-SCHEMAS
title: Phase 2 Knowledge Platform API
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 2 API Schemas

## Conventions

All endpoints are under `/api/v1`, use session-derived workspace and actor, UUID identifiers, ISO-8601 timestamps, signed cursors, idempotency keys for creates/actions and `If-Match` for mutable updates. Cross-workspace IDs return 404.

## Proposed surface

```text
GET|POST /knowledge/entities
GET|PATCH /knowledge/entities/{id}
POST /knowledge/entities/{id}/archive|restore
GET|POST /knowledge/entities/{id}/claims
GET|POST /knowledge/entities/{id}/relationships
GET /knowledge/entities/{id}/timeline
GET /knowledge/entities/{id}/aliases
POST /knowledge/resolution/candidates
GET /knowledge/resolution/candidates
POST /knowledge/resolution/candidates/{id}/confirm|reject
POST /knowledge/entities/merge
POST /knowledge/entity-operations/{id}/reverse
GET /knowledge/retrieve
```

## Shared representations

`KnowledgeEntity` includes id, kind, canonical_name, summary, status, aliases, version and timestamps. `EvidenceRef` includes source type, locator, evidence state, observed time and optional excerpt. `MatchExplanation` includes lexical, semantic, recency and authority factors without exposing sensitive internal features.

## Mutation rules

- Create requires kind and canonical name.
- Patch cannot change workspace, ID or entity kind.
- Claims and relationships are superseded; historical versions remain queryable through timeline.
- Merge requires two or more active entities, a target strategy, reason and current versions.
- Merge is atomic with redirects, lineage, audit and outbox.
- Reverse operation validates that later operations do not make reversal unsafe.
- Resolution confirmation is a human-confirmed identity operation, not a generic update.

## Retrieval query

Parameters: `q`, optional entity kinds, time range, source types, limit and cursor. Mode is `lexical|hybrid`; hybrid falls back to lexical and returns `degraded=true` plus reason when embeddings are unavailable.

## Errors

Use the standard problem schema. Required codes include `version_conflict`, `invalid_relationship`, `ambiguous_resolution`, `unsafe_reversal`, `evidence_unavailable`, `feature_disabled` and `cursor_invalid`.
