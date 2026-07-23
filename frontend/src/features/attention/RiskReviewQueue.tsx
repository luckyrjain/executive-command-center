import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'

export type ReviewOutcome = 'no_change' | 'escalated' | 'mitigated' | 'closed'
export type ReviewUrgency = 'overdue' | 'due_soon' | 'scheduled' | 'unscheduled'

export type ReviewQueueItem = {
  risk_id: string
  description: string
  status: string
  review_at: string | null
  urgency: ReviewUrgency
}

type ReviewQueueList = { items: ReviewQueueItem[] }

export type Draft = {
  outcome: ReviewOutcome
  notes: string
  nextReviewAt: string
}

const emptyDraft: Draft = { outcome: 'no_change', notes: '', nextReviewAt: '' }
const OUTCOMES: ReviewOutcome[] = ['no_change', 'escalated', 'mitigated', 'closed']

/** Urgency uses neutral, non-alarming language per UX-STATES.md's ethics
 * rules ("no shame language") -- the copy names the timing fact, not a
 * judgment about the item or its owner. */
const URGENCY_LABEL: Record<ReviewUrgency, string> = {
  overdue: 'Review overdue',
  due_soon: 'Review due soon',
  scheduled: 'Review scheduled',
  unscheduled: 'No review scheduled',
}

function errorMessage(error: Error): string {
  if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
    return 'This risk changed since the queue loaded. Refresh and try again.'
  }
  return error.message
}

export default function RiskReviewQueue() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['risk-review-queue'],
    queryFn: () => apiRequest<ReviewQueueList>('/api/v1/risks/review-queue'),
    retry: 1,
  })
  const [reviewing, setReviewing] = useState<{ item: ReviewQueueItem; draft: Draft; version: number } | null>(null)

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['risk-review-queue'] })
    void queryClient.invalidateQueries({ queryKey: ['risks'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard', 'today'] })
  }

  const reviewMutation = useMutation({
    mutationFn: ({ item, draft, version }: { item: ReviewQueueItem; draft: Draft; version: number }) =>
      apiRequest(`/api/v1/risks/${item.risk_id}/review`, {
        method: 'POST',
        body: {
          expected_version: version,
          outcome: draft.outcome,
          notes: draft.notes.trim() || null,
          next_review_at: draft.nextReviewAt ? new Date(draft.nextReviewAt).toISOString() : null,
        },
      }),
    onSuccess: () => { setReviewing(null); refresh() },
  })
  const pending = reviewMutation.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!reviewing) return
    reviewMutation.mutate(reviewing)
  }

  const items = query.data?.items ?? []

  return (
    <section className="work-panel" aria-labelledby="risk-review-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">GOVERNANCE</p>
          <h1 id="risk-review-title">Risk review queue</h1>
          <p>Risks ordered by review cadence urgency.</p>
        </div>
      </div>

      {query.isLoading ? <p role="status">Loading review queue…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {reviewMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(reviewMutation.error)}</div> : null}
      {query.data && items.length === 0 ? <p className="empty-state">No risks are due for review.</p> : null}

      <ol className="work-list">
        {items.map((item) => (
          <li key={item.risk_id}>
            <div>
              <strong>{item.description}</strong>
              <small>{URGENCY_LABEL[item.urgency]}{item.review_at ? ` · ${new Date(item.review_at).toLocaleDateString()}` : ''}</small>
            </div>
            <div className="work-actions" role="group" aria-label={`Actions for ${item.description}`}>
              <button
                type="button"
                disabled={pending}
                aria-label={`Record review for ${item.description}`}
                onClick={() => setReviewing({ item, draft: emptyDraft, version: 1 })}
              >
                Record review
              </button>
            </div>
          </li>
        ))}
      </ol>

      {reviewing ? (
        <form onSubmit={submit}>
          <h2>Record review: {reviewing.item.description}</h2>
          <label>Expected version<input aria-label="Expected version" type="number" min={1} value={reviewing.version} onChange={(e) => setReviewing({ ...reviewing, version: Number(e.target.value) })} /></label>
          <label>Outcome
            <select aria-label="Review outcome" value={reviewing.draft.outcome} onChange={(e) => setReviewing({ ...reviewing, draft: { ...reviewing.draft, outcome: e.target.value as ReviewOutcome } })}>
              {OUTCOMES.map((outcome) => <option key={outcome} value={outcome}>{outcome.replaceAll('_', ' ')}</option>)}
            </select>
          </label>
          <label>Notes<textarea aria-label="Review notes" value={reviewing.draft.notes} onChange={(e) => setReviewing({ ...reviewing, draft: { ...reviewing.draft, notes: e.target.value } })} /></label>
          <label>Next review at<input aria-label="Next review at" type="datetime-local" value={reviewing.draft.nextReviewAt} onChange={(e) => setReviewing({ ...reviewing, draft: { ...reviewing.draft, nextReviewAt: e.target.value } })} /></label>
          <button type="submit" disabled={pending}>Save review</button>
          <button type="button" disabled={pending} onClick={() => setReviewing(null)}>Discard</button>
        </form>
      ) : null}
    </section>
  )
}
