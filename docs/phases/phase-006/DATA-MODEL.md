---
id: PHASE-006-DATA-MODEL
title: Phase 6 Engineering Workspace Data Model
status: Approved for Implementation
version: 0.4.0
owner: Lucky Jain
---

# Phase 6 Data Model

Core projections: `connector_accounts`, `sync_cursors`, `sync_runs`, `repositories`, `engineering_work_items`, `changes`, `reviews`, `deployments`, `incidents`, `engineering_decisions`, `service_links`, `delivery_metric_snapshots` and `source_tombstones`.

The "every projection stores provider, external ID, source URL, observed/updated times, permission/freshness state and raw-content hash" rule below governs synced-content projections specifically (`repositories` onward) -- `connector_accounts`/`sync_cursors`/`sync_runs` are connector-platform bookkeeping tables, not synced-content records with their own external business identity, so they do not carry a `source_url` or content-hash column; they are scoped instead by `(workspace_id, provider, external_account_id)` and `(workspace_id, connector_account_id, resource_type)` respectively (`backend/migrations/versions/0044_phase6_connector_platform.py`).

**Task 1-4 status**: `connector_accounts`, `sync_cursors`, `sync_runs` (migration `0044`), `repositories` (migration `0045`) and `engineering_work_items` (migration `0047_phase6_work_items.py`) are implemented. Task 3 (GitLab) populated `repositories` with `provider = 'gitlab'` rows -- no new migration, since the table's own `ck_repositories_provider` CHECK constraint already named `gitlab` alongside `github`/`jira`/`sandbox`. Task 4 (Jira) adds `engineering_work_items`, mirroring `repositories`' shape (provider/external ID/source URL/observed-updated times/permission-freshness state/content hash) plus work-item-specific `item_type`/`status`/`reporter_external_id`/`assignee_external_id` columns -- the latter two store raw, unresolved Jira `accountId` strings; resolving them against Phase 2 `Person` entities is explicit Task 6 scope, not this task's. `changes` through `source_tombstones` do not exist yet -- each is added by the task that first populates it (`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`, Tasks 5-6), matching Phase 5's own precedent of adding tables task-by-task rather than all upfront.

Every projection stores provider, external ID, source URL, observed/updated times, permission/freshness state and raw-content hash. Unique keys are workspace/provider/account/external-ID scoped. Raw provider payload retention is minimized. People link to Phase 2 entities; ambiguous identities remain unresolved. Metric snapshots store definition/version, population, window, numerator, denominator and coverage.
