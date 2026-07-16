---
id: PHASE-001-API-SCHEMAS
title: Phase 1 API Schemas
status: Approved
version: 1.0.3
owner: Lucky Jain
---

# Phase 1 API Schemas

## Common rules

All endpoints are under `/api/v1`. Actor, workspace and Phase 1 accountable owner come from the authenticated server-side session. Browser-supplied `workspace_id`, `actor_id`, or owner fields are rejected with `422 FORBIDDEN_FIELD`. Counterparty and related-person references may be supplied only where the contract permits.

Mutations require `Idempotency-Key` and `expected_version` when updating or transitioning an existing aggregate. Responses include `request_id`, `correlation_id`, and entity `version`. Pagination uses `{items,next_cursor}`. Errors use the canonical envelope.

Due inputs use mutually exclusive `due_date` (`YYYY-MM-DD`) and `due_at` (ISO-8601 datetime). Supplying both returns `422 MUTUALLY_EXCLUSIVE_FIELDS`.

Every successful lifecycle action returns `200` with the current entity representation. Phase 1 lifecycle actions do not return `204`.

## Task endpoints

- `POST /tasks`: title, optional description, manual_priority, optional due_date or due_at, optional initial status and source_ref. Owner is session-derived.
- `GET /tasks`: filters status[], due_before, due_after, priority[], pinned, include_archived, cursor, limit 1..100.
- `GET /tasks/{id}`.
- `PATCH /tasks/{id}`: expected_version and mutable fields.
- `POST /tasks/{id}/complete`: expected_version.
- `POST /tasks/{id}/cancel`: expected_version and optional reason.
- `POST /tasks/{id}/archive`: expected_version.
- `POST /tasks/{id}/restore`: expected_version; restores pre_archive_status.

Initial status cannot be completed or cancelled. Idempotent completion/cancel/archive/restore returns the current representation when already in the requested state.

## Commitment endpoints

`POST /commitments`, `GET /commitments`, `GET /commitments/{id}`, `PATCH /commitments/{id}`, and actions `/confirm`, `/fulfil`, `/cancel`, `/archive`, `/restore`.

Create accepts summary, direction, optional counterparty_person_id/name, importance, mutually exclusive due_date/due_at and optional evidence_id. Owner is session-derived. Detected commitments cannot become active without confirm. Lifecycle actions require expected_version and return the current representation.

## Note endpoints

`POST /notes`, `GET /notes`, `GET /notes/{id}`, `PATCH /notes/{id}`, `POST /notes/{id}/archive`, `POST /notes/{id}/restore`.

Create/update accepts title, body, note_type and optional meeting_id. Body is 1..100000 characters. Notes use optimistic concurrency. Autosave clients debounce and submit the last known version. Audit exposes change history but not reconstructable prior bodies.

## Meeting and calendar endpoints

`GET /calendar/events?date=YYYY-MM-DD`, `POST /calendar/events`, `PATCH /calendar/events/{id}`, `GET /meetings`, `GET /meetings/{id}`, `POST /meetings`, `PATCH /meetings/{id}`.

Phase 1 supports local/manual events only. Linked meetings reject independent timing fields with `422 LINKED_MEETING_TIMING_READ_ONLY`; timing is projected from CalendarEvent. Standalone Meeting API fields map as follows:

- `starts_at` -> `meetings.standalone_starts_at`
- `ends_at` -> `meetings.standalone_ends_at`
- `timezone` -> `meetings.standalone_timezone`

Standalone meetings require all three API fields. Linked Meeting responses expose projected `starts_at`, `ends_at`, and `timezone` from CalendarEvent. Linking a standalone Meeting adopts CalendarEvent timing.

`PATCH /meetings/{id}` accepts meeting-owned content plus `expected_version`. For a standalone Meeting, rescheduling supplies `starts_at`, `ends_at`, and `timezone` together; partial or null timing, offset-naive instants, invalid IANA zones, and `ends_at <= starts_at` return `422`. These public fields update the corresponding `standalone_*` columns. A linked Meeting rejects any of the three timing fields with `422 LINKED_MEETING_TIMING_READ_ONLY`; clients reschedule it only through `PATCH /calendar/events/{calendar_event_id}`.

## Risk endpoints

`POST /risks`, `GET /risks`, `GET /risks/{id}`, `PATCH /risks/{id}`, `POST /risks/{id}/close`. Probability and impact are integers 1..5. Owner is session-derived.

## Dashboard

`GET /dashboard/today` accepts optional date, defaulting to today in workspace timezone. Ranked items include entity_ref, score, confidence, factors, explanation and evidence.

## Morning brief

`GET /briefs/morning?date=YYYY-MM-DD` returns the persisted current brief or deterministic fallback. `POST /briefs/morning` requests idempotent refresh. AI enrichment is optional and feature-flagged.

## Recommendations

`GET /recommendations`, `GET /recommendations/{id}`, and actions `/publish`, `/confirm`, `/reject`, `/defer`, `/pin`.

- `POST /recommendations/{id}/publish` requires `expected_version`, is valid only from `proposed`, transitions to `pending_confirmation`, and returns the current recommendation.
- `POST /recommendations/{id}/confirm` includes expected_recommendation_version and target expected_version. It is valid only from `pending_confirmation`; it atomically transitions to accepted, mutates the local target, transitions to executed, writes audit records and outbox events, then commits.
- Rejected, expired and superseded recommendations cannot execute.
- Failures roll back the target and successful state transitions; a separate failed-attempt record may then be written.

## Search

`GET /search`: q 1..500, types[]=task|commitment|note|meeting|calendar_event|risk, include_archived=false, cursor, limit. Results include entity type/id, title, snippet, matched_fields, score_components, source, updated_at, archived and evidence references.

## Audit

`GET /audit?aggregate_type=&aggregate_id=&actor_id=&event_type=&from=&to=&cursor=&limit=`. Access is workspace-scoped and read-only.

## Common response models and status codes

Every entity contains id, timestamps, version, archived_at and links.audit. Evidence summaries contain id, source_type, label, captured_at, optional excerpt and access status `available|missing|permission_denied|deleted`.

Status codes: 200 query/update/lifecycle action, 201 create, 400 malformed, 401 unauthenticated, 403 forbidden capability, 404 absent or cross-workspace, 409 version/idempotency/state conflict, 422 validation, 429 throttled and 503 dependency unavailable with fallback metadata where available.
