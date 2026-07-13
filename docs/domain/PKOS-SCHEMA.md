---
id: PKOS-SCHEMA
title: Personal Knowledge Operating System Schema
status: Accepted
version: 1.1.0
owner: Lucky Jain
related:
  - ADR-0003
  - ADR-0006
  - DOMAIN-MODEL
  - EVENT-CATALOG
  - PHASE-001
  - PHASE-001-SEARCH-CONTRACT
---

# PKOS Schema

## Purpose

PKOS is ECC's canonical knowledge subsystem. It preserves immutable evidence, normalizes entities, stores typed relationships and supports temporal and lexical retrieval. Semantic/vector retrieval remains deferred in Phase 1.

## Architectural rules

1. Source evidence is immutable.
2. Derived knowledge never overwrites source evidence.
3. Every derived assertion records provenance and confidence.
4. Every relationship is typed and temporally bounded.
5. Search indexes and attention projections are rebuildable.
6. Entity merges are auditable and reversible.
7. Workspace isolation applies to every row, relationship and query.
8. Actor and workspace come from the authenticated server-side session, never client payloads.

## Logical-to-physical mapping

The logical PKOS vocabulary is stable even when physical tables differ by phase.

| Logical concept | Phase 0/1 physical representation | Rule |
|---|---|---|
| `pkos_entities` | Domain aggregate tables plus `pkos_nodes` projection | Domain tables remain authoritative; nodes are rebuildable references |
| `pkos_relationships` | `pkos_edges` | Composite `(workspace_id, node_id)` foreign keys enforce isolation |
| `pkos_evidence` | `pkos_evidence` | Authoritative immutable evidence metadata |
| `pkos_provenance` | Evidence references and aggregate provenance JSON | Dedicated provenance table may be introduced later by migration |
| `pkos_aliases` | Deferred | Required before external identity resolution or merges |
| `pkos_merge_records` | Deferred | Required before non-trivial entity merging |
| `pkos_embeddings` | Deferred | Not a Phase 1 dependency |
| `pkos_chunks` | Deferred | Required only for long-document ingestion phases |

Phase 1 MUST NOT create a second authoritative entity store inside PKOS. Tasks, commitments, notes, meetings, calendar events, risks and recommendations remain owned by their canonical domains. PKOS stores evidence-backed projections and relationships.

## Core physical contracts

### `pkos_nodes`

A typed reference to a canonical domain aggregate.

Required fields:

- `id`: UUID
- `workspace_id`: UUID
- `entity_type`: controlled vocabulary
- `entity_id`: canonical aggregate UUID
- `canonical_name`: optional searchable label
- `status`: projection status
- `attributes`: approved JSONB projection fields only
- `confidence`: 0..1; `1` for authoritative local entities
- `version`: source aggregate version used to build the projection
- `created_at`, `updated_at`, `deleted_at`

Unique constraints:

- `(workspace_id, id)`
- `(workspace_id, entity_type, entity_id)`

Phase 1 entity types:

```text
task commitment note calendar_event meeting risk attention_item
recommendation brief evidence person organization project goal decision
```

### `pkos_edges`

A typed directed relationship between two PKOS nodes.

Required fields:

- `id`, `workspace_id`
- `source_node_id`, `target_node_id`
- `relationship_type`
- `attributes`
- `confidence`
- `evidence_id` where externally or model derived
- `valid_from`, `valid_to`
- `status`: `active|disputed|invalidated`
- `created_at`, `invalidated_at`

Composite foreign keys MUST reference `(workspace_id, id)` on both source and target nodes.

Initial vocabulary:

```text
MEMBER_OF PARTICIPATES_IN OWNS ASSIGNED_TO MAKES MADE_TO
RELATES_TO ADVANCES THREATENS BLOCKS DEPENDS_ON PRODUCES
SUPPORTS SUPERSEDES ABOUT MENTIONS DERIVED_FROM SCHEDULED_FOR
PROPOSES_ACTION_ON HIGHLIGHTS
```

### `pkos_evidence`

Immutable pointer to source material.

Required fields:

- `id`, `workspace_id`
- `source_system`
- `source_id`, optional `source_revision`
- `media_type`
- `content_uri`
- `checksum` using SHA-256
- `captured_at`, `observed_at`
- optional excerpt boundaries
- redacted metadata
- `access_state`: `available|missing|permission_denied|deleted`

Unique identity: `(workspace_id, source_system, source_id, source_revision)` where supported.

## Phase 1 search boundary

Phase 1 search is deterministic and PostgreSQL-only:

1. normalized exact match,
2. prefix match,
3. PostgreSQL full-text search,
4. approved trigram similarity,
5. bounded recency and entity-type boosts.

Every result includes score components, entity type, timestamp context, source context and evidence access state.

The following are explicitly deferred:

- embeddings,
- ANN indexes,
- semantic similarity,
- external vector databases,
- dedicated graph databases,
- multi-hop graph ranking.

No Phase 1 manifest, migration, Docker service or runtime code may require those deferred capabilities.

## Temporal and conflict rules

`created_at` records when ECC learned information. `valid_from` and `valid_to` record when it is true. Conflicting assertions are retained independently with evidence; resolution creates a new authoritative assertion or invalidates a relationship without deleting evidence.

## Indexing for Phase 1

Required indexes:

- workspace + entity type + status,
- workspace + entity type + entity ID,
- canonical name normalized/prefix search,
- approved PostgreSQL full-text index,
- optional trigram index behind `phase1.search_trigram`,
- edge source/type/target,
- evidence source identity,
- validity ranges.

Embedding ANN indexes are not permitted in Phase 1.

## Backup and rebuild

Authoritative backups include domain aggregate tables, relationships and evidence metadata/content references. PKOS nodes, lexical indexes, briefs and attention projections may be rebuilt, but restore tests must validate referential integrity, checksums, composite workspace constraints and representative search results.

## Change policy

Breaking changes require a migration plan, rollback or forward-fix plan, API and event review, retrieval regression tests, backup/restore verification and an ADR when ownership or storage technology changes.