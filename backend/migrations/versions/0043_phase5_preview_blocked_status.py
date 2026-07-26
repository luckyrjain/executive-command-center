"""Widen workflow_runs.status to include 'preview_blocked' for Phase 5
Automation (`preview_only` policy mode now mechanically prevents real
dispatch).

Numbered ``0043`` -- the true next-in-chain after
``0042_phase5_compensation_retry_kill_switch.py``, per this codebase's
documented migration-numbering convention (migration ``0029``'s docstring
precedent, restated by ``0038``-``0042``'s own docstrings).

**Why a new run status at all, rather than reusing one of the fourteen
``0039`` already allows.** `docs/phases/phase-005/APPROVAL-POLICY.md` and
`docs/runbooks/PHASE-5-DOGFOOD.md` both describe `automation_policies.
approval_mode = 'preview_only'` as a mode that mechanically guarantees zero
real side effects -- Stage 1 of the staged dogfood window depends on it
("zero real side effects of any kind during this stage"). Until this change,
`preview_only` was implemented identically to `per_run` (approval gates
every step, but an *approved* step still called the adapter's `execute()`),
so that guarantee held by operator discipline only, not mechanically. The
fix (`ecc.domains.automation.worker._evaluate_dispatch_gate`) blocks a
`preview_only` step at dispatch time regardless of approval state, which
needs a run-level outcome that is:

- **not** `needs_review` -- nothing is broken, no human can resolve
  anything, and `needs_review` is the run state `docs/runbooks/
  PHASE-5-RECOVERY.md` tells an operator to *investigate*; a
  preview-only run reaching it would generate exactly the false alarm that
  runbook exists to prevent.
- **not** `failed`/`cancelled` -- neither is honest (nothing failed, nobody
  cancelled), and `UX-STATES.md`'s own operator-facing vocabulary treats
  both as outcomes worth reacting to.
- **terminal** -- a run under a `preview_only` policy can never make
  further progress. `automation_policies` has no update path at all (only
  create/revoke, `policy.py`), and a run pins its `policy_id` at enqueue
  time, so there is no reachable state change that could ever let this run
  dispatch. Recording it as terminal (`finished_at` stamped, lease
  released, excluded from `worker._CLAIMABLE_PREDICATE` like every other
  terminal status) is the honest shape; a resumable-looking state would
  imply an operator action that does not exist.

`'preview_blocked'` is therefore added to `ck_workflow_runs_status`, using
the identical drop-and-recreate-the-CHECK mechanism migration ``0042``
already used to widen `ck_workflow_run_steps_status` for `'retrying'`.
`workflow_run_steps` needs **no** equivalent widening: a preview-blocked
step deliberately writes no `workflow_run_steps` row at all (the same
"blocked outcomes leave no row" discipline `StepBlockedByPolicy`/
`StepAwaitingApproval` already follow, so a later re-evaluation of the same
step re-runs the gate from scratch rather than finding a row it must
interpret), so no new step-status value exists to allow.

Cleanly reversible: `downgrade()` restores ``0039``'s exact fourteen-value
constraint. A `downgrade` run against a database that already holds a
`'preview_blocked'` row would fail the recreated CHECK -- deliberately, and
identically to how ``0042``'s own downgrade behaves against a `'retrying'`
step row: a constraint-narrowing downgrade cannot silently discard rows the
narrower vocabulary has no honest value for.
"""

from alembic import op

revision = "0043_phase5_preview_blocked"
down_revision = "0042_phase5_comp_retry_kill"
branch_labels = None
depends_on = None

# Migration 0039's own fourteen, plus `preview_blocked`. Kept as a literal
# tuple here (not imported from `worker.py`'s `RunStatus`) matching every
# prior Phase 5 migration's own convention: a migration is a historical
# record of the schema at one point in time and must not change meaning if
# application code's own enumeration later does.
_RUN_STATUSES = (
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
    "preview_blocked",
)

_RUN_STATUSES_0039 = _RUN_STATUSES[:-1]


def upgrade() -> None:
    op.drop_constraint("ck_workflow_runs_status", "workflow_runs", type_="check")
    op.create_check_constraint(
        "ck_workflow_runs_status",
        "workflow_runs",
        "status IN (" + ", ".join(f"'{s}'" for s in _RUN_STATUSES) + ")",
    )


def downgrade() -> None:
    op.drop_constraint("ck_workflow_runs_status", "workflow_runs", type_="check")
    op.create_check_constraint(
        "ck_workflow_runs_status",
        "workflow_runs",
        "status IN (" + ", ".join(f"'{s}'" for s in _RUN_STATUSES_0039) + ")",
    )
