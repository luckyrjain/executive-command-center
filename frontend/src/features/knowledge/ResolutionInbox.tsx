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

export default function ResolutionInbox() {
  const queryClient = useQueryClient()
  const [reasons, setReasons] = useState<Record<string, string>>({})

  const query = useQuery({
    queryKey: ['knowledge', 'resolution', 'candidates', 'open'],
    queryFn: listOpenCandidates,
    retry: 1,
  })

  const decisionMutation = useMutation({
    mutationFn: ({ candidate, decision, reason }: { candidate: ResolutionCandidate; decision: Decision; reason: string }) =>
      apiRequest<ResolutionCandidate>(`/api/v1/knowledge/resolution/candidates/${candidate.id}/${decision}`, {
        method: 'POST',
        body: { reason },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'resolution', 'candidates'] })
    },
  })

  function reasonFor(candidateId: string): string {
    return reasons[candidateId] ?? ''
  }

  function setReasonFor(candidateId: string, value: string) {
    setReasons((current) => ({ ...current, [candidateId]: value }))
  }

  return (
    <section className="work-panel knowledge-resolution-inbox" aria-labelledby="resolution-inbox-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">KNOWLEDGE</p>
          <h2 id="resolution-inbox-title">Resolution review</h2>
          <p>Confirm or reject candidate identity matches before they can be merged.</p>
        </div>
      </div>

      {decisionMutation.error ? (
        <div role="alert" className="inline-status error-panel">{decisionMutation.error.message}</div>
      ) : null}
      {query.isLoading ? <p role="status">Loading resolution candidates…</p> : null}
      {query.isError ? <div role="alert">{query.error.message}</div> : null}
      {query.data?.items.length ? (
        <ul className="work-list">
          {query.data.items.map((candidate) => (
            <li key={candidate.id}>
              <div>
                <strong>{candidate.left_entity_id}</strong> ↔ <strong>{candidate.right_entity_id}</strong>
                <small> · score {candidate.score.toFixed(2)} ({factorSummary(candidate)})</small>
              </div>
              <label>
                {`Reason for ${candidate.id}`}
                <input
                  aria-label={`Reason for ${candidate.id}`}
                  value={reasonFor(candidate.id)}
                  onChange={(event) => setReasonFor(candidate.id, event.target.value)}
                />
              </label>
              <div className="work-actions" role="group" aria-label={`Actions for candidate ${candidate.id}`}>
                <button
                  type="button"
                  disabled={decisionMutation.isPending || !reasonFor(candidate.id).trim()}
                  onClick={() =>
                    decisionMutation.mutate({ candidate, decision: 'confirm', reason: reasonFor(candidate.id).trim() })
                  }
                >
                  Confirm match
                </button>
                <button
                  type="button"
                  disabled={decisionMutation.isPending || !reasonFor(candidate.id).trim()}
                  onClick={() =>
                    decisionMutation.mutate({ candidate, decision: 'reject', reason: reasonFor(candidate.id).trim() })
                  }
                >
                  Reject
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : !query.isLoading ? (
        <p className="empty-state">No resolution candidates awaiting review.</p>
      ) : null}
    </section>
  )
}
