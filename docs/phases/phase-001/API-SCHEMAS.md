---
id: PHASE-001-API-SCHEMAS
title: Phase 1 API Schemas
status: Approved
version: 1.0.0
owner: Lucky Jain
---

# Phase 1 API Schemas

## Common rules

All endpoints are under `/api/v1`. Actor and workspace come from the authenticated server-side session. Browser-supplied `workspace_id`, `actor_id`, or ownership fields are rejected with `422 FORBIDDEN_FIELD`.

Mutations require `Idempotency-Key` and `expected_version` where updating an existing aggregate. Responses include `request_id`, `correlation_id`, and entity `version`. Pagination uses `{items,next_cursor}`. Errors use the canonical error envelope.

## Task endpoints

### POST `/tasks`

Request: `title`, optional `description`, `manual_priority`, optional `due_at`, optional `status`, optional `source_ref`.
Validation: title 1..500; priority enum; due_at ISO-8601; initial status cannot be completed or cancelled.
Returns `201 TaskResponse`.
Errors: `422 VALIDATION_ERROR`, `409 IDEMPOTENCY_CONFLICT`.

### GET `/tasks`

Filters: status[], due_before, due_after, priority[], pinned, include_archived, cursor, limit 1..100.

### GET `/tasks/{task_id}`
Returns `404 TASK_NOT_FOUND` when outside the session workspace as well as when absent.

### PATCH `/tasks/{task_id}`
Request: `expected_version`, mutable patch fields. Returns `409 VERSION_CONFLICT` with current version and reload hint.

### POST `/tasks/{task_id}/complete`
Request: `expected_version`. Idempotent completion returns the existing completed representation.

### POST `/tasks/{task_id}/archive`
Request: `expected_version`.

## Commitment endpoints

`POST /commitments`, `GET /commitments`, `GET /commitments/{id}`, `PATCH /commitments/{id}`, `POST /commitments/{id}/confirm`, `POST /commitments/{id}/fulfil`, `POST /commitments/{id}/archive`.

Create request includes summary, direction, optional counterparty, owner, importance, due_at, evidence_id. Detected commitments cannot become active without confirm. Fulfil requires `expected_version` and optional evidence_id.

## Note endpoints

`POST /notes`, `GET /notes`, `GET /notes/{id}`, `PATCH /notes/{id}`, `POST /notes/{id}/archive`.

Create/update accepts title, body, note_type, optional meeting_id. Body is 1..100000 characters. Notes use optimistic concurrency and autosave clients must debounce and submit the last known version.

## Meeting and calendar endpoints

`GET /calendar/events?date=YYYY-MM-DD`, `POST /calendar/events`, `PATCH /calendar/events/{id}`, `GET /meetings`, `GET /meetings/{id}`, `POST /meetings`, `PATCH /meetings/{id}`.

Phase 1 supports local/manual events only. The server rejects external connector types with `409 FEATURE_NOT_ENABLED`.

## Risk endpoints

`POST /risks`, `GET /risks`, `GET /risks/{id}`, `PATCH /risks/{id}`, `POST /risks/{id}/close`.

Probability and impact are integers 1..5.

## Dashboard

### GET `/dashboard/today`

Query: optional `date`; defaults to today in workspace timezone.
Response:

```json
{
  "date": "2026-07-14",
  "timezone": "Asia/Kolkata",
  "meetings": [],
  "top_priorities": [],
  "overdue_commitments": [],
  "risks": [],
  "waiting_on": [],
  "recent_changes": [],
  "generated_at": "...",
  "request_id": "uuid"
}
```

Each ranked item includes `entity_ref`, `score`, `confidence`, `factors`, `explanation`, and `evidence[]`.

## Morning brief

`GET /briefs/morning?date=YYYY-MM-DD` returns the persisted current brief or generates the deterministic fallback if absent. `POST /briefs/morning` requests refresh and requires idempotency. AI enrichment is optional and feature-flagged.

## Recommendations

`GET /recommendations`, `GET /recommendations/{id}`, `POST /recommendations/{id}/confirm`, `POST /recommendations/{id}/reject`, `POST /recommendations/{id}/defer`, `POST /recommendations/{id}/pin`.

Confirm request includes `expected_recommendation_version` and the target aggregate `expected_version`. The server revalidates evidence, expiry, and target version before execution. A confirmation creates an audit event before and after execution.

## Search

### GET `/search`

Parameters: `q` 1..500, `types[]=task|commitment|note|meeting|risk`, `include_archived=false`, `cursor`, `limit`.
Response item includes entity type/id, title, snippet, matched_fields, score_components, source, updated_at, archived, and evidence references.

## Audit

`GET /audit?aggregate_type=&aggregate_id=&actor_id=&from=&to=&cursor=&limit=`. Access is workspace-scoped and read-only.

## Common response models

Every entity response contains `id`, timestamps, `version`, `archived_at`, and `links.audit`. Evidence summaries contain `id`, `source_type`, `label`, `captured_at`, optional excerpt, and access status `available|missing|permission_denied`.

## Status codes

`200` query/update, `201` create, `204` no-content action when no body is useful, `400` malformed, `401` unauthenticated, `403` forbidden capability, `404` absent or cross-workspace, `409` version/idempotency/state conflict, `422` validation, `429` throttled, `503` dependency unavailable with deterministic fallback metadata where available.