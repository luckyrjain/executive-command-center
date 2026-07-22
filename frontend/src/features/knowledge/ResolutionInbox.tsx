import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { ResolutionCandidate, ResolutionCandidateList } from './types'

type Decision = 'confirm' | 'reject'

function listOpenCandidates(): Promise<ResolutionCandidateList> {
  return apiRequest('/api/v1/knowledge/resolution/candidates?status=open&limit=50')
}

function factorSummary(candidate: ResolutionCandidate): string {
  return Object.entries(candidate.factors)
    .map(([factor, value]) => `${factor}: ${value.toFixed(2)}`)
    .join(', ')
}

type ResolutionCandidateRowProps = { candidate: ResolutionCandidate }

// Each row owns its own mutations (mirrors MergeReview.tsx's
// MergeCandidateRow) rather than sharing one decisionMutation/deferMutation
// across the whole list -- a shared mutation means one row's in-flight
// request disables every other row's buttons, and one row's error renders
// as an unattributed alert a reader has no way to tie back to which
// candidate it came from.
function ResolutionCandidateRow({ candidate }: ResolutionCandidateRowProps) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')

  const decisionMutation = useMutation({
    mutationFn: (decision: Decision) =>
      apiRequest<ResolutionCandidate>(`/api/v1/knowledge/resolution/candidates/${candidate.id}/${decision}`, {
        method: 'POST',
        body: { reason: reason.trim() },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'resolution', 'candidates'] })
    },
  })

  // UX-STATES.md names confirm/reject/defer as the three primary review
  // actions -- defer postpones the decision (24h snooze) without confirming
  // or rejecting, matching the identical pattern Phase 1's attention items
  // already use for their own defer action.
  const deferMutation = useMutation({
    mutationFn: () =>
      apiRequest<ResolutionCandidate>(`/api/v1/knowledge/resolution/candidates/${candidate.id}/defer`, {
        method: 'POST',
        body: { deferred_until: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString() },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'resolution', 'candidates'] })
    },
  })

  return (
    <li>
      <div>
        <strong>{candidate.left_entity_id}</strong> ↔ <strong>{candidate.right_entity_id}</strong>
        <small> · score {candidate.score.toFixed(2)} ({factorSummary(candidate)})</small>
      </div>
      {decisionMutation.error ? (
        <div role="alert" className="inline-status error-panel">{decisionMutation.error.message}</div>
      ) : null}
      {deferMutation.error ? (
        <div role="alert" className="inline-status error-panel">{deferMutation.error.message}</div>
      ) : null}
      <label>
        {`Reason for ${candidate.id}`}
        <input
          aria-label={`Reason for ${candidate.id}`}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <div className="work-actions" role="group" aria-label={`Actions for candidate ${candidate.id}`}>
        <button
          type="button"
          disabled={decisionMutation.isPending || !reason.trim()}
          onClick={() => decisionMutation.mutate('confirm')}
        >
          Confirm match
        </button>
        <button
          type="button"
          disabled={decisionMutation.isPending || !reason.trim()}
          onClick={() => decisionMutation.mutate('reject')}
        >
          Reject
        </button>
        <button
          type="button"
          disabled={deferMutation.isPending}
          onClick={() => deferMutation.mutate()}
        >
          Defer
        </button>
      </div>
    </li>
  )
}

export default function ResolutionInbox() {
  const query = useQuery({
    queryKey: ['knowledge', 'resolution', 'candidates', 'open'],
    queryFn: listOpenCandidates,
    retry: 1,
  })

  return (
    <section className="work-panel knowledge-resolution-inbox" aria-labelledby="resolution-inbox-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">KNOWLEDGE</p>
          <h2 id="resolution-inbox-title">Resolution review</h2>
          <p>Confirm or reject candidate identity matches before they can be merged.</p>
        </div>
      </div>

      {query.isLoading ? <p role="status">Loading resolution candidates…</p> : null}
      {query.isError ? <div role="alert">{query.error.message}</div> : null}
      {query.data?.items.length ? (
        <ul className="work-list">
          {query.data.items.map((candidate) => (
            <ResolutionCandidateRow key={candidate.id} candidate={candidate} />
          ))}
        </ul>
      ) : query.isSuccess ? (
        <p className="empty-state">No resolution candidates awaiting review.</p>
      ) : null}
    </section>
  )
}
