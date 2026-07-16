---
id: PHASE-006-DATA-MODEL
title: Phase 6 Engineering Workspace Data Model
status: Draft
version: 0.1.0
owner: Lucky Jain
---

# Phase 6 Data Model

Core projections: `connector_accounts`, `sync_cursors`, `sync_runs`, `repositories`, `engineering_work_items`, `changes`, `reviews`, `deployments`, `incidents`, `engineering_decisions`, `service_links`, `delivery_metric_snapshots` and `source_tombstones`.

Every projection stores provider, external ID, source URL, observed/updated times, permission/freshness state and raw-content hash. Unique keys are workspace/provider/account/external-ID scoped. Raw provider payload retention is minimized. People link to Phase 2 entities; ambiguous identities remain unresolved. Metric snapshots store definition/version, population, window, numerator, denominator and coverage.
