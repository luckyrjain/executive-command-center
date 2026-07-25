---
id: PHASE-005-TEST-PLAN
title: Phase 5 Test Plan
status: Approved for Implementation
version: 0.2.0
owner: Lucky Jain
---

# Phase 5 Test Plan

Test workflow validation, simulation, schedules/DST, triggers, approval scopes, expiry/revocation, durable restart, idempotency, concurrency, retries, cancellation, compensation and unknown outcomes. Use fake connectors and fault injection before sandbox integrations.

Concrete coverage resolved by `docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`:

- **Graph/schema property tests** (Decision 2): a workflow `graph` containing a cycle or a parallel-fan-out shape is rejected at `publish`, not merely undocumented.
- **Simulation fault injection** (Decision 4): per registered adapter, asserts `simulate()` produces zero rows changed in every domain table its `execute()` touches.
- **Crash-recovery tests** (Decision 3): simulate a worker killed mid-step; assert at most one effect per `action_digest` across the crash/restart boundary, and that a step whose digest was persisted but not confirmed surfaces as `unknown`, never a blind retry.
- **Concurrent-claim tests** (Decision 3): two workers racing the same `workflow_runs` row under real concurrent database connections (not mocked) -- assert exactly one claims it.
- **DST/misfire tests** (Decision 7): a schedule trigger spanning a real DST transition in its declared timezone; a trigger whose fire window was missed (worker down) fires at most once on recovery unless `skip_missed=true`.
- **Approval scope/expiry/revocation tests** (Decision 6): an approval bound to a stale `action_digest` is rejected (`digest_mismatch`); an unresponded approval auto-expires at 24 hours; revocation blocks the next not-yet-started step but not an already-dispatched one.
- **Rate-limit boundary tests** (Decision 6): the 11th run within an hour under a 10/hour policy is rejected `rate_limited`, not silently queued.
- **Adversarial tests** (Threat model): confused-deputy (a request cannot supply its own `policy_id`), replay (a reused approval for a changed digest is rejected), payload substitution (altering resolved input between approval and execution invalidates the approval), secret leakage (no `secret_references` value ever appears in a persisted step trace).
- **Compensation tests** (Decision 9): a declared `compensate_ref` runs only on its specific failure path, never automatically; a failing compensation surfaces `compensation_failed` for review rather than retrying indefinitely.

Security tests cover privilege escalation, payload substitution after approval, secret leakage, replay, cross-workspace access and kill switches. Browser acceptance simulates, approves, runs, pauses, resumes, cancels and inspects recovery -- including the `needs_review` state's operator-resolution flow (`docs/runbooks/PHASE-5-RECOVERY.md`). Dogfood begins preview-only and advances through explicit exit review, mirroring Phase 1/3's own staged-dogfood precedent (`docs/runbooks/PHASE-1-DAILY-USE.md`, `docs/runbooks/PHASE-3-DOGFOOD.md`).

All of the above run against the local/fake action adapters (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision 8) -- no test in this activation requires a real external system or network call.
