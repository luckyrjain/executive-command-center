import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import type { Decision, DecisionListResponse } from './types'

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  if (error.code === 'OFFLINE') return 'You are offline, so this request was not sent.'
  if (error.code === 'NETWORK_ERROR') return 'Could not reach the server. Nothing was sent.'
  if (error.code === 'IDEMPOTENCY_CONFLICT') return 'A different request was already recorded under this request key. Reload and retry.'
  if (error.code === 'CHANGE_NOT_FOUND') {
    const detail = error.current as { change_ids?: string[] } | undefined
    return `One or more change IDs do not exist in this workspace: ${detail?.change_ids?.join(', ') ?? 'unknown'}.`
  }
  if (error.code === 'DECISION_NOT_FOUND') return 'This decision no longer exists in this workspace.'
  if (error.code === 'DECISION_NOT_PROPOSED') return 'This decision has already been decided -- reload to see its current state.'
  if (error.status === 401) return 'Your session is no longer valid. Sign in again.'
  if (error.status === 403) return 'You are not permitted to manage decisions in this workspace.'
  return error.message
}

function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'not yet decided'
}

function DecisionRow({ decision, onChanged }: { decision: Decision; onChanged: () => void }) {
  const [rationale, setRationale] = useState(decision.rationale ?? '')

  const decideMutation = useMutation({
    mutationFn: () =>
      apiRequest<Decision>(`/api/v1/engineering/decisions/${decision.id}/decide`, {
        method: 'POST',
        body: { decided_at: new Date().toISOString(), rationale: rationale.trim() || null },
      }),
    onSuccess: onChanged,
  })

  return (
    <li>
      <div>
        <strong>{decision.title}</strong>
        <small>{decision.status} · {timestamp(decision.decided_at)}</small>
      </div>
      {decision.description ? <p>{decision.description}</p> : null}
      {decision.rationale ? <p>Rationale: {decision.rationale}</p> : null}
      {decision.change_ids.length ? (
        <p>Linked changes: {decision.change_ids.join(', ')}</p>
      ) : null}
      {decision.status === 'proposed' ? (
        <>
          <label>Rationale (optional)
            <input aria-label={`Rationale for ${decision.title}`} value={rationale} onChange={(e) => setRationale(e.target.value)} />
          </label>
          <div className="work-actions">
            <button type="button" disabled={decideMutation.isPending} onClick={() => decideMutation.mutate()}>Record decision</button>
          </div>
        </>
      ) : null}
      {decideMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(decideMutation.error)}</div> : null}
    </li>
  )
}

export default function DecisionsPanel() {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<'' | 'proposed' | 'decided' | 'superseded'>('proposed')
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')

  const query = useQuery({
    queryKey: ['engineering', 'decisions', statusFilter],
    queryFn: () => apiRequest<DecisionListResponse>(`/api/v1/engineering/decisions${statusFilter ? `?status=${statusFilter}` : ''}`),
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: () =>
      apiRequest<Decision>('/api/v1/engineering/decisions', {
        method: 'POST',
        body: { title, description: description.trim() || null },
      }),
    onSuccess: () => {
      setTitle('')
      setDescription('')
      refresh()
    },
  })

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['engineering', 'decisions'] })
  }

  const decisions = query.data?.decisions ?? []

  return (
    <section className="work-panel" aria-labelledby="engineering-decisions-title">
      <h2 id="engineering-decisions-title">Decisions</h2>
      <p>Engineering decisions proposed and decided by this workspace, correlated to changes where known.</p>

      <form onSubmit={(event) => { event.preventDefault(); createMutation.mutate() }}>
        <label>Title
          <input aria-label="Decision title" value={title} onChange={(e) => setTitle(e.target.value)} />
        </label>
        <label>Description (optional)
          <input aria-label="Decision description" value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
        <div className="work-actions">
          <button type="submit" disabled={createMutation.isPending || !title.trim()}>Propose decision</button>
        </div>
      </form>
      {createMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(createMutation.error)}</div> : null}

      <div role="radiogroup" aria-label="Filter by status" className="work-actions">
        {(['', 'proposed', 'decided', 'superseded'] as const).map((value) => (
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

      {query.isLoading ? <p role="status">Loading decisions…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(query.error)}</div> : null}
      {query.data && decisions.length === 0 ? <p className="empty-state">No decisions match this filter.</p> : null}

      <ul className="work-list">
        {decisions.map((decision) => <DecisionRow key={decision.id} decision={decision} onChanged={refresh} />)}
      </ul>
    </section>
  )
}
