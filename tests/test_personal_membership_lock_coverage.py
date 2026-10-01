"""Every caller-authorized write transaction in `domains/personal/*` takes
the membership lock first (ADR-0014). The lock is opted into per call site,
so a new write transaction that forgets it silently reopens the removal
race for that endpoint. The allowlist below is every transaction that
legitimately skips the helper, with the reason.
"""

from __future__ import annotations

from collections import Counter

from membership_lock_race_support import DOMAINS, unlocked_transactions

# (module, function) -> number of `session.begin()` blocks allowed to skip
# `authz.lock_membership_for_write`.
_ALLOWLIST: Counter[tuple[str, str]] = Counter(
    {
        # The idempotency-cache read before the model call (writes nothing)
        # and the three bookkeeping transactions (feature-disabled, failed
        # run, and after the locked insight insert) that store only the
        # caller's own cached response in `idempotency_records` -- no
        # personal data, no audit. The insight insert itself locks, after
        # the model call, never across it.
        ("ai_insights.py", "generate_insight_endpoint"): 4,
        # Read-only `consent_id` -> `domain_key` lookup; the write is
        # `_disable_domain`'s own locked transaction.
        ("domains.py", "revoke_consent_endpoint"): 1,
        # Background Gmail sync and action detection: no caller request, the
        # actor is the mailbox owner. Their write transactions already take
        # `lock_membership_shared` first and re-check the owner's membership
        # (`require_active_members_locked` / `member_is_active`), per
        # `connector_security`'s lock-ordering note; the rest are unlocked
        # reads (owner lookup, consent/membership pre-checks) that write
        # nothing. Sync is not a caller-authorized write, and the helper's
        # 403 is not the sync paths' `MembershipInactiveError` contract, so
        # none is switched to it.
        ("gmail_action_detection.py", "detect_actions_since"): 4,
        ("gmail_action_detection.py", "_detect_action_for_message"): 2,
        ("gmail_adapter.py", "_sync_messages"): 3,
        ("gmail_adapter.py", "_sync_history"): 3,
        ("gmail_adapter.py", "_process_message"): 1,
        # Plus its `retry_session` re-read of the winning alias after a lost
        # insert race, which writes nothing.
        ("gmail_adapter.py", "resolve_or_create_person"): 2,
        # Also reached from the owner's own `GET .../threads/{id}` (the body
        # fetch tool): after the Google call it takes `lock_membership_shared`
        # first and re-checks the message owner's membership, storing
        # nothing (not a 403) if inactive -- the lock is already there.
        ("gmail_adapter.py", "fetch_and_store_body"): 1,
        # The OAuth callback's persist transaction (`create_session`) starts
        # with `require_active_members_locked` (the shared lock plus the
        # caller's active-membership re-check, mapped to 403
        # MEMBERSHIP_INACTIVE with the minted grant revoked), then re-checks
        # the `write` role under it, because its only role gate ran before
        # the consent screen.
        ("gmail_oauth.py", "gmail_oauth_callback_endpoint"): 1,
    }
)


def test_every_personal_write_transaction_takes_the_membership_lock_first() -> None:
    assert unlocked_transactions((DOMAINS / "personal").glob("*.py")) == _ALLOWLIST
