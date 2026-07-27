import type { MetricSnapshot } from './types'
import { METRIC_DEFINITIONS, METRIC_LABELS } from './metricDefinitions'

function coverageClass(status: MetricSnapshot['coverage_status']): string {
  if (status === 'insufficient_coverage') return 'inline-status error-panel'
  if (status === 'partial') return 'inline-status degraded-panel'
  return 'inline-status'
}

// `value`'s own unit is metric-specific, read directly off `metrics.py`'s
// own computation (never guessed): `_compute_time_to_restore`/`_compute_
// review_latency` both store a raw `.total_seconds()` median with no
// day conversion (confirmed against `_compute_time_to_restore`'s own
// test asserting `value == timedelta(hours=4).total_seconds()`), unlike
// `_compute_work_ageing`, which explicitly divides by 86400 into a
// median AGE IN DAYS -- despite `work_ageing` and `blocked_work` sharing
// this component's "open work item health" framing, their `value`
// fields are not the same kind of number: `_compute_blocked_work`'s
// `value` is `float(blocked_count)`, a plain count, while `work_ageing`'s
// is a duration. `lead_time_for_changes` is not yet computed by any
// function in this codebase (always `insufficient_coverage`), but its
// design-doc definition ("commit-to-merge + merge-to-deploy duration")
// is the same shape as `time_to_restore`/`review_latency`, so it is
// grouped with them here rather than left to default to the wrong unit
// once a future task makes it real.
const SECONDS_METRICS = new Set(['time_to_restore', 'review_latency', 'lead_time_for_changes'])

export function formatValue(metric: MetricSnapshot): string {
  if (metric.value === null) return 'not yet available'
  if (metric.metric_key === 'change_failure_rate') return `${(metric.value * 100).toFixed(1)}%`
  if (metric.metric_key === 'delivery_frequency') return `${metric.value.toFixed(2)} / day`
  if (metric.metric_key === 'blocked_work') return `${metric.value.toFixed(0)} items`
  if (metric.metric_key === 'work_ageing') return `${metric.value.toFixed(1)} days`
  if (SECONDS_METRICS.has(metric.metric_key)) return `${(metric.value / 86400).toFixed(1)} days`
  return `${metric.value.toFixed(1)} days`
}

/**
 * One metric snapshot, rendered with its definition, window, coverage and
 * evidence drill-down always visible together -- design doc Decision 3:
 * "charts always show definition, window, coverage and evidence
 * drill-down. Never display person rankings or shame language." There is
 * no `person_id`-scoped variant of any metric in this schema at all
 * (a hard constraint, not a UI-layer choice), so this component has no
 * per-person breakdown to accidentally expose.
 */
export default function MetricCard({ metric }: { metric: MetricSnapshot }) {
  const insufficient = metric.coverage_status === 'insufficient_coverage'

  return (
    <li aria-labelledby={`engineering-metric-${metric.metric_key}-title`}>
      <div>
        <strong id={`engineering-metric-${metric.metric_key}-title`}>{METRIC_LABELS[metric.metric_key]}</strong>
        <small>{metric.window_label}</small>
      </div>
      <p>{METRIC_DEFINITIONS[metric.metric_key]}</p>

      <p aria-label={`${METRIC_LABELS[metric.metric_key]} value`}>
        <strong>{formatValue(metric)}</strong>
      </p>

      <div role="status" className={coverageClass(metric.coverage_status)}>
        Coverage: {metric.coverage_status.replaceAll('_', ' ')} ({metric.coverage_percentage.toFixed(0)}%)
        {metric.coverage_gap_description ? ` -- ${metric.coverage_gap_description}` : ''}
      </div>
      {insufficient ? (
        <p className="empty-state">Fewer than 50% of this metric's population has synced -- no number is shown to avoid a misleading value.</p>
      ) : null}

      <details>
        <summary>Evidence</summary>
        <dl>
          <dt>Population</dt>
          <dd>{metric.population}</dd>
          <dt>Numerator</dt>
          <dd>{metric.numerator ?? 'n/a'}</dd>
          <dt>Denominator</dt>
          <dd>{metric.denominator ?? 'n/a'}</dd>
          {metric.details ? (
            <>
              <dt>Details</dt>
              <dd><pre>{JSON.stringify(metric.details, null, 2)}</pre></dd>
            </>
          ) : null}
        </dl>
        <p>Computed {new Date(metric.computed_at).toLocaleString()}.</p>
      </details>
    </li>
  )
}
