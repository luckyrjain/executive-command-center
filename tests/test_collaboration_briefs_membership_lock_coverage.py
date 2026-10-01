"""Every write transaction in `collaboration/*` and
`platform/dashboard_briefs.py` takes the membership lock first (ADR-0014).
The lock is opted into per call site, so a new write transaction that
forgets it silently reopens the removal/demotion race for that endpoint.
"""

from __future__ import annotations

from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

_MODULES = (
    *(DOMAINS / "collaboration").glob("*.py"),
    DOMAINS / "platform" / "dashboard_briefs.py",
)

# (module, function) -> number of `session.begin()` blocks allowed to skip
# the lock. `GET /delegations` and `GET /delegations/{id}` write only lazy
# expiry: a time-based system transition (actor NULL in `delegation_events`
# and `audit_events`) that is correct whoever triggers it, so no write
# there depends on the caller's role or membership.
_ALLOWED: Counter[tuple[str, str]] = Counter(
    {
        ("delegations.py", "list_delegations_endpoint"): 1,
        ("delegations.py", "get_delegation_endpoint"): 1,
    }
)


def test_every_delegation_and_brief_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions(_MODULES) == _ALLOWED
