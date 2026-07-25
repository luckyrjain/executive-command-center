# Phase 5 Automation Design

**Status of this document:** planning artifact only. It does not by itself change `docs/phases/PHASE-005-automation.md`'s status, does not start implementation, and does not close Phase 4's own open exit gate (the two evaluation-floor misses accepted as known limitations, and the repository owner's own independent full-repo re-verification, `docs/phases/PHASE-004-ai-runtime.md`'s "Phase 4 exit status and Phase 5 parallel-start" section). Per `docs/ROADMAP.md`'s approval gates, Phase 5 implementation may not begin until: this document's decisions are reviewed and accepted by the repository owner, `docs/phases/phase-005/*.md` contracts move from Draft to Approved for Implementation, and the four approval-gate items `docs/phases/PHASE-REVIEW.md:136` names for Phase 5 (PostgreSQL worker/lease design, high-impact action taxonomy, approval expiry/rate limits, recovery runbook) are resolved. This document exists to make that decision informed, and to leave an implementation-ready plan queued for the moment it's granted -- the repository owner has already granted the parallel-start exception covering when this design work may begin (`docs/phases/PHASE-004-ai-runtime.md`'s "Phase 4 exit status and Phase 5 parallel-start" section, 2026-07-25), the same kind of exception every prior phase received; it does not itself resolve the four gates below.

## Outcome

Design the first slice of durable, bounded, human-authorized automation: a finite versioned workflow graph, a PostgreSQL-backed durable worker with lease-based recovery, mandatory simulation before any authorization, an explicit least-privilege approval-policy model, manual/event/schedule triggers, and a connector-independent action-adapter contract that Phase 6 can later implement against without renegotiating authority semantics. Scope is exactly `docs/phases/PHASE-005-automation.md`; this document does not expand it, and registers no production external connector (that remains Phase 6, per `docs/phases/PHASE-REVIEW.md`'s F-03 resolution).

## Why this isn't a green field

Unlike Phase 4 (which had no prior code to reconcile against), Phase 5 inherits real precedent it must not silently ignore or silently reinvent:

- `ADR-0005-event-bus.md` (Accepted) already commits this repository to durable, versioned, at-least-once domain events over a PostgreSQL outbox/inbox, published only after the originating transaction commits, with idempotent consumers. `platform/events/bus.py` currently implements only `NonDurableInProcessEventBus` (a test/development adapter, explicitly documented in its own docstring as providing "no persistence, retry, deduplication, inbox, outbox, or dead-letter guarantees") -- the durable outbox-backed implementation ADR-0005 committed to has not been built yet by any phase. This document's event-triggered workflows (Decision 7) are a *consumer* of that eventual durable event bus, not a redesign of it; where this phase's own durable-worker mechanics (Decision 3) overlap with what a durable outbox implementation would need, this document reuses the same lease/claim pattern rather than inventing a second one, so a future durable-event-bus implementation and this phase's worker can plausibly share infrastructure later without either being rearchitected.
- `RFC-005.md`'s "Approved later" table pre-registers **Temporal** specifically for "Durable workflows," gated on "Automation phase and ADR" -- this is the one technology-activation question this document must answer explicitly, the same way Phase 4's design doc had to answer Ollama's pre-registered gate. Decision 1 below is that answer, and it is "no, not this activation" -- a real decision, not a skipped one.
- `docs/phases/PHASE-005-automation.md`'s own Architecture impact line already commits to a specific default before this document exists: "Use PostgreSQL queues/leases in the modular monolith unless an ADR approves new infrastructure." This document does not override that default; it makes it concrete (Decision 3) and explains why the alternative (Temporal) is not chosen for this first activation (Decision 1).
- Phase 3's `MeetingPrep.tsx` timezone-mislabeling bug (fixed during this project's own Phase 3 completeness audit, `IMPLEMENTATION-STATUS.md` Task pass) is a paid-for lesson this document does not repeat: Decision 7's schedule/DST handling stores and evaluates timezone explicitly per trigger, never implicitly from server-local time.
- Phase 3's `expected_version` optimistic-concurrency pattern (already used across `waiting_links`, `risk_reviews`, `plans`, `capacity_profiles`) is the established idiom this repository uses for "claim a row exactly once under concurrent access" -- Decision 3's worker-lease claim reuses that same compare-and-swap idiom rather than introducing a second concurrency-control pattern for the same underlying problem.
- Phase 4's prompt/tool immutable-versioning mechanism (`ADR-0004`/`ADR-0007`, made concrete in `docs/superpowers/specs/2026-07-23-phase-4-ai-runtime-design.md` Decision 3: stable slug id, integer version, `sha256` template hash, a PostgreSQL trigger rejecting mutation once a row leaves `draft`, exactly one `active` row per id via a partial unique index) is the established idiom for "a versioned, auditable artifact whose already-referenced past versions must never silently change." Decision 2 below reuses this idiom for workflow definitions rather than inventing a third versioning mechanism in three phases.

