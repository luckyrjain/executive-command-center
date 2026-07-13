---
id: PKOS-SCHEMA
title: Personal Knowledge Operating System Schema
status: Accepted
version: 1.0.0
owner: Lucky Jain
related:
  - ADR-0003
  - ADR-0006
  - DOMAIN-MODEL
  - EVENT-CATALOG
---

# PKOS Schema

## Purpose

PKOS is the canonical knowledge subsystem for ECC. It preserves evidence, normalizes entities, stores typed relationships and supports temporal, lexical, semantic and graph retrieval.

## Architectural rules

1. Source evidence is immutable.
2. Derived knowledge never overwrites source evidence.
3. Every derived assertion records provenance and confidence.
4. Every relationship is typed and temporally bounded.
5. Embeddings, summaries and search indexes are rebuildable projections.
6. Entity merges are auditable and reversible.
7. Workspace isolation applies to every row and retrieval query.

## Logical schema

### `pkos_entities`

| Field | Type | Rule |
|---|---|---|
| id | uuid | primary key |
| workspace_id | uuid | required, indexed |
| entity_type | text | controlled vocabulary |
| canonical_name | text | required where meaningful |
| attributes | jsonb | type-specific attributes |
| status | text | lifecycle status |
| valid_from | timestamptz | optional temporal start |
| valid_to | timestamptz | optional temporal end |
| confidence | decimal | 0..1 for derived entities; 1 for authoritative local entities |
| provenance_id | uuid | nullable for authoritative local entities |
| version | bigint | optimistic concurrency |
| created_at | timestamptz | required |
| updated_at | timestamptz | required |
| deleted_at | timestamptz | soft deletion |

Allowed initial `entity_type` values:

```text
workspace user person organization project goal task commitment
calendar_event meeting conversation message document decision
knowledge_item evidence risk attention_item recommendation reminder
```

### `pkos_relationships`

| Field | Type | Rule |
|---|---|---|
| id | uuid | primary key |
| workspace_id | uuid | required |
| from_entity_id | uuid | required |
| relationship_type | text | controlled vocabulary |
| to_entity_id | uuid | required |
| attributes | jsonb | relationship metadata |
| confidence | decimal | 0..1 |
| provenance_id | uuid | required for inferred/imported relationships |
| valid_from | timestamptz | optional |
| valid_to | timestamptz | optional |
| status | text | active, disputed, invalidated |
| created_at | timestamptz | required |
| invalidated_at | timestamptz | optional |

Initial relationship vocabulary:

```text
MEMBER_OF PARTICIPATES_IN OWNS ASSIGNED_TO MAKES MADE_TO
RELATES_TO ADVANCES THREATENS BLOCKS DEPENDS_ON PRODUCES
SUPPORTS SUPERSEDES ABOUT MENTIONS DERIVED_FROM SCHEDULED_FOR
```

### `pkos_evidence`

| Field | Type | Rule |
|---|---|---|
| id | uuid | primary key |
| workspace_id | uuid | required |
| source_system | text | local, gmail, calendar, github, jira, file, etc. |
| source_id | text | immutable source identifier |
| source_revision | text | optional revision/etag |
| media_type | text | required |
| content_uri | text | content-addressed object reference |
| checksum | text | SHA-256 |
| captured_at | timestamptz | required |
| observed_at | timestamptz | required |
| excerpt_start | integer | optional |
| excerpt_end | integer | optional |
| metadata | jsonb | source-specific metadata |

Unique constraint: `(workspace_id, source_system, source_id, source_revision)` where supported.

### `pkos_provenance`

| Field | Type | Rule |
|---|---|---|
| id | uuid | primary key |
| workspace_id | uuid | required |
| derivation_type | text | imported, user_asserted, rule_derived, model_derived |
| evidence_ids | uuid[] | at least one for imported/model-derived assertions |
| model_execution_id | uuid | required for model-derived output |
| rule_id | text | required for rule-derived output |
| explanation | text | human-readable rationale |
| confidence | decimal | 0..1 |
| created_at | timestamptz | required |

### `pkos_aliases`

Maps source identities and historical merged IDs to canonical entities.

| Field | Type |
|---|---|
| workspace_id | uuid |
| alias_type | text |
| alias_value | text |
| entity_id | uuid |
| source_system | text |
| valid_from | timestamptz |
| valid_to | timestamptz |

Unique active alias: `(workspace_id, alias_type, alias_value, source_system)`.

### `pkos_merge_records`

Stores entity-resolution decisions:

- surviving entity ID
- merged entity IDs
- proposal evidence
- actor or model execution
- timestamp
- reversible field-level change set
- status: proposed, confirmed, reversed

### `pkos_embeddings`

| Field | Type | Rule |
|---|---|---|
| workspace_id | uuid | required |
| object_type | text | entity, evidence, chunk |
| object_id | uuid | required |
| model_id | text | exact embedding model/version |
| content_hash | text | detects stale embeddings |
| embedding | vector | dimension determined by approved model |
| created_at | timestamptz | required |

Unique constraint: `(workspace_id, object_type, object_id, model_id, content_hash)`.

### `pkos_chunks`

Documents and long messages are chunked into immutable content-derived units with:

- parent evidence ID
- ordinal
- start/end offsets
- text checksum
- text content or secure content reference
- token count
- heading/path metadata

## Retrieval contract

PKOS supports four retrieval modes through one service contract:

1. Exact/entity lookup
2. Filtered lexical search
3. Semantic similarity search
4. Relationship traversal

Hybrid search combines normalized lexical score, semantic score, recency, authority and relationship proximity. The response must include score components and evidence references.

## Temporal reasoning

Relationships and assertions may use `valid_from` and `valid_to`. `created_at` records when ECC learned the information; validity records when it was true. Queries must distinguish transaction time from valid time.

## Conflict handling

Conflicting assertions are stored independently. PKOS marks them as disputed and retains all evidence. Resolution creates a new authoritative assertion or invalidates a relationship; it does not delete contradictory evidence.

## Entity resolution

Resolution stages:

```text
normalize identifiers
-> generate candidates
-> score candidates
-> auto-link only below-risk deterministic matches
-> propose high-impact merges
-> human confirmation
-> preserve aliases and merge record
```

Persons, organizations, commitments and decisions require human confirmation for non-deterministic merges during Phase 0/1.

## Indexing

Required indexes:

- workspace + entity type + status
- canonical name trigram/full-text
- relationship from/type/to
- evidence source identity
- validity ranges
- embedding ANN index
- JSONB GIN only for approved query paths

## Backup and rebuild

Authoritative backup includes transactional tables and source evidence objects. Embeddings, lexical indexes, summaries and attention projections may be excluded because they are rebuildable. Restore validation must prove referential integrity, checksums and workspace isolation.

## Schema change policy

All breaking changes require:

- migration plan
- rollback plan
- event compatibility review
- API contract update
- retrieval regression tests
- backup/restore verification
- ADR when ownership or storage technology changes
