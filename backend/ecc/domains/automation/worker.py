"""The durable local worker: enqueue, poll/lease/claim, per-step dispatch
and heartbeat, cancellation, and crash recovery
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`
Decision 3, `docs/adr/ADR-0013-durable-workflow-execution.md`,
`docs/phases/phase-005/EXECUTION-CONTRACT.md`,
`docs/runbooks/PHASE-5-RECOVERY.md`).

**No HTTP surface in this module**, matching `triggers.py`'s precedent
exactly: `docs/phases/phase-005/API-SCHEMAS.md`'s `/runs`/`/approvals`
endpoints need this module's worker to exist first, but are more
meaningfully built once Task 3's approval-gate logic and a later task's
real adapters exist too -- this module is an internally-invokable
component (a function/class other code, and this task's own tests, call
directly), not a router.

**Claim mechanic -- Decision 3's SQL, generalized by one documented
step.** Decision 3's own compare-and-swap `UPDATE` literally reads:

    UPDATE workflow_runs
    SET status = 'leased', leased_by = $worker_id,
        leased_until = now() + interval '30 seconds',
        lease_heartbeat_at = now()
    WHERE id = $run_id
      AND (status = 'queued' OR (status = 'leased' AND leased_until < now()))
    RETURNING *;

Two things worth being explicit about, since this task's own instructions
ask for a flagged finding rather than a silent deviation wherever reality
and the design-time document diverge:

1. **The `WHERE id = $run_id` predicate implies a candidate is already
   chosen before this statement runs.** `claim_next_run` below makes that
   explicit: a plain, unlocked `SELECT id ... ORDER BY queued_at LIMIT 1`
   picks one candidate, then the exact compare-and-swap `UPDATE` above
   (parameterized by that `id`) is the sole atomic claim -- no `SELECT
   ... FOR UPDATE` precedes it (this task's own instruction: that would be
   a different, arguably worse locking strategy than the one-statement
   compare-and-swap Decision 3 actually specifies). Two workers racing the
   same candidate simply both attempt the same targeted `UPDATE`; exactly
   one affects a row, the other's `UPDATE` matches zero rows and that
   worker polls again -- "no error path, this is the expected steady-state
   outcome under concurrent workers" (Decision 3, verbatim).
2. **The expired-lease branch here checks `status IN ('leased',
   'running')`, not `status = 'leased'` alone.** `DATA-MODEL.md`'s own
   run-state vocabulary requires a distinct `running` state (reached while
   a run is actively dispatching steps, as opposed to `leased`: claimed
   but not yet past its first step) -- this task's own instructions list
   `running` among the states this worker "needs to legitimately reach".
   Decision 3's literal SQL, restricted to `status = 'leased'`, would never
   reclaim a run that crashed *after* transitioning to `running` -- silently
   violating `docs/runbooks/PHASE-5-RECOVERY.md`'s own central claim that
   "recovery is the same lease-expiry reclaim logic every worker already
   runs on every poll cycle, not a distinct disaster-recovery code path"
   for exactly the crash timing that matters most (mid-run, not merely
   post-claim-pre-start). Both `leased` and `running` are lease-bearing
   states (both carry `leased_by`/`leased_until`/`lease_heartbeat_at`,
   both are renewed by the identical `renew_lease` heartbeat call below) --
   so this implementation's reclaim predicate is `status IN ('leased',
   'running') AND leased_until < now()`, a one-word-wider, disclosed
   generalization of Decision 3's own SQL, not a second code path. See
   `docs/runbooks/PHASE-5-RECOVERY.md`'s own updated text (this task's
   commit) and this PR's evidence section for the same note.

**Idempotency (Decision 3, made concrete).** `run_step` computes
`action_digest = sha256({workflow_id, workflow_version, step_id,
resolved_input})` and `INSERT`s the `workflow_run_steps` row -- `status =
'dispatched'`, digest set -- in a statement that is durably `commit()`-ed
(not merely `flush()`-ed -- see the commit-placement note below) to the
database **before** the resolved adapter's `execute()` is called. A row
already `succeeded` under this exact `(run_id, step_index)` key is never
re-dispatched (`execute()` is not called a second time); a row still
`dispatched` when a worker next examines it (digest persisted, no outcome
ever recorded -- the crash-between-persist-and-confirm case) is marked
`unknown` and the run moves to `needs_review`, never blindly retried.

**Every durability-critical write in this module commits immediately, on
a bare session -- not `flush()`, and not left to an enclosing `with
session.begin():` block.** This mirrors `ecc.domains.ai_runtime.runtime.
_persist_terminal`'s own documented reasoning exactly: a `flush()` sends
statements to the server but is rolled back along with everything else in
an open transaction if the process dies before that transaction's own
`COMMIT` -- which would silently defeat Decision 3's entire guarantee
("computed once and persisted to `workflow_run_steps.action_digest`
**before** the adapter's `execute()` is called", design doc verbatim)
for the one crash timing that guarantee exists to cover. Calling `session.
commit()` from inside a caller's `with session.begin():` block raises
`InvalidRequestError` (confirmed directly against this codebase's own
`Session`/`SessionFactory` wiring while reviewing this task -- SQLAlchemy
2.0's context-manager form holds a reference to the specific transaction
it started and cannot complete against one an inner call already ended),
so every function in this module that needs a real, crash-surviving
commit -- `claim_next_run`, `renew_lease`, `run_step`, and the internal
`_mark_running`/`_finish_run` helpers `process_claimed_run` calls -- must
be invoked against a **bare** `session` (`with SessionFactory() as
session:`, no `.begin()`), exactly like every `execute_run` call site in
`tests/test_ai_runtime_runtime_postgres.py`. `enqueue_run` and
`cancel_run` are the exception: neither has a risky external call to
protect against, so both remain ordinary caller-committed mutations
(`with session.begin():` is correct for them, matching every Task 1
endpoint's own convention).

**Heartbeat shape (a documented choice, not the only possible one).**
`renew_lease` is a plain, independently callable, independently testable
function (`renew_lease(session, workspace_id, run_id, worker_id)`) --
`process_claimed_run` calls it once per step, immediately before that
step's `run_step` dispatch, which is the "called between steps at
minimum" shape this task's own instructions explicitly allow. This
activation's own adapters (this task's tests' fakes; later tasks' local/
fake adapters) are fast in-process calls with no real long-running I/O, so
a single per-step renewal comfortably keeps `leased_until` ahead of the
10-second heartbeat cadence Decision 3 specifies without needing a
background thread mid-`execute()`. A later task whose adapters are
genuinely slow (a Phase-4 AI-runtime step budgeted up to 75s, an external
connector call) can wrap a single slow `execute()` call in a background
timer that calls this exact same `renew_lease` function every 10 seconds
while that one call is in flight -- the mechanism does not change, only
the calling shape around one specific slow step would need to.

**Cancellation.** `cancel_run` sets `cancel_requested_at` (immediately,
for a run still `queued`, also flips straight to `cancelled` -- there is
no in-flight step to protect). For a claimed run, `process_claimed_run`
checks `cancel_requested_at` immediately before dispatching each
not-yet-dispatched step and, if set, stops there (`status = 'cancelled'`)
without calling `run_step` for that step at all -- a step already
mid-`run_step` (already committed its `dispatched` row and called
`execute()`) is never interrupted, matching `EXECUTION-CONTRACT.md`
verbatim ("a step already dispatched to its adapter completes ... rather
than being force-interrupted").

**Task 3's approval/policy gate -- inserted between "no existing
`workflow_run_steps` row for this step" and "`INSERT` the `dispatched`
row," never after.** `run_step`'s digest is computed in memory first
(unchanged from Task 2); the `SELECT ... FOR UPDATE` existing-row check
runs next (also unchanged -- it is what tells this call whether a prior
attempt already dispatched, succeeded, failed, or left an ambiguous
`unknown`-shaped gap for *this exact step*). Only once that check confirms
there is **no row at all yet** does `_evaluate_dispatch_gate` run: it
resolves the run's policy (`policy.get_policy` via `run.policy_id`;
`policy.is_policy_usable`), and, if the policy is usable, evaluates
`approvals.evaluate_approval_requirement` against the resolved adapter. A
step this gate does not clear returns `StepBlockedByPolicy` or
`StepAwaitingApproval` -- a `workflow_run_steps` row is deliberately never
written for either outcome. This ordering is the one genuinely
load-bearing design choice in this task's own wiring, worth stating
explicitly rather than leaving implicit: writing the `'dispatched'` row
*before* evaluating the gate (the naive reading of "after computing digest
... before calling `execute()`") would leave a step paused for approval
looking identical, to any later `run_step` call examining
`workflow_run_steps`, to Task 2's own crash-in-the-gap case (`'dispatched'`
status, a real digest, no recorded outcome) -- which Task 2's own existing
branch immediately and unconditionally marks `unknown`/`needs_review` on
sight, with no way to tell "still legitimately waiting for a human" apart
from "the process that wrote this row is dead." Evaluating the gate
*before* that row exists at all sidesteps the ambiguity entirely: a step
paused for approval or blocked by policy leaves **no** `workflow_run_steps`
row, so a later `run_step` call for the same `step_index` (a resumed poll
cycle, or an explicit approval decision requeuing the run -- `approvals.
_advance_run_after_decision`) falls through the same "no existing row"
branch and simply re-evaluates the gate from scratch, which is exactly the
resume behavior this task needs (see `process_claimed_run`'s own note
below) with no separate "resume" code path required inside `run_step`
itself.

`_evaluate_dispatch_gate`'s two blocking outcomes:

1. **No usable policy (`StepBlockedByPolicy`).** `run.policy_id is None`,
   or it names a row `policy.get_policy` cannot find, or `policy.
   is_policy_usable` reports `False` (revoked or expired) -- all three
   block the step, fail-closed ("no policy means no authority," this
   task's own instruction: an unset `policy_id` is not an implicit
   all-bounded default). `process_claimed_run` maps this to `run.status =
   'needs_review'` -- reusing the existing terminal-until-human-action
   state Task 2 already built for the crash-ambiguous `unknown` case
   (`docs/runbooks/PHASE-5-RECOVERY.md`'s own precedent), rather than
   inventing a fifth run status: an unusable policy is exactly the kind of
   thing a human, not an automatic retry, must resolve (renew or
   re-authorize the policy, or cancel the run), which is precisely what
   `needs_review` already means. No new `workflow_runs` column records
   *which* of the three sub-reasons applied -- `run.policy_id` plus the
   named `automation_policies` row's own `revoked_at`/`expires_at` fields
   are already sufficient for an operator or a future UI to determine why,
   without this task adding a column purely to restate what those two
   fields already say.
2. **Approval required, no `approved` row for this exact digest yet
   (`StepAwaitingApproval`).** `approvals.evaluate_approval_requirement`
   said yes; `approvals.get_approved_request` (keyed by the live,
   freshly-computed `digest` -- never a previously-cached value) found
   nothing. `_evaluate_dispatch_gate` then looks for an already-`pending`
   request for this step (`approvals.get_pending_approval`) and reuses it,
   or creates a fresh one (`approvals.create_approval_request`) bound to
   this exact digest, then durably commits that write immediately
   (`session.commit()`, on a bare session -- this module's own
   commit-placement discipline applies identically here: a crash between
   creating the request and committing it must never leave a request that
   silently vanishes, which would otherwise strand the run in
   `waiting_approval` with nothing in the approval inbox for a human to
   act on). `process_claimed_run` maps this to `run.status =
   'waiting_approval'`.

**Resuming a `waiting_approval` run reuses the existing claim/poll
machinery -- there is no separate reclaim predicate or resume-specific
dispatch path.** `approvals.decide_approval`'s own `_advance_run_after_
decision` helper (called for every decision, HTTP or direct) flips an
`approved` run's `status` straight back to `'queued'` (clearing its stale
lease fields) the moment a human approves -- `claim_next_run`'s ordinary
`_CLAIMABLE_PREDICATE` already matches `'queued'`, so the very next poll
cycle (any worker's) claims it exactly as it would any other queued run,
`process_claimed_run` resumes at `run.current_step_index` (never rewound
to `0` -- unchanged from Task 2's own crash-recovery checkpoint, since
`waiting_approval` never advances `current_step_index` past the step it
paused on), and `run_step`'s gate re-evaluates for that same step,
finds the now-`approved` row matching the live digest via `get_approved_
request`, and proceeds to dispatch. A `rejected` decision instead flips
the run straight to `'failed'` (a human declined -- there is nothing left
to resume, matching how an adapter's own raised exception also produces
`failed`, not `needs_review`, for a definitively-classified outcome). This
was a genuine design choice this task's own instructions flagged as
"architecturally significant" -- the alternative (a distinct `claim_next_
run`-adjacent reclaim predicate specifically for `waiting_approval` rows)
was rejected because `waiting_approval` is not a lease-bearing state (it
carries no `leased_until` a poll cycle could compare against `now()`) and
because the moment a human approval-decides is already a natural,
explicit trigger to requeue -- no polling-based reclaim is needed for a
state change that only ever happens as a direct consequence of a
synchronous, already-transactional HTTP decision.

**Task 4: user-initiated `pause_run`/`resume_run` -- public functions,
deliberately not to be confused with the private `_pause_run` helper
above.** `_pause_run` (added by Task 3, kept unchanged by this task) is
`process_claimed_run`'s own internal state-transition helper for a
*different* purpose entirely: releasing a run's lease when it pauses
itself mid-dispatch onto `waiting_approval` or `needs_review`. `pause_run`/
`resume_run` below are the **public**, user-initiated feature
`API-SCHEMAS.md` names (`POST /automations/runs/{id}/pause|resume`,
"`pause`/`resume` behave identically [to cancel] for suspension/
continuation without terminating the run") -- a human explicitly asking
"stop making progress on this run for now, but don't cancel it." Python's
leading-underscore convention already disambiguates the two at the
call-site level (`_pause_run` is a module-private implementation detail;
`pause_run` is this module's public API), but the naming is close enough
on a skim that this is called out explicitly here, once, for a future
reader: **do not add a call from `pause_run` to `_pause_run` or vice
versa** -- they serve unrelated triggers (a human's explicit request vs.
an automatic mid-dispatch pause) even though their underlying SQL shape
(release the lease, do not touch `finished_at`) happens to be identical.

**Mechanically, `pause_run` is architecturally almost identical to
`cancel_run`**, reusing the exact same shape deliberately (module's own
instruction: "reuse as much of that existing mechanic as makes sense
rather than inventing a parallel one") with one difference that matters:
`cancel_run` sets `cancel_requested_at` (a one-way flag -- a cancelled run
never un-cancels, matching `_TERMINAL_RUN_STATUSES` including
`'cancelled'`); `pause_run` sets the new `pause_requested_at` column
(migration `0041_phase5_scheduler_and_pause.py`) instead of overloading
`cancel_requested_at`, specifically *because* `'paused'` is not terminal
and needs its own, independently-resumable flag a human can clear again.
Both flags are checked by `process_claimed_run` at the identical point in
its loop (immediately before dispatching each not-yet-dispatched step,
after that step's lease has been freshly renewed) and both stop the run
there without calling `run_step` for that step at all -- a step already
mid-`run_step` (already committed its `'dispatched'` row and called
`execute()`) is never interrupted by either, matching
`EXECUTION-CONTRACT.md` verbatim for both cancellation *and* pause
(`API-SCHEMAS.md`: "behave identically ... for suspension/continuation").
`cancel_requested_at` is checked first when both happen to be set (an
operator who cancels a paused run should get a terminal outcome, not a
run stuck forever in `'paused'` because a stale pause flag never gets
reconsidered) -- cancellation is the strictly stronger, more final
request of the two.

**`resume_run` clears `pause_requested_at` back to `NULL`.** This is the
one genuinely easy-to-get-wrong detail worth spelling out explicitly
(discovered and designed around during this task's own self-review,
before it could become a real bug the way Task 2/3's own reviews each
found one): if `resume_run` only flipped `status` back to `'queued'`
without also clearing the flag, the very next `process_claimed_run` call
for this run would immediately observe `pause_requested_at` still set (the
check runs at the *top* of the loop, before any step dispatches) and
re-pause the run instantly -- an unresumable run that looks superficially
resumed (`status='queued'` momentarily) but can never actually make
progress again. `cancel_run` has no analogous concern: cancellation is
one-way, so `cancel_requested_at` never needs clearing.

**Task 5: `WorkspaceScopeMismatch`, a small defense-in-depth addition found
during this task's own self-review, not part of Tasks 2-4's original
shape.** Task 5 ("Connector action adapters and sandbox tests") registers
this activation's first adapter with a real database side effect (`local.
create_note`) and reviewed, as its own explicit instruction required,
exactly where `run_step`'s `action_input.workspace_id` field (for any
adapter whose `input_schema` happens to declare one) actually comes from.
The answer: `_resolve_step`'s `resolved_input` is a step's static `input_
mapping`, authored into the run's own pinned, immutable `workflow_versions.
graph` by an already workspace-scoped human at `workflows.create_workflow_
draft` time -- there is no live templating/substitution step in this
codebase yet (`_resolve_step` returns the graph step's `input_mapping`
verbatim; see `local_adapters.py`'s own module docstring for the full
trace). That makes `action_input.workspace_id` only as trustworthy as
workflow-authoring authorization already is -- not a new confused-deputy
hole this task introduces, but also not mechanically enforced anywhere
before this addition. `run_step` now compares any validated `action_input.
workspace_id` against `run.workspace_id` immediately after `input_schema.
model_validate(...)` and before `execute()` is ever called, raising
`WorkspaceScopeMismatch` (caught by the same broad `except Exception`
already wrapping the `execute()` call) on a mismatch -- generic (keys off
an attribute name, not a specific adapter), and inert for any adapter whose
`input_schema` carries no `workspace_id` field at all (every one of Task
2-4's own test fakes, and `fake.external_action`).

**Security audit batch C: `ActorScopeMismatch`, the actor half of the same
check.** Task 5's `WorkspaceScopeMismatch` covered only *which workspace* a
graph-authored `input_mapping` could steer an adapter's write into; it left
*which user* entirely unchecked. `local.create_note` is the one registered
adapter whose `input_schema` declares an `actor_id`, and it writes that value
into `notes.owner_id`/`created_by`/`updated_by` plus an `audit_events` row's
own `actor_id` (`authorization_result='allowed'`, `source='automation'`) --
so a workflow author could publish a step naming a *different* member of
their own workspace and produce both a note owned by that member and a forged
audit entry attributing their own automation to that member. `run_step` and
`_dispatch_compensation_step` now call `_enforce_actor_scope` alongside
`_enforce_workspace_scope` at every one of the three pre-`execute()`/
pre-`compensate()` call sites, comparing any validated `action_input.actor_id`
against `workflow_runs.created_by` -- the run-starting user `enqueue_run`
persists from its own server-resolved `actor_id` argument, never from a
request body or a graph. Same generic attribute-sniffing shape, same
"surfaces as an ordinary classified step failure" integration
(`error_class = 'ActorScopeMismatch'`), same inertness for an adapter with no
such field. See `ActorScopeMismatch`'s own docstring for the full reasoning
and for why "must equal `created_by`" (rather than the looser "must be some
member of this workspace") is the deliberate bar.

**Task 6: bounded retry (`docs/phases/phase-005/EXECUTION-CONTRACT.md`'s
"Retries use bounded exponential backoff only for classified transient
failures ... never for a step whose side effect may have already partially
occurred").** No such mechanism existed before this task -- `run_step`'s
`except Exception` block caught literally anything and immediately marked
the step `'failed'`, with no distinction between "definitely no side
effect, safe to retry" and "ambiguous." An adapter now opts in explicitly,
per attempt, by raising `adapters.TransientAdapterError` instead of an
ordinary exception (`adapters.py`'s own docstring has the full contract);
every other exception keeps today's exact unconditional-`'failed'`
behavior -- `run_step` never guesses retry-safety for an unclassified
error. `MAX_RETRY_ATTEMPTS = 3`, backoff `2 ** attempt_count` seconds (2s,
4s, 8s for attempts 1/2/3) -- see those constants below. On a
`TransientAdapterError` with `attempt_count < MAX_RETRY_ATTEMPTS`,
`run_step` itself (not `process_claimed_run`) durably commits
`workflow_run_steps.status = 'retrying'`/`attempt_count += 1` and releases
the run's own lease back to `'queued'` with `next_attempt_at = now() +
backoff` -- the identical lease-release shape `_pause_run` already uses,
except the retry gate lives on `workflow_runs.next_attempt_at`, not a new
run-level status (`DATA-MODEL.md`'s run-state vocabulary gets no new
`'retrying'` value; `UX-STATES.md`'s required "retrying" UI state is
realized by an ordinary `'queued'` run whose *current step's own row*
shows `status='retrying'`). `_CLAIMABLE_PREDICATE` is widened so a
retry-pending run is not reclaimed before `next_attempt_at` elapses. Once
exhausted (`attempt_count >= MAX_RETRY_ATTEMPTS` at the moment of yet
another `TransientAdapterError`), `run_step` falls through to the ordinary,
unconditional `'failed'` path unchanged -- bounded, never indefinite. A
`'retrying'`-status existing row (a reclaimed, now-due run resuming the
exact step it paused on) is handled in `run_step`'s existing-row branch by
falling through to a fresh dispatch attempt that `UPDATE`s the same row
(never a second `INSERT`), reusing its existing `attempt_count`.

**Task 6: compensation dispatch (`docs/superpowers/specs/
2026-07-25-phase-5-automation-design.md` Decision 9).** `workflows.
GraphStepModel` gained `compensate_ref` this same task (a real, disclosed
gap between two already-merged tasks' own assumptions -- see that module's
own docstring) -- this module is what actually dispatches it. Two changes
to `process_claimed_run`'s main loop were required first, not merely
additive: (1) the main per-step walk now skips forward over any step whose
`step_type != "action"` without ever calling `run_step` for it and without
writing a `workflow_run_steps` row -- previously, a `step_type=
'compensation'` (or, by the same latent gap, `'approval_gate'`/
`'condition'`) step sitting inline in the flat `steps` list would, if ever
linearly reached, be passed to `run_step`, which raises `UnsupportedStepType`
unhandled, crashing the whole claimed-run dispatch; a `step_type=
'compensation'` step is now reachable *only* through the dedicated
compensation-dispatch path below, never through this linear walk. (2) When
a step's outcome is `'failed'`, the loop no longer unconditionally calls
`_finish_run(..., "failed")` -- it first looks at the graph's own
already-`succeeded` action steps with index `< step_index` (the step that
just failed is never itself compensated -- only earlier, completed steps
that declared a `compensate_ref`) and collects any with one set,
**descending** by original step index (`_qualifying_compensations`,
standard saga rollback order: undo the most recently completed effect
first). No qualifying step: unchanged behavior, `_finish_run(..., "failed")`.
One or more: `_mark_compensating` flips `run.status` to `'compensating'`
via a direct `UPDATE` that deliberately does **not** release the lease
(this is an automatic continuation of the same dispatch, not a human-wait
state the way `waiting_approval`/`needs_review` are), then `_run_
compensation_sequence` dispatches each qualifying compensation step in
turn, renewing the lease before each (`renew_lease`, same heartbeat
function every other dispatch uses) -- continuing to attempt every
qualifying compensation even after one fails, so partial compensation is
visible (`PHASE-005-automation.md`'s own acceptance criterion), never
stopping early. `_dispatch_compensation_step` computes its own
`action_digest` from the *compensation* step's own `step_id`/`input_
mapping` (never the original step's -- this identifies *this compensation
dispatch attempt*, distinct from the original step it is undoing), writes
its own `workflow_run_steps` row (`step_type='compensation'`, at the
compensation step's own `step_index` in the graph -- a distinct row from
the step it compensates per `DATA-MODEL.md`) plus its own `compensation_
steps` ledger row (`compensates_step_index` = the *original* step's
index), and dispatches through whichever of two shapes applies (a genuine
judgment call, resolved and documented -- see that function's own
docstring for the full reasoning, not left ambiguous): if the *original*
step's own registered adapter declares `compensate()` (`adapters.
compensable`), that adapter's `compensate()` is called with the
*original* step's own resolved input (matching `FakeExternalActionAdapter.
compensate`'s actual documented signature -- "the same `action_input`
`execute()` was originally called with, not the prior `execute()` call's
output"); otherwise, if the *compensation* step's own `action_ref` names a
distinct registered adapter, that adapter's ordinary `execute()` is called
with the compensation step's own resolved input, exactly like any action
step. Once every qualifying compensation has been attempted: all
succeeded -> `_finish_run(..., "compensated")`; any failed/unknown ->
`_finish_run(..., "compensation_failed")`. `process_claimed_run`'s own
cancel/pause/kill-switch checks are deliberately not re-consulted anywhere
inside this sequence -- once compensation starts, it runs to its own
completion, matching this module's existing "an already-dispatched step is
never force-interrupted" philosophy, generalized to a whole compensation
sequence rather than one step.

**Task 6: a disclosed, real scope boundary -- compensation dispatch is not
crash-resumable.** `_CLAIMABLE_PREDICATE` is deliberately *not* widened to
reclaim `status = 'compensating'` rows the way it already is for `'leased'`/
`'running'` (module docstring point 2, above) -- doing so correctly would
require rebuilding `_qualifying_compensations`' own list idempotently on
resume (which it already can, by construction: the list is always
recomputed live from `run.current_step_index` plus which `workflow_run_
steps` rows are actually `'succeeded'`, so a second call is naturally
idempotent) *and* auditing `_dispatch_compensation_step`'s own crash-safety
the same rigor `run_step` itself received in Task 2's review. This task's
own required test list does not name a crash-mid-compensation case, and
building + proving that property correctly is a genuinely separate unit of
work from the compensation-dispatch mechanics themselves -- deliberately
left out of this task's scope rather than half-built and half-tested. **The
concrete consequence a reviewer should know:** a worker crash while
`status = 'compensating'` leaves that run permanently stuck (its lease
eventually expires, but no reclaim predicate ever picks it up again) until
an operator manually intervenes -- the same 90-second manual-confirmation
diagnostic query `docs/runbooks/PHASE-5-RECOVERY.md` already documents for
the `'leased'`/`'running'` case would need to additionally check `status =
'compensating'` to catch this today, which that runbook's own text does
not yet say. Flagged here, in this task's own PR evidence, and as a
concrete next step for a later task, not silently left implicit.

**Task 6: kill switches (`docs/runbooks/PHASE-5-RECOVERY.md`'s "Kill
switches and their recovery interaction" section).** `_CLAIMABLE_PREDICATE`
gained two correlated `NOT EXISTS` subqueries against `automation_kill_
switches` (global: `workflow_id IS NULL AND active`; per-workflow:
`workflow_id = workflow_runs.workflow_id AND active`; both scoped to
`workspace_id = workflow_runs.workspace_id`) -- a killed workflow's rows
are "never claimed, reclaimed, or resumed regardless of lease state,
including across a crash/restart cycle" (runbook, verbatim), satisfied
mechanically by this predicate being the *same* one statement every claim
and reclaim already goes through, not a second check bolted on elsewhere.
`process_claimed_run`'s per-step loop checks `kill_switches.
is_workflow_killed` at the identical point its `cancel_requested_at`/
`pause_requested_at` checks already run (before dispatching each
not-yet-dispatched step) -- a kill switch discovered mid-run stops the run
via `_pause_run(session, run, "needs_review")`, reusing the existing
terminal-until-human-action state (this module's own "unusable policy"
precedent, Task 3) rather than inventing a new run status; per the runbook,
re-enabling the kill switch later does **not** implicitly resume this run
-- `needs_review` is not in `_CLAIMABLE_PREDICATE`'s claimable set
regardless of kill-switch state, so there is no code path that could
auto-resume it either way. `enqueue_run` checks `kill_switches.
is_workflow_killed` before creating any `workflow_runs` row at all and
rejects with `WorkflowKilled` if killed -- "Global/workflow kill switches
stop new runs" (`PHASE-005-automation.md`'s Rollback plan, verbatim), not
only a claim-time gate that would otherwise let `queued` rows silently pile
up forever behind an active switch; `scheduler.run_scheduler_once`'s fire
path and `runs.py`'s `POST /automations/runs` both call this same
`enqueue_run`, so neither needs its own separate kill-switch check.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.observability import (
    queue_cancellation_latency,
    queue_run_state_transition,
    record_cancellation_latency,
    record_compensation_outcome,
    record_duplicate_dispatch_suppressed,
    record_run_queue_age,
    record_run_state_transition,
    record_step_outcome,
    record_step_retry,
    record_unknown_outcome,
)

from . import kill_switches
from . import policy as policy_module
from .adapters import AdapterRegistry, TransientAdapterError, call_compensate, compensable
from .approvals import (
    create_approval_request,
    evaluate_approval_requirement,
    get_approved_request,
    get_pending_approval,
)
from .workflows import get_active_workflow_version, get_workflow_version

RunStatus = Literal[
    "queued",
    "leased",
    "running",
    "waiting_approval",
    "paused",
    "needs_review",
    "succeeded",
    "failed",
    "cancelled",
    "compensating",
    "compensated",
    "compensation_failed",
    "expired",
    "rate_limited",
]
StepStatus = Literal[
    "pending", "dispatched", "succeeded", "failed", "unknown", "skipped", "retrying"
]

# Decision 3's concrete numbers -- referenced by docstrings/tests, and
# interpolated into the lease SQL below so there is exactly one place
# these three numbers live (never a second hardcoded literal that could
# silently drift from this module's own constants).
POLL_INTERVAL_SECONDS = 2
LEASE_DURATION_SECONDS = 30
HEARTBEAT_INTERVAL_SECONDS = 10

# Task 6's bounded-retry numbers (`EXECUTION-CONTRACT.md`: "bounded
# exponential backoff only for classified transient failures"). A step may
# be automatically re-dispatched up to `MAX_RETRY_ATTEMPTS` times after a
# `TransientAdapterError`; backoff for the Nth retry is `RETRY_BACKOFF_
# BASE_SECONDS ** N` seconds -- 2s, 4s, 8s for attempts 1/2/3 with the
# current numbers. Referenced by this module's own docstring/tests, same
# "exactly one place these numbers live" discipline as the three constants
# above.
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_BASE_SECONDS = 2

_TERMINAL_RUN_STATUSES = frozenset(
    {
        "succeeded",
        "failed",
        "cancelled",
        "compensated",
        "compensation_failed",
        "expired",
        "rate_limited",
    }
)

# See module docstring point 2: the reclaim predicate's expired-lease
# branch spans both lease-bearing states, not `leased` alone. Task 6 adds
# two more conjuncts: `next_attempt_at` gates a retry-pending run until its
# backoff elapses (module docstring's "Task 6: bounded retry" section),
# and the two correlated `NOT EXISTS` subqueries against `automation_kill_
# switches` are the mechanical enforcement of `docs/runbooks/
# PHASE-5-RECOVERY.md`'s "a killed workflow's rows are never claimed,
# reclaimed, or resumed regardless of lease state" (module docstring's
# "Task 6: kill switches" section) -- both scoped to `workspace_id =
# workflow_runs.workspace_id`, the first for a global switch (`workflow_id
# IS NULL`), the second for a switch naming this exact `workflow_id`.
_CLAIMABLE_PREDICATE = (
    "(status = 'queued' OR (status IN ('leased', 'running') AND leased_until < now())) "
    "AND (next_attempt_at IS NULL OR next_attempt_at <= now()) "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM automation_kill_switches aks_global"
    "  WHERE aks_global.workspace_id = workflow_runs.workspace_id"
    "    AND aks_global.workflow_id IS NULL AND aks_global.active"
    ") "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM automation_kill_switches aks_workflow"
    "  WHERE aks_workflow.workspace_id = workflow_runs.workspace_id"
    "    AND aks_workflow.workflow_id = workflow_runs.workflow_id AND aks_workflow.active"
    ")"
)

_RUN_FIELDS = """
    id, workspace_id, workflow_id, workflow_version, policy_id, trigger_ref,
    status, current_step_index, leased_by, leased_until, lease_heartbeat_at,
    cancel_requested_at, pause_requested_at, next_attempt_at, queued_at,
    started_at, finished_at, created_by, created_at, updated_at
