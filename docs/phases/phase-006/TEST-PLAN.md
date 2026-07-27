---
id: PHASE-006-TEST-PLAN
title: Phase 6 Test Plan
status: Approved for Implementation
version: 0.4.0
owner: Lucky Jain
---

# Phase 6 Test Plan

**Task 1 status**: `tests/test_engineering_connectors_postgres.py` covers the sandbox adapter, connector account lifecycle, cursor durability across backfill/incremental sync, disconnect, and workspace isolation. Webhook dedupe, rate limits, access loss, deletion, rename, metric fixtures and ambiguous identities have no real provider or metric computation to test against yet -- each lands with the task that implements it (`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`, Tasks 2-6).

**Task 2 status**: `tests/test_engineering_github_sync_postgres.py` closes the "rate limits" gap Task 1 left open -- `GitHubAdapter`'s bounded rate-limit retry is covered against a real (mocked-transport) GitHub response shape: a single rate-limited response that succeeds after the bounded wait, a rate limit that persists beyond the bound (`partial`, cursor preserved), and a rate limit that persists through the one retry as well (also `partial`, not an unhandled failure). Also covered: `authorize` (success/401/non-200/network-error/scope-checking), pagination via `Link`-header traversal, the incremental-cursor stop-early condition, the `_MAX_PAGES_PER_CALL` bound reporting `partial` rather than a silent `succeeded`, `refresh_permissions`, `disconnect`, `handle_webhook`'s parsing/upsert logic (not its receiving endpoint -- there isn't one yet), and end-to-end coverage through the real `/sync` endpoint including the concurrent-sync-in-progress guard (`uq_sync_runs_running_per_account`, migration `0046`). Access loss, deletion, rename and a real webhook-receiving endpoint remain untested because the code they'd test does not exist yet (Task 2's own disclosed deferrals -- see `github_adapter.py`'s module docstring); metric fixtures and ambiguous identities remain Task 5/6 scope.

**Task 3 status**: `tests/test_engineering_gitlab_sync_postgres.py` mirrors Task 2's own coverage shape against `GitLabAdapter`'s real API shapes: `authorize` (success/missing-scope/revoked-token/inactive-token/401/non-200/network-error -- no fine-grained-PAT-style unverifiable case, since GitLab always returns real scopes, see `gitlab_adapter.py`'s module docstring), pagination via `Link`-header traversal (including a comma-containing `next` URL), the incremental-cursor stop-early condition, rate-limit retry succeeding/giving-up/still-limited-after-retry, the `_MAX_PAGES_PER_CALL` bound reporting `partial`, `refresh_permissions`, `handle_webhook`'s parsing/upsert logic, and `disconnect`'s real (not no-op) revocation attempt covering both the success (204) and the connector's-own-scope-realistically-fails (403) paths. End-to-end coverage through the real `/sync` endpoint matches Task 2's own. Same deferrals as Task 2 (access loss, deletion, rename, real webhook-receiving endpoint, metric fixtures, ambiguous identities) for the identical reasons.

Test sandbox adapters, backfill/incremental sync, cursor durability, webhook dedupe, rate limits, access loss, deletion, rename, disconnect and rebuild. Validate metric fixtures and definitions against hand-calculated results. Verify partial coverage and ambiguous identities.

Security covers token redaction, least scopes, webhook signatures, malicious payloads, isolation and approved writes. Ethics checks prohibit person scores/leaderboards. Browser acceptance connects a sandbox, observes sync/coverage, traces a risk to evidence and handles degraded states.
