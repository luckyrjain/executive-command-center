import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import MetricCard from './MetricCard'
import type { MetricKey, MetricsListResponse } from './types'

// Delivery/flow metrics -- everything about how change and work move
// through the system, as distinct from Reliability's incident-recovery
// metric (`ReliabilityPanel.tsx`). Neither `docs/phases/phase-006/UX-
// STATES.md` nor the design doc's Decision 3 table assigns each of the
// seven metrics to a named surface -- this split is this task's own
// judgment call, disclosed here rather than left implicit: `time_to_
// restore` is the one metric whose population is incidents, not changes
// or work items, so it is the only one that moved to Reliability.
const DELIVERY_METRICS: ReadonlyArray<MetricKey> = [
  'delivery_frequency',
  'lead_time_for_changes',
  'change_failure_rate',
  'review_latency',
  'work_ageing',
  'blocked_work',
]

export default function DeliveryPanel() {
  const query = useQuery({
    queryKey: ['engineering', 'metrics'],
    // `csrf: true` -- this GET has a real server-side side effect (writes
    // fresh metric snapshot rows on every call) and requires `CsrfDep` on
    // the backend despite being a GET; see `ApiRequestOptions.csrf`'s own
    // comment in `api/types.ts`.
    queryFn: () => apiRequest<MetricsListResponse>('/api/v1/engineering/metrics', { csrf: true }),
    retry: 1,
  })

  const metrics = (query.data?.metrics ?? []).filter((metric) => DELIVERY_METRICS.includes(metric.metric_key))

  return (
    <section className="work-panel" aria-labelledby="engineering-delivery-title">
      <h2 id="engineering-delivery-title">Delivery</h2>
      <p>How change and work move through this workspace's connected repositories -- delivery frequency, lead time, change failure rate, review latency, and open-work-item health. Every number shows its own coverage; a metric below 50% source coverage is never shown as if it were complete.</p>

      {query.isLoading ? <p role="status">Loading delivery metrics…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && metrics.length === 0 ? <p className="empty-state">No delivery metrics yet.</p> : null}

      <ul className="work-list">
        {metrics.map((metric) => <MetricCard key={metric.id} metric={metric} />)}
      </ul>
    </section>
  )
}
