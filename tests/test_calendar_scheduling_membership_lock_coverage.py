"""Every write transaction in `calendar/*` and `scheduling/*` takes the
membership lock first (ADR-0014). The lock is opted into per call site, so
a new write transaction that forgets it silently reopens the
removal/demotion race for that endpoint.
"""

from __future__ import annotations

from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

_MODULES = (*(DOMAINS / "calendar").glob("*.py"), *(DOMAINS / "scheduling").glob("*.py"))

# (module, function) -> transactions allowed to skip the lock. Empty: the
# list/get endpoints only read (on an autobegun transaction they roll back,
# never commit), and `get_calendar_event_summary` runs inside its caller's
# already-locked transaction.
_ALLOWED: Counter[tuple[str, str]] = Counter()


def test_every_calendar_and_meeting_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions(_MODULES) == _ALLOWED
