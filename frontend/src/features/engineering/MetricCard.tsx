import type { MetricSnapshot } from './types'
import { METRIC_DEFINITIONS, METRIC_LABELS } from './metricDefinitions'

function coverageClass(status: MetricSnapshot['coverage_status']): string {
  if (status === 'insufficient_coverage') return 'inline-status error-panel'
  if (status === 'partial') return 'inline-status degraded-panel'
  return 'inline-status'
}

function formatValue(metric: MetricSnapshot): string {
  if (metric.value === null) return 'not yet available'
  if (metric.metric_key === 'change_failure_rate') return `${(metric.value * 100).toFixed(1)}%`
  if (metric.metric_key === 'delivery_frequency') return `${metric.value.toFixed(2)} / day`
  if (metric.metric_key === 'blocked_work' || metric.metric_key === 'work_ageing') return `${metric.value.toFixed(0)} items`
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
