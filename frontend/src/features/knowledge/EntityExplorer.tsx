import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import EntityDetail from './EntityDetail'
import type { EntityKind, EntityList, KnowledgeEntity, RetrievalResponse } from './types'

const ENTITY_KINDS: EntityKind[] = ['person', 'organization', 'project', 'topic', 'decision', 'document']

type CreateDraft = { kind: EntityKind; canonicalName: string; summary: string }

const emptyDraft: CreateDraft = { kind: 'person', canonicalName: '', summary: '' }

function listEntities(): Promise<EntityList> {
  return apiRequest('/api/v1/knowledge/entities?limit=100')
}

export default function EntityExplorer() {
  const queryClient = useQueryClient()
  const [create, setCreate] = useState<CreateDraft>(emptyDraft)
  const [query, setQuery] = useState('')
  const [submittedQuery, setSubmittedQuery] = useState('')
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)

  const entitiesQuery = useQuery({ queryKey: ['knowledge', 'entities'], queryFn: listEntities, retry: 1 })
  const retrievalQuery = useQuery({
    queryKey: ['knowledge', 'retrieve', submittedQuery],
    queryFn: () =>
      apiRequest<RetrievalResponse>(`/api/v1/knowledge/retrieve?q=${encodeURIComponent(submittedQuery)}`),
    enabled: submittedQuery.length > 0,
  })

  const createMutation = useMutation({
    mutationFn: (draft: CreateDraft) =>
      apiRequest<KnowledgeEntity>('/api/v1/knowledge/entities', {
        method: 'POST',
        body: {
          kind: draft.kind,
          canonical_name: draft.canonicalName.trim(),
          summary: draft.summary.trim() || null,
        },
      }),
    onSuccess: (created) => {
      setCreate(emptyDraft)
      void queryClient.invalidateQueries({ queryKey: ['knowledge', 'entities'] })
      setSelectedEntityId(created.id)
    },
  })

  function submitCreate(event: FormEvent) {
    event.preventDefault()
    if (create.canonicalName.trim()) createMutation.mutate(create)
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault()
    setSubmittedQuery(query.trim())
  }

  return (
    <section className="work-panel knowledge-explorer" aria-labelledby="knowledge-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">KNOWLEDGE</p>
          <h1 id="knowledge-title">Knowledge</h1>
          <p>Explore entities, evidence and relationships across your workspace.</p>
        </div>
      </div>

      {createMutation.error ? (
        <div role="alert" className="inline-status error-panel">{createMutation.error.message}</div>
      ) : null}
      <form onSubmit={submitCreate}>
        <h2>Create entity</h2>
        <label>
          Entity kind
          <select
            aria-label="Entity kind"
            value={create.kind}
            onChange={(event) => setCreate({ ...create, kind: event.target.value as EntityKind })}
          >
            {ENTITY_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
          </select>
        </label>
        <label>
          Canonical name
          <input
            aria-label="Canonical name"
            required
            value={create.canonicalName}
            onChange={(event) => setCreate({ ...create, canonicalName: event.target.value })}
          />
        </label>
        <label>
          Summary
          <textarea
            aria-label="Summary"
            value={create.summary}
            onChange={(event) => setCreate({ ...create, summary: event.target.value })}
          />
        </label>
        <button type="submit" disabled={createMutation.isPending}>Create entity</button>
      </form>

      <form onSubmit={submitSearch} role="search">
        <label>
          Search entities
          <input
            aria-label="Search entities"
            type="search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        <button type="submit">Search</button>
      </form>

      {retrievalQuery.data?.degraded ? (
        <div role="status" className="inline-status degraded-panel">
          Semantic search is unavailable; showing lexical results only.
        </div>
      ) : null}
      {submittedQuery ? (
        <section aria-labelledby="search-results-heading">
          <h2 id="search-results-heading">Search results for “{submittedQuery}”</h2>
          {retrievalQuery.isLoading ? <p role="status">Searching…</p> : null}
          {retrievalQuery.data?.items.length ? (
            <ul className="work-list">
              {retrievalQuery.data.items.map((result) => (
                <li key={result.entity_id}>
                  <button type="button" onClick={() => setSelectedEntityId(result.entity_id)}>
                    {result.title}
                  </button>
                  <small> · {result.matching_mode} · score {result.score.toFixed(2)}{result.stale ? ' · stale' : ''}</small>
                  <p>{result.snippet}</p>
                </li>
              ))}
            </ul>
          ) : !retrievalQuery.isLoading ? (
            <p className="empty-state">No matches found.</p>
          ) : null}
        </section>
      ) : null}

      <section aria-labelledby="entity-list-heading">
        <h2 id="entity-list-heading">All entities</h2>
        {entitiesQuery.isLoading ? <p role="status">Loading entities…</p> : null}
        {entitiesQuery.isError ? <div role="alert">{entitiesQuery.error.message}</div> : null}
        <ol className="work-list">
          {(entitiesQuery.data?.items ?? []).map((entity) => (
            <li key={entity.id}>
              <button type="button" onClick={() => setSelectedEntityId(entity.id)}>
                {entity.canonical_name}
              </button>
              <small> · {entity.kind}{entity.status !== 'active' ? ` · ${entity.status}` : ''}</small>
            </li>
          ))}
        </ol>
      </section>

      {selectedEntityId ? (
        <EntityDetail entityId={selectedEntityId} onClose={() => setSelectedEntityId(null)} />
      ) : null}
    </section>
  )
}
