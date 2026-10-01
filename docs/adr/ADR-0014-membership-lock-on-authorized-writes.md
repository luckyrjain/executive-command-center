---
id: ADR-0014
title: Membership Lock on Authorized Writes
status: Proposed
version: 0.1.0
date: 2026-09-30
owner: Lucky Jain
related:
  - ADR-0008
---

# ADR-0014 — Membership Lock on Authorized Writes

## Context

A caller's role and membership can change while one of their requests is in flight. `identity/membership_removal.py` removes a member or changes a role while holding the workspace's membership-mutation advisory lock (`connector_security.membership_mutation_lock_key`) exclusively. Before this decision, the shared side of that lock (`lock_membership_shared`) was taken only by the paths listed in `connector_security`'s lock-ordering note: Gmail and connector sync, the opt-in `ai_runtime` persist, recommendation creation, and ownership transfer. Ordinary write endpoints did not take it.

That left a gap. A request could pass `authorize()`, an admin could then demote the caller to `viewer` or remove them, the membership change could commit at once, and the request's write could commit afterwards. The result was a write by someone who no longer had write access. Row locks such as `meetings FOR SHARE` or `attention_items FOR UPDATE` (PRs #314, #315, #316) close the same window for ownership and visibility changes, but not for role or membership changes, because those rows are never locked.

A second, smaller gap existed. Create endpoints check the caller's role once with `require_role_action`, and that check runs before the write transaction begins. So even a correctly locked transaction would never check the role again.

## Decision

**Every authorized write transaction takes the shared membership lock as its first statement, using `authz.lock_membership_for_write(session, auth, *, role_action=None)`, and authorizes only after it.**

- The helper takes `lock_membership_shared(session, auth.workspace_id)`. With `role_action`, it also checks the caller's current role inside the transaction and raises `403 INSUFFICIENT_ROLE`, as `require_role_action` does. Endpoints whose only role gate runs before the transaction (creates, and the bulk `regenerate`) pass `role_action="write"`. `role_action="read"` means "any active member".
- The call goes before `idempotency.lock_idempotency` and before any row lock. This follows the normative lock order in `connector_security`: membership, then idempotency, then rows.
- Paths that span several transactions (meeting-prep enrichment and, since the personal adoption, personal insight generation, both under `held_idempotency_lock`) take the lock in each write transaction, never across the model call. That places it after the session-scoped idempotency lock. This inversion of the lock order is accepted, for the reasons under Risks.
- The helper is called explicitly at each site, one line per write transaction. Adoption is per module. This change covers `attention/*` (attention items, feedback, regenerate, capacity, plans, planning constraints, risk reviews, waiting links) and `attention/meeting_prep`. The remaining domains adopt it in follow-up changes. Each follow-up needs a race test and a coverage guard, and adds a row under Adoption.

## Adoption

Each row is one adoption change. The race tests share `tests/membership_lock_race_support.py`, and each coverage guard runs its `unlocked_transactions()` scan over the listed modules.

