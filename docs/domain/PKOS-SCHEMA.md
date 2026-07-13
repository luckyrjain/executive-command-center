---
id: PKOS-SCHEMA
title: Personal Knowledge Operating System Schema
status: Accepted
version: 1.1.1
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

PKOS is ECC's canonical knowledge subsystem. It preserves immutable evidence, normalizes entity references, stores typed relationships and supports temporal and lexical retrieval. Semantic/vector retrieval remains deferred in Phase 1.

## Architectural rules

Source evidence is immutable; derived knowledge never overwrites evidence; derived assertions record provenance and confidence; relationships are typed and temporal; projections are rebuildable; workspace isolation applies to every row and query; actor/workspace come from the server-side session.

## Logical-to-physical mapping

Domain aggregate tables are authoritative. `pkos_nodes` and `pkos_edges` are rebuildable projections and relationships, not a second aggregate store.

| Logical field | Phase 0 physical source | Phase 1 rule |
|---|---|---|
| entity identity | `pkos_nodes.id` plus domain aggregate ID | add/use `entity_id` and unique `(workspace_id,entity_type,entity_id)` |
| workspace boundary | `pkos_nodes.workspace_id` | retain composite workspace constraints |
| entity type | `pkos_nodes.node_type` | map/rename logically to controlled `entity_type` |
| canonical name | `pkos_nodes.label` or attributes | use approved label column/projection; nullable |
| attributes | `pkos_nodes.properties` JSONB | only approved searchable/display projection fields |
| lifecycle status | absent or properties JSONB | Phase 1 migration adds explicit `status` or documented generated projection |
| confidence | absent or properties JSONB | Phase 1 migration adds explicit decimal 0..1 |
| source version | absent | Phase 1 migration adds `version bigint` copied from aggregate |
| temporal validity | absent on nodes | deferred for nodes; edges use valid_from/valid_to in Phase 1 migration |
| provenance | evidence relation/properties | dedicated provenance table deferred; evidence IDs and rule/model metadata remain explicit |
| deleted state | existing deleted/archived marker where present | normalize to `deleted_at` projection semantics |

| Logical relationship field | Phase 0 physical source | Phase 1 rule |
|---|---|---|
| from/to entity | `pkos_edges.source_node_id`, `target_node_id` | retain with composite `(workspace_id,id)` FKs |
| relationship type | `pkos_edges.edge_type` | controlled vocabulary |
| attributes | `pkos_edges.properties` JSONB | approved metadata only |
| confidence | absent or JSONB | Phase 1 migration adds explicit decimal |
| evidence | evidence reference absent/JSONB | Phase 1 migration adds nullable `evidence_id` composite FK |
| validity | absent | Phase 1 migration adds valid_from/valid_to |
| status | absent | Phase 1 migration adds `active|disputed|invalidated` |

| Logical concept | Physical representation | Status |
|---|---|---|
| `pkos_entities` | domain tables plus `pkos_nodes` projection | active |
| `pkos_relationships` | `pkos_edges` | active with Phase 1 column migrations above |
| `pkos_evidence` | `pkos_evidence` | active authoritative evidence metadata |
| `pkos_provenance` | evidence references and projection metadata | dedicated table deferred |
| aliases and merge records | none | deferred until identity-resolution phase |
| embeddings and chunks | none | explicitly deferred |

No statement in this document implies that Phase 0 already contains every logical field; the tables become Phase 1-compatible only after the listed migrations.

## Phase 1 entity and relationship vocabulary

Entity types: task, commitment, note, calendar_event, meeting, risk, attention_item, recommendation, brief, evidence, person, organization, project, goal and decision.

Relationships: MEMBER_OF, PARTICIPATES_IN, OWNS, ASSIGNED_TO, MAKES, MADE_TO, RELATES_TO, ADVANCES, THREATENS, BLOCKS, DEPENDS_ON, PRODUCES, SUPPORTS, SUPERSEDES, ABOUT, MENTIONS, DERIVED_FROM, SCHEDULED_FOR, PROPOSES_ACTION_ON and HIGHLIGHTS.

## Evidence contract

`pkos_evidence` stores id, workspace_id, source_system, source_id/revision, media_type, content_uri, SHA-256 checksum, captured_at, observed_at, excerpt boundaries, redacted metadata and access_state `available|missing|permission_denied|deleted`. Source identity is unique per workspace where supported.

## Phase 1 search boundary

Search is PostgreSQL-only: normalized exact, prefix, full-text, approved trigram, recency and entity-type boosts. Results expose score components, timestamps, source context and evidence access state. Embeddings, ANN, semantic similarity, external vector databases, dedicated graph databases and multi-hop ranking are not permitted Phase 1 dependencies.

## Indexing, backup and change policy

Required indexes cover workspace/entity/status, entity identity, normalized/prefix/full-text labels, optional trigram, edge source/type/target, evidence identity and validity ranges. Backups include authoritative aggregates, relationships and evidence. Rebuildable projections may be regenerated, but restore validates checksums, referential integrity, composite workspace constraints and representative search results. Breaking changes require migration, rollback/forward-fix, API/event review, retrieval tests, backup/restore verification and an ADR when ownership or technology changes.