"""

_STEP_FIELDS = """
    id, workspace_id, run_id, step_index, step_type, status, action_digest,
    attempt_count, input, output, started_at, finished_at, error_class,
    created_at, updated_at
"""

_COMPENSATION_STEP_FIELDS = """
    id, workspace_id, run_id, compensates_step_index, action_digest, status,
    started_at, finished_at, error_class, created_at, updated_at
"""

# Redaction markers (design doc Decision 8's Threat model section /
# `DATA-MODEL.md`'s "step payloads are redacted" requirement): any dict key
# whose name contains one of these (case-insensitive) has its value
# replaced before either `workflow_run_steps.input` or `.output` is ever
# written. This is a real, load-bearing transform on stored bytes, not a
# comment -- `_redact_payload` runs on every step INSERT/UPDATE in this
# module, and this task's own tests assert a marked key never reaches the
# database unredacted.
_REDACTION_MARKERS = ("secret", "password", "token", "credential", "api_key", "authorization")

_REDACTED_VALUE = "[REDACTED]"

# The natural key `run_step` addresses a `workflow_run_steps` row by,
# reused verbatim across its INSERT-once/UPDATE-on-outcome statements.
_STEP_ROW_WHERE = (
    "WHERE workspace_id = :workspace_id AND run_id = :run_id AND step_index = :step_index"
)


# ---------------------------------------------------------------------------
# Result / value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    id: UUID
    workspace_id: UUID
    workflow_id: str
    workflow_version: int
    policy_id: UUID | None
    trigger_ref: str | None
    status: RunStatus
    current_step_index: int
    leased_by: str | None
    leased_until: datetime | None
    lease_heartbeat_at: datetime | None
    cancel_requested_at: datetime | None
    pause_requested_at: datetime | None
    next_attempt_at: datetime | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowRunStep:
    id: UUID
    workspace_id: UUID
    run_id: UUID
    step_index: int
    step_type: str
    status: StepStatus
    action_digest: str | None
    attempt_count: int
    input: dict[str, Any]
    output: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None
    error_class: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CompensationStep:
    """The explicit compensation ledger row `DATA-MODEL.md` names
    (`compensation_steps`, Decision 9) -- a distinct table from `workflow_
    run_steps`'s own `step_type='compensation'` rows (module docstring's
    "Task 6: compensation dispatch" section explains the division: this
    table records the *relationship* -- which earlier step, by index, this
    compensation undoes -- while `workflow_run_steps` records the
    compensation step's own dispatch).
    """

    id: UUID
    workspace_id: UUID
    run_id: UUID
    compensates_step_index: int
    action_digest: str | None
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    error_class: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StepOutcome:
    step_index: int
    status: StepStatus
    output: dict[str, Any] | None
    error_class: str | None


@dataclass(frozen=True, slots=True)
class StepAwaitingApproval:
    """`run_step` did not dispatch `step_index` -- a fresh, digest-bound
    human approval is required and none exists yet for this exact digest
    (module docstring's "Task 3's approval/policy gate" section). No
    `workflow_run_steps` row is written for this outcome; `approval_id`
    names the `pending` (or already-`pending`, reused) `approval_requests`
    row a human resolves via `approvals.decide_approval`.
    """

    step_index: int
    approval_id: UUID


@dataclass(frozen=True, slots=True)
class StepBlockedByPolicy:
    """`run_step` did not dispatch `step_index` -- the run's policy is
    unusable (module docstring: unset, not found, revoked, or expired). No
    `workflow_run_steps` row is written for this outcome either.
    """

    step_index: int
    reason: Literal["no_policy", "policy_revoked", "policy_expired"]


@dataclass(frozen=True, slots=True)
class WorkflowNotActive:
    """`enqueue_run` was asked to run a `workflow_id` with no `active`
    `workflow_versions` row -- the mechanical source of `API-SCHEMAS.md`'s
    `WORKFLOW_NOT_ACTIVE` error code, matching Task 1's existing error-code
    convention (`workflows.py:WorkflowVersionNotActive`) exactly.
    """

    workflow_id: str


@dataclass(frozen=True, slots=True)
class WorkflowKilled:
    """`enqueue_run` was asked to start a run against a workflow a global
    or per-workflow kill switch currently blocks (Task 6, `docs/runbooks/
    PHASE-5-RECOVERY.md`'s "Kill switches" section / `PHASE-005-automation.
    md`'s Rollback plan: "Global/workflow kill switches stop new runs") --
    the mechanical source of `API-SCHEMAS.md`'s `kill_switch_active` error
    code for this call site (`runs.py`'s `POST /automations/runs` maps this
    to `409`).
    """

    workflow_id: str


@dataclass(frozen=True, slots=True)
class WorkflowRunNotFound:
    """No `workflow_runs` row matches the given lookup key in this
    workspace -- mirrors every other `*NotFound` dataclass in this
    package (`workflows.py:WorkflowVersionNotFound`,
    `policy.py:PolicyNotFound`).
    """


@dataclass(frozen=True, slots=True)
class WorkflowRunNotPaused:
    """`resume_run` (Task 4) was asked to resume a run that is not
    currently `'paused'` -- mirrors `approvals.ApprovalAlreadyDecided`'s
    precedent of surfacing a specific, informative non-actionable state
    rather than silently no-op-ing (resuming an already-`'succeeded'` run,
    for instance, is meaningfully different from resuming a run that was
    never paused in the first place, but this dataclass does not need to
    distinguish those further -- `status` alone is enough for a caller to
    decide what to show).
    """

    status: RunStatus


class WorkspaceScopeMismatch(ValueError):
    """Raised by `run_step` (Task 5's own self-review addition -- see
    `local_adapters.py`'s module docstring for the full confused-deputy
    discussion this closes) when a validated `action_input` declares a
    `workspace_id` field that does not match the dispatching `run`'s own
    `workspace_id`. `ActionAdapter.execute(action_input)` is given no
    `run`/`workspace_id`/session of its own (module docstring's
    commit-placement section), so an adapter whose `input_schema` happens
    to carry a `workspace_id` field has no independent way to verify it
    itself; `run_step` is the one place that already holds both values and
    can cheaply compare them before ever calling `execute()`. Caught by
    `run_step`'s existing broad `except Exception` around the `execute()`
    call, so this surfaces as an ordinary classified step failure
    (`error_class = 'WorkspaceScopeMismatch'`), not an unhandled crash.
    Inert (never raised) for any adapter whose `input_schema` carries no
    `workspace_id` field at all.
    """


class ActorScopeMismatch(ValueError):
    """`WorkspaceScopeMismatch`'s sibling for the *actor* half of the same
    confused-deputy question, closing a real forged-attribution hole found
    during this batch's own security audit. `WorkspaceScopeMismatch` already
    stops a workflow author from steering an adapter's write into another
    *workspace*; nothing stopped that same author from steering it onto
    another *user* inside their own workspace. `local.create_note`'s
    `CreateNoteInput.actor_id` is populated verbatim from the graph-authored
    `input_mapping` (`_resolve_step` returns it unchanged -- there is still
    no live templating engine, see this module's Task 5 section), and its
    `execute()` writes that value straight into `notes.owner_id`/`created_by`
    /`updated_by` *and* into an `audit_events` row's own `actor_id` with
    `authorization_result='allowed'`, `source='automation'`. So member A
    could publish a workflow naming member B's UUID and produce a note owned
    by B plus an audit row attributing A's automation to B -- a forged entry
    in the one table the whole system treats as authoritative provenance.

    `run_step` is again the one place that already holds both values and can
    cheaply compare them before `execute()` is ever called: `workflow_runs.
    created_by` (written by `enqueue_run` from its own `actor_id` argument,
    which every caller -- `runs.py`'s `POST /automations/runs` via `auth.
    user_id`, and `scheduler.py`'s fire path -- resolves server-side, never
    from a request body or a graph) records which user actually started this
    run. Raised when a validated `action_input` declares an `actor_id` field
    that is not that user. Caught by the same broad `except Exception`
    already wrapping the `execute()`/`compensate()` calls, so this surfaces
    as an ordinary classified step failure (`error_class =
    'ActorScopeMismatch'`), exactly like `WorkspaceScopeMismatch`, never an
    unhandled crash. Inert (never raised) for any adapter whose
    `input_schema` carries no `actor_id` field at all -- today that is every
    registered adapter except `local.create_note`, and every one of Tasks
    2-4's own test fakes.

    Note this is deliberately *stricter* than "same workspace": an author
    may only ever attribute an automation write to themselves, because
    `created_by` is the only user identity this dispatch path can prove.
    A future adapter that legitimately needs to act on another user's behalf
    would need a real delegation record to check against, not a graph-
    authored literal -- which is precisely the check this class exists to
    refuse to guess at.
    """


class UnsupportedStepType(ValueError):
    """`run_step` was asked to dispatch a step whose `step_type` is not
    `action`. Handling `approval_gate` (Task 3's approval-gate logic) and
    `condition` (never listed in this task's own scope) is deliberately
    out of this task's scope -- raised, not silently skipped or silently
    treated as an action, so a graph that reaches an unsupported step type
    fails loudly during this task's own tests rather than producing a
    misleading outcome. Every graph this task's own tests construct uses
    `action` steps only, so this is never hit by this task's own test
    suite; it exists as a guard against a future caller accidentally
    routing an unsupported step type through this task's dispatch path
    before the task that actually implements it lands.
    """


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def _redact_payload(value: Any) -> Any:
    """Recursively replaces the value of any dict key matching a
    `_REDACTION_MARKERS` substring (case-insensitive) with
    `_REDACTED_VALUE`. Applied to both `input` and `output` before either
    is ever written to `workflow_run_steps` -- the mechanical enforcement
    of "step payloads are redacted" (`DATA-MODEL.md`), not merely a
    documented intention. Non-dict/list values pass through unchanged.
    """
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, inner in value.items():
            if any(marker in key.lower() for marker in _REDACTION_MARKERS):
                redacted[key] = _REDACTED_VALUE
            else:
                redacted[key] = _redact_payload(inner)
        return redacted
    if isinstance(value, list):
        return [_redact_payload(item) for item in value]
    return value


# ---------------------------------------------------------------------------
# Row <-> dataclass mapping
# ---------------------------------------------------------------------------


def _row_to_run(row: dict[str, Any]) -> WorkflowRun:
    return WorkflowRun(
        id=row["id"],
        workspace_id=row["workspace_id"],
        workflow_id=row["workflow_id"],
        workflow_version=row["workflow_version"],
        policy_id=row["policy_id"],
        trigger_ref=row["trigger_ref"],
        status=row["status"],
        current_step_index=row["current_step_index"],
        leased_by=row["leased_by"],
        leased_until=row["leased_until"],
        lease_heartbeat_at=row["lease_heartbeat_at"],
        cancel_requested_at=row["cancel_requested_at"],
        pause_requested_at=row["pause_requested_at"],
        next_attempt_at=row["next_attempt_at"],
        queued_at=row["queued_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_step(row: dict[str, Any]) -> WorkflowRunStep:
    return WorkflowRunStep(
        id=row["id"],
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        step_index=row["step_index"],
        step_type=row["step_type"],
        status=row["status"],
        action_digest=row["action_digest"],
        attempt_count=row["attempt_count"],
        input=row["input"],
        output=row["output"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_class=row["error_class"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_compensation_step(row: dict[str, Any]) -> CompensationStep:
    return CompensationStep(
        id=row["id"],
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        compensates_step_index=row["compensates_step_index"],
        action_digest=row["action_digest"],
        status=row["status"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error_class=row["error_class"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def get_run(session: Session, workspace_id: UUID, run_id: UUID) -> WorkflowRun | None:
    row = (
        session.execute(
            text(
                f"SELECT {_RUN_FIELDS} FROM workflow_runs "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_run(dict(row)) if row is not None else None


def list_runs(
    session: Session, workspace_id: UUID, *, status_filter: RunStatus | None = None
) -> list[WorkflowRun]:
    """Workspace-scoped run listing (Task 4's `GET /automations/runs`) --
    added alongside `pause_run`/`resume_run` rather than in `runs.py`
    itself, matching this package's established convention that reads
    against a table live in that table's own owning module
    (`approvals.list_approvals`, `policy.list_policies`), with the router
    module itself staying a thin HTTP-shape layer.
    """
    clause = "AND status = :status_filter" if status_filter is not None else ""
    params: dict[str, Any] = {"workspace_id": workspace_id}
    if status_filter is not None:
        params["status_filter"] = status_filter
    rows = (
        session.execute(
            text(
                f"SELECT {_RUN_FIELDS} FROM workflow_runs "
                f"WHERE workspace_id = :workspace_id {clause} ORDER BY queued_at DESC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [_row_to_run(dict(row)) for row in rows]


def list_run_steps(session: Session, workspace_id: UUID, run_id: UUID) -> list[WorkflowRunStep]:
    rows = (
        session.execute(
            text(
                f"SELECT {_STEP_FIELDS} FROM workflow_run_steps "
                "WHERE workspace_id = :workspace_id AND run_id = :run_id ORDER BY step_index ASC"
            ),
            {"workspace_id": workspace_id, "run_id": run_id},
        )
        .mappings()
        .all()
    )
    return [_row_to_step(dict(row)) for row in rows]


def list_compensation_steps(
    session: Session, workspace_id: UUID, run_id: UUID
) -> list[CompensationStep]:
    """The compensation ledger for one run, `compensates_step_index`
    ascending -- Task 6's addition, mirroring `list_run_steps`'s own shape
    exactly for the sibling table.
    """
    rows = (
        session.execute(
            text(
                f"SELECT {_COMPENSATION_STEP_FIELDS} FROM compensation_steps "
                "WHERE workspace_id = :workspace_id AND run_id = :run_id "
                "ORDER BY compensates_step_index ASC"
            ),
            {"workspace_id": workspace_id, "run_id": run_id},
        )
        .mappings()
        .all()
    )
    return [_row_to_compensation_step(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------


def enqueue_run(
    session: Session,
    workspace_id: UUID,
    actor_id: UUID,
    *,
    workflow_id: str,
    trigger_ref: str | None = None,
) -> WorkflowRun | WorkflowNotActive | WorkflowKilled:
    """Creates a `queued` `workflow_runs` row pinned to `workflow_id`'s
    current `active` `workflow_versions` row -- reuses `workflows.
    get_active_workflow_version` (Task 1) rather than re-querying for an
    active version here, matching this task's own instruction. Rejects a
    workflow with no active version (`WorkflowNotActive`, Task 1's
    `WORKFLOW_NOT_ACTIVE`-shaped error-code convention) before any row is
    written.

    **Task 6.** Also rejects (`WorkflowKilled`) if `kill_switches.
    is_workflow_killed` reports a global or per-workflow kill switch
    currently active for this workspace/workflow -- checked before any row
    is written, the same "reject before writing" shape as the `WorkflowNot
    Active` check immediately above. This is the single choke point both
    `scheduler.run_scheduler_once`'s fire path and `runs.py`'s `POST
    /automations/runs` go through, so neither needs its own separate
    kill-switch check (module docstring's "Task 6: kill switches" section).
    """
    active = get_active_workflow_version(session, workspace_id, workflow_id)
    if active is None:
        return WorkflowNotActive(workflow_id=workflow_id)
    if kill_switches.is_workflow_killed(session, workspace_id, workflow_id):
        return WorkflowKilled(workflow_id=workflow_id)

    now = datetime.now(UTC)
    run_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO workflow_runs (
                id, workspace_id, workflow_id, workflow_version, policy_id,
                trigger_ref, status, current_step_index, queued_at,
                created_by, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :workflow_id, :workflow_version, :policy_id,
                :trigger_ref, 'queued', 0, :now, :created_by, :now, :now
            )
            """
        ),
        {
            "id": run_id,
            "workspace_id": workspace_id,
            "workflow_id": workflow_id,
            "workflow_version": active.version,
            "policy_id": active.policy_ref,
            "trigger_ref": trigger_ref,
            "created_by": actor_id,
            "now": now,
        },
    )
    result = get_run(session, workspace_id, run_id)
    assert result is not None  # just inserted, in the same transaction
    return result


# ---------------------------------------------------------------------------
# Claim / lease / heartbeat
# ---------------------------------------------------------------------------


def claim_next_run(session: Session, worker_id: str) -> WorkflowRun | None:
    """Design doc Decision 3's compare-and-swap claim, generalized per this
    module's docstring point 2. A plain, unlocked `SELECT` (no `FOR
    UPDATE` -- this task's own instruction) identifies one candidate row
    (`ORDER BY queued_at ASC`, oldest first); the single-statement
    `UPDATE ... WHERE id = :id AND <the identical predicate> RETURNING *`
    is the sole atomic claim. Returns `None` when there is nothing
    claimable, or when this worker lost the race for the one candidate it
    picked (the caller's next poll cycle, 2 seconds later per
    `POLL_INTERVAL_SECONDS`, tries again) -- both are the ordinary,
    expected steady state under concurrent workers, never an error.
    """
    candidate_id = session.execute(
        text(
            f"SELECT id FROM workflow_runs WHERE {_CLAIMABLE_PREDICATE} "
            "ORDER BY queued_at ASC LIMIT 1"
        )
    ).scalar()
    if candidate_id is None:
        return None

    row = (
        session.execute(
            text(
                f"""
                UPDATE workflow_runs
                SET status = 'leased', leased_by = :worker_id,
                    leased_until = now() + interval '{LEASE_DURATION_SECONDS} seconds',
                    lease_heartbeat_at = now(), updated_at = now()
                WHERE id = :id AND {_CLAIMABLE_PREDICATE}
                RETURNING {_RUN_FIELDS}
                """
            ),
            {"worker_id": worker_id, "id": candidate_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    # Durable before returning: a lease this worker believes it holds must
    # survive a crash on the very next line, or a subsequent claim attempt
    # would see this run still 'queued' and let a second worker dispatch it
    # from scratch -- see module docstring's commit-placement note.
    session.commit()
    # Task 6 observability: queue age (PHASE-005-automation.md's
    # Observability line, "queue age"). `queued_at` is never reset by a
    # reclaim (only a genuinely fresh `enqueue_run`/`resume_run` sets it),
    # so this also captures a reclaimed run's total time-to-claim across
    # its original queueing, not merely time since the most recent lease
    # expired -- the more useful end-to-end signal.
    record_run_queue_age((datetime.now(UTC) - row["queued_at"]).total_seconds())
    return _row_to_run(dict(row))


def renew_lease(
    session: Session, workspace_id: UUID, run_id: UUID, worker_id: str
) -> WorkflowRun | None:
    """The heartbeat (Decision 3: renewed every 10 seconds while a step is
    actively executing) -- an independently callable, independently
    testable function, not folded invisibly into `process_claimed_run`
    (this task's own instruction). Only renews a lease this exact
    `worker_id` currently holds on a still-lease-bearing run (`status IN
    ('leased', 'running', 'compensating')` -- Task 6 widens this by one
    more lease-bearing state, mirroring `claim_next_run`'s own `leased`/
    `running` generalization: `_mark_compensating` deliberately keeps the
    lease rather than releasing it, since compensation is an automatic
    continuation of the same claimed dispatch, not a human-wait state; `_
    run_compensation_sequence` renews this same heartbeat before each
    compensation step it dispatches, exactly like the main loop does for
    ordinary steps) -- a worker that has already lost its lease (reclaimed
    by another worker after a missed heartbeat) gets `None` back and must
    stop dispatching further steps for this run, never silently keep going
    under a lease it no longer holds.
    """
    row = (
        session.execute(
            text(
                f"""
                UPDATE workflow_runs
                SET leased_until = now() + interval '{LEASE_DURATION_SECONDS} seconds',
                    lease_heartbeat_at = now(), updated_at = now()
                WHERE workspace_id = :workspace_id AND id = :id AND leased_by = :worker_id
                  AND status IN ('leased', 'running', 'compensating')
                RETURNING {_RUN_FIELDS}
                """
            ),
            {"workspace_id": workspace_id, "id": run_id, "worker_id": worker_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    # Durable immediately: a heartbeat that isn't committed is, from a
    # recovering worker's perspective, a heartbeat that never happened --
    # module docstring's commit-placement note.
    session.commit()
    return _row_to_run(dict(row))


def _mark_running(session: Session, run: WorkflowRun) -> WorkflowRun:
    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE workflow_runs SET status = 'running', "
            "started_at = COALESCE(started_at, :now), updated_at = :now "
            "WHERE id = :id AND status = 'leased'"
        ),
        {"now": now, "id": run.id},
    )
    # Durable before the first step is dispatched -- module docstring's
    # commit-placement note; matches every other write in this function's
    # call chain.
    session.commit()
    result = get_run(session, run.workspace_id, run.id)
    assert result is not None
    return result


def _finish_run(session: Session, run: WorkflowRun, status: RunStatus) -> WorkflowRun:
    """Reserved for genuinely terminal states (`_TERMINAL_RUN_STATUSES`) --
    stamps `finished_at`. Never call this for `waiting_approval` or
    `needs_review`: both are excluded from `_TERMINAL_RUN_STATUSES`
    precisely because they are resumable (approval decided; an operator
    resolves the ambiguity), and `waiting_approval` in particular has a
    real, reachable resume path (`approvals._advance_run_after_decision`
    flips it straight back to `'queued'`) that does not clear a stale
    `finished_at` -- stamping it here would leave a `'queued'` (i.e.
    still-active) run showing a non-null `finished_at` from the moment it
    merely paused, until it next reaches a real terminal state. Use
    `_pause_run` for those two instead.
    """
    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE workflow_runs SET status = :status, finished_at = :now, updated_at = :now, "
            "leased_by = NULL, leased_until = NULL WHERE id = :id"
        ),
        {"status": status, "now": now, "id": run.id},
    )
    session.commit()
    record_run_state_transition(status)
    result = get_run(session, run.workspace_id, run.id)
    assert result is not None
    return result


def _pause_run(session: Session, run: WorkflowRun, status: RunStatus) -> WorkflowRun:
    """For `waiting_approval`/`needs_review` -- releases the lease (this
    worker is done with the run for now) exactly like `_finish_run`, but
    deliberately never touches `finished_at`: neither state is terminal
    (`_TERMINAL_RUN_STATUSES` excludes both), and `waiting_approval` has a
    real resume path back to `'queued'` (`approvals._advance_run_after_
    decision`) that a stale `finished_at` would otherwise leave behind on
    an actively-progressing run -- see `_finish_run`'s own docstring for
    the full reasoning.
    """
    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE workflow_runs SET status = :status, updated_at = :now, "
            "leased_by = NULL, leased_until = NULL WHERE id = :id"
        ),
        {"status": status, "now": now, "id": run.id},
    )
    session.commit()
    record_run_state_transition(status)
    result = get_run(session, run.workspace_id, run.id)
    assert result is not None
    return result


def _mark_compensating(session: Session, run: WorkflowRun) -> WorkflowRun:
    """Task 6: transitions a run whose most recent step failed (with at
    least one qualifying compensation to attempt) to `'compensating'` --
    module docstring's "Task 6: compensation dispatch" section. Deliberately
    does **not** release the lease (`leased_by`/`leased_until` untouched,
    unlike `_finish_run`/`_pause_run`): this is an automatic continuation
    of the same claimed run's own dispatch, not a state a different worker
    should be able to pick up mid-sequence.
    """
    now = datetime.now(UTC)
    session.execute(
        text("UPDATE workflow_runs SET status = 'compensating', updated_at = :now WHERE id = :id"),
        {"now": now, "id": run.id},
    )
    session.commit()
    record_run_state_transition("compensating")
    result = get_run(session, run.workspace_id, run.id)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Step dispatch -- idempotency-gated, digest-before-execute (Decision 3)
# ---------------------------------------------------------------------------


def compute_action_digest(
    *, workflow_id: str, workflow_version: int, step_id: str, resolved_input: dict[str, Any]
) -> str:
    """`sha256` over `{workflow_id, workflow_version, step_id,
    resolved_input}` (Decision 3, Threat model: "the digest covers the
    step's *resolved input*"), canonical (UTF-8, sorted-object-keys,
    compact-separator) JSON bytes -- identical hashing convention to
    `workflows.compute_definition_hash`.
    """
    material = {
        "workflow_id": workflow_id,
        "workflow_version": workflow_version,
        "step_id": step_id,
        "resolved_input": resolved_input,
    }
    canonical = dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _retry_backoff_seconds(attempt_count: int) -> int:
    """Task 6's bounded-exponential-backoff schedule: `RETRY_BACKOFF_BASE_
    SECONDS ** attempt_count` -- 2s/4s/8s for `attempt_count` 1/2/3 with
    this module's current constants. `attempt_count` here is the *new*
    (post-increment) attempt number a `TransientAdapterError` is about to
    schedule, never the pre-increment value.
    """
    # mypy's stub for `int.__pow__` returns `Any` (it must accommodate a
    # negative-exponent overload that can produce a float) -- both operands
    # are plain non-negative ints in every call this module makes, so the
    # real runtime type is always int; narrowed explicitly rather than
    # silencing the check.
    return int(RETRY_BACKOFF_BASE_SECONDS**attempt_count)


def _enforce_workspace_scope(action_input: Any, run: WorkflowRun, step: dict[str, Any]) -> None:
    """Task 5's defense-in-depth confused-deputy backstop (`WorkspaceScope
    Mismatch`'s own docstring has the full reasoning), extracted into a
    shared helper this task reuses verbatim for compensation dispatch
    (`_dispatch_compensation_step`) -- the identical trust question applies
    equally to a compensation action's own resolved input.
    """
    input_workspace_id = getattr(action_input, "workspace_id", None)
    if input_workspace_id is not None and input_workspace_id != run.workspace_id:
        raise WorkspaceScopeMismatch(
            f"step '{step.get('step_id')}' resolved input names "
            f"workspace_id={input_workspace_id}, which does not match run {run.id}'s "
            f"own workspace_id={run.workspace_id}"
        )


def _enforce_actor_scope(action_input: Any, run: WorkflowRun, step: dict[str, Any]) -> None:
    """`_enforce_workspace_scope`'s sibling for the actor half of the same
    confused-deputy question (`ActorScopeMismatch`'s own docstring has the
    full forged-attribution reasoning). Called from the identical call sites
    -- `run_step` and both branches of `_dispatch_compensation_step` --
    immediately after the workspace check and always before `execute()`/
    `compensate()`. `getattr(..., None)` keeps this inert for any adapter
    whose `input_schema` declares no `actor_id` field at all, exactly like
    the workspace check does for `workspace_id`.
    """
    input_actor_id = getattr(action_input, "actor_id", None)
    if input_actor_id is not None and input_actor_id != run.created_by:
        raise ActorScopeMismatch(
            f"step '{step.get('step_id')}' resolved input names "
            f"actor_id={input_actor_id}, which does not match run {run.id}'s "
            f"own created_by={run.created_by} -- an automation write may only ever "
            f"be attributed to the user who started the run"
        )


def _resolve_step(session: Session, run: WorkflowRun, step_index: int) -> dict[str, Any]:
    """Reads `step_index` out of the run's pinned `workflow_versions.graph`
    -- reuses `workflows.get_workflow_version` (Task 1) rather than
    re-querying `workflow_versions` directly, matching this task's own
    instruction to reuse Task 1's lookups rather than duplicate them.
    """
    version = get_workflow_version(session, run.workspace_id, run.workflow_id, run.workflow_version)
    if version is None:
        # A run can only ever be created against a version that existed at
        # enqueue time (enqueue_run's own FK-backed insert), and workflow
        # versions are immutable/never deleted -- reaching this branch
        # would indicate a real data-integrity bug, not a recoverable
        # runtime condition.
        raise RuntimeError(
            f"workflow_runs row {run.id} pins workflow_version "
            f"{run.workflow_id}@{run.workflow_version}, which no longer exists"
        )
    steps: list[dict[str, Any]] = version.graph.get("steps", [])
    return steps[step_index]


def _count_dispatched_action_steps(session: Session, workspace_id: UUID, run_id: UUID) -> int:
    """How many `workflow_run_steps` rows (any status) this run has
    already written -- every row this module ever inserts is for an
    `action` step (module docstring / `UnsupportedStepType`), so this is
    exactly "how many action steps has this run already attempted," the
    count `policy-limit-exceeding` (`approvals.evaluate_approval_
    requirement`) needs. Counts rows for the step currently being gated
    too if one somehow already existed (it never does here -- this is only
    ever called from the `else:` branch below, which is only reached when
    no row exists yet for this exact `step_index`), so it is exactly the
    count of *prior* steps, never off-by-one against the step under
    evaluation.
    """
    result = session.execute(
        text(
            "SELECT COUNT(*) FROM workflow_run_steps "
            "WHERE workspace_id = :workspace_id AND run_id = :run_id"
        ),
        {"workspace_id": workspace_id, "run_id": run_id},
    ).scalar()
    return int(result or 0)


def _evaluate_dispatch_gate(
    session: Session,
    run: WorkflowRun,
    step_index: int,
    digest: str,
    step: dict[str, Any],
    adapter_registry: AdapterRegistry,
) -> StepBlockedByPolicy | StepAwaitingApproval | None:
    """Task 3's gate (module docstring's own section has the full
    reasoning) -- only ever called from `run_step`'s `else:` branch, i.e.
    only when no `workflow_run_steps` row exists yet for this step at all.
    Returns `None` when the step is cleared to dispatch immediately:
    either the policy is usable and no approval is required, or an
    `approved` request already matches this exact, freshly-computed
    `digest`.
    """
    if run.policy_id is None:
        return StepBlockedByPolicy(step_index, "no_policy")
    policy_row = policy_module.get_policy(session, run.workspace_id, run.policy_id)
    if policy_row is None:
        return StepBlockedByPolicy(step_index, "no_policy")
    if not policy_module.is_policy_usable(policy_row):
        lifecycle = policy_module.policy_status(policy_row)
        reason: Literal["policy_revoked", "policy_expired"] = (
            "policy_revoked" if lifecycle == "revoked" else "policy_expired"
        )
        return StepBlockedByPolicy(step_index, reason)

    adapter = adapter_registry.get(step["action_ref"])
    if adapter is None:
        # Not a policy/approval concern -- the existing "AdapterNotRegistered"
        # branch further down handles an unregistered action_ref; clear
        # this gate and let dispatch proceed to that branch unchanged from
        # Task 2.
        return None

    action_step_count_so_far = _count_dispatched_action_steps(session, run.workspace_id, run.id)
    if not evaluate_approval_requirement(
        adapter, policy_row, action_step_count_so_far=action_step_count_so_far
    ):
        return None

    approved = get_approved_request(session, run.workspace_id, run.id, step_index, digest)
    if approved is not None:
        return None

    pending = get_pending_approval(session, run.workspace_id, run.id, step_index)
    if pending is not None:
        return StepAwaitingApproval(step_index, pending.id)

    created = create_approval_request(
        session, run.workspace_id, run.id, step_index, digest, adapter.high_impact_categories
    )
    # Durable immediately -- module docstring's "Task 3's approval/policy
    # gate" section: a crash here must never strand the run in
    # waiting_approval with no corresponding approval_requests row for a
    # human to act on.
    session.commit()
    return StepAwaitingApproval(step_index, created.id)


def run_step(
    session: Session, run: WorkflowRun, step_index: int, adapter_registry: AdapterRegistry
) -> StepOutcome | StepAwaitingApproval | StepBlockedByPolicy:
    """Dispatches (or resumes) exactly one step. At-most-one-effect
    (Decision 3): a `workflow_run_steps` row already `succeeded` under
    this `(run_id, step_index)` key is returned as-is, `execute()` is
    never called a second time. A row still `dispatched` (the digest was
    persisted before a prior attempt but no outcome was ever recorded --
    the crash-in-the-gap case) is marked `unknown` here and returned as
    such, again without calling `execute()` -- this is the mechanical
    surface of "unknown external outcome moves to review, never blind
    retry" (`EXECUTION-CONTRACT.md`).

    Only `step_type == "action"` is dispatched; anything else raises
    `UnsupportedStepType` (module docstring / class docstring: out of this
    task's scope). `process_claimed_run`'s own main loop never reaches this
    function with a non-`action` step (Task 6's own fix, module docstring's
    "Task 6: compensation dispatch" section) -- this remains a defensive
    guard against a future caller routing an unsupported step type through
    this path directly.

    **Task 3.** When no `workflow_run_steps` row exists yet for this step,
    `_evaluate_dispatch_gate` runs before any row is written -- an unusable
    policy or a not-yet-approved high-impact/policy-limit-exceeding step
    returns `StepBlockedByPolicy`/`StepAwaitingApproval` instead of a
    `StepOutcome`, and no row is written at all (module docstring's own
    "Task 3's approval/policy gate" section has the full reasoning for why
    this ordering, not "write `dispatched` first," is required).

    **Task 6.** A `'retrying'`-status existing row (a reclaimed, now-due
    run resuming the exact step a prior `TransientAdapterError` paused it
    on) falls through to a fresh dispatch attempt below exactly like the
    `'pending'`/`'skipped'` case always has -- the row is `UPDATE`d, never
    re-`INSERT`ed, and its `attempt_count` carries forward. If that
    dispatch attempt raises `TransientAdapterError` again and `attempt_
    count < MAX_RETRY_ATTEMPTS`, `run_step` itself durably commits another
    `'retrying'` transition and releases the run's lease back to `'queued'`
    (module docstring's "Task 6: bounded retry" section) -- once exhausted,
    it falls through to the ordinary, unconditional `'failed'` path below,
    unchanged from Task 2.
    """
    step = _resolve_step(session, run, step_index)
    if step.get("step_type") != "action":
        raise UnsupportedStepType(
            f"run_step only dispatches step_type='action' in this task's scope; "
            f"step '{step.get('step_id')}' (index {step_index}) is "
            f"'{step.get('step_type')}'"
        )

    resolved_input: dict[str, Any] = step.get("input_mapping", {})
    digest = compute_action_digest(
        workflow_id=run.workflow_id,
        workflow_version=run.workflow_version,
        step_id=step["step_id"],
        resolved_input=resolved_input,
    )

    existing = (
        session.execute(
            text(
                f"SELECT {_STEP_FIELDS} FROM workflow_run_steps "
                "WHERE workspace_id = :workspace_id AND run_id = :run_id "
                "AND step_index = :step_index FOR UPDATE"
            ),
            {"workspace_id": run.workspace_id, "run_id": run.id, "step_index": step_index},
        )
        .mappings()
        .one_or_none()
    )

    # Task 6: `attempt_count` carries forward across a retry-resume; a
    # fresh dispatch (no existing row, or a first-ever attempt) starts at 0.
    attempt_count = existing["attempt_count"] if existing is not None else 0

    if existing is not None:
        if existing["status"] == "succeeded":
            # Task 6 observability: at-most-one-effect's own steady-state
            # signal -- a step already succeeded under this digest is
            # returned as-is, execute() is never called again.
            record_duplicate_dispatch_suppressed()
            return StepOutcome(step_index, "succeeded", existing["output"], None)
        if existing["status"] in ("failed", "unknown"):
            return StepOutcome(
                step_index, existing["status"], existing["output"], existing["error_class"]
            )
        if existing["status"] == "dispatched":
            now = datetime.now(UTC)
            session.execute(
                text(
                    "UPDATE workflow_run_steps SET status = 'unknown', finished_at = :now, "
                    "updated_at = :now WHERE id = :id"
                ),
                {"now": now, "id": existing["id"]},
            )
            session.commit()
            record_step_outcome("unknown")
            record_unknown_outcome()
            return StepOutcome(step_index, "unknown", None, None)
        # 'pending'/'skipped'/'retrying' rows fall through to a fresh
        # dispatch attempt below -- 'pending'/'skipped' are never written by
        # this module (Task 2's own original comment); 'retrying' (Task 6)
        # is written by this same function on a prior TransientAdapterError
        # and is deliberately treated identically: not yet dispatched *this*
        # attempt, so re-dispatch below, reusing (never re-INSERTing) this
        # row.

    else:
        # Task 3's gate -- must run before any workflow_run_steps row for
        # this step exists at all (module docstring / run_step's own
        # docstring: writing 'dispatched' first would make an
        # approval-paused step indistinguishable from Task 2's own
        # crash-in-the-gap 'unknown' case on any later re-examination).
        gate = _evaluate_dispatch_gate(session, run, step_index, digest, step, adapter_registry)
        if gate is not None:
            return gate

        now = datetime.now(UTC)
        session.execute(
            text(
                """
                INSERT INTO workflow_run_steps (
                    id, workspace_id, run_id, step_index, step_type, status,
                    action_digest, input, started_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :run_id, :step_index, :step_type, 'dispatched',
                    :digest, CAST(:input AS jsonb), :now, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": step_index,
                "step_type": step["step_type"],
                "digest": digest,
                "input": dumps(_redact_payload(resolved_input)),
                "now": now,
            },
        )
        # Decision 3's core guarantee: the digest is durably committed to
        # the database *before* execute() is ever called -- a real COMMIT,
        # not merely `flush()` (module docstring's commit-placement note:
        # a flush alone is rolled back with the rest of an open transaction
        # if the process dies before that transaction's own COMMIT, which
        # would silently defeat this exact guarantee for the one crash
        # timing it exists to cover). A crash on the very next line leaves
        # exactly this row -- status='dispatched', a real, committed,
        # durable digest, no outcome -- for a later claim attempt's
        # `existing["status"] == "dispatched"` branch above to catch.
        session.commit()

    adapter = adapter_registry.get(step["action_ref"])
    if adapter is None:
        now = datetime.now(UTC)
        session.execute(
            text(
                "UPDATE workflow_run_steps SET status = 'failed', "
                "error_class = 'AdapterNotRegistered', finished_at = :now, updated_at = :now "
                f"{_STEP_ROW_WHERE}"
            ),
            {
                "now": now,
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": step_index,
            },
        )
        session.commit()
        record_step_outcome("failed")
        return StepOutcome(step_index, "failed", None, "AdapterNotRegistered")

    try:
        action_input = adapter.input_schema.model_validate(resolved_input)
        # Defense-in-depth confused-deputy backstop (Task 5's own
        # self-review addition, extracted into _enforce_workspace_scope by
        # Task 6 so compensation dispatch can reuse it verbatim) -- see
        # WorkspaceScopeMismatch's own docstring immediately above for why
        # this check, and not a different mechanism, is the right place
        # for it. _enforce_actor_scope closes the same hole's actor half
        # (ActorScopeMismatch's own docstring: a graph-authored actor_id
        # could otherwise forge note ownership and audit attribution onto
        # another user in the same workspace). Both run before execute() is
        # ever called, and neither is skipped on any path that reaches it.
        _enforce_workspace_scope(action_input, run, step)
        _enforce_actor_scope(action_input, run, step)
        output_model = adapter.execute(action_input)
        output_dict = output_model.model_dump(mode="json")
    except TransientAdapterError as exc:
        # Task 6: bounded retry (module docstring's own section). Only
        # this specific, adapter-asserted exception class is ever treated
        # as retry-safe -- every other exception (the `except Exception`
        # branch below) keeps Task 2's original unconditional-'failed'
        # behavior.
        if attempt_count < MAX_RETRY_ATTEMPTS:
            new_attempt_count = attempt_count + 1
            backoff_seconds = _retry_backoff_seconds(new_attempt_count)
            now = datetime.now(UTC)
            next_attempt_at = now + timedelta(seconds=backoff_seconds)
            session.execute(
                text(
                    "UPDATE workflow_run_steps SET status = 'retrying', "
                    "attempt_count = :attempt_count, error_class = :error_class, "
                    f"updated_at = :now {_STEP_ROW_WHERE}"
                ),
                {
                    "attempt_count": new_attempt_count,
                    "error_class": type(exc).__name__,
                    "now": now,
                    "workspace_id": run.workspace_id,
                    "run_id": run.id,
                    "step_index": step_index,
                },
            )
            # Release the lease back to 'queued', resumable at this exact
            # step_index (current_step_index is already persisted as
            # step_index by process_claimed_run's own loop before calling
            # run_step) -- the identical lease-release shape _pause_run
            # uses, except this is a direct UPDATE here (run_step's own
            # commit-placement discipline, not a call to _pause_run, since
            # _pause_run has no next_attempt_at parameter and this is not
            # one of its two named states).
            session.execute(
                text(
                    "UPDATE workflow_runs SET status = 'queued', "
                    "next_attempt_at = :next_attempt_at, "
                    "leased_by = NULL, leased_until = NULL, updated_at = :now WHERE id = :id"
                ),
                {"next_attempt_at": next_attempt_at, "now": now, "id": run.id},
            )
            session.commit()
            record_step_outcome("retrying")
            record_step_retry(new_attempt_count)
            return StepOutcome(step_index, "retrying", None, type(exc).__name__)

        # Exhausted -- bounded, never indefinite (EXECUTION-CONTRACT.md).
        # Falls through to the identical unconditional-'failed' write the
        # general `except Exception` branch below performs.
        now = datetime.now(UTC)
        session.execute(
            text(
                "UPDATE workflow_run_steps SET status = 'failed', error_class = :error_class, "
                f"finished_at = :now, updated_at = :now {_STEP_ROW_WHERE}"
            ),
            {
                "error_class": type(exc).__name__,
                "now": now,
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": step_index,
            },
        )
        session.commit()
        record_step_outcome("failed")
        return StepOutcome(step_index, "failed", None, type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 -- an adapter may raise any error class
        now = datetime.now(UTC)
        session.execute(
            text(
                "UPDATE workflow_run_steps SET status = 'failed', error_class = :error_class, "
                f"finished_at = :now, updated_at = :now {_STEP_ROW_WHERE}"
            ),
            {
                "error_class": type(exc).__name__,
                "now": now,
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": step_index,
            },
        )
        session.commit()
        record_step_outcome("failed")
        return StepOutcome(step_index, "failed", None, type(exc).__name__)

    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE workflow_run_steps SET status = 'succeeded', "
            "output = CAST(:output AS jsonb), finished_at = :now, updated_at = :now "
            f"{_STEP_ROW_WHERE}"
        ),
        {
            "output": dumps(_redact_payload(output_dict)),
            "now": now,
            "workspace_id": run.workspace_id,
            "run_id": run.id,
            "step_index": step_index,
        },
    )
    # Durable after the side effect too (Decision 3: "persists state
    # before and after each side effect") -- without this, a crash right
    # after a real (non-idempotent) adapter's execute() succeeded would
    # roll back the only record that it did, and a recovering worker would
    # see the digest-committed-but-no-outcome 'dispatched' state above and
    # correctly (but unnecessarily) surface it as needs_review rather than
    # knowing the effect already completed cleanly.
    session.commit()
    record_step_outcome("succeeded")
    return StepOutcome(step_index, "succeeded", output_dict, None)


# ---------------------------------------------------------------------------
# Task 6: compensation dispatch (Decision 9). Module docstring's "Task 6:
# compensation dispatch" section has the full design reasoning.
# ---------------------------------------------------------------------------


def _index_by_step_id(steps: list[dict[str, Any]]) -> dict[str, int]:
    return {step["step_id"]: index for index, step in enumerate(steps)}


def _qualifying_compensations(
    session: Session, run: WorkflowRun, steps: list[dict[str, Any]], boundary: int
) -> list[tuple[int, dict[str, Any], int, dict[str, Any]]]:
    """The graph's own already-`succeeded` `action` steps with index `<
    boundary` (the step that just failed, never itself compensated) that
    declare a `compensate_ref`, returned as `(original_index, original_step,
    compensation_index, compensation_step)` tuples in **descending**
    `original_index` order -- standard saga rollback order, undoing the
    most recently completed effect first (module docstring). Walking `range
    (boundary - 1, -1, -1)` directly produces this order without a separate
    reverse step.
    """
    index_by_id = _index_by_step_id(steps)
    qualifying: list[tuple[int, dict[str, Any], int, dict[str, Any]]] = []
    for original_index in range(boundary - 1, -1, -1):
        original_step = steps[original_index]
        if original_step.get("step_type") != "action":
            continue
        compensate_ref = original_step.get("compensate_ref")
        if not compensate_ref:
            continue
        succeeded = session.execute(
            text(
                "SELECT 1 FROM workflow_run_steps WHERE workspace_id = :workspace_id "
                "AND run_id = :run_id AND step_index = :step_index AND status = 'succeeded'"
            ),
            {
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": original_index,
            },
        ).first()
        if succeeded is None:
            # Declared but never actually dispatched/succeeded (e.g. the
            # workflow failed before reaching it) -- nothing to undo.
            continue
        compensation_index = index_by_id.get(compensate_ref)
        if compensation_index is None:
            # validate_graph_shape (workflows.py) already rejects a
            # compensate_ref that does not resolve at publish time -- this
            # branch should be unreachable against any workflow that
            # actually published, kept as a defensive skip rather than a
            # crash for a hand-constructed graph that bypassed validation.
            continue
        qualifying.append(
            (original_index, original_step, compensation_index, steps[compensation_index])
        )
    return qualifying


def _dispatch_compensation_step(
    session: Session,
    run: WorkflowRun,
    original_index: int,
    original_step: dict[str, Any],
    compensation_index: int,
    compensation_step: dict[str, Any],
    adapter_registry: AdapterRegistry,
) -> StepOutcome:
    """Dispatches one compensation step, idempotency-gated identically in
    spirit to `run_step` (existing-row short-circuit, digest committed
    before any adapter call, outcome committed immediately after) --
    module docstring's "Task 6: compensation dispatch" section has the
    full design, including the two-shapes judgment call this function
    resolves below.

    The `action_digest` this writes (both to `workflow_run_steps` and to
    the `compensation_steps` ledger row) is computed from the
    *compensation* step's own `step_id`/`input_mapping` -- it identifies
    *this compensation dispatch attempt*, distinct from the original step
    it is undoing, never the original step's own digest.
    """
    resolved_comp_input: dict[str, Any] = compensation_step.get("input_mapping", {})
    digest = compute_action_digest(
        workflow_id=run.workflow_id,
        workflow_version=run.workflow_version,
        step_id=compensation_step["step_id"],
        resolved_input=resolved_comp_input,
    )

    existing = (
        session.execute(
            text(
                f"SELECT {_STEP_FIELDS} FROM workflow_run_steps "
                "WHERE workspace_id = :workspace_id AND run_id = :run_id "
                "AND step_index = :step_index FOR UPDATE"
            ),
            {
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": compensation_index,
            },
        )
        .mappings()
        .one_or_none()
    )

    if existing is not None:
        if existing["status"] == "succeeded":
            return StepOutcome(compensation_index, "succeeded", existing["output"], None)
        if existing["status"] in ("failed", "unknown"):
            return StepOutcome(
                compensation_index, existing["status"], existing["output"], existing["error_class"]
            )
        if existing["status"] == "dispatched":
            now = datetime.now(UTC)
            session.execute(
                text(
                    "UPDATE workflow_run_steps SET status = 'unknown', finished_at = :now, "
                    "updated_at = :now WHERE id = :id"
                ),
                {"now": now, "id": existing["id"]},
            )
            session.execute(
                text(
                    "UPDATE compensation_steps SET status = 'unknown', finished_at = :now, "
                    "updated_at = :now WHERE workspace_id = :workspace_id AND run_id = :run_id "
                    "AND compensates_step_index = :original_index"
                ),
                {
                    "now": now,
                    "workspace_id": run.workspace_id,
                    "run_id": run.id,
                    "original_index": original_index,
                },
            )
            session.commit()
            record_step_outcome("unknown")
            record_unknown_outcome()
            record_compensation_outcome("unknown")
            return StepOutcome(compensation_index, "unknown", None, None)
        # 'pending'/'skipped'/'retrying' -- never written for a compensation
        # step by this function; fall through defensively, mirroring
        # run_step's own identical fallback.
    else:
        now = datetime.now(UTC)
        session.execute(
            text(
                """
                INSERT INTO workflow_run_steps (
                    id, workspace_id, run_id, step_index, step_type, status,
                    action_digest, input, started_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :run_id, :step_index, 'compensation', 'dispatched',
                    :digest, CAST(:input AS jsonb), :now, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": compensation_index,
                "digest": digest,
                "input": dumps(_redact_payload(resolved_comp_input)),
                "now": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO compensation_steps (
                    id, workspace_id, run_id, compensates_step_index, action_digest, status,
                    started_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :run_id, :compensates_step_index, :digest, 'dispatched',
                    :now, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "compensates_step_index": original_index,
                "digest": digest,
                "now": now,
            },
        )
        # Identical digest-before-execute durability discipline as
        # run_step's own INSERT -- module docstring's "no new crash-unsafe
        # path" requirement.
        session.commit()

    # The two-shapes judgment call (module docstring): prefer the
    # *original* step's own registered adapter's compensate(), called with
    # the *original* step's own resolved input (matches
    # FakeExternalActionAdapter.compensate's actual documented signature);
    # fall back to the *compensation* step's own action_ref naming a
    # distinct registered adapter, dispatched via that adapter's ordinary
    # execute() with the compensation step's own resolved input.
    original_adapter = adapter_registry.get(original_step.get("action_ref", ""))
    compensation_adapter = adapter_registry.get(compensation_step.get("action_ref", ""))

    if original_adapter is None and compensation_adapter is None:
        now = datetime.now(UTC)
        session.execute(
            text(
                "UPDATE workflow_run_steps SET status = 'failed', "
                "error_class = 'AdapterNotRegistered', finished_at = :now, updated_at = :now "
                f"{_STEP_ROW_WHERE}"
            ),
            {
                "now": now,
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": compensation_index,
            },
        )
        session.execute(
            text(
                "UPDATE compensation_steps SET status = 'failed', "
                "error_class = 'AdapterNotRegistered', finished_at = :now, updated_at = :now "
                "WHERE workspace_id = :workspace_id AND run_id = :run_id "
                "AND compensates_step_index = :original_index"
            ),
            {
                "now": now,
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "original_index": original_index,
            },
        )
        session.commit()
        record_step_outcome("failed")
        record_compensation_outcome("failed")
        return StepOutcome(compensation_index, "failed", None, "AdapterNotRegistered")

    try:
        if original_adapter is not None and compensable(original_adapter):
            original_resolved_input: dict[str, Any] = original_step.get("input_mapping", {})
            action_input = original_adapter.input_schema.model_validate(original_resolved_input)
            _enforce_workspace_scope(action_input, run, original_step)
            _enforce_actor_scope(action_input, run, original_step)
            output_model = call_compensate(original_adapter, action_input)
        else:
            assert compensation_adapter is not None
            action_input = compensation_adapter.input_schema.model_validate(resolved_comp_input)
            _enforce_workspace_scope(action_input, run, compensation_step)
            _enforce_actor_scope(action_input, run, compensation_step)
            output_model = compensation_adapter.execute(action_input)
        output_dict = output_model.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001 -- an adapter may raise any error class
        now = datetime.now(UTC)
        session.execute(
            text(
                "UPDATE workflow_run_steps SET status = 'failed', error_class = :error_class, "
                "finished_at = :now, updated_at = :now "
                f"{_STEP_ROW_WHERE}"
            ),
            {
                "error_class": type(exc).__name__,
                "now": now,
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "step_index": compensation_index,
            },
        )
        session.execute(
            text(
                "UPDATE compensation_steps SET status = 'failed', error_class = :error_class, "
                "finished_at = :now, updated_at = :now WHERE workspace_id = :workspace_id "
                "AND run_id = :run_id AND compensates_step_index = :original_index"
            ),
            {
                "error_class": type(exc).__name__,
                "now": now,
                "workspace_id": run.workspace_id,
                "run_id": run.id,
                "original_index": original_index,
            },
        )
        session.commit()
        record_step_outcome("failed")
        record_compensation_outcome("failed")
        return StepOutcome(compensation_index, "failed", None, type(exc).__name__)

    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE workflow_run_steps SET status = 'succeeded', "
            "output = CAST(:output AS jsonb), finished_at = :now, updated_at = :now "
            f"{_STEP_ROW_WHERE}"
        ),
        {
            "output": dumps(_redact_payload(output_dict)),
            "now": now,
            "workspace_id": run.workspace_id,
            "run_id": run.id,
            "step_index": compensation_index,
        },
    )
    session.execute(
        text(
            "UPDATE compensation_steps SET status = 'succeeded', finished_at = :now, "
            "updated_at = :now WHERE workspace_id = :workspace_id AND run_id = :run_id "
            "AND compensates_step_index = :original_index"
        ),
        {
            "now": now,
            "workspace_id": run.workspace_id,
            "run_id": run.id,
            "original_index": original_index,
        },
    )
    session.commit()
    record_step_outcome("succeeded")
    record_compensation_outcome("succeeded")
    return StepOutcome(compensation_index, "succeeded", output_dict, None)


def _run_compensation_sequence(
    session: Session,
    run: WorkflowRun,
    adapter_registry: AdapterRegistry,
    worker_id: str,
    steps: list[dict[str, Any]],
    qualifying: list[tuple[int, dict[str, Any], int, dict[str, Any]]],
) -> WorkflowRun:
    """Dispatches every qualifying compensation in order, renewing the
    lease before each -- continues attempting every one even after an
    earlier one fails (module docstring: "so partial compensation is
    visible"), never stopping early. `cancel_requested_at`/`pause_
    requested_at`/kill-switch are deliberately never re-checked here --
    module docstring's "once compensation starts, it runs to its own
    completion" note.
    """
    all_succeeded = True
    for original_index, original_step, compensation_index, compensation_step in qualifying:
        renewed = renew_lease(session, run.workspace_id, run.id, worker_id)
        if renewed is None:
            current = get_run(session, run.workspace_id, run.id)
            assert current is not None
            return current
        run = renewed

        outcome = _dispatch_compensation_step(
            session,
            run,
            original_index,
            original_step,
            compensation_index,
            compensation_step,
            adapter_registry,
        )
        if outcome.status != "succeeded":
            all_succeeded = False

    final_status: RunStatus = "compensated" if all_succeeded else "compensation_failed"
    return _finish_run(session, run, final_status)


# ---------------------------------------------------------------------------
# The claimed-run driving loop
# ---------------------------------------------------------------------------


def process_claimed_run(
    session: Session, run: WorkflowRun, adapter_registry: AdapterRegistry, worker_id: str
) -> WorkflowRun:
    """Runs a freshly-claimed (or reclaimed) run's steps sequentially from
    `current_step_index` to a terminal state -- Decision 3's concurrency
    model ("steps execute strictly sequentially ... never more than one
    in-flight step per run"). Renews the lease (`renew_lease`, the
    heartbeat) immediately before each step's dispatch; if the lease was
    lost (this worker's own heartbeat lost the race to a reclaim -- should
    not happen under this task's own numbers, since 30s lease vastly
    exceeds one in-process fake adapter call, but handled defensively
    regardless), this worker stops advancing the run immediately rather
    than dispatching further steps under a lease it no longer holds.

    Checks `cancel_requested_at`/`pause_requested_at`/a kill switch
    (Task 6) before each not-yet-dispatched step and stops there (module
    docstring's Cancellation / "Task 6: kill switches" sections) without
    dispatching that step at all.

    **Task 6.** Never calls `run_step` for a step whose `step_type !=
    "action"` -- the main walk skips forward over such a step without
    writing any `workflow_run_steps` row for it (module docstring's "Task
    6: compensation dispatch" section, point 1). A `'failed'` outcome no
    longer unconditionally finishes the run: it first checks for qualifying
    compensations and, if any exist, dispatches them via `_run_
    compensation_sequence` instead (module docstring, point 2).
    """
    run = _mark_running(session, run)
    version = get_workflow_version(session, run.workspace_id, run.workflow_id, run.workflow_version)
    assert version is not None
    steps: list[dict[str, Any]] = version.graph.get("steps", [])

    step_index = run.current_step_index
    while step_index < len(steps):
        step = steps[step_index]
        if step.get("step_type") != "action":
            # Task 6 fix: a step_type='compensation' (or, by the same
            # latent gap, 'approval_gate'/'condition') step must never be
            # passed to run_step by this linear walk -- run_step raises
            # UnsupportedStepType for any non-'action' step_type, which
            # would otherwise crash the whole claimed-run dispatch
            # unhandled. A compensation step is reachable only through the
            # dedicated compensation-dispatch path below.
            step_index += 1
            continue

        renewed = renew_lease(session, run.workspace_id, run.id, worker_id)
        if renewed is None:
            current = get_run(session, run.workspace_id, run.id)
            assert current is not None
            return current
        run = renewed

        if run.cancel_requested_at is not None:
            cancellation_latency = (datetime.now(UTC) - run.cancel_requested_at).total_seconds()
            record_cancellation_latency(cancellation_latency)
            return _finish_run(session, run, "cancelled")
        if run.pause_requested_at is not None:
            # Task 4 -- the user-initiated pause flag, checked at the
            # identical point in the loop as cancel_requested_at (module
            # docstring's "Task 4: user-initiated pause_run/resume_run"
            # section). Reuses _pause_run (Task 3's helper) purely because
            # its SQL shape -- release the lease, never touch finished_at
            # -- happens to be exactly what a non-terminal pause needs too;
            # this is not the same pause as Task 3's own waiting_approval/
            # needs_review transitions, only the same underlying mechanic.
            return _pause_run(session, run, "paused")
        if kill_switches.is_workflow_killed(session, run.workspace_id, run.workflow_id):
            # Task 6 -- module docstring's "Task 6: kill switches" section.
            return _pause_run(session, run, "needs_review")

        session.execute(
            text(
                "UPDATE workflow_runs SET current_step_index = :step_index, updated_at = now() "
                "WHERE id = :id"
            ),
            {"step_index": step_index, "id": run.id},
        )

        outcome = run_step(session, run, step_index, adapter_registry)
        if isinstance(outcome, StepAwaitingApproval):
            # current_step_index is already persisted as step_index (the
            # UPDATE immediately above) -- the run pauses exactly where it
            # is, never rewound; resuming re-enters this same step_index
            # (module docstring's "Resuming a waiting_approval run" note).
            return _pause_run(session, run, "waiting_approval")
        if isinstance(outcome, StepBlockedByPolicy):
            return _pause_run(session, run, "needs_review")
        if outcome.status == "succeeded":
            step_index += 1
            continue
        if outcome.status == "retrying":
            # Task 6: run_step itself already released the lease and set
            # run.status='queued'/next_attempt_at (module docstring's "Task
            # 6: bounded retry" section) -- nothing further to do here
            # except stop advancing this run.
            current = get_run(session, run.workspace_id, run.id)
            assert current is not None
            return current
        if outcome.status == "failed":
            # Task 6: compensation dispatch, module docstring point 2.
            qualifying = _qualifying_compensations(session, run, steps, step_index)
            if not qualifying:
                return _finish_run(session, run, "failed")
            run = _mark_compensating(session, run)
            return _run_compensation_sequence(
                session, run, adapter_registry, worker_id, steps, qualifying
            )
        if outcome.status == "unknown":
            return _pause_run(session, run, "needs_review")
        # 'skipped' -- never produced by run_step in this task's scope;
        # handled here only so a future step type that legitimately skips
        # itself advances cleanly rather than looping forever.
        step_index += 1

    session.execute(
        text(
            "UPDATE workflow_runs SET current_step_index = :step_index, updated_at = now() "
            "WHERE id = :id"
        ),
        {"step_index": len(steps), "id": run.id},
    )
    return _finish_run(session, run, "succeeded")


def run_worker_once(
    session: Session, worker_id: str, adapter_registry: AdapterRegistry
) -> WorkflowRun | None:
    """One full poll-cycle iteration: `claim_next_run` then, if a run was
    claimed, `process_claimed_run` to completion. Returns `None` when
    nothing was claimable this cycle (the ordinary steady state, matching
    `claim_next_run`'s own contract) -- callers that want a real polling
    loop call this repeatedly on `POLL_INTERVAL_SECONDS`; no such loop
    exists in this task's own scope (no process entrypoint is added here,
    matching how this module has no HTTP surface either) -- this task's
    own tests call `run_worker_once` (or `claim_next_run`/
    `process_claimed_run` directly) to exercise exactly one iteration at a
    time, deterministically.
    """
    run = claim_next_run(session, worker_id)
    if run is None:
        return None
    return process_claimed_run(session, run, adapter_registry, worker_id)


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


def cancel_run(
    session: Session, workspace_id: UUID, run_id: UUID
) -> WorkflowRun | WorkflowRunNotFound:
    """Sets `cancel_requested_at` (idempotent -- a second call never moves
    an already-recorded timestamp). A `queued` run (never claimed, no
    in-flight step to protect at all) is cancelled immediately. A claimed
    run's own `process_claimed_run` loop observes the flag before its next
    not-yet-dispatched step and stops there -- this function itself never
    touches `status` for anything already claimed, matching
    `EXECUTION-CONTRACT.md`'s "stops before the next side effect" (a step
    already dispatched to its adapter is never interrupted by this call).
    A run already in a terminal state is returned unchanged (cancelling a
    finished run is a no-op, not an error).

    **A `'paused'` run (Task 4) is also cancelled immediately, exactly
    like `'queued'` -- a real bug found and fixed during this task's own
    self-review, not part of the original Task 2 shape.** `'paused'` is
    deliberately excluded from `_CLAIMABLE_PREDICATE` (a paused run must
    never be silently reclaimed and resumed by a poll cycle -- only an
    explicit `resume_run` call may do that), which means a `'paused'` run
    is *never* revisited by `process_claimed_run` on its own. Before this
    fix, cancelling a `'paused'` run only set `cancel_requested_at` and
    left `status` unchanged (mirroring every other non-`'queued'` branch)
    -- since nothing ever calls `process_claimed_run` for a `'paused'` run
    again without an intervening `resume_run`, the run would sit forever
    in `'paused'` with a cancellation flag no code path ever consults, a
    permanently stuck state, not a genuinely cancelled one. This is safe
    to fix the same way `'queued'` already is: by the time a run reaches
    `'paused'`, `_pause_run` has already released its lease
    (`leased_by`/`leased_until` are both `NULL`), so there is no in-flight
    step to protect -- cancelling it has exactly the same safety
    properties as cancelling a never-claimed `'queued'` run.
    """
    row = (
        session.execute(
            text(
                "SELECT status FROM workflow_runs WHERE workspace_id = :workspace_id AND id = :id "
                "FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return WorkflowRunNotFound()

    if row["status"] in _TERMINAL_RUN_STATUSES:
        result = get_run(session, workspace_id, run_id)
        assert result is not None
        return result

    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE workflow_runs SET cancel_requested_at = COALESCE(cancel_requested_at, :now), "
            "updated_at = :now WHERE id = :id"
        ),
        {"now": now, "id": run_id},
    )
    if row["status"] in ("queued", "paused"):
        # 'paused' included alongside 'queued' -- module docstring's own
        # "real bug found and fixed" note above has the full reasoning.
        session.execute(
            text(
                "UPDATE workflow_runs SET status = 'cancelled', finished_at = :now, "
                "updated_at = :now WHERE id = :id"
            ),
            {"now": now, "id": run_id},
        )
        # Task 6 observability: this fast path sets cancel_requested_at and
        # reaches 'cancelled' in the same call, so latency is ~0 -- still
        # recorded for a complete signal. cancel_run is caller-committed
        # (module docstring: "no risky external call to protect against"),
        # so these are deferred until the enclosing transaction actually
        # commits, not recorded directly.
        queue_run_state_transition(session, "cancelled")
        queue_cancellation_latency(session, 0.0)

    result = get_run(session, workspace_id, run_id)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Task 4: user-initiated pause/resume (module docstring's own section has
# the full reasoning for why these are distinct from Task 3's private
# _pause_run helper despite the similar name and near-identical mechanic).
# ---------------------------------------------------------------------------


def pause_run(
    session: Session, workspace_id: UUID, run_id: UUID
) -> WorkflowRun | WorkflowRunNotFound:
    """Sets `pause_requested_at` (idempotent -- a second call never moves
    an already-recorded timestamp, identical to `cancel_run`'s own
    `COALESCE` precedent). A `queued` run (no in-flight step to protect)
    pauses immediately, exactly like `cancel_run`'s identical `queued`
    fast path. A claimed (`leased`/`running`) run's own `process_claimed_
    run` loop observes the flag before its next not-yet-dispatched step
    and stops there -- this function itself never touches `status` for
    anything already claimed, matching `cancel_run`'s own division of
    responsibility exactly. A run already in a terminal state
    (`_TERMINAL_RUN_STATUSES`) is returned unchanged (pausing a finished
    run is a no-op, not an error, matching `cancel_run`'s identical
    precedent for the same case). A run already `'paused'` is also
    returned unchanged -- idempotent, not an error.
    """
    row = (
        session.execute(
            text(
                "SELECT status FROM workflow_runs WHERE workspace_id = :workspace_id AND id = :id "
                "FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return WorkflowRunNotFound()

    if row["status"] in _TERMINAL_RUN_STATUSES or row["status"] == "paused":
        result = get_run(session, workspace_id, run_id)
        assert result is not None
        return result

    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE workflow_runs SET pause_requested_at = COALESCE(pause_requested_at, :now), "
            "updated_at = :now WHERE id = :id"
        ),
        {"now": now, "id": run_id},
    )
    if row["status"] == "queued":
        # No in-flight step to protect -- identical reasoning to
        # cancel_run's own 'queued' fast path. Does not touch finished_at
        # ('paused' is not terminal, _TERMINAL_RUN_STATUSES excludes it) --
        # the one point this diverges from cancel_run's otherwise-identical
        # 'queued' branch, since a paused run must remain resumable.
        session.execute(
            text("UPDATE workflow_runs SET status = 'paused', updated_at = :now WHERE id = :id"),
            {"now": now, "id": run_id},
        )

    result = get_run(session, workspace_id, run_id)
    assert result is not None
    return result


def resume_run(
    session: Session, workspace_id: UUID, run_id: UUID
) -> WorkflowRun | WorkflowRunNotFound | WorkflowRunNotPaused:
    """Flips a `'paused'` run back to `'queued'` -- `claim_next_run`'s
    ordinary `_CLAIMABLE_PREDICATE` already matches `'queued'`, so the
    very next poll cycle (any worker's) claims it exactly as it would any
    other queued run and `process_claimed_run` resumes at the unchanged
    `current_step_index`, mirroring `approvals._advance_run_after_
    decision`'s identical "flip straight back to queued, reuse the
    existing claim/poll machinery, no separate resume-specific reclaim
    path" precedent for `waiting_approval` exactly.

    **Clears `pause_requested_at` back to `NULL`** -- module docstring's
    own "resume_run clears pause_requested_at" section explains why this
    is required, not optional: leaving it set would make the very next
    `process_claimed_run` call re-pause the run instantly, before any
    step could dispatch, since that check runs at the top of the loop
    before `run_step` is ever called for the next step.
    """
    row = (
        session.execute(
            text(
                "SELECT status FROM workflow_runs WHERE workspace_id = :workspace_id AND id = :id "
                "FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return WorkflowRunNotFound()
    if row["status"] != "paused":
        return WorkflowRunNotPaused(status=row["status"])

    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE workflow_runs SET status = 'queued', queued_at = :now, "
            "pause_requested_at = NULL, leased_by = NULL, leased_until = NULL, "
            "updated_at = :now WHERE id = :id"
        ),
        {"now": now, "id": run_id},
    )
    result = get_run(session, workspace_id, run_id)
    assert result is not None
    return result