| Modules | Race test | Coverage guard | Notes |
| --- | --- | --- | --- |
| `attention/*`, `attention/meeting_prep` | `test_attention_membership_lock_race_postgres.py` | `test_attention_membership_lock_coverage.py` | Enrichment locks per write transaction, never across the model call. |
| `planning/tasks`, `communication/commitments` | `test_tasks_commitments_membership_lock_race_postgres.py` | `test_tasks_commitments_membership_lock_coverage.py` | Creates pass `role_action="write"`. The shared write helpers (`insert_task`, `lifecycle_write`, ...) run inside their caller's transaction, so the caller locks. |
| `calendar/events`, `scheduling/meetings` | `test_calendar_scheduling_membership_lock_race_postgres.py` | `test_calendar_scheduling_membership_lock_coverage.py` | Event and meeting creates (standalone and event-linked) pass `role_action="write"`. No exceptions. |
| `automation/*`, `ai_runtime/prompts` | `test_automation_prompts_membership_lock_race_postgres.py` | `test_automation_prompts_membership_lock_coverage.py` | Creates and kill switches pass `role_action="write"`. `activate_policy` locks, then re-checks owner/admin inside the transaction. The worker, scheduler and `local_adapters` sessions have no caller and stay unlocked, so no adapter call runs under the lock. |
| `collaboration/delegations`, `platform/dashboard_briefs` | `test_collaboration_briefs_membership_lock_race_postgres.py` | `test_collaboration_briefs_membership_lock_coverage.py` | `role_action="read"`. Delegation access comes from being a party, and the brief is per-user data, so demotion does not revoke either; tests pin this. The brief GET locks because it may generate the brief, and it takes the lock before the per-user brief lock. It holds the lock even when the brief already exists and nothing is written, the same trade-off accepted for `get_prep`. The lazy-expiry transactions in delegation list/get are system-attributed and allowlisted. |
| `engineering/connector_accounts`, `engineering/decisions_incidents` | `test_engineering_membership_lock_race_postgres.py` | `test_engineering_membership_lock_coverage.py` | Creates and the metrics snapshot write pass `role_action="write"`. Connector create locks in both of its write transactions, so the role is re-checked after `adapter.authorize()` and the lock is never held across it. Sync keeps its existing `require_active_members_locked` phases. The adapter projection upserts commit between provider calls with no caller, and are allowlisted. |
| `personal/*` | `test_personal_membership_lock_race_postgres.py` | `test_personal_membership_lock_coverage.py` | `role_action="read"`. Personal had no role or membership gate beyond the session, because rows are the caller's own: removal now refuses with 403, and demotion to viewer does not. The exception is the Gmail OAuth callback, which requires the `write` role; it now re-checks that role under the lock after the Google round trip, so a member demoted on the consent screen gets 403 and the minted grant is revoked. `GET /personal/insights` locks because it upserts gap insights. Insight generation locks only the insert after the model call (the accepted `held_idempotency_lock` inversion, as in meeting prep). Background Gmail sync and detection keep their owner-based `lock_membership_shared` checks. |
| `governance/*` | `test_governance_membership_lock_race_postgres.py` | `test_governance_membership_lock_coverage.py` | Risk and recommendation creates pass `role_action="write"`. `create_recommendation` uses the helper for every caller, replacing the Gmail hook's raw shared lock; for an inactive actor it still raises `MembershipInactiveError`. Confirm's writes to tasks, commitments and risks run inside its own locked transaction. `GET /recommendations/{id}` locks because it may expire the recommendation in the caller's name. |
| `knowledge/*` | `test_knowledge_membership_lock_race_postgres.py`, `test_knowledge_embedding_after_commit_postgres.py` | `test_knowledge_membership_lock_coverage.py` | Entity, note, merge and candidate creates pass `role_action="write"`. The local embedding inference used to run inside the write transaction. It now runs after commit, with no transaction open (`embed_after_commit` / `embed_committed`), followed by a short write that is skipped if the document has changed since. These post-commit transactions are an allowlisted system projection. |

## Consequences

### Positive

- Removal and demotion now happen either before or after a write. A membership change that commits first is seen by every `authorize()` or `current_role()` read later in the write transaction, because each statement reads its own READ COMMITTED snapshot. A membership change that starts later waits for the write to commit. The race tests (`tests/test_attention_membership_lock_race_postgres.py`) hold the exclusive lock, demote or remove the caller, and assert `403`/`404` with no row written.
- Demotion revokes write access that came from the role. It does not revoke access to rows the caller owns, because `authorize()` always allows the owner (existing policy, pinned by `test_demoted_owner_still_writes_own_row`). `role_action="read"` answers a removed caller `403 INSUFFICIENT_ROLE`, not the `404` that per-row paths give.
- Writers never conflict with each other, because shared advisory locks are compatible. Ordinary write throughput is unchanged, apart from one extra lock statement per write transaction.

