---
id: ADR-0013
title: Durable Workflow Execution
status: Accepted
version: 1.0.0
date: 2026-07-25
owner: Lucky Jain
related:
  - RFC-005
  - ADR-0002
  - ADR-0005
  - PHASE-005
---

# ADR-0013 — Durable Workflow Execution

## Context

`PHASE-005-automation.md` requires a durable local worker that persists state before and after every side effect, recovers safely after restart, and prevents duplicate execution under crash/retry. `RFC-005.md`'s "Approved later" table pre-registers **Temporal** specifically for "Durable workflows," gated on "Automation phase and ADR" — the same two-part activation pattern Ollama (Phase 4, `ADR-0012`) and `pgvector`/`sentence-transformers` (Phase 2, `ADR-0011`) each used. `docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` (Decision 1) is the phase specification half of that gate; this ADR is the other half — except the outcome here is *not* to activate Temporal, for reasons this document records explicitly rather than leaving the gate silently unresolved.

## Decision

**Do not activate Temporal for this first automation activation. Implement durable workflow execution as a PostgreSQL-backed lease/claim worker inside the existing modular monolith**, reusing this repository's already-established `expected_version` optimistic-concurrency idiom (used across Phase 3's `waiting_links`, `risk_reviews`, `plans`, `capacity_profiles`) for claim semantics, and Phase 4's already-established immutable-versioning idiom (stable slug id, integer version, `sha256` content hash, a PostgreSQL trigger rejecting mutation once a row leaves `draft`, exactly one `active` row per id via a partial unique index) for workflow-definition versioning.

Concrete mechanism (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision 3): a `workflow_runs` row is claimed via a single-statement compare-and-swap `UPDATE ... WHERE status = 'queued' OR (status = 'leased' AND leased_until < now()) RETURNING *`; a 30-second lease renewed by a heartbeat (`renew_lease`) as each step is dispatched; worker restart recovers automatically through the same lease-expiry reclaim path that handles a single worker crashing mid-step — there is no separate "disaster recovery" code path. Idempotency is enforced by a `sha256` `action_digest` over `{workflow_id, workflow_version, step_id, resolved_input}`, persisted before the corresponding adapter's side effect executes; a crash between digest-persisted and side-effect-confirmed surfaces as an `unknown` outcome requiring human review, never a blind retry.

**One mechanism detail this decision's own wording overstated, corrected against the implementation:** `HEARTBEAT_INTERVAL_SECONDS = 10` is a declared constant, not a timer. `worker.renew_lease` is called once per step (and once per compensation dispatch) by `process_claimed_run` — the "called between steps at minimum" shape — so a single adapter `execute()` call lasting longer than the 30-second lease is not heartbeated while it is in flight, and that run becomes reclaimable by another worker mid-step. The consequence is bounded rather than unsafe, and by two independent mechanisms: the `action_digest` gate means a reclaiming worker never re-dispatches a step already recorded under its digest, and every run-state write carries a `leased_by = :worker_id` ownership predicate (`worker._write_owned_run_state`), so the original worker cannot write to a run it no longer owns. Wrapping a slow `execute()` in a background timer that calls `renew_lease` every 10 seconds — which is what this paragraph's original wording describes — remains the natural follow-up, disclosed in `docs/phases/phase-005/IMPLEMENTATION-STATUS.md`'s Task 2 judgment call 2 and `DATA-MODEL.md`'s `workflow_runs` row.

## Consequences

### Positive

- No new operational service: the durable worker runs in-process against the same PostgreSQL instance every other domain already uses — no second stateful distributed system to deploy, monitor, back up or recover separately from the rest of the schema, consistent with `ADR-0006`'s "one operational database" consequence (the same reasoning `ADR-0011` used to reject a dedicated vector database for Phase 2).
- Fully consistent with `ADR-0002`'s local-first, single-machine architecture — a Temporal server (or Temporal Cloud) is a genuinely different deployment shape than "one executive's own machine running Docker Compose," and this activation's actual concurrency/durability needs (a handful of user-authored workflows, sequential steps, no cross-machine fan-out) do not require it.
- Reuses two idioms this repository has already built, tested and audited (optimistic-concurrency claims, immutable versioning) rather than introducing a third concurrency-control mechanism and a fourth versioning mechanism for the same underlying problems Phase 3 and Phase 4 already solved.
- Minimizes blast radius on a first activation into a new risk category (durable execution with real, authorized external side effects) — the same reasoning `ADR-0011` and Phase 4's design doc (Decision 7, no remote provider on first activation) both used: don't compound a first activation's genuinely new risk (real side effects, approval policies, compensation) with a second, independently-risky new dependency in the same pass.

