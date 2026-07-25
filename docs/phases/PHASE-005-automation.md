---
id: PHASE-005
title: Automation
status: Approved for Implementation
version: 0.3.0
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

### Approved decisions (approved 2026-07-25)

Resolves `docs/phases/PHASE-REVIEW.md:136`'s four named approval-gate items for this first activation, per `docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` and `docs/adr/ADR-0013-durable-workflow-execution.md`, proposed by that design pass and recorded here as the phase's own approved resolution:

- **PostgreSQL worker/lease design:** a lease-based worker inside the existing modular monolith, not Temporal (`RFC-005.md`'s pre-registered gate is evaluated and explicitly not activated this round, `ADR-0013`) -- 2-second poll interval, 30-second lease duration renewed by a 10-second heartbeat, `sha256` `action_digest`-gated idempotent dispatch persisted before every side effect. Recovery is the same lease-expiry reclaim path a worker restart uses, comfortably inside the phase's own 60-second recovery target (`docs/runbooks/PHASE-5-RECOVERY.md`).
- **High-impact action taxonomy:** a closed, fail-closed seven-category enumeration (`destructive`, `financial`, `legal`, `credential`, `person-directed`, `public`, `policy-limit-exceeding`) -- an adapter that cannot classify itself defaults to requiring per-run approval, never to `bounded`.
- **Approval expiry/rate limits:** 90-day policy expiry (7-day renewal prompt), 24-hour per-run approval-request expiry, 10 runs/workflow/hour policy default; monetary value and action-count limits have no system-wide default and are required, non-nullable per-policy fields instead.
- **Recovery runbook:** `docs/runbooks/PHASE-5-RECOVERY.md` -- what the 60-second worker-restart-recovery target means operationally, and what an operator does (and does not need to do) on a worker crash.

This resolution registers no production external connector in this first activation (`docs/phases/phase-005/DATA-MODEL.md`'s action adapters are local or explicitly fake, matching `docs/phases/PHASE-REVIEW.md`'s F-03 resolution) -- Phase 6 implements the same connector-independent action-adapter contract (design doc Decision 8) against real GitHub/GitLab/Jira systems without renegotiating approval, simulation, idempotency or compensation semantics.

## Dependency exit posture (approved 2026-07-25)

Phase 5 design work and contract approval proceed in parallel with Phase 4's own still-open exit gate (two evaluation-floor misses accepted as documented known limitations, and the repository owner's own independent full-repo re-verification, both still open per `docs/phases/PHASE-004-ai-runtime.md`'s "Phase 4 exit status and Phase 5 parallel-start" section) -- the same kind of parallel-start exception Phase 2, Phase 3 and Phase 4 each already received, recorded in `docs/ROADMAP.md`'s Phase 5 status note. This does not itself claim Phase 4 has exited, and Phase 5's own exit criteria above still apply in full as implementation proceeds. Implementation (Task 1 onward) begins once the repository owner has reviewed this document's "Approved decisions" section above.
