---
id: EVENT-CATALOG
title: Domain Event Catalog
status: Accepted
version: 1.4.0
owner: Lucky Jain
related:
  - ADR-0005
  - DOMAIN-MODEL
  - PHASE-001
  - PHASE-002
---

# Domain Event Catalog

## Envelope

Every event is immutable and uses the canonical Phase 0 envelope. Events are past tense, schema version is part of `event_type`, consumers are idempotent by `event_id`, sensitive content is referenced rather than copied, and breaking payload changes create a new version.

## Phase 1 catalog

| Event | Producer | Required payload |
|---|---|---|
| `task.created.v1` | Planning | task_id, owner_id, status, priority |
| `task.updated.v1` | Planning | task_id, changed_fields |
| `task.completed.v1` | Planning | task_id, completed_at |
| `task.cancelled.v1` | Planning | task_id, reason |
| `task.archived.v1` | Planning | task_id, archived_at, pre_archive_status |
| `task.restored.v1` | Planning | task_id, restored_status |
| `commitment.created.v1` | Communication | commitment_id, direction, importance |
| `commitment.detected.v1` | Communication | commitment_id, evidence_ids, confidence |
| `commitment.confirmed.v1` | Communication | commitment_id, owner_id, due_date, due_at |
| `commitment.updated.v1` | Communication | commitment_id, changed_fields |
| `commitment.fulfilled.v1` | Communication | commitment_id, fulfilled_at |
| `commitment.cancelled.v1` | Communication | commitment_id, reason |
| `commitment.archived.v1` | Communication | commitment_id, archived_at, pre_archive_status |
| `commitment.restored.v1` | Communication | commitment_id, restored_status |
| `note.created.v1` | Knowledge | note_id, note_type, meeting_id |
| `note.updated.v1` | Knowledge | note_id, changed_fields, body_checksum |
| `note.archived.v1` | Knowledge | note_id, archived_at |
| `note.restored.v1` | Knowledge | note_id |
| `calendar_event.created.v1` | Planning | calendar_event_id, starts_at, ends_at |
| `calendar_event.changed.v1` | Planning | calendar_event_id, changed_fields |
| `meeting.created.v1` | Planning | meeting_id, calendar_event_id |
| `meeting.updated.v1` | Planning | meeting_id, changed_fields |
| `risk.identified.v1` | Executive Intelligence | risk_id, probability, impact, owner_id |
| `risk.updated.v1` | Executive Intelligence | risk_id, changed_fields |
| `risk.closed.v1` | Executive Intelligence | risk_id, closed_at |
| `attention_item.created.v1` | Executive Intelligence | attention_item_id, entity_ref, score, factors |
| `attention_item.updated.v1` | Executive Intelligence | attention_item_id, score, changed_factors |
| `recommendation.generated.v1` | Executive Intelligence | recommendation_id, source, evidence_ids, confidence |
| `recommendation.confirmation_requested.v1` | Executive Intelligence | recommendation_id, target_ref, target_version |
| `recommendation.accepted.v1` | Executive Intelligence | recommendation_id, accepted_by |
| `recommendation.rejected.v1` | Executive Intelligence | recommendation_id, rejected_by, reason |
| `recommendation.deferred.v1` | Executive Intelligence | recommendation_id, deferred_until |
| `recommendation.pinned.v1` | Executive Intelligence | recommendation_id, pinned_by |
| `recommendation.executed.v1` | Owning domain | recommendation_id, target_ref, resulting_version |
| `recommendation.failed.v1` | Owning domain | recommendation_id, error_code, retryable |
| `morning_brief.requested.v1` | Executive Intelligence | user_id, briefing_date, refresh_reason |
| `morning_brief.generated.v1` | Executive Intelligence | brief_id, user_id, evidence_ids, generation_version |
| `morning_brief.stale.v1` | Executive Intelligence | brief_id, stale_reason |
| `feedback.recorded.v1` | Executive Intelligence | feedback_id, recommendation_id, action |

Existing foundation events remain valid.

## Phase 2 catalog

Added incrementally, one entry per delivery slice, alongside the code that emits it (`docs/superpowers/plans/2026-07-21-phase-2-knowledge-platform.md`). Producer is Knowledge Platform (Person/Organization creation goes through the same producer -- see that plan's Task 1 -- since Identity is the domain owner per `DOMAIN-MODEL.md` but the event describes the shared `pkos_nodes` aggregate, not a separate Identity-owned table).

| Event | Producer | Required payload |
|---|---|---|
| `knowledge_entity.created.v1` | Knowledge Platform | entity_id, version |
| `knowledge_entity.updated.v1` | Knowledge Platform | entity_id, version |
| `knowledge_entity.archived.v1` | Knowledge Platform | entity_id, version |
| `knowledge_entity.restored.v1` | Knowledge Platform | entity_id, version |
| `knowledge_entity.claim_recorded.v1` | Knowledge Platform | entity_id, claim_id |
| `relationship.created.v1` | Knowledge Platform | relationship_id |
| `relationship.invalidated.v1` | Knowledge Platform | relationship_id |
| `resolution_candidate.created.v1` | Knowledge Platform | candidate_id |
| `resolution_candidate.confirmed.v1` | Knowledge Platform | candidate_id |
| `resolution_candidate.rejected.v1` | Knowledge Platform | candidate_id |
| `resolution_candidate.deferred.v1` | Knowledge Platform | candidate_id |
| `entity_operation.merged.v1` | Knowledge Platform | operation_id |
| `entity_operation.reversed.v1` | Knowledge Platform | operation_id |
| `entity_operation.split.v1` | Knowledge Platform | operation_id |

All Phase 2 catalog events are now implemented; no remaining speculative entries.

## Recommendation publication rule

`recommendation.generated.v1` records creation in `proposed`. `recommendation.confirmation_requested.v1` is emitted only by `PublishRecommendation`, which transitions the aggregate from `proposed` to `pending_confirmation`. Confirmation and execution events cannot occur before that publication event.

## Audit relationship

Domain events do not replace audit events. `AUDIT-CONTRACT.md` contains the normative API-action -> audit-event -> domain-event mapping. Successful mutations write the aggregate, redacted audit record and outbox event in one transaction. Rejected authorization and version-conflict attempts may create audit records without domain events.

## Compatibility and failure handling

Consumers support current versions and may support the previous version during migrations. Deprecated versions require migration and replay tests. Failed deliveries move to the dead-letter store with the original envelope, failure category, retry count and next action. Manual replay preserves `event_id` and creates a new delivery-attempt identifier.
