"""Every authorized write transaction in `domains/engineering/*` takes the
membership lock first (ADR-0014). The lock is opted into per call site, so
a new write transaction that forgets it silently reopens the
removal/demotion race for that endpoint.

The shared scanner only sees transactions on a variable named `session`,
so a second, local check covers the other session names this domain opens
(`create_session`, `outcome_session`, `run_session`).
"""

from __future__ import annotations

import ast
from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

_MODULES = tuple((DOMAINS / "engineering").glob("*.py"))

# (module, function) -> count of transactions deliberately left without
# `authz.lock_membership_for_write` first.
_ALLOWED: list[tuple[str, str]] = [
    # Connector sync (`POST .../sync` and the auto-backfill). Phase 1
    # already takes the shared membership lock as its first statement
    # through `require_active_members_locked` (Spec A S1.11), which also
    # maps a removed actor to the MEMBERSHIP_INACTIVE skip, and authorizes
    # on the locked row after it; its OAuth token refresh under that lock is
    # the documented exception in `connector_security`'s lock-ordering note.
    # Phase 2 is the adapter's network call, holding nothing.
    ("connector_accounts.py", "_run_connector_sync"),
    # Sync-internal projection upserts, committed on the adapter's own
    # session between provider network calls in sync phase 2: no caller
    # AuthContext, and holding the lock would span the network walk. The
    # sync's phase-3 outcome transaction re-checks membership under the lock.
    ("datadog_adapter.py", "_upsert_monitor"),
    ("datadog_adapter.py", "_upsert_service_definition"),
    ("datadog_adapter.py", "_upsert_dashboard"),
    ("github_adapter.py", "_upsert_change"),
    ("github_adapter.py", "_upsert_review"),
    ("jira_adapter.py", "_upsert_work_item"),
    ("repository_sync.py", "upsert_repository"),
]

# Transactions on other session names, same allowlist shape.
_ALLOWED_OTHER_SESSIONS: list[tuple[str, str]] = [
    # Sync phase 3: first statement is `require_active_members_locked`
    # (the shared lock plus an active-membership re-check), as above.
    ("connector_accounts.py", "_run_connector_sync"),
    # Closes a skipped sync's reserved `sync_runs` row as `failed`: system
    # bookkeeping after a refusal, no caller write.
    ("connector_accounts.py", "_close_skipped_run"),
]

_LOCK_CALL = "authz.lock_membership_for_write("


def _unlocked_other_session_transactions() -> Counter[tuple[str, str]]:
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(_MODULES):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.With):
                    continue
                begins = [
                    item.context_expr
                    for item in node.items
                    if isinstance(item.context_expr, ast.Call)
                    and isinstance(item.context_expr.func, ast.Attribute)
                    and item.context_expr.func.attr == "begin"
                    and isinstance(item.context_expr.func.value, ast.Name)
                    and item.context_expr.func.value.id.endswith("session")
                    and item.context_expr.func.value.id != "session"
                ]
                if begins and not ast.unparse(node.body[0]).startswith(_LOCK_CALL):
                    found[(path.name, fn.name)] += 1
    return found


def test_every_engineering_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions(_MODULES) == Counter(_ALLOWED)


def test_engineering_transactions_on_other_sessions_take_the_membership_lock_first() -> None:
    assert _unlocked_other_session_transactions() == Counter(_ALLOWED_OTHER_SESSIONS)
