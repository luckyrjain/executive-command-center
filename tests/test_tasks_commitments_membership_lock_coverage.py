"""Every write transaction in `planning/tasks.py` and
`communication/commitments.py` takes the membership lock first (ADR-0014).
The lock is opted into per call site, so a new write transaction that
forgets it silently reopens the removal/demotion race for that endpoint.
"""

from __future__ import annotations

from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

_MODULES = (DOMAINS / "planning" / "tasks.py", DOMAINS / "communication" / "commitments.py")


def test_every_task_and_commitment_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions(_MODULES) == Counter()
