---
id: PHASE-002-DATA-MODEL
title: Phase 2 Knowledge Platform Data Model
status: Approved for Implementation
version: 0.4.0
owner: Lucky Jain
---

# Phase 2 Data Model

## Rules

PostgreSQL is authoritative. Every table carries `workspace_id`, timestamps and optimistic version where mutable. Composite foreign keys include `workspace_id`. Derived search, embedding and timeline projections are rebuildable.

## Reconciliation with PKOS (ADR-0003 / PKOS-SCHEMA.md)

Version 0.1.0 of this document proposed `knowledge_entities`, `relationships`, and `source_refs` as independently-named new tables. That would have forked a second entity/relationship store disconnected from `pkos_nodes`/`pkos_edges`/`pkos_evidence` — the tables Phase 0 already shipped and ADR-0003 (Accepted) already declared the canonical knowledge subsystem, and which `docs/domain/PKOS-SCHEMA.md`'s own mapping table already earmarked for exactly this phase ("aliases and merge records" / "embeddings and chunks" listed as "deferred until identity-resolution phase"). Resolved: extend the existing PKOS tables instead of forking. See `docs/superpowers/specs/2026-07-21-phase-2-knowledge-platform-design.md`'s Open decision 1 for the full analysis.

## Core records

| Record | Purpose | Required fields | Physical table |
|---|---|---|---|
| `knowledge_entities` | Canonical person, organization, project, topic, decision or document | id, workspace_id, kind, canonical_name, summary, status, version | `pkos_nodes`, extended with `entity_id`, `status`, `confidence`, `version` (`node_type`/`canonical_name`/`attributes` already exist and map to `kind`/`canonical_name`/free-form fields) |
| `entity_aliases` | Names and external identifiers | entity_id, alias_type, normalized_value, source_id, confidence | new table, `entity_id` FK to `pkos_nodes` |
| `knowledge_claims` | Atomic attributable facts | subject_id, predicate, value_json, source_id, confidence, valid_from, valid_to, superseded_by | new table, `subject_id` FK to `pkos_nodes` |
| `relationships` | Typed directed connection | from_entity_id, relationship_type, to_entity_id, source_id, confidence, valid interval | `pkos_edges`, extended with `confidence`, `evidence_id`, `valid_from`, `valid_to`, `status` (`source_node_id`/`target_node_id`/`edge_type`/`attributes` already exist and map to `from_entity_id`/`to_entity_id`/`relationship_type`/free-form fields) |
| `source_refs` | Provenance pointer and permission state | source_type, source_entity_id, locator, content_hash, evidence_state, observed_at | `pkos_evidence`, extended with `evidence_state`, `observed_at` |
| `resolution_candidates` | Reviewable possible identity match | left_entity_id, right_entity_id, score, factors_json, resolver_version, status, deferred_until | new table, composite FKs to `pkos_nodes` |
| `entity_operations` | Merge/split lineage | operation_type, status, inputs_json, outputs_json, actor_id, reason, reverses_operation_id | new table |
| `timeline_entries` | Rebuildable chronology projection | entity_id, effective_at, recorded_at, event_type, source_id, summary | new table, `entity_id` FK to `pkos_nodes` |
| `retrieval_documents` | Normalized searchable projection | entity_type, entity_id, title, body, tsvector, source_version | new table, `entity_id` FK to `pkos_nodes` |
| `embedding_projections` | Optional derived vectors | document_id, model_id, model_version, dimensions, embedding, content_hash | migration `0015_phase2_embeddings.py`, `document_id` composite FK to `retrieval_documents`. Shipped: RFC-005 v1.2.0 amendment + ADR-0011 (Slice 7). `embedding` is the actual column name (`vector` is the pgvector type/extension name, kept distinct to avoid confusion); a `vector(384)` column via pgvector, HNSW-indexed with `vector_cosine_ops`. |

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

Entities: `active -> archived -> active`; merged entities become `redirected`. Claims and relationships are corrected through superseding records rather than destructive overwrite. Resolution candidates: `open -> confirmed|rejected|expired`; a deferred candidate stays `open` with `deferred_until` set rather than gaining a separate status, and is excluded from the default list only until that timestamp passes. Entity operations: `active -> reversed`; a split creates a new `entity_operations` row with `operation_type=split` and `reverses_operation_id` pointing at the merge it undoes, rather than mutating the merge row in place.

## Isolation and deletion

All unique constraints are workspace scoped. Cross-workspace references fail. Source deletion changes evidence to `deleted`, removes derived searchable content and embeddings, and retains only minimal redacted lineage required for audit integrity.
