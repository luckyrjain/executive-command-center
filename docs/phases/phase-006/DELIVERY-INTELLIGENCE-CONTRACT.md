---
id: PHASE-006-DELIVERY-INTELLIGENCE
title: Delivery Intelligence Contract
status: Approved for Implementation
version: 0.4.0
owner: Lucky Jain
---

# Delivery Intelligence Contract

Contracts moved from Draft after resolving `docs/phases/PHASE-REVIEW.md:137`'s "metric definitions and source-coverage thresholds" approval-gate item in `docs/superpowers/specs/2026-07-27-phase-6-engineering-workspace-design.md` (Decision 3). This document is the normative statement of that resolution.

Approved metrics include delivery frequency, lead time, change failure rate, restoration time, work ageing, blocked work and review latency only when definitions, source coverage and limitations are displayed. Metrics aggregate at system/team/workstream level. No composite engineer score, ranking, activity leaderboard or inference of performance from commits/messages.

Risk signals cite the underlying changes, incidents or work items and state confidence. Partial coverage cannot be presented as complete. Metric-definition changes create a new version and do not rewrite historical snapshots.

## Metric definitions (resolved)

| Metric | Population | Window | Numerator / denominator |
|---|---|---|---|
| Delivery frequency | Deployments to a tracked service | Rolling 7/30/90-day, selectable | Count of successful deployments / window length |
| Lead time for changes | Changes merged in window with a linked deployment | Rolling 30-day | Median (commit-to-merge) + (merge-to-deploy) duration |
| Change failure rate | Deployments in window | Rolling 30-day | Deployments followed by a linked incident within 24h / total deployments |
| Time to restore | Incidents resolved in window | Rolling 30-day | Median (detected-at to resolved-at) duration |
| Work ageing | Open work items | Point-in-time snapshot | Distribution of (now - created-at) for still-open items, bucketed |
| Blocked work | Open work items flagged blocked | Point-in-time snapshot | Count and median age of items in a blocked state |
| Review latency | Merged changes with at least one review | Rolling 30-day | Median (review-requested-at to first-review-at) duration |

Every snapshot stores `metric_definition_id`, `version`, `window`, `numerator`, `denominator`, `population` and `coverage` (`DATA-MODEL.md`). `aggregation_scope` is a closed enum of exactly `system`, `team` and `workstream` -- there is no `person`-scoped variant of any metric above, no leaderboard, and no route or field anywhere in `API-SCHEMAS.md` that returns a ranked list of people. This is a schema-level constraint, not a UI-layer omission.

## Source-coverage thresholds (resolved)

A metric is presented as `complete` coverage only when at least 95% of its population's source records have synced within their connector's freshness SLO (backfill fully complete, incremental lag under the phase's five-minute NFR) at snapshot time. Below that, it is presented as `partial`, with the exact coverage percentage and the specific gap displayed alongside the number -- never silently included in an aggregate as if complete. Below 50% coverage, a metric is shown as `insufficient_coverage` with no numeric value, rather than a number likely to mislead.

## Accepted limitation (Task 1)

No metric computation, snapshot table, or coverage-threshold enforcement exists yet -- this document's numbers are the approved target for Task 5 ("Delivery and reliability metrics", `docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`). Task 1 delivers only the connector platform these metrics will eventually read from.

## Task 5 status

`backend/ecc/domains/engineering/metrics.py` implements the snapshot-computation engine and coverage-threshold policy above against real data, via `GET /engineering/metrics`. **Only three of the seven metrics are genuinely computable in this activation: `work_ageing`, `blocked_work`, `review_latency`.** The other four all require `deployments` and/or `incidents` data -- `lead_time_for_changes`' own population above is "changes merged... *with a linked deployment*," not merely merged changes -- and neither table exists yet (`deployments` has no task assigned to it at all; `incidents` is explicit Task 6 scope, per `DATA-MODEL.md`'s own "Tasks 5-6" note). These four always report `coverage_status = 'insufficient_coverage'` and `value = None` -- the contract's own "no metric is computed or displayed below 50% coverage at all" rule applied to its most extreme case (0%, because no source exists at all), not a new exception to it. See `metrics.py`'s own module docstring for the full design, including its three disclosed accepted limitations below.

## Task 6 status

`incidents` (migration `0049_phase6_decisions_incidents.py`) makes `time_to_restore` the fourth genuinely computable metric: `_compute_time_to_restore` reads resolved incidents directly, exactly this contract's own population/window/numerator definition above (median detected-at-to-resolved-at duration over the rolling 30-day window). `delivery_frequency`, `lead_time_for_changes` and `change_failure_rate` remain `insufficient_coverage` -- each still needs `deployments`, which has no task assigned to it at all, and `change_failure_rate`'s own population ("deployments followed by a linked incident within 24h") needs both tables together, not `incidents` alone. See the new accepted-limitation section immediately below for `time_to_restore`'s own coverage semantics, deliberately different from the other three real metrics'.

## Accepted limitation (Task 6): `time_to_restore`'s coverage is unconditionally `complete`, not sync-cursor-derived

`incidents` is a workspace-authored table, not a connector-synced projection (no adapter writes it, no `sync_cursors` row exists for it) -- there is no "freshness lag" for the Task 5 coverage mechanism above to measure. Whatever has been captured in `incidents` *is* the complete, current state of that table by construction; `population = 0` (no incidents resolved in the window) is reported as a legitimate "nothing to restore from" result, not conflated with the "no source exists at all" meaning `insufficient_coverage` carries for the three metrics still blocked on `deployments`. See `metrics.py`'s own module docstring.

## Accepted limitation (Task 5): coverage is inferred from sync-cursor recency, not a dedicated backfill-completion ledger

`_coverage_for` treats a resource type as "fresh" for a connector account when `sync_cursors.updated_at` (upserted on every sync call that returns a cursor, `succeeded` or `partial` alike) falls within the phase's own five-minute incremental-lag NFR, and averages that across every active connector account of the metric's relevant provider(s). This does not distinguish "backfill fully complete" from "a bounded-page sync recently made some progress" -- a connector that just connected a repository with thousands of PRs could show `complete` coverage after one recent but far-from-exhaustive sync call. A dedicated backfill-completion ledger is a real gap this task does not close.

## Accepted limitation (Task 5): `GET /engineering/metrics` computes and stores on every call, not a pure read

This phase has no periodic sync/computation scheduler yet -- `GET /engineering/metrics` is itself the computation trigger, writing seven new immutable `delivery_metric_snapshots` rows on every call, mirroring `POST /connectors/{id}/sync`'s own identical "manual trigger only" reality since Task 1. A deliberate, disclosed departure from pure REST `GET` semantics; see `connector_accounts.py`'s `get_metrics_endpoint` docstring.

## Accepted limitation (Task 5): heuristic "open"/"blocked" work-item classification

Jira's workflow statuses are fully customizable per project; there is no per-workspace status-category configuration to consult yet. `work_ageing`'s population treats any work item whose `status` case-insensitively matches `done`/`closed`/`resolved` as closed, everything else as open; `blocked_work` treats any open item whose `status` case-insensitively contains `blocked` as blocked. A workspace using different status vocabulary (e.g. a custom "On Hold" column) will undercount -- a real, disclosed limitation, not a silently wrong number.
