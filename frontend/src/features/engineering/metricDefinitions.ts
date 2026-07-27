import type { MetricKey } from './types'

// Verbatim from `docs/superpowers/specs/2026-07-27-phase-6-engineering-
// workspace-design.md`'s Decision 3 table -- the definition text every
// metric card must show alongside its number (design doc: "charts always
// show definition, window, coverage and evidence drill-down"), never
// person-scoped (design doc: "no individual-engineer aggregation").
export const METRIC_DEFINITIONS: Record<MetricKey, string> = {
  delivery_frequency: 'Count of successful deployments to a tracked service, divided by the window length. System/service level only.',
  lead_time_for_changes: 'Median (commit-to-merge) + (merge-to-deploy) duration, for changes merged in the window with a linked deployment.',
  change_failure_rate: 'Deployments in the window followed by a linked incident within 24 hours, divided by total deployments in the window.',
  time_to_restore: 'Median (detected-at to resolved-at) duration, for incidents resolved in the window.',
  work_ageing: 'Distribution of (now - created-at) for still-open work items, bucketed. A point-in-time snapshot, not a rolling window.',
  blocked_work: 'Count and median age of open work items flagged blocked. A point-in-time snapshot, not a rolling window.',
  review_latency: 'Median (review-requested-at to first-review-at) duration, for merged changes with at least one review in the window.',
}

export const METRIC_LABELS: Record<MetricKey, string> = {
  delivery_frequency: 'Delivery frequency',
  lead_time_for_changes: 'Lead time for changes',
  change_failure_rate: 'Change failure rate',
  time_to_restore: 'Time to restore',
  work_ageing: 'Work ageing',
  blocked_work: 'Blocked work',
  review_latency: 'Review latency',
}