## Decision 1: durable execution technology -- PostgreSQL-backed, not Temporal

**No new infrastructure is activated for this first slice.** `RFC-005.md`'s pre-registered Temporal entry ("Durable workflows | Automation phase and ADR") is evaluated and explicitly not activated:

- **Deployment shape mismatch.** `ADR-0002-local-first-architecture.md` commits this repository to a single-machine, local-first deployment; `RFC-005.md`'s Phase 0 baseline is Docker Compose on one machine with PostgreSQL as the only stateful service. Temporal requires its own server process (or Temporal Cloud) plus its own persistence layer -- a second stateful distributed system running alongside PostgreSQL for a product whose entire automation surface, in this first activation, is "run a handful of user-authored workflows for one executive on their own machine." That is a materially larger operational footprint (a new process to deploy, monitor, back up and recover) than the actual concurrency and durability needs of this activation justify.
- **PHASE-005-automation.md's own stated default.** The umbrella spec already commits to "PostgreSQL queues/leases in the modular monolith unless an ADR approves new infrastructure" before this document exists -- this decision ratifies that default with a concrete design (Decision 3), rather than overriding it with a new dependency the spec did not ask for.
- **Precedent: minimize blast radius on a first activation into a new risk category.** This mirrors Phase 4 Decision 7's reasoning for deferring a remote model provider on its own first activation (real side effects, real external systems, genuinely new risk surface) -- the lowest-risk way to activate durable *automation with real side effects* for the first time is to keep every new moving part inside the already-audited, already-backed-up, already-recoverable PostgreSQL boundary, not to add a second durable-execution engine and a second recovery story in the same activation that is also introducing approval policies, action adapters and compensation for the first time.
- **Not rejected outright -- parked.** Revisit if/when a genuine cross-machine or high-throughput requirement exists (Phase 9's multi-node enterprise deployment is the named trigger in `RFC-005.md`'s own "Kubernetes | Multi-node deployment | Enterprise phase and ADR" row, which Temporal's own eventual case would likely ride alongside). Nothing in this activation's numbers (Decision 3) approaches a throughput or fan-out level a single-process Postgres-lease worker cannot serve.

This declines RFC-005's Temporal gate for this activation; `RFC-005.md` itself is not amended (Temporal's row is unchanged -- still "Approved later," not activated, not de-registered), matching how Phase 4 left the remote-provider question open rather than closing it. `docs/adr/ADR-0013-durable-workflow-execution.md` records this as an Accepted architectural decision (the pattern is new to this repository even though the underlying technology, PostgreSQL, is not), per the cross-phase invariant "every new technology requires RFC-005 and, when architectural, ADR approval" -- this is the "architectural, not new technology" branch of that rule, the same branch Phase 3's `attention_items` extension and Phase 4's model-router pattern both took without an RFC-005 amendment.

## Decision 2: workflow definition and versioning mechanism

Reuses Phase 4 Decision 3's established immutable-versioning idiom rather than inventing a third one:

