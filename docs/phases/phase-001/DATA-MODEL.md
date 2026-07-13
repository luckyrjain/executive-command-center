---
id: PHASE-001-DATA-MODEL
title: Phase 1 Data Model
status: Approved
version: 1.0.1
owner: Lucky Jain
---

# Phase 1 Data Model

## Universal rules

All rows are workspace-scoped. `workspace_id`, authenticated actor and Phase 1 accountable owner are derived from the server-side session and are never accepted from browser payloads. Every mutable entity has `id uuid`, `workspace_id uuid`, `created_at timestamptz`, `updated_at timestamptz`, `version bigint default 1`, `archived_at timestamptz null`, `pre_archive_status text null`, and actor audit fields where applicable. Composite foreign keys enforce `(workspace_id, referenced_id)` ownership.

Archived records are excluded unless `include_archived=true`. Restore clears `archived_at` and restores `pre_archive_status`. Mutations use optimistic concurrency with `expected_version`.

## Tables

### tasks

Fields: title 1..500, description nullable, owner_id derived from session, status `captured|planned|in_progress|blocked|completed|cancelled`, manual_priority `low|medium|high|critical`, `due_date date null`, `due_at timestamptz null`, blocked_reason nullable, blocked_on_person_id nullable, completed_at nullable, pinned boolean, source_type `local|meeting|import`, source_ref nullable, created_by, updated_by. Exactly one of `due_date` and `due_at` may be populated.

Indexes: `(workspace_id,status)`, `(workspace_id,due_date)`, `(workspace_id,due_at)`, `(workspace_id,manual_priority)`, `(workspace_id,pinned)`.

Events: created, updated, completed, cancelled, archived, restored.

### commitments

Fields: summary 1..500, description nullable, direction `made_by_me|made_to_me`, owner_id derived from session, counterparty_person_id nullable, counterparty_name nullable, status `detected|confirmed|active|fulfilled|broken|cancelled`, `due_date date null`, `due_at timestamptz null`, importance `low|medium|high|critical`, evidence_id nullable, confidence decimal 0..1, fulfilled_at nullable, pinned boolean. Exactly one due field may be populated.

Indexes: `(workspace_id,status,due_date)`, `(workspace_id,status,due_at)`, `(workspace_id,owner_id)`, `(workspace_id,importance)`.

AI-detected commitments remain `detected` until human confirmation. `direction=made_to_me` is the deterministic waiting-on signal.

### notes

Owned by the Knowledge Platform. Fields: title nullable, body text required, note_type `general|meeting|decision|journal`, meeting_id nullable, source_type `local|meeting`, source_ref nullable, search_document tsvector generated/projection, created_by, updated_by.

Indexes: `(workspace_id,updated_at desc)`, GIN search index. Notes retain audit change history, body checksum and changed-field metadata; Phase 1 does not provide reconstructable body revisions or hard delete.

### calendar_events

Phase 1 source is local/manual plus seed fixtures only. Fields: external_source `local`, external_id nullable, title, starts_at, ends_at, all_day, timezone IANA, location nullable, description nullable, status `confirmed|tentative|cancelled`, source_authoritative boolean default true.

Unique: `(workspace_id,external_source,external_id)` when external_id is non-null.

### meetings

Fields: calendar_event_id nullable, title, standalone_starts_at nullable, standalone_ends_at nullable, standalone_timezone nullable, status `planned|in_progress|completed|cancelled`, agenda nullable, preparation nullable, notes_summary nullable. A CalendarEvent may have zero or one Meeting. Linked meetings derive timing exclusively from CalendarEvent. Standalone timing fields are required only when calendar_event_id is null. Linking adopts CalendarEvent timing while preserving Meeting identity.

### risks

Fields: description, probability integer 1..5, impact integer 1..5, status `identified|assessed|monitoring|mitigating|materialized|closed`, owner_id derived from session, mitigation nullable, trigger nullable, review_at nullable, project_id nullable, pinned boolean. Priority impact is `probability * impact`.

### attention_items

Deterministic ranked projection. Fields: entity_type, entity_id, source_entity_version, score integer 0..100, confidence decimal 0..1, factors jsonb, explanation text, generated_at, expires_at, pinned boolean, dismissed_at nullable, dismissed_entity_version nullable, deferred_until nullable.

Unique active projection: `(workspace_id,entity_type,entity_id)`. Dismissal applies only while the source entity remains at `dismissed_entity_version`; a later entity version is a material change and clears dismissal during regeneration.

### recommendations

A proposed action, never a direct mutation. Fields: recommendation_type, target_type, target_id nullable, proposed_action jsonb, expected_version nullable, rationale, confidence, status `proposed|pending_confirmation|accepted|rejected|expired|superseded|executed|failed`, evidence_ids uuid[], expires_at, confirmed_by nullable, confirmed_at nullable, execution_result jsonb nullable, source `rule|ai`.

Allowed transitions: `proposed -> pending_confirmation`; `pending_confirmation -> rejected|expired|superseded|accepted`; `accepted -> executed|failed`.

### recommendation_feedback

Fields: recommendation_id, action `dismiss|defer|pin|accept|reject`, reason nullable, defer_until nullable, actor_id derived from session, created_at. Feedback is append-only.

### audit_events

Append-only. Fields: event_type, aggregate_type, aggregate_id, aggregate_version, actor_id nullable, request_id, correlation_id, before jsonb nullable, after jsonb nullable, changed_fields text[], occurred_at, metadata jsonb. Secrets and raw private note bodies are excluded or redacted.

Indexes: `(workspace_id,aggregate_type,aggregate_id,occurred_at desc)`, `(workspace_id,actor_id,occurred_at desc)`.

### entity_evidence

Fields: entity_type, entity_id, evidence_id, relation `supports|source|contradicts`, created_at. Composite workspace ownership is enforced.

### idempotency_records

Fields: key, actor_id, request_hash, response_status, response_body jsonb, created_at, expires_at. Unique `(workspace_id,actor_id,key)`. Reuse with a different request hash returns `409 IDEMPOTENCY_CONFLICT`.

## Time and lifecycle

UTC is used for storage. `workspace.timezone` defines “today.” Date-only due items use `due_date`; ranking interprets them at 23:59:59 in the workspace timezone without converting away the original date precision.

## Physical PKOS mapping

Phase 0 `pkos_nodes`, `pkos_edges`, and `pkos_evidence` are the physical base for logical `pkos_entities`, `pkos_relationships`, and `pkos_evidence`. `PKOS-SCHEMA.md` defines the field-level mapping, required Phase 1 migrations, JSONB mappings and deferred logical fields. No second authoritative knowledge representation may be created in Phase 1.
