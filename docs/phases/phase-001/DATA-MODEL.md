---
id: PHASE-001-DATA-MODEL
title: Phase 1 Data Model
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Phase 1 Data Model

## Universal rules

All rows are workspace-scoped. `workspace_id` is derived from the authenticated server-side session and is never accepted from browser payloads. Every mutable entity has `id uuid`, `workspace_id uuid`, `created_at timestamptz`, `updated_at timestamptz`, `version bigint default 1`, `archived_at timestamptz null`, and actor audit fields where applicable. Composite foreign keys enforce `(workspace_id, referenced_id)` ownership.

Soft deletion is the default. Archived records are excluded unless `include_archived=true`. Mutations use optimistic concurrency with `expected_version`.

## Tables

### tasks

Fields: title 1..500, description nullable, status `captured|planned|in_progress|blocked|completed|cancelled`, manual_priority `low|medium|high|critical`, due_at nullable, completed_at nullable, blocked_reason nullable, pinned boolean, source_type `local|meeting|import`, source_ref nullable, created_by, updated_by.
Indexes: `(workspace_id,status)`, `(workspace_id,due_at)`, `(workspace_id,manual_priority)`, `(workspace_id,pinned)`.
Events: created, updated, completed, archived, cancelled.

### commitments

Fields: summary 1..500, description nullable, direction `made_by_me|made_to_me`, owner_id, counterparty_name nullable, status `detected|confirmed|active|fulfilled|broken|cancelled`, due_at nullable, importance `low|medium|high|critical`, evidence_id nullable, confidence decimal 0..1, fulfilled_at nullable, pinned boolean.
Indexes: `(workspace_id,status,due_at)`, `(workspace_id,owner_id)`, `(workspace_id,importance)`.
AI-detected commitments remain `detected` until human confirmation.

### notes

Owned by the Knowledge Platform. Fields: title nullable, body text required, note_type `general|meeting|decision|journal`, meeting_id nullable, source_type `local|meeting`, source_ref nullable, search_document tsvector generated/projection, created_by, updated_by.
Indexes: `(workspace_id,updated_at desc)`, GIN search index. Notes retain version history through audit events; hard delete is not exposed in Phase 1.

### calendar_events

Phase 1 source is local/manual plus seed fixtures only. External calendar connectors are deferred. Fields: external_source `local`, external_id nullable, title, starts_at, ends_at, all_day, timezone IANA, location nullable, description nullable, status `confirmed|tentative|cancelled`, source_authoritative boolean default true.
Unique: `(workspace_id,external_source,external_id)` when external_id is non-null.

### meetings

Fields: calendar_event_id nullable, title, starts_at, ends_at, timezone, status `planned|in_progress|completed|cancelled`, agenda nullable, preparation nullable, notes_summary nullable. A CalendarEvent may have zero or one Meeting. Rescheduling preserves Meeting identity.

### risks

Fields: description, probability integer 1..5, impact integer 1..5, status `identified|assessed|monitoring|mitigating|materialized|closed`, owner_id, mitigation nullable, trigger nullable, review_at nullable, project_id nullable, pinned boolean.
Priority impact is `probability * impact`.

### attention_items

Deterministic ranked projection. Fields: entity_type, entity_id, score integer 0..100, confidence decimal 0..1, factors jsonb, explanation text, generated_at, expires_at, pinned boolean, dismissed_at nullable, deferred_until nullable.
Unique active projection: `(workspace_id,entity_type,entity_id)`.

### recommendations

A proposed action, never a direct mutation. Fields: recommendation_type, target_type, target_id nullable, proposed_action jsonb, expected_version nullable, rationale, confidence, status `proposed|pending_confirmation|accepted|rejected|expired|superseded|executed|failed`, evidence_ids uuid[], expires_at, confirmed_by nullable, confirmed_at nullable, execution_result jsonb nullable, source `rule|ai`.

### recommendation_feedback

Fields: recommendation_id, action `dismiss|defer|pin|accept|reject`, reason nullable, defer_until nullable, actor_id, created_at. Feedback is append-only.

### audit_events

Append-only. Fields: event_type, aggregate_type, aggregate_id, aggregate_version, actor_id nullable, request_id, correlation_id, before jsonb nullable, after jsonb nullable, changed_fields text[], occurred_at, metadata jsonb. Secrets and raw private note bodies are excluded or redacted.
Indexes: `(workspace_id,aggregate_type,aggregate_id,occurred_at desc)`, `(workspace_id,actor_id,occurred_at desc)`.

### entity_evidence

Fields: entity_type, entity_id, evidence_id, relation `supports|source|contradicts`, created_at. Composite workspace ownership is enforced.

### idempotency_records

Fields: key, actor_id, request_hash, response_status, response_body jsonb, created_at, expires_at. Unique `(workspace_id,actor_id,key)`. Reuse with a different request hash returns `409 IDEMPOTENCY_CONFLICT`.

## Time and lifecycle

UTC is used for storage. `workspace.timezone` is an IANA timezone and defines “today.” Date-only due items become due at 23:59:59 in the workspace timezone for ranking, while the API preserves date-only semantics.

## Physical PKOS mapping

Phase 0 `pkos_nodes`, `pkos_edges`, and `pkos_evidence` are the physical Phase 1 implementation of logical `pkos_entities`, `pkos_relationships`, and `pkos_evidence`. No second knowledge representation may be created in Phase 1.