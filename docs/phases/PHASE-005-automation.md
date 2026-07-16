---
id: PHASE-005
title: Automation
status: Draft
version: 0.2.0
owner: Lucky Jain
depends_on:
  - PHASE-004
  - RFC-001
  - RFC-003
  - RFC-004
  - RFC-005
  - STD-001
contracts:
  - phase-005/DATA-MODEL.md
  - phase-005/API-SCHEMAS.md
  - phase-005/EXECUTION-CONTRACT.md
  - phase-005/APPROVAL-POLICY.md
  - phase-005/UX-STATES.md
  - phase-005/TEST-PLAN.md
---

# PHASE-005 — Automation

## Objective

Execute bounded, recoverable local workflows on manual, scheduled or domain-event triggers under explicit user-approved policies.

## User value

The user safely delegates repetitive ECC work, knows exactly what is authorized, previews consequences and can stop or recover execution.

## In scope

Finite workflow/version schema; manual/event/schedule triggers; simulation; approval gates; durable local worker; idempotency; bounded retry; pause/cancel; explicit compensation; secret references; history/notifications; kill switches; connector-independent action adapter contract.

## Out of scope

Unbounded autonomous agents; silent side effects; self-created authority; production external connectors (Phase 6); financial/legal/medical decisions; credential discovery; cross-workspace workflows; multi-user delegation; unattended destructive/person-directed actions.

## Functional requirements

- Every side effect belongs to a published workflow version and active policy.
- Default mode is preview-only; authority is explicit, scoped, expiring and revocable.
- High-impact actions require per-run confirmation.
- Simulation shows steps, permissions, side effects, approval points and irreversibility.
- Execution persists before/after side effects and resumes safely after restart.
- Stable action digests/idempotency keys prevent duplicates.
- Unknown external outcome moves to review, never blind retry.
- Schedules define timezone, DST, misfire and concurrency behavior.
- Pause/cancel/kill switches prevent future steps at safe checkpoints.
- Compensation runs only when explicitly declared and authorized.

## Non-functional requirements

No duplicate effect under crash/retry fixtures. Revocation blocks the next not-yet-started side effect. Worker restarts recover durable state within 60 seconds locally. Queued run start p95 <5 seconds under acceptance load. Execution remains auditable without storing secrets.

## Architecture impact

Add workflow definition, policy, trigger, scheduler and durable-execution modules. Use PostgreSQL queues/leases in the modular monolith unless an ADR approves new infrastructure. Phase 4 provides bounded AI steps; Phase 6 later supplies production connector actions.

## Data changes

Add workflow definitions/versions, policies, triggers, runs/steps, approval requests, compensation state, secret references and notifications defined in `phase-005/DATA-MODEL.md`.

## API changes

Add workflow, simulation, policy/revocation, run control and approval endpoints in `phase-005/API-SCHEMAS.md`. Approval validates the exact action digest and current version.

## Frontend changes

Add workflow list/builder, simulation, authority/policy review, approval inbox, schedule controls, run history and recovery views. Exact targets and side effects appear before approval.

## Security and privacy

Least-privilege authority; secrets remain opaque references; step payloads are redacted; approvals cannot be inferred from chat or unrelated history. Replay, confused-deputy and payload-substitution protections are mandatory. Destructive, public, financial, legal, credential and person-directed actions require per-run approval.

## Observability

Measure queue age, schedule lag/misfire, run/step states, approval wait/expiry, retries, duplicate suppression, unknown outcomes, cancellation latency, compensation success and kill-switch state. Correlation spans trigger, approval, run and audit without logging secrets.

## Test strategy

Schema/property tests, simulation, DST/misfire, approval scope/expiry/revocation, crash recovery, idempotency, concurrency, timeout/retry, unknown outcome, cancellation, compensation, security, performance, accessibility and staged dogfood using local/fake adapters.

## Acceptance criteria

- Simulation never causes side effects.
- Unauthorized, expired, changed or replayed approvals are rejected.
- Crash/retry tests produce at most one effect.
- Pause/cancel/revoke/kill switches stop before the next side effect.
- Schedule/DST and recovery targets pass.
- Unknown outcomes and partial compensation are visible and recoverable.
- Browser acceptance covers the complete authority lifecycle.

## Exit criteria

- Contracts explicitly approved and threat model reviewed.
- Local-action adapters and fake external adapter conformance pass.
- Durable worker recovery and operational runbooks pass.
- Staged dogfood advances from preview to bounded actions with zero unauthorized effects.
- Zero open Critical, High or Medium findings.
- Phase 6 can add connector actions without changing authority semantics.

## Rollback plan

Global/workflow kill switches stop new runs. Revoke policies to block future steps. In-flight runs stop at safe checkpoints or enter review. Preserve run/audit history. Apply explicit compensation or manual recovery for partial effects.

## Deferred backlog

Production external connectors, multi-user delegation, distributed workflow engine, autonomous policy creation and unattended high-impact actions.
