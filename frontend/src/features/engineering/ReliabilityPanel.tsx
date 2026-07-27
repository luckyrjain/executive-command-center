import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import MetricCard from './MetricCard'
import type { MetricsListResponse } from './types'

/**
 * Reliability metrics -- everything whose population is incidents rather
 * than changes or work items. `time_to_restore` is currently the only
 * genuinely computable metric here (Phase 6 Task 6); `delivery_frequency`/
 * `lead_time_for_changes`/`change_failure_rate` all remain blocked on a
 * `deployments` table this plan never assigned to any task, and live on
 * `DeliveryPanel.tsx` instead since their population is changes, not
 * incidents -- see `DeliveryPanel.tsx`'s own comment for the split's full
 * reasoning.
 */
export default function ReliabilityPanel() {
  const query = useQuery({
    queryKey: ['engineering', 'metrics'],
    // `csrf: true` -- see `DeliveryPanel.tsx`'s identical comment.
    queryFn: () => apiRequest<MetricsListResponse>('/api/v1/engineering/metrics', { csrf: true }),
    retry: 1,
  })

  const metrics = (query.data?.metrics ?? []).filter((metric) => metric.metric_key === 'time_to_restore')

  return (
    <section className="work-panel" aria-labelledby="engineering-reliability-title">
      <h2 id="engineering-reliability-title">Reliability</h2>
      <p>How quickly this workspace recovers from incidents. Delivery frequency, lead time for changes, and change failure rate all still require a deployments source this phase has not added yet -- see Delivery for their current, disclosed insufficient-coverage state.</p>

      {query.isLoading ? <p role="status">Loading reliability metrics…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}

      <ul className="work-list">
        {metrics.map((metric) => <MetricCard key={metric.id} metric={metric} />)}
      </ul>
    </section>
  )
}
