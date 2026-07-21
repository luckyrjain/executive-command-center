import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import {
  RELATIONSHIP_TYPES,
  type Claim,
  type ClaimList,
  type KnowledgeEntity,
  type Relationship,
  type RelationshipList,
  type RelationshipType,
  type TimelineList,
} from './types'

type EntityDetailProps = {
  entityId: string
  onClose: () => void
}

type RelationshipDraft = { relationshipType: RelationshipType; toEntityId: string }

const emptyRelationshipDraft: RelationshipDraft = { relationshipType: 'RELATES_TO', toEntityId: '' }

function formatClaimValue(claim: Claim): string {
  try {
    return JSON.stringify(claim.value)
  } catch {
    return String(claim.value)
  }
}

export default function EntityDetail({ entityId, onClose }: EntityDetailProps) {
  const queryClient = useQueryClient()
  const [relationshipDraft, setRelationshipDraft] = useState<RelationshipDraft>(emptyRelationshipDraft)

  const entityQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId],
    queryFn: () => apiRequest<KnowledgeEntity>(`/api/v1/knowledge/entities/${entityId}`),
  })
  const claimsQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId, 'claims'],
    queryFn: () => apiRequest<ClaimList>(`/api/v1/knowledge/entities/${entityId}/claims`),
  })
  const relationshipsQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId, 'relationships'],
    queryFn: () => apiRequest<RelationshipList>(`/api/v1/knowledge/entities/${entityId}/relationships`),
  })
  const timelineQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId, 'timeline'],
    queryFn: () => apiRequest<TimelineList>(`/api/v1/knowledge/entities/${entityId}/timeline`),
  })

  const addRelationshipMutation = useMutation({
    mutationFn: (draft: RelationshipDraft) =>
      apiRequest<Relationship>(`/api/v1/knowledge/entities/${entityId}/relationships`, {
        method: 'POST',
        body: { relationship_type: draft.relationshipType, to_entity_id: draft.toEntityId.trim() },
      }),
    onSuccess: () => {
      setRelationshipDraft(emptyRelationshipDraft)
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'relationships'] })
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'timeline'] })
    },
  })

  function submitRelationship(event: FormEvent) {
    event.preventDefault()
    if (relationshipDraft.toEntityId.trim()) addRelationshipMutation.mutate(relationshipDraft)
  }

  const entity = entityQuery.data

  return (
    <section className="knowledge-entity-detail" aria-labelledby="entity-detail-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">KNOWLEDGE</p>
          <h2 id="entity-detail-title">{entity ? entity.canonical_name : 'Entity detail'}</h2>
          {entity ? <p>{entity.kind} · {entity.status} · version {entity.version}</p> : null}
        </div>
        <button type="button" onClick={onClose}>Close detail</button>
      </div>

      {entityQuery.isLoading ? <p role="status">Loading entity…</p> : null}
      {entityQuery.isError ? <div role="alert">{entityQuery.error.message}</div> : null}
      {entity?.summary ? <p>{entity.summary}</p> : null}

      <section aria-labelledby={`claims-heading-${entityId}`}>
        <h3 id={`claims-heading-${entityId}`}>Claims</h3>
        {claimsQuery.isLoading ? <p role="status">Loading claims…</p> : null}
        {claimsQuery.data?.items.length ? (
          <ul>
            {claimsQuery.data.items.map((claim) => (
              <li key={claim.id}>
                <strong>{claim.predicate}</strong>: {formatClaimValue(claim)}
                {claim.superseded_by ? ' (superseded)' : ''}
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-state">No claims recorded for this entity.</p>
        )}
      </section>

      <section aria-labelledby={`relationships-heading-${entityId}`}>
        <h3 id={`relationships-heading-${entityId}`}>Relationships</h3>
        {relationshipsQuery.isLoading ? <p role="status">Loading relationships…</p> : null}
        {relationshipsQuery.data?.items.length ? (
          <ul>
            {relationshipsQuery.data.items.map((relationship) => (
              <li key={relationship.id}>
                {relationship.from_entity_id === entityId
                  ? `${relationship.relationship_type} → ${relationship.to_entity_id}`
                  : `${relationship.from_entity_id} → ${relationship.relationship_type} (this entity)`}
                {relationship.status !== 'active' ? ` · ${relationship.status}` : ''}
              </li>
            ))}
          </ul>
        ) : (
          <p className="empty-state">No relationships recorded for this entity.</p>
        )}
        {addRelationshipMutation.error ? (
          <div role="alert" className="inline-status error-panel">{addRelationshipMutation.error.message}</div>
        ) : null}
        <form onSubmit={submitRelationship}>
          <h4>Add relationship</h4>
          <label>
            Relationship type
            <select
              aria-label="Relationship type"
              value={relationshipDraft.relationshipType}
              onChange={(event) =>
                setRelationshipDraft({ ...relationshipDraft, relationshipType: event.target.value as RelationshipType })
              }
            >
              {RELATIONSHIP_TYPES.map((type) => (
                <option key={type} value={type}>{type}</option>
              ))}
            </select>
          </label>
          <label>
            Related entity ID
            <input
              aria-label="Related entity ID"
              value={relationshipDraft.toEntityId}
              onChange={(event) => setRelationshipDraft({ ...relationshipDraft, toEntityId: event.target.value })}
            />
          </label>
          <button type="submit" disabled={addRelationshipMutation.isPending}>Add relationship</button>
        </form>
      </section>

      <section aria-labelledby={`timeline-heading-${entityId}`}>
        <h3 id={`timeline-heading-${entityId}`}>Timeline</h3>
        {timelineQuery.isLoading ? <p role="status">Loading timeline…</p> : null}
        {timelineQuery.data?.items.length ? (
          <ol>
            {timelineQuery.data.items.map((entry) => (
              <li key={entry.id}>
                <time>{entry.effective_at}</time> — {entry.summary}
              </li>
            ))}
          </ol>
        ) : (
          <p className="empty-state">No timeline entries yet.</p>
        )}
      </section>
    </section>
  )
}
