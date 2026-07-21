import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { EntityOperation, KnowledgeEntity, ResolutionCandidate, ResolutionCandidateList } from './types'

function listConfirmedCandidates(): Promise<ResolutionCandidateList> {
  return apiRequest('/api/v1/knowledge/resolution/candidates?status=confirmed&limit=50')
}

function fetchEntity(entityId: string): Promise<KnowledgeEntity> {
  return apiRequest(`/api/v1/knowledge/entities/${entityId}`)
}

type MergeCandidateRowProps = {
  candidate: ResolutionCandidate
  onMerged: (operation: EntityOperation) => void
}

function MergeCandidateRow({ candidate, onMerged }: MergeCandidateRowProps) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')
  const left = useQuery({
    queryKey: ['knowledge', 'entity', candidate.left_entity_id],
    queryFn: () => fetchEntity(candidate.left_entity_id),
  })
  const right = useQuery({
    queryKey: ['knowledge', 'entity', candidate.right_entity_id],
    queryFn: () => fetchEntity(candidate.right_entity_id),
  })

  const mergeMutation = useMutation({
    mutationFn: (targetId: string) => {
      if (!left.data || !right.data) throw new Error('Entity details not loaded yet')
      const target = targetId === left.data.id ? left.data : right.data
      const source = targetId === left.data.id ? right.data : left.data
      return apiRequest<EntityOperation>('/api/v1/knowledge/entities/merge', {
        method: 'POST',
        body: {
          candidate_id: candidate.id,
          target_entity_id: target.id,
          expected_target_version: target.version,
          expected_source_version: source.version,
          reason: reason.trim(),
        },
      })
    },
    onSuccess: (operation) => {
      setReason('')
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entities'] })
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'resolution', 'candidates'] })
      onMerged(operation)
    },
  })

  if (left.isLoading || right.isLoading) return <li>Loading entity details…</li>
  if (!left.data || !right.data) return null

  return (
    <li>
      <div>
        <strong>{left.data.canonical_name}</strong> ↔ <strong>{right.data.canonical_name}</strong>
        <small> · confirmed match, score {candidate.score.toFixed(2)}</small>
      </div>
      {mergeMutation.error ? (
        <div role="alert" className="inline-status error-panel">{mergeMutation.error.message}</div>
      ) : null}
      <label>
        {`Merge reason for ${candidate.id}`}
        <input
          aria-label={`Merge reason for ${candidate.id}`}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <div className="work-actions" role="group" aria-label={`Merge actions for ${candidate.id}`}>
        <button
          type="button"
          disabled={mergeMutation.isPending || !reason.trim()}
          onClick={() => mergeMutation.mutate(left.data!.id)}
        >
          Merge into {left.data.canonical_name}
        </button>
        <button
          type="button"
          disabled={mergeMutation.isPending || !reason.trim()}
          onClick={() => mergeMutation.mutate(right.data!.id)}
        >
          Merge into {right.data.canonical_name}
        </button>
      </div>
    </li>
  )
}

type CompletedMergeRowProps = { operation: EntityOperation; onReversed: (operationId: string) => void }

function CompletedMergeRow({ operation, onReversed }: CompletedMergeRowProps) {
  const queryClient = useQueryClient()
  const [reason, setReason] = useState('')

  const reverseMutation = useMutation({
    mutationFn: () =>
      apiRequest<EntityOperation>(`/api/v1/knowledge/entity-operations/${operation.id}/reverse`, {
        method: 'POST',
        body: { reason: reason.trim() },
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entities'] })
      onReversed(operation.id)
    },
  })

  if (operation.status === 'reversed') {
    return (
      <li>
        Merge {operation.source_entity_id} → {operation.target_entity_id} (reversed)
      </li>
    )
  }

  return (
    <li>
      <div>Merged {operation.source_entity_id} into {operation.target_entity_id}</div>
      {reverseMutation.error ? (
        <div role="alert" className="inline-status error-panel">{reverseMutation.error.message}</div>
      ) : null}
      <label>
        {`Reversal reason for ${operation.id}`}
        <input
          aria-label={`Reversal reason for ${operation.id}`}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <button type="button" disabled={reverseMutation.isPending || !reason.trim()} onClick={() => reverseMutation.mutate()}>
        Reverse merge
      </button>
    </li>
  )
}

export default function MergeReview() {
  const [completedMerges, setCompletedMerges] = useState<EntityOperation[]>([])

  const query = useQuery({
    queryKey: ['knowledge', 'resolution', 'candidates', 'confirmed'],
    queryFn: listConfirmedCandidates,
    retry: 1,
  })

  function recordMerge(operation: EntityOperation) {
    setCompletedMerges((current) => [operation, ...current])
  }

  function markReversed(operationId: string) {
    setCompletedMerges((current) =>
      current.map((operation) => (operation.id === operationId ? { ...operation, status: 'reversed' } : operation)),
    )
  }

  return (
    <section className="work-panel knowledge-merge-review" aria-labelledby="merge-review-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">KNOWLEDGE</p>
          <h2 id="merge-review-title">Merge review</h2>
          <p>Merge confirmed identity matches, or reverse a merge made in error.</p>
        </div>
      </div>

      {query.isLoading ? <p role="status">Loading confirmed candidates…</p> : null}
      {query.isError ? <div role="alert">{query.error.message}</div> : null}
      {query.data?.items.length ? (
        <ul className="work-list">
          {query.data.items.map((candidate) => (
            <MergeCandidateRow key={candidate.id} candidate={candidate} onMerged={recordMerge} />
          ))}
        </ul>
      ) : !query.isLoading ? (
        <p className="empty-state">No confirmed candidates awaiting merge.</p>
      ) : null}

      {completedMerges.length ? (
        <section aria-labelledby="completed-merges-heading">
          <h3 id="completed-merges-heading">Merges from this session</h3>
          <ul className="work-list">
            {completedMerges.map((operation) => (
              <CompletedMergeRow key={operation.id} operation={operation} onReversed={markReversed} />
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  )
}
