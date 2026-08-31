import { useMemo, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import {
  RELATIONSHIP_TYPES,
  type Claim,
  type ClaimList,
  type EntityAliasList,
  type EvidenceListResponse,
  type EvidenceStatus,
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

type RelationshipDraft = { relationshipType: RelationshipType; toEntityId: string; evidenceId: string }

const emptyRelationshipDraft: RelationshipDraft = {
  relationshipType: 'RELATES_TO',
  toEntityId: '',
  evidenceId: '',
}

type ClaimDraft = { predicate: string; valueText: string; sourceId: string; confidence: string }

const emptyClaimDraft: ClaimDraft = { predicate: '', valueText: '', sourceId: '', confidence: '1' }

type MemberDraft = { personEntityId: string; evidenceId: string }

const emptyMemberDraft: MemberDraft = { personEntityId: '', evidenceId: '' }

function formatClaimValue(claim: Claim): string {
  try {
    return JSON.stringify(claim.value)
  } catch {
    return String(claim.value)
  }
}

function claimValueText(claim: Claim): string {
  const value = claim.value as Record<string, unknown>
  if (typeof value.text === 'string') return value.text
  return formatClaimValue(claim)
}

function evidenceLabel(status: EvidenceStatus | undefined): string {
  if (status === undefined) return 'checking…'
  if (status === 'available') return 'available'
  if (status === 'deleted') return 'deleted'
  if (status === 'permission_denied') return 'permission denied'
  return 'missing'
}

export default function EntityDetail({ entityId, onClose }: EntityDetailProps) {
  const queryClient = useQueryClient()
  const [relationshipDraft, setRelationshipDraft] = useState<RelationshipDraft>(emptyRelationshipDraft)
  const [claimDraft, setClaimDraft] = useState<ClaimDraft>(emptyClaimDraft)
  const [correctingClaimId, setCorrectingClaimId] = useState<string | null>(null)
  const [correctionDraft, setCorrectionDraft] = useState<ClaimDraft>(emptyClaimDraft)
  const [memberDraft, setMemberDraft] = useState<MemberDraft>(emptyMemberDraft)

  const entityQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId],
    queryFn: () => apiRequest<KnowledgeEntity>(`/api/v1/knowledge/entities/${entityId}`),
  })
  const aliasesQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId, 'aliases'],
    queryFn: () => apiRequest<EntityAliasList>(`/api/v1/knowledge/entities/${entityId}/aliases`),
  })
  const claimsQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId, 'claims'],
    queryFn: () => apiRequest<ClaimList>(`/api/v1/knowledge/entities/${entityId}/claims`),
  })
  const relationshipsQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId, 'relationships'],
    queryFn: () => apiRequest<RelationshipList>(`/api/v1/knowledge/entities/${entityId}/relationships`),
  })
  // A team's roster -- `relationship_type=MEMBER_OF&direction=incoming`
  // composed against the same generic relationships endpoint (a team-
  // concept gap analysis found `MEMBER_OF` was a real, working
  // relationship type with no dedicated query or UI to see who's on a
  // team). Only fetched for `kind: 'team'` entities.
  const membersQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId, 'members'],
    queryFn: () =>
      apiRequest<RelationshipList>(
        `/api/v1/knowledge/entities/${entityId}/relationships?relationship_type=MEMBER_OF&direction=incoming`,
      ),
    enabled: entityQuery.data?.kind === 'team',
  })
  const timelineQuery = useQuery({
    queryKey: ['knowledge', 'entity', entityId, 'timeline'],
    queryFn: () => apiRequest<TimelineList>(`/api/v1/knowledge/entities/${entityId}/timeline`),
  })

  // Provenance rendering: every claim and relationship cites a source_id/
  // evidence_id, but a citation is only trustworthy if that evidence is
  // still available (not deleted -- see evidence.py's delete cascade) --
  // resolved in one batched request rather than per-claim, since GET
  // /api/v1/evidence already accepts repeated ?id= params for exactly this.
  const sourceIds = useMemo(() => {
    const ids = new Set<string>()
    for (const claim of claimsQuery.data?.items ?? []) ids.add(claim.source_id)
    for (const relationship of relationshipsQuery.data?.items ?? []) ids.add(relationship.evidence_id)
    return Array.from(ids).sort()
  }, [claimsQuery.data, relationshipsQuery.data])
  const evidenceQuery = useQuery({
    queryKey: ['knowledge', 'evidence', sourceIds],
    queryFn: () =>
      apiRequest<EvidenceListResponse>(
        `/api/v1/evidence?${sourceIds.map((id) => `id=${encodeURIComponent(id)}`).join('&')}`,
      ),
    enabled: sourceIds.length > 0,
  })
  const evidenceStatusById = useMemo(() => {
    const map = new Map<string, EvidenceStatus>()
    for (const item of evidenceQuery.data?.items ?? []) map.set(item.id, item.status)
    return map
  }, [evidenceQuery.data])

  const addRelationshipMutation = useMutation({
    mutationFn: (draft: RelationshipDraft) =>
      apiRequest<Relationship>(`/api/v1/knowledge/entities/${entityId}/relationships`, {
        method: 'POST',
        body: {
          relationship_type: draft.relationshipType,
          to_entity_id: draft.toEntityId.trim(),
          evidence_id: draft.evidenceId.trim(),
        },
      }),
    onSuccess: () => {
      setRelationshipDraft(emptyRelationshipDraft)
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'relationships'] })
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'timeline'] })
    },
  })

  // Adding a member creates the relationship from the *person's* own
  // entity ID (`MEMBER_OF`'s real direction: person -> team), not this
  // team's own `entityId` -- the generic `POST /entities/{id}/relationships`
  // always treats its path entity as the source, so this is a distinct
  // mutation from `addRelationshipMutation` above rather than a preset
  // draft on the same one.
  const addMemberMutation = useMutation({
    mutationFn: (draft: MemberDraft) =>
      apiRequest<Relationship>(`/api/v1/knowledge/entities/${draft.personEntityId.trim()}/relationships`, {
        method: 'POST',
        body: {
          relationship_type: 'MEMBER_OF',
          to_entity_id: entityId,
          evidence_id: draft.evidenceId.trim(),
        },
      }),
    onSuccess: () => {
      setMemberDraft(emptyMemberDraft)
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'members'] })
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'timeline'] })
    },
  })

  const recordClaimMutation = useMutation({
    mutationFn: (draft: ClaimDraft) =>
      apiRequest<Claim>(`/api/v1/knowledge/entities/${entityId}/claims`, {
        method: 'POST',
        body: {
          predicate: draft.predicate.trim(),
          value: { text: draft.valueText.trim() },
          source_id: draft.sourceId.trim(),
          confidence: Number(draft.confidence),
        },
      }),
    onSuccess: () => {
      setClaimDraft(emptyClaimDraft)
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'claims'] })
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'timeline'] })
    },
  })

  const correctClaimMutation = useMutation({
    mutationFn: ({ claimId, draft }: { claimId: string; draft: ClaimDraft }) =>
      apiRequest<Claim>(`/api/v1/knowledge/entities/${entityId}/claims/${claimId}/supersede`, {
        method: 'POST',
        body: {
          predicate: draft.predicate.trim(),
          value: { text: draft.valueText.trim() },
          source_id: draft.sourceId.trim(),
          confidence: Number(draft.confidence),
        },
      }),
    onSuccess: () => {
      setCorrectingClaimId(null)
      setCorrectionDraft(emptyClaimDraft)
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'claims'] })
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entity', entityId, 'timeline'] })
    },
  })

  function submitRelationship(event: FormEvent) {
    event.preventDefault()
    if (relationshipDraft.toEntityId.trim() && relationshipDraft.evidenceId.trim()) {
      addRelationshipMutation.mutate(relationshipDraft)
    }
  }

  function submitMember(event: FormEvent) {
    event.preventDefault()
    if (memberDraft.personEntityId.trim() && memberDraft.evidenceId.trim()) {
      addMemberMutation.mutate(memberDraft)
    }
  }

  function submitClaim(event: FormEvent) {
    event.preventDefault()
    if (claimDraft.predicate.trim() && claimDraft.valueText.trim() && claimDraft.sourceId.trim()) {
      recordClaimMutation.mutate(claimDraft)
    }
  }

  function startCorrection(claim: Claim) {
    setCorrectingClaimId(claim.id)
    setCorrectionDraft({
      predicate: claim.predicate,
      valueText: claimValueText(claim),
      sourceId: claim.source_id,
      confidence: String(claim.confidence),
    })
  }

  function submitCorrection(event: FormEvent) {
    event.preventDefault()
    if (
      correctingClaimId &&
      correctionDraft.predicate.trim() &&
      correctionDraft.valueText.trim() &&
      correctionDraft.sourceId.trim()
    ) {
      correctClaimMutation.mutate({ claimId: correctingClaimId, draft: correctionDraft })
    }
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

      <section aria-labelledby={`aliases-heading-${entityId}`}>
        <h3 id={`aliases-heading-${entityId}`}>Aliases</h3>
        {aliasesQuery.isLoading ? <p role="status">Loading aliases…</p> : null}
        {aliasesQuery.isError ? (
          <div role="alert">{aliasesQuery.error.message}</div>
        ) : aliasesQuery.data?.items.length ? (
          <ul>
            {aliasesQuery.data.items.map((alias) => (
              <li key={alias.id}>
                {alias.normalized_value} <small>· {alias.alias_type}</small>
              </li>
            ))}
          </ul>
        ) : aliasesQuery.isSuccess ? (
          <p className="empty-state">No aliases recorded for this entity.</p>
        ) : null}
      </section>

      <section aria-labelledby={`claims-heading-${entityId}`}>
        <h3 id={`claims-heading-${entityId}`}>Claims</h3>
        {claimsQuery.isLoading ? <p role="status">Loading claims…</p> : null}
        {claimsQuery.isError ? (
          <div role="alert">{claimsQuery.error.message}</div>
        ) : claimsQuery.data?.items.length ? (
          <ul>
            {claimsQuery.data.items.map((claim) => (
              <li key={claim.id}>
                <strong>{claim.predicate}</strong>: {formatClaimValue(claim)}
                <small>
                  {' '}
                  · confidence {Math.round(claim.confidence * 100)}% · source {claim.source_id.slice(0, 8)}
                  {' ('}
                  {evidenceLabel(evidenceStatusById.get(claim.source_id))}
                  {')'}
                </small>
                {claim.superseded_by ? <small> · superseded</small> : null}
                {!claim.superseded_by ? (
                  correctingClaimId === claim.id ? (
                    <form className="field-form" onSubmit={submitCorrection}>
                      <h4>Correct claim</h4>
                      <label>
                        Predicate
                        <input
                          aria-label={`Correction predicate for ${claim.id}`}
                          value={correctionDraft.predicate}
                          onChange={(event) =>
                            setCorrectionDraft({ ...correctionDraft, predicate: event.target.value })
                          }
                        />
                      </label>
                      <label>
                        Value
                        <input
                          aria-label={`Correction value for ${claim.id}`}
                          value={correctionDraft.valueText}
                          onChange={(event) =>
                            setCorrectionDraft({ ...correctionDraft, valueText: event.target.value })
                          }
                        />
                      </label>
                      <label>
                        Source ID
                        <input
                          aria-label={`Correction source ID for ${claim.id}`}
                          value={correctionDraft.sourceId}
                          onChange={(event) =>
                            setCorrectionDraft({ ...correctionDraft, sourceId: event.target.value })
                          }
                        />
                      </label>
                      <label>
                        Confidence
                        <input
                          aria-label={`Correction confidence for ${claim.id}`}
                          type="number"
                          min={0}
                          max={1}
                          step={0.01}
                          value={correctionDraft.confidence}
                          onChange={(event) =>
                            setCorrectionDraft({ ...correctionDraft, confidence: event.target.value })
                          }
                        />
                      </label>
                      <button type="submit" disabled={correctClaimMutation.isPending}>Save correction</button>
                      <button type="button" onClick={() => setCorrectingClaimId(null)}>Cancel</button>
                    </form>
                  ) : (
                    <button type="button" onClick={() => startCorrection(claim)}>
                      Correct “{claim.predicate}”
                    </button>
                  )
                ) : null}
              </li>
            ))}
          </ul>
        ) : claimsQuery.isSuccess ? (
          <p className="empty-state">No claims recorded for this entity.</p>
        ) : null}
        {correctClaimMutation.error ? (
          <div role="alert" className="inline-status error-panel">{correctClaimMutation.error.message}</div>
        ) : null}
        {recordClaimMutation.error ? (
          <div role="alert" className="inline-status error-panel">{recordClaimMutation.error.message}</div>
        ) : null}
        <form className="field-form" onSubmit={submitClaim}>
          <h4>Record claim</h4>
          <label>
            Predicate
            <input
              aria-label="Claim predicate"
              value={claimDraft.predicate}
              onChange={(event) => setClaimDraft({ ...claimDraft, predicate: event.target.value })}
            />
          </label>
          <label>
            Value
            <input
              aria-label="Claim value"
              value={claimDraft.valueText}
              onChange={(event) => setClaimDraft({ ...claimDraft, valueText: event.target.value })}
            />
          </label>
          <label>
            Source ID
            <input
              aria-label="Claim source ID"
              value={claimDraft.sourceId}
              onChange={(event) => setClaimDraft({ ...claimDraft, sourceId: event.target.value })}
            />
          </label>
          <label>
            Confidence
            <input
              aria-label="Claim confidence"
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={claimDraft.confidence}
              onChange={(event) => setClaimDraft({ ...claimDraft, confidence: event.target.value })}
            />
          </label>
          <button type="submit" disabled={recordClaimMutation.isPending}>Record claim</button>
        </form>
      </section>

      <section aria-labelledby={`relationships-heading-${entityId}`}>
        <h3 id={`relationships-heading-${entityId}`}>Relationships</h3>
        {relationshipsQuery.isLoading ? <p role="status">Loading relationships…</p> : null}
        {relationshipsQuery.isError ? (
          <div role="alert">{relationshipsQuery.error.message}</div>
        ) : relationshipsQuery.data?.items.length ? (
          <ul>
            {relationshipsQuery.data.items.map((relationship) => (
              <li key={relationship.id}>
                {relationship.from_entity_id === entityId
                  ? `${relationship.relationship_type} → ${relationship.to_entity_name} (${relationship.to_entity_kind})`
                  : `${relationship.from_entity_name} (${relationship.from_entity_kind}) → ${relationship.relationship_type} (this entity)`}
                {relationship.status !== 'active' ? ` · ${relationship.status}` : ''}
                <small>
                  {' '}
                  · evidence {relationship.evidence_id.slice(0, 8)}
                  {' ('}
                  {evidenceLabel(evidenceStatusById.get(relationship.evidence_id))}
                  {')'}
                </small>
              </li>
            ))}
          </ul>
        ) : relationshipsQuery.isSuccess ? (
          <p className="empty-state">No relationships recorded for this entity.</p>
        ) : null}
        {addRelationshipMutation.error ? (
          <div role="alert" className="inline-status error-panel">{addRelationshipMutation.error.message}</div>
        ) : null}
        <form className="field-form" onSubmit={submitRelationship}>
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
          <label>
            Evidence ID
            <input
              aria-label="Evidence ID"
              value={relationshipDraft.evidenceId}
              onChange={(event) => setRelationshipDraft({ ...relationshipDraft, evidenceId: event.target.value })}
            />
          </label>
          <button type="submit" disabled={addRelationshipMutation.isPending}>Add relationship</button>
        </form>
      </section>

      {entity?.kind === 'team' ? (
        <section aria-labelledby={`members-heading-${entityId}`}>
          <h3 id={`members-heading-${entityId}`}>Members</h3>
          {membersQuery.isLoading ? <p role="status">Loading members…</p> : null}
          {membersQuery.isError ? (
            <div role="alert">{membersQuery.error.message}</div>
          ) : membersQuery.data?.items.length ? (
            <ul>
              {membersQuery.data.items.map((member) => (
                <li key={member.id}>
                  {member.from_entity_name} <small>· {member.from_entity_kind}</small>
                </li>
              ))}
            </ul>
          ) : membersQuery.isSuccess ? (
            <p className="empty-state">No members recorded for this team yet.</p>
          ) : null}
          {addMemberMutation.error ? (
            <div role="alert" className="inline-status error-panel">{addMemberMutation.error.message}</div>
          ) : null}
          <form className="field-form" onSubmit={submitMember}>
            <h4>Add member</h4>
            <label>
              Person entity ID
              <input
                aria-label="Member person entity ID"
                value={memberDraft.personEntityId}
                onChange={(event) => setMemberDraft({ ...memberDraft, personEntityId: event.target.value })}
              />
            </label>
            <label>
              Evidence ID
              <input
                aria-label="Member evidence ID"
                value={memberDraft.evidenceId}
                onChange={(event) => setMemberDraft({ ...memberDraft, evidenceId: event.target.value })}
              />
            </label>
            <button type="submit" disabled={addMemberMutation.isPending}>Add member</button>
          </form>
        </section>
      ) : null}

      <section aria-labelledby={`timeline-heading-${entityId}`}>
        <h3 id={`timeline-heading-${entityId}`}>Timeline</h3>
        {timelineQuery.isLoading ? <p role="status">Loading timeline…</p> : null}
        {timelineQuery.isError ? (
          <div role="alert">{timelineQuery.error.message}</div>
        ) : timelineQuery.data?.items.length ? (
          <ol>
            {timelineQuery.data.items.map((entry) => (
              <li key={entry.id}>
                <time>{entry.effective_at}</time> — {entry.summary}
              </li>
            ))}
          </ol>
        ) : timelineQuery.isSuccess ? (
          <p className="empty-state">No timeline entries yet.</p>
        ) : null}
      </section>
    </section>
  )
}