- **Identity and hashing.** `workflow_id` is a stable slug (e.g. `triage.stale_waiting_links.v1`). `definition_hash` is `sha256` over the canonical bytes of `{graph, trigger_refs, policy_ref}`. `version` is the authoritative integer column, matching Phase 4's `prompt_versions` shape exactly.
- **Immutability.** Once a `workflow_versions` row's `status` leaves `draft` (becomes `active` or `retired`), its `graph`/`definition_hash` are immutable -- enforced by a PostgreSQL trigger rejecting any `UPDATE` touching those columns once `status <> 'draft'`, identical in mechanism to Phase 4's prompt/tool trigger. Editing a workflow always inserts a new row with `version = previous + 1`.
- **Activation.** Exactly one `active` version per `workflow_id`, enforced by a partial unique index (`WHERE status = 'active'`), identical mechanism to Phase 4. Every `workflow_run` pins the exact `workflow_version` it started with -- activating a new version never retroactively changes an in-flight or completed run's graph (`EXECUTION-CONTRACT.md`: "Workflow graphs are finite and versioned").
- **Graph shape.** A workflow's `graph` is a **finite, acyclic, sequential** ordered list of steps (no loops, no parallel fan-out in this first slice -- Decision 10). Each step declares: `step_id`, `step_type` (`action` | `approval_gate` | `condition` | `compensation`), `action_ref` (points to a registered action-adapter contract, Decision 8, for `action`/`compensation` steps), `input_mapping` (a fixed set of references to the trigger payload or a prior step's declared output fields -- never arbitrary expressions or code, matching this repository's existing avoidance of user-authored logic in stored config, e.g. Phase 3's typed capacity-constraint fields rather than a rules DSL), `on_success`/`on_failure` (the next `step_id`, or a terminal outcome: `succeeded`, `failed`, `compensating`). A `condition` step evaluates a fixed set of typed comparisons against prior step outputs (equality, presence, numeric threshold) -- not an embedded scripting language.
- **Why sequential, not a DAG.** A first durable-execution activation that also introduces approval gates, compensation and crash recovery for the first time does not additionally need concurrent-step coordination (partial-failure-of-a-fan-out is a materially harder recovery problem than partial-failure-of-a-sequence) to deliver real user value -- most useful bounded automations (triage a stale item, draft-then-request-approval-then-send) are naturally sequential. Parallel fan-out is explicitly deferred (Decision 10), not designed around, so this schema does not need to anticipate it structurally beyond "steps are a list," which a fan-out extension could still build on later without a breaking schema change.

## Decision 3: PostgreSQL worker/lease design (approval gate 1)

Concrete mechanism for `workflow_runs`/`workflow_run_steps` (`DATA-MODEL.md`), resolving `docs/phases/PHASE-REVIEW.md:136`'s "PostgreSQL worker/lease design" gate:

**Claim mechanic.** A single-statement compare-and-swap `UPDATE`, reusing this repository's existing `expected_version` optimistic-concurrency idiom rather than a separate locking primitive:

```sql
UPDATE workflow_runs
SET status = 'leased', leased_by = $worker_id,
    leased_until = now() + interval '30 seconds',
    lease_heartbeat_at = now()
WHERE id = $run_id
  AND (status = 'queued' OR (status = 'leased' AND leased_until < now()))
RETURNING *;
```

A worker that receives zero rows back lost the race to another worker (or the row was already claimed) and simply polls again -- no error path, this is the expected steady-state outcome under concurrent workers.

**Concrete numbers.**

| Control | Value | Rationale |
|---|---|---|
| Poll interval | 2s | Frequent enough that queued-run start p95 <5s (the phase's own NFR) is achievable with margin; cheap because it is a single indexed query against a small `queued`/`lease-expired` row set. |
| Lease duration | 30s | Long enough to cover one step's typical execution (a local domain call or a bounded AI-runtime call, both already budget-capped at 60-75s by Phase 4's own numbers would be the outlier case -- see the note below) without leaving a crashed worker's claim stuck for long. |
| Lease heartbeat | Every 10s while a step is actively executing | Renews `leased_until` so a step legitimately still running (not crashed) is never reclaimed out from under it; three missed heartbeats (30s of silence) is indistinguishable from a crash by design -- the same signal, not two different ones. |
| Worker-restart recovery target | <60s (phase NFR) | 30s lease timeout + 2s poll interval + step-execution margin comfortably clears the 60s target -- recovery is the *normal* lease-expiry reclaim path, not a special-cased "disaster recovery" code path (see `docs/runbooks/PHASE-5-RECOVERY.md`). |

