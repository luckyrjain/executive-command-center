"""Every authorized write transaction in `domains/engineering/*` takes the
membership lock first (ADR-0014). The lock is opted into per call site, so
a new write transaction that forgets it silently reopens the
removal/demotion race for that endpoint. The shared scanner checks every
session name (`session`, `create_session`, `outcome_session`, ...).
"""

from __future__ import annotations

from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

_MODULES = tuple((DOMAINS / "engineering").glob("*.py"))

# (module, function) -> count of transactions deliberately left without
# `authz.lock_membership_for_write` first.
_ALLOWED: Counter[tuple[str, str]] = Counter(
    {
        # Connector sync (`POST .../sync` and the auto-backfill). Phases 1
        # (`session`) and 3 (`outcome_session`) already take the shared
        # membership lock as their first statement through
        # `require_active_members_locked` (Spec A S1.11), which also maps a
        # removed actor to the MEMBERSHIP_INACTIVE skip, and authorize on the
        # locked row after it; phase 1's OAuth token refresh under that lock
        # is the documented exception in `connector_security`'s lock-ordering
        # note. Phase 2 is the adapter's network call, holding nothing.
        ("connector_accounts.py", "_run_connector_sync"): 2,
        # Closes a skipped sync's reserved `sync_runs` row as `failed`
        # (`run_session`): system bookkeeping after a refusal, no caller write.
        ("connector_accounts.py", "_close_skipped_run"): 1,
        # Sync-internal projection upserts, committed on the adapter's own
        # session between provider network calls in sync phase 2: no caller
        # AuthContext, and holding the lock would span the network walk. The
        # sync's phase-3 outcome transaction re-checks membership under the
        # lock.
        ("datadog_adapter.py", "_upsert_monitor"): 1,
        ("datadog_adapter.py", "_upsert_service_definition"): 1,
        ("datadog_adapter.py", "_upsert_dashboard"): 1,
        ("github_adapter.py", "_upsert_change"): 1,
        ("github_adapter.py", "_upsert_review"): 1,
        ("jira_adapter.py", "_upsert_work_item"): 1,
        ("repository_sync.py", "upsert_repository"): 1,
    }
)


def test_every_engineering_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions(_MODULES) == _ALLOWED
