"""The adoption guard (`membership_lock_race_support.unlocked_transactions`)
itself: it must see transactions on every session name, not only
`session`, and both of its checks must run in the same function."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from membership_lock_race_support import unlocked_transactions

_SOURCE = """
def locked(session, auth):
    with session.begin():
        authz.lock_membership_for_write(session, auth)
        session.execute(x)


def unlocked_begin(session):
    with session.begin():
        session.execute(x)


def unlocked_other_name(auth):
    with SessionFactory() as create_session, create_session.begin():
        create_session.execute(x)


def locked_other_name(auth):
    with SessionFactory() as create_session, create_session.begin():
        authz.lock_membership_for_write(create_session, auth)


def unlocked_autobegin(session):
    session.execute(x)
    session.commit()


def locked_autobegin(session, auth):
    authz.lock_membership_for_write(session, auth)
    session.execute(x)
    session.commit()


def both_shapes(session, auth):
    with session.begin():
        authz.lock_membership_for_write(session, auth)
    retry_session.execute(x)
    retry_session.commit()
"""


def test_scanner_flags_unlocked_transactions_on_any_session_name(tmp_path: Path) -> None:
    module = tmp_path / "sample.py"
    module.write_text(_SOURCE)

    assert unlocked_transactions([module]) == Counter(
        {
            ("sample.py", "unlocked_begin"): 1,
            ("sample.py", "unlocked_other_name"): 1,
            ("sample.py", "unlocked_autobegin"): 1,
            ("sample.py", "both_shapes"): 1,
        }
    )