**A step whose own action legitimately needs longer than the 30s lease** (e.g. a Phase-4 AI-runtime step, budgeted up to 75s per Phase 4's own numbers) is not starved: the heartbeat renews the lease every 10s specifically so a genuinely-still-running step's lease never expires out from under it -- 30s is the *crash-detection* threshold (no heartbeat = presumed dead), not a hard step-duration cap. A step's own real duration cap comes from its action adapter's own declared timeout (Decision 8), not the lease duration.

**Concurrency model.** Multiple `workflow_runs` execute concurrently, each independently claimed. Within a single run, steps execute strictly sequentially (Decision 2) -- a run's `current_step_index` only ever advances after its current step reaches a terminal state (`succeeded`, `failed`, or `awaiting_approval`), so there is never more than one in-flight step per run, which is what makes the lease-per-run (not lease-per-step) granularity in the table above sufficient.

**Idempotency.** `action_digest` = `sha256` over `{workflow_id, workflow_version, step_id, resolved_input}`, computed once and persisted to `workflow_run_steps.action_digest` **before** the adapter's `execute()` is called -- "the worker persists state before and after each side effect" (`EXECUTION-CONTRACT.md`, restated concretely here). A step already recorded with the same `action_digest` in `succeeded` or `in_progress` status is never re-dispatched by a worker that picks up the run after a crash. A crash between "digest persisted" and "adapter confirms completion" is the one genuinely ambiguous case -- external state may or may not have changed -- and it surfaces as an `unknown` step outcome on recovery, moving the run to human review exactly as `PHASE-005-automation.md`'s own functional requirement already states ("Unknown external outcome moves to review, never blind retry"), never a blind retry of a step whose side effect may have already happened.

## Decision 4: simulation mechanism

**Simulation runs the identical graph-walk and step-resolution code path as real execution**, with a `dry_run=true` flag threaded through to the action-adapter contract (Decision 8) -- not a separate, parallel-maintained simulation engine that could silently drift from what actually executes. Every registered action adapter implements both:

- `simulate(input) -> {preview, side_effects_declared, reversible}` -- must not perform the real side effect, by contract.
- `execute(input) -> {output}` -- the real side effect.

The runtime never calls `execute()` during a simulation request; only `simulate()` is reachable from the simulation code path at all (not merely "not called" -- the orchestration loop for a simulation request has no code path that can reach `execute`). `simulate()` being genuinely side-effect-free is an adapter-author contract obligation the runtime cannot mechanically prove for an arbitrary future adapter -- stated explicitly as a limitation here, matching Phase 4's own honest treatment of what its mitigations do and do not guarantee (its Threat model section). Two structural backstops narrow this gap for this activation specifically: (1) every adapter registered in this first slice is local or explicitly fake (Decision 8) and code-reviewed before registration -- there is no third-party or dynamically-loaded adapter code path yet; (2) `TEST-PLAN.md` requires a fault-injection test per registered adapter asserting `simulate()` produces zero rows changed in every domain table the adapter's `execute()` touches.

## Decision 5: high-impact action taxonomy (approval gate 2)

Concrete, closed enumeration resolving `docs/phases/PHASE-REVIEW.md:136`'s "high-impact action taxonomy" gate and making `PHASE-005-automation.md`'s prose ("Destructive, public, financial, legal, credential and person-directed actions require per-run approval") into an enforceable adapter-declared field:

| Category | Definition | Example (this activation's own adapters, Decision 8) |
|---|---|---|
| `destructive` | Irreversible mutation of any record (the adapter's own `reversible=false` declaration) | N/A in this slice's local adapters (all reversible or additive) |
| `financial` | Touches payment, invoicing or monetary transfer | None registered this slice; category defined now so a later connector cannot ship without it |
| `legal` | Contract, compliance or regulatory-filing action | None registered this slice |
| `credential` | Creates, rotates or reveals any credential/secret | None registered this slice |
| `person-directed` | Sends a message/notification visible to a human other than the workspace owner | `local.send_test_notification` (Decision 8) |
| `public` | Creates content visible outside the workspace owner's private systems | None registered this slice (no production connector exists yet -- Phase 6) |
| `policy-limit-exceeding` | This specific run's value/count/rate would exceed the authorizing policy's own configured limit (Decision 6), regardless of the action's own category | Any adapter, evaluated per-run against the policy in force |

Each action adapter statically declares its `high_impact_categories` (a subset of the six named categories above) at registration time -- not computed at runtime from the input, so policy evaluation can reject an unauthorized combination before any execution attempt, not after. **An adapter that cannot classify itself into any category still defaults to requiring per-run approval** -- there is no "none of the above, therefore bounded" default; `bounded` (eligible for pre-authorized, no-per-run-prompt execution under an active policy) is a category an adapter must explicitly and correctly claim, mirroring this repository's existing fail-closed conventions (e.g. Phase 4 Decision 7's conservative `sensitive` default for unclassified data).

## Decision 6: approval expiry and rate limits (approval gate 3)

Concrete numbers resolving `docs/phases/PHASE-REVIEW.md:136`'s "approval expiry/rate limits" gate:

| Control | Value | Behavior on limit |
|---|---|---|
| Policy default expiry | 90 days from creation or last renewal | A policy within 7 days of expiry surfaces a renewal prompt (`UX-STATES.md`); an expired policy blocks all *future* runs under it -- an already-dispatched, in-flight run continues to its next durability checkpoint rather than hard-aborting mid-step. |
| Per-run approval-request expiry | 24 hours unresponded | Auto-expires; the step/run moves to `expired`, never proceeds silently and is never treated as implicitly approved. |
| Rate limit: runs per workflow per hour | 10 (policy default, overridable per policy) | The next run past the limit is rejected at enqueue time with `rate_limited`, not silently queued past the window. |
| Rate limit: monetary value per policy per day | No system-wide default; the field is required (non-nullable) on every policy, forcing an explicit author choice | No connector in this activation has monetary side effects (Phase 6 concern) -- the field exists now for schema stability, matching Phase 4 Decision 5's `cost=$0` field precedent (tracked, not yet meaningfully enforceable). |
| Rate limit: action count per policy per day | No system-wide default; required (non-nullable) on every policy | Same reasoning -- a numeric default with no concrete connector behind it would be an arbitrary number, not a considered one; `APPROVAL-POLICY.md` already frames value/count/rate limits as per-policy fields, not global constants. |
| Revocation | Takes effect immediately for any not-yet-started step | Matches the phase NFR verbatim ("Revocation blocks the next not-yet-started side effect"). An in-flight step already dispatched to its adapter completes or reaches its next durability checkpoint, then the run halts -- revocation does not attempt to interrupt an adapter mid-`execute()` (that guarantee does not exist for an arbitrary adapter; only cancellation of a *not-yet-dispatched* step is guaranteed). |

## Decision 7: scheduler and triggers

- **Manual.** `POST /automations/workflows/{id}/runs` -- a user-initiated run, subject to the same policy/approval evaluation as any other trigger source (no manual-trigger bypass of authority checks).
- **Event.** A trigger declares an `event_type` filter against the existing durable event-bus contract (`ADR-0005`) -- Phase 5 subscribes to that contract as a consumer; a matching event enqueues a `workflow_run`. This is deliberately the same durable-event mechanism every other domain already uses, not a second pub/sub path invented for automation specifically.
- **Schedule.** A cron-style expression stored per trigger, evaluated by the worker's own scheduler tick (60s interval -- coarser than the 2s run-claim poll, since schedule granularity coarser than a minute is not a real product requirement). **Timezone is stored explicitly per trigger as an IANA zone name, never inferred from server-local time** -- this repository already paid for the alternative once (Phase 3's `MeetingPrep.tsx` timezone-mislabeling bug, fixed during the Phase 3 completeness audit) and this document deliberately does not repeat it. DST transitions are handled by computing next-fire-time in the trigger's own declared timezone (via a standard IANA-timezone-aware cron/DST library, no new RFC-005 dependency -- Python's standard library `zoneinfo` plus an existing cron-parsing dependency already available to the backend covers this without a new external service).
- **Misfire policy.** A schedule that could not fire on time (worker was down across its fire time) fires **at most once** on recovery -- a catch-up-once policy, not catch-up-N-times for N missed windows -- unless the trigger explicitly opts into `skip_missed=true` (no catch-up at all). This is a per-trigger field, not implicit behavior, so a workflow author must consciously choose which failure mode they want for their own schedule.

