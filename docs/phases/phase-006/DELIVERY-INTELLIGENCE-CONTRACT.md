---
id: PHASE-006-DELIVERY-INTELLIGENCE
title: Delivery Intelligence Contract
status: Approved for Implementation
version: 0.2.0
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