### Negative

- A single-process Postgres-lease worker does not horizontally scale across machines the way a dedicated workflow engine would — accepted because this activation's actual load (one executive's own automations) is orders of magnitude below where that would matter, and the schema (Decision 3's lease table) does not preclude a later migration to a distributed executor if that need materializes.
- Building lease/heartbeat/idempotency logic in application code is more implementation surface than delegating it to a mature workflow engine's own battle-tested runtime — accepted because the mechanism is small and reuses already-proven patterns (`expected_version`, Phase 4's versioning trigger) rather than being built from scratch, and because `docs/phases/phase-005/TEST-PLAN.md` requires explicit crash-recovery and concurrent-claim tests to cover exactly the class of bug a mature engine would otherwise absorb.
- `platform/events/bus.py` still has no durable outbox-backed implementation (`NonDurableInProcessEventBus` is the only adapter that exists today, despite `ADR-0005` committing to a durable one since Phase 0) — this ADR's lease mechanism is architecturally compatible with that future implementation (both are PostgreSQL-backed claim patterns) but does not build it. **Outcome, recorded against this forecast rather than left reading as delivered:** Phase 5 shipped with no event-trigger firing at all, so the question of which event-bus implementation event-triggered workflows consume never arose. An `event` trigger is creatable (`triggers.create_trigger`, its `event_type_filter` required by a database `CHECK`), and nothing fires it: the scheduler tick's own read (`triggers.list_schedule_triggers`) filters to `trigger_type = 'schedule'` at the SQL level, so an `event` row is structurally unreachable by the only loop that fires anything. `platform/events/bus.py` still provides only `NonDurableInProcessEventBus`. Consuming the event bus as a trigger source is therefore deferred work, not a delivered capability — disclosed in `docs/phases/phase-005/IMPLEMENTATION-STATUS.md`'s Task 4 evidence and in `DATA-MODEL.md`'s `triggers` row.

### Risks

- If a later phase's throughput or cross-machine requirement genuinely exceeds what a single-process lease worker can serve, migrating to Temporal (or an equivalent) after workflow/run/step data already exists is a real migration project, not a drop-in swap — accepted as a future cost, revisited only if that need is concrete (most plausibly alongside Phase 9's multi-node enterprise deployment, the same phase `RFC-005.md`'s own Kubernetes row names as the analogous trigger).

## Alternatives considered

- **Temporal** (pre-registered in `RFC-005.md`'s "Approved later" table specifically for this decision): the technology this ADR's own gate exists to evaluate. Rejected for this first activation — deployment-shape mismatch with `ADR-0002`'s single-machine local-first architecture, a second stateful distributed system disproportionate to this activation's actual concurrency needs, and unnecessary compounded risk on a first activation that is already introducing durable execution, approval policies and compensation for the first time. Not rejected outright: `RFC-005.md`'s Temporal row is left unchanged (still "Approved later," not activated, not de-registered) so a future phase can reopen this decision on its own evidence rather than re-litigating this ADR.
- **A bespoke in-memory scheduler with periodic Postgres checkpointing** (state lives primarily in process memory, persisted only at intervals rather than before/after every side effect): rejected — directly contradicts `EXECUTION-CONTRACT.md`'s own hard requirement ("The worker persists state before and after each side effect") and would make "worker restarts recover durable state within 60 seconds" a best-effort claim rather than a mechanical guarantee.
- **NATS or Kafka as a distributed queue in front of the worker** (both also pre-registered in `RFC-005.md`'s "Approved later" table, gated on "Deployment architecture change and ADR" / "Enterprise-scale requirement and ADR" respectively): rejected for the same deployment-shape-mismatch reasoning as Temporal — a message-queue service is a new operational dependency this activation's single-machine, single-worker-process scale does not need; PostgreSQL's own `SELECT ... FOR UPDATE SKIP LOCKED`-shaped claim pattern (the `UPDATE ... WHERE ... RETURNING` form used here) already serves the queueing need this activation actually has.
