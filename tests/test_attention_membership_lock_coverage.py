"""Every write transaction in `attention/*` takes the membership lock first
(ADR-0014). The lock is opted into per call site, so a new write
transaction that forgets it silently reopens the removal/demotion race for
that endpoint. This scans the adopted modules and fails on any
`with session.begin():` block whose first statement is not
`authz.lock_membership_for_write(...)`, unless it is a known read-only
transaction listed below.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

_ATTENTION = Path(__file__).resolve().parents[1] / "backend" / "ecc" / "domains" / "attention"

# (module, function) -> number of read-only `session.begin()` blocks allowed
# to skip the lock: meeting-prep enrichment's idempotency-cache read and its
# pre-enrichment authorize-and-generate transaction, which write nothing
# (the final write transaction re-locks and re-authorizes).
_READ_ONLY: Counter[tuple[str, str]] = Counter(
    {
        ("meeting_prep.py", "create_prep"): 2,
        ("meeting_prep.py", "refresh_prep"): 2,
    }
)


def _unlocked_transactions() -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(_ATTENTION.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.With) or not any(
                    ast.unparse(item.context_expr) == "session.begin()" for item in node.items
                ):
                    continue
                first = ast.unparse(node.body[0])
                if not first.startswith("authz.lock_membership_for_write("):
                    found[(path.name, fn.name)] += 1
    return found


def test_every_attention_write_transaction_takes_the_membership_lock_first() -> None:
    assert _unlocked_transactions() == _READ_ONLY