## Decision 8: connector-independent action-adapter contract (resolves F-03)

Resolves `docs/phases/PHASE-REVIEW.md`'s F-03 ("Phase 5 defines connector-independent action interfaces and validates with local/fake adapters. Phase 6 owns production GitHub/GitLab/Jira adapters; every write uses Phase 5 approval semantics."):

**Contract shape.** An `action_adapter` declares: `adapter_id`, `input_schema`, `output_schema` (both Pydantic models, matching Phase 4's structured-output validation idiom -- no new validation mechanism), `reversible` (bool), `high_impact_categories` (Decision 5, static per adapter), `simulate()`, `execute()`, and an optional `compensate()` (present only if the adapter declares a genuine compensating action -- Decision 9).

**This first activation registers local and explicitly-fake adapters only** -- no production external connector, matching both `PHASE-005-automation.md`'s own "Out of scope" line and F-03's resolution:

- `local.create_note` -- wraps the existing Phase 1 note-creation domain call. `reversible=true` (a note can be deleted through the existing Phase 1 path), `high_impact_categories=[]` (`bounded`).
- `local.send_test_notification` -- an internal, in-workspace notification (not an external message), used specifically to exercise `person-directed` approval semantics end to end without any real external side effect. `reversible=false` (a delivered notification cannot be un-delivered), `high_impact_categories=[person-directed]`.
- `fake.external_action` -- a deliberately, visibly fake adapter (its `adapter_id` and every UI label are prefixed `fake.`) used only in tests and simulation walkthroughs to exercise the full external-connector shape (input/output schema, simulate/execute/compensate, high-impact categorization) without a real network call or real external system -- this is what lets `TEST-PLAN.md`'s adapter-conformance suite prove the contract itself is sound before Phase 6 has to implement a single real connector against it.

Phase 6, when it ships, implements this exact contract for GitHub/GitLab/Jira -- it does not renegotiate approval, simulation, idempotency or compensation semantics; those are fully specified here and do not change shape when a real connector is added, only the concrete `execute()` body does.

## Decision 9: compensation model

A workflow step may declare a paired `compensate_ref` (Decision 2's `graph`), invoked **only** when a later step in the same run fails on a path the workflow's own graph explicitly marks as requiring this step's compensation -- never automatic or inferred, matching `EXECUTION-CONTRACT.md` verbatim ("Compensation runs only when explicitly declared and authorized"). Compensation is dispatched through the exact same durable lease/claim/idempotency mechanics as any other step (Decision 3) -- no special-cased execution path -- and is recorded as its own `workflow_run_steps` row with `step_type='compensation'`, visible in run history distinctly from the step it compensates, never silently folded into that step's own record. A compensation action that itself fails does not auto-retry indefinitely: it surfaces the run in a `compensation_failed` state requiring human review, satisfying the phase's own acceptance criterion ("partial compensation are visible and recoverable") rather than silently swallowing the failure.

## Decision 10: what is deferred out of this first activation

- **Production external connectors** (Phase 6, F-03) -- every adapter registered in this slice is local or explicitly fake.
- **Temporal or any distributed/multi-node workflow engine** (Decision 1) -- revisit only if a genuine cross-machine or high-throughput need materializes, likely alongside Phase 9.
- **Parallel/fan-out step graphs** (Decision 2/3) -- sequential only; the schema does not preclude adding this later, but this document does not design the partial-failure semantics fan-out would require.
- **Loops within a workflow graph** (Decision 2) -- acyclic only, to keep durable-recovery and idempotency reasoning tractable for a first activation.
- **Multi-user delegation, cross-workspace workflows** -- matches `PHASE-005-automation.md`'s own Out of scope.
- **Autonomous policy creation** -- a human always authors and approves a policy; nothing in this activation can expand its own authority.

## Threat model summary

Concrete mitigations for each of `PHASE-005-automation.md`'s named security concerns:

- **Confused deputy.** The runtime, never the browser or an inbound event payload, resolves which policy authorizes a given run -- a trigger event or API request can ask "run workflow X" but cannot supply its own `policy_id` or elevated scope; the policy bound to the workflow and workspace is looked up server-side, the same pattern Phase 4 Decision 2 already established ("resolved server-side by the router, never by the browser payload").
- **Replay.** Every approval decision is bound to the exact `action_digest` (Decision 3) it approved. A replayed or reused approval for a different digest (the workflow was edited, or the resolved input changed) is rejected -- satisfies the acceptance criterion "changed or replayed approvals are rejected" as a direct mechanical consequence of the digest binding, not a separate check bolted on afterward.
- **Payload substitution.** The digest covers the step's *resolved input*, not merely the step or workflow identity -- an attacker able to alter upstream data between approval time and execution time changes the digest and invalidates the prior approval, rather than silently executing under a stale one.
- **Secret leakage.** Secret references (`APPROVAL-POLICY.md`) are opaque handles resolved only at the moment of adapter `execute()`, never rendered into a step's persisted `input`/`output` trace -- matches `PHASE-005-automation.md`'s "secrets remain opaque references; step payloads are redacted" verbatim, using the same redaction discipline Phase 4 already established for AI-runtime traces (`ai_run_steps`).

## Architecture impact

New backend package `backend/ecc/domains/automation/` -- `workflows.py` (Decision 2's versioning), `worker.py` (Decision 3's poll/lease/claim loop), `scheduler.py` (Decision 7), `policy.py` (Decisions 5/6), `adapters.py` (Decision 8's registry plus the three local/fake adapters themselves), `runtime.py` (the orchestration loop: claim -> resolve step -> evaluate approval gate -> simulate-or-execute -> persist -> advance), `compensation.py` (Decision 9). No change to `platform/events/`'s existing contract -- Phase 5 is a consumer of the durable event-bus interface `ADR-0005` already committed to (Decision 7), not a redesign of it; if `NonDurableInProcessEventBus` is still the only implementation when Phase 5 implementation begins, that is a pre-existing gap this document does not attempt to close (out of scope: building the durable outbox implementation itself is not a Phase 5 deliverable unless a later task explicitly reopens it).

## Test strategy

Mirrors `PHASE-005-automation.md`'s own Test strategy line, made concrete per decision above: workflow schema/graph-shape property tests (Decision 2); simulation-produces-zero-writes fault injection per adapter (Decision 4); crash-recovery tests asserting at most one effect per `action_digest` under simulated worker-kill-mid-step (Decision 3); DST/misfire tests across a timezone that has a DST transition in the test window (Decision 7); approval scope/expiry/revocation and rate-limit boundary tests (Decision 6); replay/confused-deputy/payload-substitution adversarial tests (Threat model); compensation-failure-surfaces-for-review tests (Decision 9); concurrency tests asserting two workers never both claim the same `workflow_run` (Decision 3's `RETURNING *` claim, tested under real concurrent connections, not mocked). All against the local/fake adapters (Decision 8) -- no test in this activation requires a real external system.

## Approval decision gates (per `docs/phases/PHASE-REVIEW.md:136`)

Four decisions the repository owner must resolve before Phase 5 contracts move to Approved for Implementation. This document proposes a concrete answer for each so the decision is a review, not a blank page:

1. **PostgreSQL worker/lease design.** Proposed: Decision 3's concrete lease/claim mechanic, numbers and idempotency scheme (2s poll, 30s lease, 10s heartbeat, `action_digest`-gated dispatch).
2. **High-impact action taxonomy.** Proposed: Decision 5's six-category closed enumeration (`destructive`, `financial`, `legal`, `credential`, `person-directed`, `public`, plus the cross-cutting `policy-limit-exceeding`), fail-closed by default.
3. **Approval expiry/rate limits.** Proposed: Decision 6's table (90-day policy expiry, 24-hour approval-request expiry, 10 runs/workflow/hour default, no system-wide default for value/count limits -- required per-policy fields instead).
4. **Recovery runbook.** Proposed: `docs/runbooks/PHASE-5-RECOVERY.md` (authored as part of this same pass), defining what the 60-second worker-restart-recovery NFR means operationally and what an operator does (and does not need to do) on a worker crash.

## Completion boundary for this planning pass

This document and its paired ADR (`docs/adr/ADR-0013-durable-workflow-execution.md`) are complete when: both are checked in, `docs/phases/phase-005/IMPLEMENTATION-STATUS.md` links them, `docs/runbooks/PHASE-5-RECOVERY.md` exists, and all ten decisions plus the four approval gates above are stated clearly enough that the repository owner can resolve them without re-deriving this research. No code, migration or worker implementation ships as part of this pass -- this remains a documentation-only change. An implementation plan (mirroring `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`'s task breakdown) is deliberately not authored in this same pass -- it follows once the repository owner has reviewed and accepted (or amended) the decisions above, the same sequencing Phase 4's own Task 0 used (design doc and ADR first, contracts moved to Approved, *then* the implementation plan and Task 1 begin).
