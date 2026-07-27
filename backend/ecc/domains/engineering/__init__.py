"""Phase 6 Engineering Workspace domain package (Task 1: connector
framework and source projections; Task 2: GitHub read sync; Task 3:
GitLab read sync; Task 4: Jira work-item sync).

`docs/superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md`
/ `docs/phases/phase-006/DATA-MODEL.md`. This package owns
`connector_accounts`/`sync_cursors`/`sync_runs` (`connector_accounts.py`)
-- the three tables migration `0044_phase6_connector_platform.py` creates
-- plus the connector-adapter contract (`connectors.py`), credential
encryption (`crypto.py`), one deliberately-fake sandbox adapter
(`sandbox_adapter.py`) exercising that contract end to end without a real
network call, and three real, non-sandbox adapters: `github_adapter.py`
and `gitlab_adapter.py` (repository backfill/incremental sync, populating
the shared `repositories` table from migration
`0045_phase6_repositories.py`), and `jira_adapter.py` (work-item backfill/
incremental sync, populating `engineering_work_items` from migration
`0047_phase6_work_items.py`) -- all three against the identical
`ConnectorAdapter` contract.

Delivery/reliability metric computation, decision/incident linking and
write actions do not exist in this package yet -- each is a later task per
`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`.
Changes/reviews/deployments and webhook ingestion's receiving endpoint are
also deferred for all three real adapters -- see each adapter's own
module docstring.
"""
