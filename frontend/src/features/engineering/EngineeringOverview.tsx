import { useQuery } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type {
  ConnectorAccountListResponse,
  DecisionListResponse,
  EngineeringView,
  IncidentListResponse,
  MetricsListResponse,
} from './types'
import { METRIC_LABELS } from './metricDefinitions'
import { formatValue } from './MetricCard'

/**
 * A single-glance summary of this workspace's engineering surfaces --
 * connector health, open incidents, proposed decisions, and the two
 * headline metrics (`delivery_frequency`, `time_to_restore`) with their
 * own coverage state, never a bare number without it. Every count here
 * links to the tab that has the real detail; nothing here is itself an
 * editable surface.
 */
export default function EngineeringOverview({ onNavigate }: { onNavigate: (view: EngineeringView) => void }) {
  const connectors = useQuery({
    queryKey: ['engineering', 'connectors'],
    queryFn: () => apiRequest<ConnectorAccountListResponse>('/api/v1/engineering/connectors'),
    retry: 1,
  })
  const incidents = useQuery({
    queryKey: ['engineering', 'incidents', 'open'],
    queryFn: () => apiRequest<IncidentListResponse>('/api/v1/engineering/incidents?status=open'),
    retry: 1,
  })
  const decisions = useQuery({
    queryKey: ['engineering', 'decisions', 'proposed'],
    queryFn: () => apiRequest<DecisionListResponse>('/api/v1/engineering/decisions?status=proposed'),
    retry: 1,
  })
  const metrics = useQuery({
    queryKey: ['engineering', 'metrics'],
    // `csrf: true` -- see `DeliveryPanel.tsx`'s identical comment.
    queryFn: () => apiRequest<MetricsListResponse>('/api/v1/engineering/metrics', { csrf: true }),
    retry: 1,
  })

  const connectorList = connectors.data?.connectors ?? []
  // `disable_connector_endpoint` never deletes a connector_accounts row, it
  // only flips status to 'disconnected' -- such rows persist in `GET
  // /connectors` forever, so they must be excluded from both counts below:
  // a deliberately-disconnected account is not "connected" (it provides no
  // data) and does not "need attention" (nothing is wrong with it, its
  // owner already acted).
  const connectedConnectors = connectorList.filter((c) => c.status !== 'disconnected')
  const degradedConnectors = connectedConnectors.filter((c) => c.status !== 'active' && c.status !== 'pending')
  const headline = (metrics.data?.metrics ?? []).filter(
    (metric) => metric.metric_key === 'delivery_frequency' || metric.metric_key === 'time_to_restore',
  )

  return (
    <section className="work-panel" aria-labelledby="engineering-overview-title">
      <h2 id="engineering-overview-title">Engineering overview</h2>
      <p>A single glance across every connected source, open incident, proposed decision, and this workspace's two headline delivery/reliability metrics.</p>

      <ul className="work-list">
        <li>
          <strong>Connectors</strong>
          {connectors.isLoading ? <p role="status">Loading…</p> : null}
          {connectors.isError ? <div role="alert" className="inline-status error-panel">{connectors.error.message}</div> : null}
          {connectors.data ? (
            <>
              <p>{connectedConnectors.length} connected. {degradedConnectors.length > 0 ? `${degradedConnectors.length} need attention.` : 'All healthy.'}</p>
              <div className="work-actions">
                <button type="button" onClick={() => onNavigate('connector-health')}>View connector health</button>
              </div>
            </>
          ) : null}
        </li>

        <li>
          <strong>Open incidents</strong>
          {incidents.isLoading ? <p role="status">Loading…</p> : null}
          {incidents.isError ? <div role="alert" className="inline-status error-panel">{incidents.error.message}</div> : null}
          {incidents.data ? (
            <>
              <p>{incidents.data.incidents.length} open.</p>
              <div className="work-actions">
                <button type="button" onClick={() => onNavigate('incidents')}>View incidents</button>
              </div>
            </>
          ) : null}
        </li>

        <li>
          <strong>Proposed decisions</strong>
          {decisions.isLoading ? <p role="status">Loading…</p> : null}
          {decisions.isError ? <div role="alert" className="inline-status error-panel">{decisions.error.message}</div> : null}
          {decisions.data ? (
            <>
              <p>{decisions.data.decisions.length} awaiting a decision.</p>
              <div className="work-actions">
                <button type="button" onClick={() => onNavigate('decisions')}>View decisions</button>
              </div>
            </>
          ) : null}
        </li>

        <li>
          <strong>Headline metrics</strong>
          {metrics.isLoading ? <p role="status">Loading…</p> : null}
          {metrics.isError ? <div role="alert" className="inline-status error-panel">{metrics.error.message}</div> : null}
          {metrics.data ? (
            <>
              {headline.map((metric) => (
                <p key={metric.id}>
                  {METRIC_LABELS[metric.metric_key]}: {formatValue(metric)}
                  {' '}(coverage: {metric.coverage_status.replaceAll('_', ' ')})
                </p>
              ))}
              <div className="work-actions">
                <button type="button" onClick={() => onNavigate('delivery')}>View delivery</button>
                <button type="button" onClick={() => onNavigate('reliability')}>View reliability</button>
              </div>
            </>
          ) : null}
        </li>
      </ul>
    </section>
  )
}
