"""Every write transaction in `knowledge/*` takes the membership lock first
(ADR-0014). The lock is opted into per call site, so a new write
transaction that forgets it silently reopens the removal/demotion race for
that endpoint. Every `session.begin()` block in the domain is an
authorized user write except the post-commit embedding step below.
"""

from __future__ import annotations

from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

# (module, function) -> number of transactions allowed to skip the lock:
# `embed_committed`'s read and its content-hash-guarded upsert of the
# derived `embedding_projections` row. They run after the request's write
# transaction has committed, with no caller authorization (a system-derived
# projection of an already-authorized write), and must hold no lock across
# the model call that sits between them.
_ALLOWED: Counter[tuple[str, str]] = Counter({("embeddings.py", "embed_committed"): 2})


def test_every_knowledge_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions((DOMAINS / "knowledge").glob("*.py")) == _ALLOWED
