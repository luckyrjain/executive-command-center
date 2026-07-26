---
id: PHASE-005-API-SCHEMAS
title: Phase 5 Automation API
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 5 API Schemas

Resolved administrative/runtime surface for this first activation (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`):

```text
GET|POST /automations/workflows
GET /automations/workflows/{id}
POST /automations/workflows/{id}/publish|disable|simulate
GET|POST /automations/policies
POST /automations/policies/{id}/revoke
GET|POST /automations/runs
GET /automations/runs/{id}
POST /automations/runs/{id}/pause|resume|cancel
GET /automations/approvals
POST /automations/approvals/{id}/approve|reject
POST /automations/workflows/{id}/kill_switch
POST /automations/kill_switch
```

**`POST /automations/workflows/{id}/kill_switch` (per-workflow) and `POST /automations/kill_switch` (global) are a Task 6 addition** to this originally-approved endpoint list -- neither existed when this document was first moved to "Approved for Implementation" (Task 0); only the `kill_switch_active` error code was named. Disclosed here explicitly, matching how prior tasks disclosed generalizations rather than silently changing an "Approved for Implementation" doc. Both are actor-authenticated, workspace-scoped, CSRF- and idempotency-key-protected, matching every other mutating Phase 5 endpoint's convention exactly. Body: `{active: bool, reason: string | null}`. `active=true` activates a switch for the named scope (idempotent -- an already-active exact scope is a no-op, no duplicate row); `active=false` deactivates it (idempotent -- no active row for that scope is also a no-op). `automation_kill_switches` (`docs/phases/phase-005/DATA-MODEL.md`) is an append-only-ish audit trail, not just current state: deactivating marks the existing active row rather than deleting it; reactivating inserts a fresh row. `ecc.domains.automation.worker.enqueue_run` (the single choke point both `POST /automations/runs` and the schedule-trigger fire path go through) rejects a killed workflow with `kill_switch_active` (`409`) before creating any `workflow_runs` row at all -- "Global/workflow kill switches stop new runs" (`PHASE-005-automation.md`'s Rollback plan, verbatim). `claim_next_run`'s own predicate additionally ensures a killed workflow's already-`queued`/lease-expired rows are never claimed or reclaimed either, matching `docs/runbooks/PHASE-5-RECOVERY.md`'s "never claimed, reclaimed, or resumed regardless of lease state."

`POST /automations/workflows/{id}/simulate` runs the identical graph-walk/step-resolution code path as `POST /automations/runs` with `dry_run=true` threaded through to every action adapter (design doc Decision 4) -- the orchestration loop reachable from this endpoint has no code path that can call an adapter's `execute()`, only `simulate()`. The response returns predicted steps, requested permissions/scopes, declared side effects (per adapter, `reversible` and `high_impact_categories`), and every approval point the run would hit -- never a partial or best-effort preview; a workflow with an adapter that cannot be simulated (no `simulate()` implementation) is rejected at `publish` time, not discovered mid-simulation.

`POST /automations/approvals/{id}/approve` requires the caller to echo the exact `action_digest` the approval request names (design doc Decision 3/Threat model) -- an approval whose digest does not match the step's current resolved input is rejected as `digest_mismatch`, never silently accepted against a since-changed step. This is the mechanical enforcement of the acceptance criterion "unauthorized, expired, changed or replayed approvals are rejected."

`POST /automations/runs/{id}/cancel` blocks the run's next not-yet-started step from being dispatched (matching the phase NFR verbatim: "Revocation blocks the next not-yet-started side effect") -- it does not attempt to interrupt a step already dispatched to its adapter's `execute()` mid-flight, since that guarantee cannot be made for an arbitrary adapter (design doc Decision 6). `pause`/`resume` behave identically for suspension/continuation without terminating the run.

`GET /automations/runs/{id}` exposes step state and redacted evidence (`workflow_run_steps.input`/`output`, redacted per `DATA-MODEL.md`) -- raw adapter payloads containing resolved secret values are never returned by any endpoint, matching `secret_references`' opaque-handle design.

Every endpoint that creates or mutates a `workflow_runs`/`approval_requests`/`automation_policies` row is resolved server-side against the actor's own workspace and the policy already bound to the target workflow -- no endpoint accepts a caller-supplied `policy_id` override for an existing workflow, closing the confused-deputy path the design doc's Threat model section names (a request can ask "run workflow X," never "run workflow X under policy Y").

Idempotency, session-derived identity, audit redaction and 404 isolation apply, matching every existing Phase 1-4 endpoint convention.

## Errors

Required codes: `schema_invalid` (a run/policy/trigger payload failing its Pydantic contract), `workflow_not_active` (target workflow has no `active` version), `policy_expired`, `policy_revoked`, `rate_limited` (design doc Decision 6's per-policy runs/hour or value/count ceiling), `digest_mismatch` (an approval's digest does not match the step's current resolved input), `approval_expired` (24-hour unresponded window elapsed), `simulation_only` (an adapter without a `simulate()` implementation attempted at `publish`), `kill_switch_active` (a global or per-workflow kill switch blocks dispatch, `docs/runbooks/PHASE-5-RECOVERY.md` -- implemented Task 6: `POST /automations/runs` returns this `409` when `enqueue_run` rejects a killed workflow).
