import { Fragment, useState, type FormEvent, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import EntityDetail from './EntityDetail'
import type { EntityKind, EntityList, KnowledgeEntity, RetrievalResponse } from './types'

const ENTITY_KINDS: EntityKind[] = ['person', 'organization', 'project', 'topic', 'decision', 'document']

type CreateDraft = { kind: EntityKind; canonicalName: string; summary: string }

const emptyDraft: CreateDraft = { kind: 'person', canonicalName: '', summary: '' }

type SearchFilters = { kind: EntityKind | ''; updatedFrom: string; updatedTo: string }

const emptyFilters: SearchFilters = { kind: '', updatedFrom: '', updatedTo: '' }

// Query-state persistence: the search query and filters live in the URL so
// a reload, a shared link, or browser back/forward restores the same
// search rather than losing it -- this app has no router, so the URL is
// read once on mount and kept in sync with history.replaceState (never
// pushState: a search-as-you-refine flow shouldn't spam back-button
// history entries) rather than adding a routing library for one feature.
function readSearchStateFromUrl(): { query: string; filters: SearchFilters } {
  const params = new URLSearchParams(window.location.search)
  const kind = params.get('kind')
  return {
    query: params.get('q') ?? '',
    filters: {
      kind: kind && (ENTITY_KINDS as string[]).includes(kind) ? (kind as EntityKind) : '',
      updatedFrom: params.get('updated_from') ?? '',
      updatedTo: params.get('updated_to') ?? '',
    },
  }
}

function writeSearchStateToUrl(query: string, filters: SearchFilters) {
  const params = new URLSearchParams(window.location.search)
  if (query) params.set('q', query)
  else params.delete('q')
  if (filters.kind) params.set('kind', filters.kind)
  else params.delete('kind')
  if (filters.updatedFrom) params.set('updated_from', filters.updatedFrom)
  else params.delete('updated_from')
  if (filters.updatedTo) params.set('updated_to', filters.updatedTo)
  else params.delete('updated_to')
  const next = params.toString()
  const url = next ? `${window.location.pathname}?${next}` : window.location.pathname
  window.history.replaceState(window.history.state, '', url)
}

function buildRetrievalUrl(query: string, filters: SearchFilters): string {
  const params = new URLSearchParams({ q: query, mode: 'hybrid' })
  if (filters.kind) params.set('kind', filters.kind)
  if (filters.updatedFrom) params.set('updated_from', new Date(filters.updatedFrom).toISOString())
  if (filters.updatedTo) params.set('updated_to', new Date(filters.updatedTo).toISOString())
  return `/api/v1/knowledge/retrieve?${params.toString()}`
}

// Match highlighting: wraps every case-insensitive occurrence of a query
// term in <mark> so a scanning reader can see *why* a result matched
// without reading the full snippet. Splits the query into words rather
// than matching the whole phrase literally, since a hybrid/semantic result
// often doesn't contain the query as one contiguous substring at all.
function highlightMatches(text: string, query: string): ReactNode {
  const terms = query
    .split(/\s+/)
    .map((term) => term.trim())
    .filter((term) => term.length > 0)
  if (terms.length === 0) return text
  const escaped = terms.map((term) => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
  const pattern = new RegExp(`(${escaped.join('|')})`, 'gi')
  const parts = text.split(pattern)
  return (
    <Fragment>
      {parts.map((part, index) =>
        pattern.test(part) ? <mark key={index}>{part}</mark> : <Fragment key={index}>{part}</Fragment>,
      )}
    </Fragment>
  )
}

function listEntities(): Promise<EntityList> {
  return apiRequest('/api/v1/knowledge/entities?limit=100')
}

export default function EntityExplorer() {
  const queryClient = useQueryClient()
  const [create, setCreate] = useState<CreateDraft>(emptyDraft)
  const initialSearchState = useState(() => readSearchStateFromUrl())[0]
  const [query, setQuery] = useState(initialSearchState.query)
  const [filters, setFilters] = useState<SearchFilters>(initialSearchState.filters)
  const [submittedQuery, setSubmittedQuery] = useState(initialSearchState.query)
  const [submittedFilters, setSubmittedFilters] = useState<SearchFilters>(initialSearchState.filters)
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null)

  const entitiesQuery = useQuery({ queryKey: ['knowledge', 'entities'], queryFn: listEntities, retry: 1 })
  const retrievalQuery = useQuery({
    queryKey: ['knowledge', 'retrieve', submittedQuery, submittedFilters],
    queryFn: () => apiRequest<RetrievalResponse>(buildRetrievalUrl(submittedQuery, submittedFilters)),
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
    const trimmed = query.trim()
    setSubmittedQuery(trimmed)
    setSubmittedFilters(filters)
    writeSearchStateToUrl(trimmed, filters)
  }

  function clearFilters() {
    setFilters(emptyFilters)
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
        <label>
          Filter by kind
          <select
            aria-label="Filter by kind"
            value={filters.kind}
            onChange={(event) => setFilters({ ...filters, kind: event.target.value as EntityKind | '' })}
          >
            <option value="">All kinds</option>
            {ENTITY_KINDS.map((kind) => <option key={kind} value={kind}>{kind}</option>)}
          </select>
        </label>
        <label>
          Updated from
          <input
            aria-label="Updated from"
            type="date"
            value={filters.updatedFrom}
            onChange={(event) => setFilters({ ...filters, updatedFrom: event.target.value })}
          />
        </label>
        <label>
          Updated to
          <input
            aria-label="Updated to"
            type="date"
            value={filters.updatedTo}
            onChange={(event) => setFilters({ ...filters, updatedTo: event.target.value })}
          />
        </label>
        <button type="submit">Search</button>
        <button type="button" onClick={clearFilters}>Clear filters</button>
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
          {retrievalQuery.isError ? (
            <div role="alert">{retrievalQuery.error.message}</div>
          ) : retrievalQuery.data?.items.length ? (
            <ul className="work-list">
              {retrievalQuery.data.items.map((result) => (
                <li key={result.entity_id}>
                  <button type="button" onClick={() => setSelectedEntityId(result.entity_id)}>
                    {highlightMatches(result.title, submittedQuery)}
                  </button>
                  <small>
                    {' '}
                    · {result.matching_mode} · score {result.score.toFixed(2)}
                    {result.stale ? ' · stale' : ''}
                    {result.evidence_state !== 'available' ? ` · evidence ${result.evidence_state}` : ''}
                  </small>
                  <p>{highlightMatches(result.snippet, submittedQuery)}</p>
                </li>
              ))}
            </ul>
          ) : retrievalQuery.isSuccess ? (
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
