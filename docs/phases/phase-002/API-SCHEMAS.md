---
id: PHASE-002-API-SCHEMAS
title: Phase 2 Knowledge Platform API
status: Approved for Implementation
version: 0.5.0
owner: Lucky Jain
---

# Phase 2 API Schemas

## Conventions

All endpoints are under `/api/v1`, use session-derived workspace and actor, UUID identifiers, ISO-8601 timestamps, signed cursors, idempotency keys for creates/actions and an `expected_version` request-body field for mutable updates (this proposal originally named `If-Match`, but every shipped mutation endpoint checks a version field in the JSON body against the row's `version` column instead -- `entities_mutations.py`'s `EntityPatch`/`EntityAction`, `entity_operations.py`'s `EntityMergeRequest` with its paired `expected_target_version`/`expected_source_version`, and `notes.py` all follow this; no endpoint reads or requires an `If-Match` header). Cross-workspace IDs return 404.

## Proposed surface

```text
GET|POST /knowledge/entities
GET|PATCH /knowledge/entities/{id}
POST /knowledge/entities/{id}/archive|restore
GET|POST /knowledge/entities/{id}/claims
POST /knowledge/entities/{id}/claims/{claim_id}/supersede
GET|POST /knowledge/entities/{id}/relationships (GET accepts optional `relationship_type`, `direction=incoming|outgoing`)
POST /knowledge/relationships/{relationship_id}/invalidate
GET /knowledge/entities/{id}/timeline
GET /knowledge/entities/{id}/aliases
POST /knowledge/resolution/candidates
GET /knowledge/resolution/candidates
POST /knowledge/resolution/candidates/{id}/confirm|reject
POST /knowledge/resolution/candidates/{id}/defer
POST /knowledge/entities/merge
POST /knowledge/entity-operations/{id}/reverse
POST /knowledge/entity-operations/{id}/split
GET /knowledge/retrieve
GET /evidence
POST /evidence/{id}/delete
POST /identity/people
POST /identity/organizations
```

## Shared representations

`KnowledgeEntity` includes id, kind, canonical_name, summary, status, aliases, version and timestamps. `EvidenceRef` includes source type, locator, evidence state, observed time and optional excerpt. `MatchExplanation` includes the lexical (trigram, full-text) and, when hybrid mode is enabled, semantic factors that actually drove the ranking, without exposing sensitive internal features -- recency and authority are not scoring factors in the shipped implementation (see `RETRIEVAL-CONTRACT.md`'s ranking-order note).

`RelationshipResponse` (updated, a team-concept gap analysis found the original shape unusable for a human-facing roster/relationship list): id, from_entity_id, to_entity_id, relationship_type, confidence, evidence_id, valid_from, valid_to, status, plus four fields resolved via a join against the entity table rather than left as bare UUIDs -- `from_entity_name`, `from_entity_kind`, `to_entity_name`, `to_entity_kind` (all required strings). `GET .../relationships` additionally accepts `relationship_type` (any value from the relationship-type vocabulary) and `direction` (`incoming` filters to edges where this entity is the target; `outgoing` filters to edges where it is the source; omitting both preserves the original both-directions/every-type behavior). The combination `relationship_type=MEMBER_OF&direction=incoming` composes a team's membership roster without a dedicated `/teams/{id}/members` endpoint, consistent with this API's fully-generic-entity design.

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

Use the standard problem schema. Codes are UPPER_SNAKE_CASE, matching this repo's error-code convention everywhere else (an earlier draft of this section used lower_snake_case; every shipped endpoint uses upper). Required codes include `VERSION_CONFLICT`, `INVALID_RELATIONSHIP`, `AMBIGUOUS_RESOLUTION`, `UNSAFE_REVERSAL`, `EVIDENCE_UNAVAILABLE` and `MALFORMED_CURSOR` (a malformed pagination cursor; this section originally called it `cursor_invalid`, which no endpoint implements). `FEATURE_DISABLED` was proposed here but was never implemented: Phase 2's only optionally-gated capability, hybrid retrieval's embeddings dependency, degrades to `degraded=true` lexical-only results per RETRIEVAL-CONTRACT.md rather than erroring, so there is no real endpoint for this code to guard.
