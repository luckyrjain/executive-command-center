"""Every write transaction in `governance/*` takes the membership lock first
(ADR-0014). The lock is opted into per call site, so a new write
transaction that forgets it silently reopens the removal/demotion race for
that endpoint. This covers both shapes the domain uses: `with
session.begin():` blocks (risks) and autobegun transactions the function
commits itself (recommendations), including `GET /recommendations/{id}`,
whose expiry flip is a write attributed to the caller.
"""

from __future__ import annotations

from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

_MODULES = tuple((DOMAINS / "governance").glob("*.py"))


def test_every_governance_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions(_MODULES) == Counter()
