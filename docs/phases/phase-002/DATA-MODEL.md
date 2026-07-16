---
id: PHASE-002-DATA-MODEL
title: Phase 2 Knowledge Platform Data Model
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 2 Data Model

## Rules

PostgreSQL is authoritative. Every table carries `workspace_id`, timestamps and optimistic version where mutable. Composite foreign keys include `workspace_id`. Derived search, embedding and timeline projections are rebuildable.

## Core records

| Record | Purpose | Required fields |
|---|---|---|
| `knowledge_entities` | Canonical person, organization, project, topic, decision or document | id, workspace_id, kind, canonical_name, summary, status, version |
| `entity_aliases` | Names and external identifiers | entity_id, alias_type, normalized_value, source_id, confidence |
| `knowledge_claims` | Atomic attributable facts | subject_id, predicate, value_json, source_id, confidence, valid_from, valid_to |
| `relationships` | Typed directed connection | from_entity_id, relationship_type, to_entity_id, source_id, confidence, valid interval |
| `source_refs` | Provenance pointer and permission state | source_type, source_entity_id, locator, content_hash, evidence_state, observed_at |
| `resolution_candidates` | Reviewable possible identity match | left_entity_id, right_entity_id, score, factors_json, status |
| `entity_operations` | Merge/split lineage | operation_type, inputs_json, outputs_json, actor_id, reason |
| `timeline_entries` | Rebuildable chronology projection | entity_id, effective_at, recorded_at, event_type, source_id, summary |
| `retrieval_documents` | Normalized searchable projection | entity_type, entity_id, title, body, tsvector, source_version |
| `embedding_projections` | Optional derived vectors | document_id, model_id, model_version, dimensions, vector, content_hash |

## Invariants

- Supported entity kinds are versioned enums.
- A claim or relationship has at least one source reference.
- Confidence is in [0,1] and user-confirmed records use confidence 1.
- Valid intervals cannot be inverted.
- An entity cannot relate to itself unless the relationship type explicitly permits it.
- Confirmed merges redirect old IDs but never reuse or delete them.
- Split operations restore traceable descendants and invalidate obsolete projections.
- Source content is not duplicated into audit records.

## Lifecycle

Entities: `active -> archived -> active`; merged entities become `redirected`. Claims and relationships are corrected through superseding records rather than destructive overwrite. Resolution candidates: `open -> confirmed|rejected|expired`.

## Isolation and deletion

All unique constraints are workspace scoped. Cross-workspace references fail. Source deletion changes evidence to `deleted`, removes derived searchable content and embeddings, and retains only minimal redacted lineage required for audit integrity.
