import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import type { Incident, IncidentListResponse, IncidentSeverity } from './types'

const SEVERITIES: ReadonlyArray<IncidentSeverity> = ['low', 'medium', 'high', 'critical']

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  if (error.code === 'OFFLINE') return 'You are offline, so this request was not sent.'
  if (error.code === 'NETWORK_ERROR') return 'Could not reach the server. Nothing was sent.'
  if (error.code === 'IDEMPOTENCY_CONFLICT') return 'A different request was already recorded under this request key. Reload and retry.'
  if (error.code === 'CSRF_TOKEN_REQUIRED' || error.code === 'CSRF_TOKEN_INVALID') return 'Your session\'s security token is missing or stale. Reload the page and try again.'
  if (error.code === 'CHANGE_NOT_FOUND') {
    const detail = error.current as { change_ids?: string[] } | undefined
    return `One or more change IDs do not exist in this workspace: ${detail?.change_ids?.join(', ') ?? 'unknown'}.`
  }
  if (error.code === 'INCIDENT_NOT_FOUND') return 'This incident no longer exists in this workspace.'
  if (error.code === 'INCIDENT_ALREADY_RESOLVED') return 'This incident was already resolved -- reload to see its current state.'
  if (error.code === 'RESOLVED_AT_BEFORE_DETECTED_AT') return 'The resolved time cannot be before the detected time.'
  if (error.status === 401) return 'Your session is no longer valid. Sign in again.'
  if (error.status === 403) return 'You are not permitted to manage incidents in this workspace.'
  return error.message
}

function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'unresolved'
}

function IncidentRow({ incident, onChanged }: { incident: Incident; onChanged: () => void }) {
  const resolveMutation = useMutation({
    mutationFn: () =>
      apiRequest<Incident>(`/api/v1/engineering/incidents/${incident.id}/resolve`, {
        method: 'POST',
        body: { resolved_at: new Date().toISOString() },
      }),
    onSuccess: onChanged,
    // An INCIDENT_ALREADY_RESOLVED conflict means this row's status is
    // already stale (someone else resolved it first) -- without a refetch
    // here, the "Resolve" button stays enabled against that stale state, so
    // a retry click just 409s again in a loop. Matches DecisionsPanel.tsx's
    // own identical fix, and TaskWorkspace.tsx's/RiskWorkspace.tsx's
    // onError-refetch precedent for VERSION_CONFLICT.
    onError: (error) => { if (error instanceof ApiError && error.code === 'INCIDENT_ALREADY_RESOLVED') onChanged() },
  })

  return (
    <li>
      <div>
        <strong>{incident.title}</strong>
        <small>{incident.severity} · detected {new Date(incident.detected_at).toLocaleString()} · {incident.status} ({timestamp(incident.resolved_at)})</small>
      </div>
      {incident.description ? <p>{incident.description}</p> : null}
      {incident.change_ids.length ? (
        <p>Linked changes: {incident.change_ids.join(', ')}</p>
      ) : null}
      {incident.status === 'open' ? (
        <div className="work-actions">
          <button type="button" disabled={resolveMutation.isPending} onClick={() => resolveMutation.mutate()}>Resolve now</button>
        </div>
      ) : null}
      {resolveMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(resolveMutation.error)}</div> : null}
    </li>
  )
}

export default function IncidentsPanel() {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<'' | 'open' | 'resolved'>('open')
  const [title, setTitle] = useState('')
  const [severity, setSeverity] = useState<IncidentSeverity>('medium')

  const query = useQuery({
    queryKey: ['engineering', 'incidents', statusFilter],
    queryFn: () => apiRequest<IncidentListResponse>(`/api/v1/engineering/incidents${statusFilter ? `?status=${statusFilter}` : ''}`),
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: () =>
      apiRequest<Incident>('/api/v1/engineering/incidents', {
        method: 'POST',
        body: { title, severity, detected_at: new Date().toISOString() },
      }),
    onSuccess: () => {
      setTitle('')
      refresh()
    },
  })

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'incidents'] })
  }

  const incidents = query.data?.incidents ?? []

  return (
    <section className="work-panel" aria-labelledby="engineering-incidents-title">
      <h2 id="engineering-incidents-title">Incidents</h2>
      <p>Workspace-authored incidents, correlated to changes where known. There is no automatic incident-management connector in this activation -- every incident here was captured by hand.</p>

      <form onSubmit={(event) => { event.preventDefault(); createMutation.mutate() }}>
        <label>Title
          <input aria-label="Incident title" value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>Severity
          <select aria-label="Severity" value={severity} onChange={(e) => setSeverity(e.target.value as IncidentSeverity)}>
            {SEVERITIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <div className="work-actions">
          <button type="submit" disabled={createMutation.isPending || !title.trim()}>Capture incident</button>
        </div>
      </form>
      {createMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(createMutation.error)}</div> : null}

      <div role="radiogroup" aria-label="Filter by status" className="work-actions">
        {(['', 'open', 'resolved'] as const).map((value) => (
          <button
            key={value || 'all'}
            type="button"
            aria-pressed={statusFilter === value}
            onClick={() => setStatusFilter(value)}
          >
            {value || 'all'}
          </button>
        ))}
      </div>

      {query.isLoading ? <p role="status">Loading incidents…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(query.error)}</div> : null}
      {query.data && incidents.length === 0 ? <p className="empty-state">No incidents match this filter.</p> : null}

      <ul className="work-list">
        {incidents.map((incident) => <IncidentRow key={incident.id} incident={incident} onChanged={refresh} />)}
      </ul>
    </section>
  )
}
