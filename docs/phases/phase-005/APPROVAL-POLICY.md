---
id: PHASE-005-APPROVAL-POLICY
title: Automation Approval Policy
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Automation Approval Policy

Authority is least-privilege, explicit, time-bound and revocable. A policy identifies workflow/version, action types, connector targets, data classes, value/count/rate limits, schedule, approval mode and expiry (`DATA-MODEL.md`'s `automation_policies`).

Modes are `preview_only|per_run|bounded_recurring`. **High-impact action taxonomy** (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision 5), a closed enumeration -- always requires per-run approval regardless of policy mode:

| Category | Definition |
|---|---|
| `destructive` | Irreversible mutation (adapter declares `reversible=false`) |
| `financial` | Payment, invoicing or monetary transfer |
| `legal` | Contract, compliance or regulatory-filing action |
| `credential` | Creates, rotates or reveals a credential/secret |
| `person-directed` | Sends a message/notification visible to a human other than the workspace owner |
| `public` | Creates content visible outside the workspace owner's private systems |
| `policy-limit-exceeding` | The specific run's value/count/rate would exceed the authorizing policy's own configured limit, regardless of the action's own category |

Each action adapter statically declares its `high_impact_categories` at registration time, checked before any execution attempt. **A rollback (`step_type='compensation'`) step is the one place this per-run-approval requirement cannot be satisfied at dispatch time**, because compensation runs as an automatic continuation of a failing run and has no approval-inbox surface to wait in; the requirement is upheld there by forbidding the situation entirely -- a compensation step whose `action_ref` names an adapter with any declared `high_impact_categories` is rejected at publish time (`EXECUTION-CONTRACT.md`, `422 COMPENSATION_ACTION_REF_HIGH_IMPACT`), so no high-impact action can ever execute as a rollback. **An adapter that cannot classify itself into any category still defaults to requiring per-run approval** -- `bounded` (eligible for `bounded_recurring` mode, no per-run prompt) is a category an adapter must explicitly and correctly claim; there is no "none of the above, therefore bounded" default (fail closed, matching Phase 4's conservative data-class default precedent).

## Expiry and rate limits (resolved)

| Control | Value | Behavior on limit |
|---|---|---|
| Policy expiry | 90 days from creation/last renewal (default); renewal prompt at 7 days remaining | Expired policy blocks future runs; an already in-flight run continues to its next checkpoint rather than hard-aborting. |
| Per-run approval-request expiry | 24 hours unresponded | Auto-expires to `expired`; never implicitly approved. |
| Runs per workflow per hour | 10 (policy default, overridable per policy) | Next run past the limit rejected at enqueue with `rate_limited`. |
| Monetary value / action count per policy per day | No system-wide default -- required, non-nullable per-policy field | No connector in this activation has monetary side effects; the field exists for schema stability and forces an explicit author choice rather than an arbitrary inherited default. |

Approval displays exact target, payload summary, risk category, reversible status and expiry. **Material changes invalidate approval**: an approval is bound to the exact `action_digest` (`EXECUTION-CONTRACT.md`) it was granted against -- any change to the step's resolved input, the workflow version, or the policy in force produces a new digest, and the prior approval does not carry forward to it (the mechanical enforcement of "unauthorized, expired, changed or replayed approvals are rejected").

**Revocation** takes effect immediately for any not-yet-started step (matches the phase NFR verbatim); an in-flight step already dispatched completes or reaches its next durability checkpoint before the run halts. This covers **compensation steps too**: a rollback step is not-yet-started until its adapter is actually invoked, so the worker re-reads the authorizing policy immediately before that call and blocks the compensation (`error_class='PolicyUnusableDuringCompensation'`, run ends `compensation_failed`) if the policy has since been revoked or expired -- revocation is never bypassed by a run that happens to be rolling back rather than progressing.
