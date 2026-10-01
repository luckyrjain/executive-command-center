"""Every user-facing write transaction in `automation/*` and
`ai_runtime/prompts.py` takes the membership lock first (ADR-0014). The
lock is opted into per call site, so a new write transaction that forgets
it silently reopens the removal/demotion race for that endpoint.
"""

from __future__ import annotations

from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

_MODULES = (*(DOMAINS / "automation").glob("*.py"), DOMAINS / "ai_runtime" / "prompts.py")

# (module, function) -> write transactions allowed to skip the lock. All are
# background/system transactions with no caller AuthContext: nobody's
# membership authorized them, so there is nothing for a removal or
# demotion to revoke mid-transaction (the run's actor is re-checked at
# enqueue, and `membership_removal` cancels a removed member's runs under
# the exclusive lock).
_ALLOWED: Counter[tuple[str, str]] = Counter(
    {
        # Scheduler tick: fires due schedule triggers (enqueue + trigger
        # bookkeeping) per commit. No caller `AuthContext`, so not
        # `lock_membership_for_write`: its fire transaction takes the raw
        # `lock_membership_shared` first, so `enqueue_run`'s check that the
        # trigger's creator is still a member holds.
        ("scheduler.py", "run_scheduler_once"): 1,
        # Worker lease/claim and run/step state machine, driven by the
        # background worker loop. `run_step` and
        # `_dispatch_compensation_step` commit around adapter `execute()`
        # calls; these must never hold the membership lock.
        ("worker.py", "claim_next_run"): 1,
        ("worker.py", "renew_lease"): 1,
        ("worker.py", "_write_owned_run_state"): 1,
        ("worker.py", "_mark_running"): 1,
        ("worker.py", "_evaluate_approval_gate"): 1,
        ("worker.py", "run_step"): 1,
        ("worker.py", "_fail_compensation_row"): 1,
        ("worker.py", "_dispatch_compensation_step"): 1,
        # `local.create_note` adapter: its own session, opened from inside
        # the worker's `run_step` dispatch, never from an HTTP request.
        ("local_adapters.py", "execute"): 1,
    }
)


def test_every_automation_and_prompt_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions(_MODULES) == _ALLOWED
