"""Phase 6 Engineering Workspace domain package (Task 1: connector
framework and source projections; Task 2: GitHub read sync; Task 3:
GitLab read sync).

`docs/superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md`
/ `docs/phases/phase-006/DATA-MODEL.md`. This package owns
`connector_accounts`/`sync_cursors`/`sync_runs` (`connector_accounts.py`)
-- the three tables migration `0044_phase6_connector_platform.py` creates
-- plus the connector-adapter contract (`connectors.py`), credential
encryption (`crypto.py`), one deliberately-fake sandbox adapter
(`sandbox_adapter.py`) exercising that contract end to end without a real
network call, and two real, non-sandbox adapters populating the shared
`repositories` table (migration `0045_phase6_repositories.py`):
`github_adapter.py` and `gitlab_adapter.py`, both repository backfill/
incremental sync only, against the identical `ConnectorAdapter` contract.

No Jira adapter, delivery/reliability metric computation, decision/
incident linking or write action exists in this package yet -- each is a
later task per `docs/superpowers/plans/2026-07-27-phase-6-engineering-
workspace.md`. Work items/changes/reviews/deployments and webhook
ingestion's receiving endpoint are also deferred for both real adapters
-- see each adapter's own module docstring.
"""