### Negative

- Removal and role change now wait for every in-flight write transaction in the workspace. While removal waits, Postgres queues new shared requests behind the pending exclusive one, so writes across the whole workspace pause until the removal commits. Removal is a rare admin action, so this is accepted. The requirement that follows is that a locked write transaction must stay short and must never make a network or model call. The one existing exception, sync phase 1's token refresh, is documented in `connector_security`.
- Removal's wait for the lock is itself a statement, capped at `STATEMENT_TIMEOUT_MS` (5 s). If any write transaction in the workspace holds the shared lock longer than that, the removal or role change fails with a retryable 500. It is not simply delayed. `regenerate_attention` on a very large workspace is the likeliest such transaction. It is accepted for now. The fix, if it ever bites, is a `lock_timeout` on removal plus a retry, or a shorter `regenerate`.
- Idempotent replays (`load_cached`) run after the lock but before any membership check on endpoints that authorize per row, so a removed member can still read back a response it was already given. Nothing is written, so this is accepted.
- `get_prep` (a GET) takes the lock too, because it can flip a pack to `stale` in the caller's name. This puts every prep view into the set of transactions a removal waits on and queues it behind a pending removal. That is accepted because the transaction is short. Taking the lock only right before the flip (then re-reading and re-authorizing) is the fallback if it ever matters.
- Each explicit call site can be forgotten. A new write endpoint that skips the helper reopens the window for that endpoint only. `tests/test_attention_membership_lock_coverage.py` guards the adopted modules: it fails on any `session.begin()` block there that doesn't start with the helper, except an explicit allowlist of read-only transactions. Each later adopter adds a sibling guard over its own modules (see Adoption).

### Risks

- **Lock-order inversion on the enrichment path.** The same reasoning covers personal insight generation (`personal/ai_insights.generate_insight_endpoint`), which takes the membership lock only for its insert after the model call, while holding `held_idempotency_lock`. `create_prep` and `refresh_prep` take the session-scoped idempotency lock K on the separate `lock_engine` connection, and only then take the membership lock per transaction. A cycle needs all four of the following at once: this request waiting for the membership lock behind a pending removal; the removal waiting for a second request's shared lock; that second request, from the same user with the same `Idempotency-Key`, waiting for K inside a transaction-scoped `lock_idempotency`; and K held by the first request. Postgres cannot see this cycle, because K sits on a different backend. The 5 s `statement_timeout` on the main engine connections breaks it. The waiting write or the removal fails with a retryable 500. It does not hang.

## Alternatives considered

- **Accept the window.** Rejected. The result is a write authorized by a role the caller no longer holds, which is a security-boundary violation. Membership changes are rare, so closing the window is cheap.
- **Fold the shared lock into `idempotency.lock_idempotency`.** This would cover about a hundred call sites in one change, with no edits to the files touched by the open lock-before-authz PRs. Rejected for three reasons. It hides a security lock inside a function named for something else. It misses write transactions that take no idempotency key (attention dismiss/defer/restore, waiting fulfil/cancel, constraint archive, regenerate, and the enrichment write transactions). And it would silently extend the removal wait to every idempotent transaction in every domain, including ones not yet audited for network calls under the lock.
- **Take the lock automatically on every transaction (a SQLAlchemy `after_begin` hook).** Rejected. It would also lock read-only transactions, and it would lock removal's own transaction, which would then hold the shared lock and try to upgrade to exclusive. Two concurrent removals would deadlock. It also cannot re-check the role for endpoints that gate on role before the transaction.
- **A FastAPI dependency.** Rejected. Dependencies run before the endpoint opens its transaction, and a transaction-scoped lock taken there would be released before the write.
- **Hold the membership lock for the whole enrichment request.** Rejected. It would make every removal in the workspace wait on a model call that can take minutes.
