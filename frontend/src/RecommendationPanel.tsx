import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

type RecommendationStatus =
  | 'proposed'
  | 'pending_confirmation'
  | 'accepted'
  | 'rejected'
  | 'expired'
  | 'superseded'
  | 'executed'
  | 'failed'

export type Recommendation = {
  id: string
  recommendation_type: string
  target_type: string
  target_id?: string | null
  proposed_action: Record<string, unknown>
  expected_version?: number | null
  rationale: string
  confidence: number
  status: RecommendationStatus
  evidence_ids: string[]
  expires_at?: string | null
  execution_result?: Record<string, unknown> | null
  source: string
  pinned: boolean
  deferred_until?: string | null
  version: number
}

type RecommendationList = {
  items: Recommendation[]
  next_cursor?: string | null
}

type ErrorEnvelope = {
  error?: { code?: string; message?: string }
}

type ActionName = 'publish' | 'confirm' | 'reject' | 'defer' | 'pin'

type ActionRequest = {
  item: Recommendation
  action: ActionName
}

function csrfToken(): string {
  return (
    document.cookie
      .split('; ')
      .find((value) => value.startsWith('ecc_csrf='))
      ?.split('=')[1] ?? ''
  )
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers)
  headers.set('Accept', 'application/json')
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers,
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorEnvelope
    const error = new Error(payload.error?.message ?? 'Recommendation action failed')
    error.name = payload.error?.code ?? `HTTP_${response.status}`
    throw error
  }
  return response.json()
}

function fetchRecommendations(): Promise<RecommendationList> {
  const statuses = new URLSearchParams()
  statuses.append('status', 'proposed')
  statuses.append('status', 'pending_confirmation')
  statuses.append('status', 'executed')
  statuses.append('status', 'failed')
  statuses.set('limit', '20')
  return request(`/api/v1/recommendations?${statuses}`)
}

export function actionPayload(item: Recommendation, action: ActionName): Record<string, unknown> {
  if (action === 'confirm') {
    return {
      expected_version: item.version,
      target_expected_version: item.expected_version,
    }
  }
  if (action === 'defer') {
    return {
      expected_version: item.version,
      defer_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
    }
  }
  if (action === 'pin') {
    return { expected_version: item.version, pinned: !item.pinned }
  }
  if (action === 'reject') {
    return { expected_version: item.version, reason: 'Rejected from executive review' }
  }
  return { expected_version: item.version }
}

function mutateRecommendation({ item, action }: ActionRequest): Promise<Recommendation> {
  return request(`/api/v1/recommendations/${item.id}/${action}`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Idempotency-Key': crypto.randomUUID(),
      'X-CSRF-Token': csrfToken(),
    },
    body: JSON.stringify(actionPayload(item, action)),
  })
}

export function actionSummary(action: Record<string, unknown>): string {
  const operation = typeof action.operation === 'string' ? action.operation : 'update'
  const fields = Object.keys(action).filter((key) => key !== 'operation')
  return fields.length ? `${operation.replaceAll('_', ' ')} · ${fields.join(', ')}` : operation.replaceAll('_', ' ')
}

export function confidenceLabel(confidence: number): string {
  return `${Math.round(confidence * 100)}% confidence`
}

export function recommendationErrorMessage(error: Error): string {
  if (error.name === 'VERSION_CONFLICT' || error.name === 'TARGET_VERSION_CONFLICT') {
    return 'This recommendation changed while you were reviewing it. The latest version has been reloaded.'
  }
  return error.message
}

export default function RecommendationPanel() {
  const queryClient = useQueryClient()
  const recommendations = useQuery({
    queryKey: ['recommendations', 'review'],
    queryFn: fetchRecommendations,
    refetchInterval: 60_000,
    retry: 1,
  })
  const mutation = useMutation({
    mutationFn: mutateRecommendation,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['recommendations', 'review'] }),
    onError: (error) => {
      if (error.name === 'VERSION_CONFLICT' || error.name === 'TARGET_VERSION_CONFLICT') {
        void queryClient.invalidateQueries({ queryKey: ['recommendations', 'review'] })
      }
    },
  })

  const items = recommendations.data?.items ?? []

  return (
    <section className="recommendation-panel" aria-labelledby="recommendations-title">
      <div className="recommendation-heading">
        <div>
          <p className="eyebrow">HUMAN CONFIRMATION REQUIRED</p>
          <h2 id="recommendations-title">Recommendations</h2>
          <p>Review rationale and evidence metadata before any authoritative change executes.</p>
        </div>
        <button type="button" onClick={() => recommendations.refetch()} disabled={recommendations.isFetching}>
          {recommendations.isFetching ? 'Refreshing…' : 'Refresh recommendations'}
        </button>
      </div>

      {recommendations.isLoading ? <div className="inline-status" role="status">Loading recommendations…</div> : null}
      {recommendations.isError ? <div className="inline-status error-panel" role="alert">{recommendations.error.message}</div> : null}
      {mutation.isError ? (
        <div className="inline-status error-panel" role="alert">
          {recommendationErrorMessage(mutation.error)}
        </div>
      ) : null}

      {items.length ? (
        <ol className="recommendation-list">
          {items.map((item) => {
            const busy = mutation.isPending && mutation.variables?.item.id === item.id
            const canPublish = item.status === 'proposed'
            const canDecide = item.status === 'pending_confirmation'
            return (
              <li key={item.id} className={item.pinned ? 'is-pinned' : undefined}>
                <div className="recommendation-copy">
                  <div className="recommendation-meta">
                    <span>{item.status.replaceAll('_', ' ')}</span>
                    <span>{item.source}</span>
                    <span>{confidenceLabel(item.confidence)}</span>
                  </div>
                  <h3>{item.recommendation_type.replaceAll('_', ' ')}</h3>
                  <p>{item.rationale}</p>
                  <dl>
                    <div><dt>Target</dt><dd>{item.target_type}</dd></div>
                    <div><dt>Action</dt><dd>{actionSummary(item.proposed_action)}</dd></div>
                    <div><dt>Evidence</dt><dd>{item.evidence_ids.length} reference{item.evidence_ids.length === 1 ? '' : 's'}</dd></div>
                  </dl>
                  {item.execution_result ? <p className="execution-result">Execution recorded.</p> : null}
                </div>
                <div className="recommendation-actions" aria-label={`Actions for ${item.recommendation_type}`}>
                  {canPublish ? (
                    <button type="button" onClick={() => mutation.mutate({ item, action: 'publish' })} disabled={busy}>Publish for confirmation</button>
                  ) : null}
                  {canDecide ? (
                    <>
                      <button className="primary-action" type="button" onClick={() => mutation.mutate({ item, action: 'confirm' })} disabled={busy || item.expected_version == null}>Confirm and execute</button>
                      <button type="button" onClick={() => mutation.mutate({ item, action: 'reject' })} disabled={busy}>Reject</button>
                      <button type="button" onClick={() => mutation.mutate({ item, action: 'defer' })} disabled={busy}>Defer 24h</button>
                    </>
                  ) : null}
                  {(canPublish || canDecide) ? (
                    <button type="button" onClick={() => mutation.mutate({ item, action: 'pin' })} disabled={busy}>{item.pinned ? 'Unpin' : 'Pin'}</button>
                  ) : null}
                  {busy ? <span role="status">Applying…</span> : null}
                </div>
              </li>
            )
          })}
        </ol>
      ) : recommendations.isSuccess ? (
        <p className="empty-state recommendation-empty">No recommendations currently need review.</p>
      ) : null}
    </section>
  )
}
