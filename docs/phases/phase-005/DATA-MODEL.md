---
id: PHASE-005-DATA-MODEL
title: Phase 5 Automation Data Model
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 5 Data Model

| Record | Purpose |
|---|---|
| workflow_definitions / workflow_versions | Immutable typed workflow graph |
| automation_policies | Approved scope, limits, expiry and revocation |
| triggers | Event, manual or schedule configuration |
| workflow_runs | Durable run state and correlation |
| workflow_steps | Attempt, idempotency key, status and redacted result |
| approval_requests | Requested action, impact, expiry and decision |
| compensation_steps | Explicit recovery action/state |
| secret_references | Opaque reference; never secret value |
| notifications | Delivery state for run events |

Run states are `queued|waiting_approval|running|paused|needs_review|succeeded|failed|cancelled|compensating|compensated`. `needs_review` holds a run when an external outcome cannot be classified as success or transient failure (see Execution Contract) and blocks automatic retry until a human resolves it. Definitions and policies are immutable once active. All external actions carry stable idempotency keys and workspace scope.
