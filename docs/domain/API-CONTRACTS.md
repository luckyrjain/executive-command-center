---
id: API-CONTRACTS
title: Domain API Contracts
status: Accepted
version: 1.0.0
owner: Lucky Jain
related:
  - DOMAIN-MODEL
  - EVENT-CATALOG
  - RFC-004
---

# Domain API Contracts

## Contract rules

- External HTTP APIs use `/api/v1` and JSON.
- Internal domains communicate through typed application interfaces and events.
- Commands may change state; queries never change state.
- Every request carries `workspace_id`, `request_id` and authenticated actor context.
- All mutations require an idempotency key.
- Dates are ISO-8601 UTC. IDs are UUID strings.
- Pagination uses opaque cursors.
- Errors use the common envelope below.

```json
{
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "Task was not found",
    "request_id": "uuid",
    "details": {}
  }
}
```

## Identity

### Commands

- `CreateWorkspace(owner)` -> `Workspace`
- `CreatePerson(display_name, source_refs[])` -> `Person`
- `ProposePersonMerge(candidate_ids, evidence_ids[])` -> `MergeProposal`
- `ConfirmPersonMerge(proposal_id)` -> `MergeRecord`

### Queries

- `GetWorkspace(workspace_id)`
- `GetPerson(person_id)`
- `FindPeople(query, source_ref?)`

## Planning

### Commands

- `CreateProject(title, owner_id, goal_ids[])`
- `ChangeProjectStatus(project_id, expected_version, status)`
- `CreateTask(title, owner_id, project_id?, due_at?, priority)`
- `UpdateTask(task_id, expected_version, patch)`
- `CompleteTask(task_id, expected_version)`
- `CreateReminder(entity_ref, trigger)`

### Queries

- `GetToday(user_id, date)`
- `GetTask(task_id)`
- `ListTasks(filters, cursor)`
- `GetProject(project_id)`
- `ListCalendarEvents(range, cursor)`

## Communication

### Commands

- `ConfirmCommitment(commitment_id, owner_id, due_at?)`
- `FulfilCommitment(commitment_id, evidence_id?)`
- `RejectDetectedCommitment(commitment_id, reason)`

### Queries

- `GetConversation(conversation_id)`
- `ListMessages(conversation_id, cursor)`
- `GetCommitment(commitment_id)`
- `ListCommitments(filters, cursor)`

## Knowledge Platform

### Commands

- `ImportDocument(source_ref, content_ref, metadata)`
- `RecordDecision(context, choice, rationale, alternatives, evidence_ids[])`
- `CreateKnowledgeItem(kind, content, evidence_ids[], confidence)`
- `CreateRelationship(from_ref, type, to_ref, evidence_ids[], confidence)`
- `InvalidateRelationship(relationship_id, reason)`

### Queries

- `GetEntity(entity_ref)`
- `GetTimeline(entity_ref, range, cursor)`
- `GetRelated(entity_ref, relationship_types[], depth)`
- `SearchKnowledge(query, filters, cursor)`
- `GetDecision(decision_id)`
- `GetEvidence(evidence_id)`

## Executive Intelligence

### Commands

- `GenerateMorningBrief(user_id, date)`
- `RequestMeetingPreparation(meeting_id, sections[])`
- `CreateRisk(project_id?, description, probability, impact, owner_id)`
- `AcceptRecommendation(recommendation_id)`
- `RejectRecommendation(recommendation_id, reason)`

### Queries

- `GetDashboard(user_id, date)`
- `GetMorningBrief(user_id, date)`
- `ListAttentionItems(user_id, filters)`
- `GetMeetingPreparation(meeting_id)`
- `ListRisks(filters, cursor)`

## AI Platform

### Commands

- `ExecuteModel(task_type, input_refs[], output_schema, policy)`
- `RunAgent(agent_definition_id, objective, context_refs[])`
- `RecordEvaluation(execution_id, metric, result)`

### Queries

- `GetModelExecution(execution_id)`
- `GetPromptDefinition(prompt_id, version?)`
- `ListEvaluations(filters, cursor)`

## Integration Platform

### Commands

- `RegisterConnector(type, credentials_ref, configuration)`
- `StartSync(connector_id, cursor?)`
- `DisableConnector(connector_id)`

### Queries

- `GetConnector(connector_id)`
- `GetSyncStatus(connector_id)`
- `ListSourceRecords(connector_id, cursor)`

## HTTP surface for Phase 0/1

```text
GET    /api/v1/health
GET    /api/v1/dashboard/today
GET    /api/v1/tasks
POST   /api/v1/tasks
PATCH  /api/v1/tasks/{task_id}
POST   /api/v1/tasks/{task_id}/complete
GET    /api/v1/calendar/events
GET    /api/v1/commitments
POST   /api/v1/commitments/{id}/confirm
GET    /api/v1/knowledge/search
GET    /api/v1/meetings/{id}/preparation
POST   /api/v1/meetings/{id}/preparation
GET    /api/v1/briefs/morning
POST   /api/v1/briefs/morning
```

## Concurrency and idempotency

Mutations reject stale `expected_version` with `409 VERSION_CONFLICT`. Repeating a mutation with the same idempotency key returns the original result. Commands that invoke external side effects require explicit approval state and audit records.
