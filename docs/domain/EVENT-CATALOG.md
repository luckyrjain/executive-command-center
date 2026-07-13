---
id: EVENT-CATALOG
title: Domain Event Catalog
status: Accepted
version: 1.0.0
owner: Lucky Jain
related:
  - ADR-0005
  - DOMAIN-MODEL
---

# Domain Event Catalog

## Event envelope

Every event uses the following envelope:

```json
{
  "event_id": "uuid",
  "event_type": "task.created.v1",
  "occurred_at": "2026-07-13T00:00:00Z",
  "published_at": "2026-07-13T00:00:00Z",
  "workspace_id": "uuid",
  "actor_id": "uuid|null",
  "aggregate_type": "task",
  "aggregate_id": "uuid",
  "aggregate_version": 1,
  "correlation_id": "uuid",
  "causation_id": "uuid|null",
  "producer": "planning",
  "data": {}
}
```

Rules:

- Events are immutable and named in past tense.
- Schema version is part of `event_type`.
- Consumers are idempotent by `event_id`.
- Personally sensitive content is referenced by entity ID rather than copied unless required.
- Breaking payload changes create a new event version.

## Catalog

| Event | Producer | Primary consumers | Required payload |
|---|---|---|---|
| `workspace.created.v1` | Identity | Audit, Configuration | workspace_id, owner_id |
| `person.created.v1` | Identity | Knowledge | person_id, display_name |
| `person.merge_proposed.v1` | Knowledge | Identity, Executive UI | candidate_ids, confidence, evidence_ids |
| `person.merged.v1` | Identity | All referencing domains | surviving_id, alias_ids, merge_record_id |
| `organization.created.v1` | Identity | Knowledge, Planning | organization_id, name |
| `project.created.v1` | Planning | Knowledge, Executive Intelligence | project_id, owner_id, status |
| `project.status_changed.v1` | Planning | Knowledge, Risk, Executive Intelligence | project_id, old_status, new_status |
| `goal.created.v1` | Planning | Knowledge, Executive Intelligence | goal_id, owner_id |
| `task.created.v1` | Planning | Knowledge, Executive Intelligence | task_id, owner_id, status, priority |
| `task.updated.v1` | Planning | Knowledge, Executive Intelligence | task_id, changed_fields |
| `task.completed.v1` | Planning | Knowledge, Executive Intelligence | task_id, completed_at |
| `commitment.detected.v1` | Communication | Executive Intelligence | commitment_id, parties, evidence_ids, confidence |
| `commitment.confirmed.v1` | Communication | Planning, Knowledge | commitment_id, owner_id, due_at |
| `commitment.fulfilled.v1` | Communication | Knowledge, Executive Intelligence | commitment_id, fulfilled_at |
| `calendar_event.imported.v1` | Integration | Planning, Knowledge | calendar_event_id, source_ref |
| `calendar_event.changed.v1` | Planning | Knowledge, Executive Intelligence | calendar_event_id, changed_fields |
| `meeting.preparation_requested.v1` | Executive Intelligence | Knowledge, AI Platform | meeting_id, requested_sections |
| `meeting.preparation_generated.v1` | Executive Intelligence | Planning, Audit | meeting_id, brief_id, evidence_ids |
| `conversation.imported.v1` | Integration | Communication, Knowledge | conversation_id, source_ref |
| `message.received.v1` | Communication | Knowledge, Commitment Extraction | message_id, conversation_id, evidence_id |
| `document.imported.v1` | Integration | Knowledge | document_id, evidence_id, checksum |
| `document.indexed.v1` | Knowledge | Search, Executive Intelligence | document_id, index_version |
| `decision.recorded.v1` | Knowledge | Planning, Executive Intelligence | decision_id, decision_maker_ids, evidence_ids |
| `decision.superseded.v1` | Knowledge | Executive Intelligence | decision_id, superseded_by |
| `knowledge_item.created.v1` | Knowledge | Search, Executive Intelligence | knowledge_item_id, kind, confidence |
| `relationship.created.v1` | Knowledge | Search, Executive Intelligence | relationship_id, from_id, type, to_id |
| `relationship.invalidated.v1` | Knowledge | Search, Executive Intelligence | relationship_id, reason |
| `risk.identified.v1` | Executive Intelligence | Planning, Knowledge | risk_id, probability, impact, owner_id |
| `risk.status_changed.v1` | Executive Intelligence | Planning, Knowledge | risk_id, old_status, new_status |
| `attention_item.created.v1` | Executive Intelligence | Dashboard | attention_item_id, entity_ref, score, explanation |
| `recommendation.generated.v1` | AI Platform | Executive Intelligence, Audit | recommendation_id, evidence_ids, confidence |
| `recommendation.accepted.v1` | Executive Intelligence | Owning domain, Audit | recommendation_id, accepted_by |
| `recommendation.rejected.v1` | Executive Intelligence | AI Evaluation, Audit | recommendation_id, rejected_by, reason |
| `reminder.triggered.v1` | Planning | Notification, Executive Intelligence | reminder_id, entity_ref |
| `connector.sync_started.v1` | Integration | Operations | connector_id, cursor |
| `connector.sync_completed.v1` | Integration | Operations | connector_id, new_cursor, counts |
| `connector.sync_failed.v1` | Integration | Operations, Executive Intelligence | connector_id, error_code, retryable |
| `morning_brief.requested.v1` | Scheduler | Executive Intelligence | user_id, briefing_date |
| `morning_brief.generated.v1` | Executive Intelligence | Dashboard, Audit | brief_id, user_id, evidence_ids |

## Compatibility

Producers must support the current event version. Consumers may support the current and previous version during migrations. Deprecated versions require a migration plan and replay test before removal.

## Delivery and failure handling

Failed events move to a dead-letter store with the original envelope, failure category, retry count and next action. Manual replay must preserve the original `event_id` and assign a new delivery attempt identifier.
