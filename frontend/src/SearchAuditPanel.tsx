import { FormEvent, KeyboardEvent, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8000'

type SearchResult = {
  entity_type: string
  entity_id: string
  title: string
  snippet: string
  matched_fields: string[]
  score: number
  updated_at: string
  timestamp_context?: string | null
  source_type: string
  archived: boolean
}

type SearchResponse = {
  items: SearchResult[]
  next_cursor?: string | null
  degraded: boolean
}

type AuditEvent = {
  id: string
  event_type: string
  aggregate_type: string
  aggregate_id: string
  aggregate_version: number
  actor_id?: string | null
  changed_fields: string[]
  authorization_result: string
  source: string
  failure_code?: string | null
  occurred_at: string
}

type AuditResponse = {
  items: AuditEvent[]
  next_cursor?: string | null
}

type ErrorEnvelope = { error?: { message?: string } }
type View = 'search' | 'audit'

async function request<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: 'include',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    const payload = (await response.json().catch(() => ({}))) as ErrorEnvelope
    throw new Error(payload.error?.message ?? 'Request failed')
  }
  return response.json()
}

function formatDate(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(parsed)
}

export default function SearchAuditPanel() {
  const [view, setView] = useState<View>('search')
  const [draftQuery, setDraftQuery] = useState('')
  const [query, setQuery] = useState('')
  const [searchCursor, setSearchCursor] = useState<string | null>(null)
  const [auditCursor, setAuditCursor] = useState<string | null>(null)
  const [eventType, setEventType] = useState('')
  const searchTabRef = useRef<HTMLButtonElement>(null)
  const auditTabRef = useRef<HTMLButtonElement>(null)

  const search = useQuery({
    queryKey: ['search', query, searchCursor],
    queryFn: () => {
      const params = new URLSearchParams({ q: query, limit: '20' })
      if (searchCursor) params.set('cursor', searchCursor)
      return request<SearchResponse>(`/api/v1/search?${params}`)
    },
    enabled: query.length > 0,
    retry: 1,
  })

  const audit = useQuery({
    queryKey: ['audit', eventType, auditCursor],
    queryFn: () => {
      const params = new URLSearchParams({ limit: '20' })
      if (eventType) params.set('event_type', eventType)
      if (auditCursor) params.set('cursor', auditCursor)
      return request<AuditResponse>(`/api/v1/audit?${params}`)
    },
    enabled: view === 'audit',
    retry: 1,
  })

  function submitSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const normalized = draftQuery.trim()
    if (!normalized) return
    setSearchCursor(null)
    setQuery(normalized)
  }

  function selectView(next: View) {
    setView(next)
    if (next === 'search') searchTabRef.current?.focus()
    else auditTabRef.current?.focus()
  }

  function handleTabKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault()
      selectView(view === 'search' ? 'audit' : 'search')
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault()
      selectView(view === 'search' ? 'audit' : 'search')
    } else if (event.key === 'Home') {
      event.preventDefault()
      selectView('search')
    } else if (event.key === 'End') {
      event.preventDefault()
      selectView('audit')
    }
  }

  return (
    <section className="explore-panel" aria-labelledby="explore-title">
      <div className="explore-heading">
        <div>
          <p className="eyebrow">DISCOVER AND TRACE</p>
          <h2 id="explore-title">Search & Audit</h2>
          <p>Find work across the command center or inspect immutable change history.</p>
        </div>
        <div className="tab-list" role="tablist" aria-label="Search and audit views">
          <button
            ref={searchTabRef}
            id="search-tab"
            type="button"
            role="tab"
            aria-selected={view === 'search'}
            aria-controls="search-panel"
            tabIndex={view === 'search' ? 0 : -1}
            onKeyDown={handleTabKey}
            onClick={() => setView('search')}
          >
            Search
          </button>
          <button
            ref={auditTabRef}
            id="audit-tab"
            type="button"
            role="tab"
            aria-selected={view === 'audit'}
            aria-controls="audit-panel"
            tabIndex={view === 'audit' ? 0 : -1}
            onKeyDown={handleTabKey}
            onClick={() => setView('audit')}
          >
            Audit history
          </button>
        </div>
      </div>

      {view === 'search' ? (
        <div id="search-panel" role="tabpanel" aria-labelledby="search-tab">
          <form className="search-form" role="search" onSubmit={submitSearch}>
            <label htmlFor="global-search">Search tasks, commitments, notes, meetings, events and risks</label>
            <div>
              <input
                id="global-search"
                type="search"
                value={draftQuery}
                onChange={(event) => setDraftQuery(event.target.value)}
                maxLength={500}
                placeholder="Search the command center"
              />
              <button type="submit" disabled={!draftQuery.trim() || search.isFetching}>
                {search.isFetching ? 'Searching…' : 'Search'}
              </button>
            </div>
          </form>

          {!query ? <p className="explore-empty">Enter a query to search all Phase 1 entities.</p> : null}
          {search.isError ? <div className="inline-status error-panel" role="alert">{search.error.message}</div> : null}
          {search.data?.degraded ? (
            <div className="inline-status degraded-panel" role="status">Search is using degraded prefix matching.</div>
          ) : null}
          {search.data && search.data.items.length === 0 ? <p className="explore-empty">No matching entities found.</p> : null}
          {search.data?.items.length ? (
            <ol className="search-results" aria-live="polite">
              {search.data.items.map((item) => (
                <li key={`${item.entity_type}:${item.entity_id}`}>
                  <div className="result-copy">
                    <div className="result-meta">
                      <span>{item.entity_type.replaceAll('_', ' ')}</span>
                      <span>{Math.round(item.score * 100)}%</span>
                      {item.archived ? <span>archived</span> : null}
                    </div>
                    <h3>{item.title}</h3>
                    {item.snippet ? <p>{item.snippet}</p> : null}
                    <small>Matched {item.matched_fields.join(', ') || 'content'} · Updated {formatDate(item.updated_at)}</small>
                  </div>
                </li>
              ))}
            </ol>
          ) : null}
          {search.data?.next_cursor ? (
            <button type="button" onClick={() => setSearchCursor(search.data?.next_cursor ?? null)}>
              Load more results
            </button>
          ) : null}
        </div>
      ) : (
        <div id="audit-panel" role="tabpanel" aria-labelledby="audit-tab">
          <div className="audit-toolbar">
            <label htmlFor="audit-event-type">Filter by event type</label>
            <input
              id="audit-event-type"
              value={eventType}
              onChange={(event) => {
                setEventType(event.target.value)
                setAuditCursor(null)
              }}
              placeholder="For example task.updated"
            />
          </div>
          {audit.isLoading ? <div className="inline-status" role="status">Loading immutable history…</div> : null}
          {audit.isError ? <div className="inline-status error-panel" role="alert">{audit.error.message}</div> : null}
          {audit.data && audit.data.items.length === 0 ? <p className="explore-empty">No audit events match this filter.</p> : null}
          {audit.data?.items.length ? (
            <ol className="audit-list" aria-live="polite">
              {audit.data.items.map((item) => (
                <li key={item.id}>
                  <div>
                    <strong>{item.event_type}</strong>
                    <span>{item.aggregate_type} · version {item.aggregate_version}</span>
                    {item.changed_fields.length ? <small>Changed: {item.changed_fields.join(', ')}</small> : null}
                  </div>
                  <div className="audit-meta">
                    <time dateTime={item.occurred_at}>{formatDate(item.occurred_at)}</time>
                    <span>{item.authorization_result}</span>
                  </div>
                </li>
              ))}
            </ol>
          ) : null}
          {audit.data?.next_cursor ? (
            <button type="button" onClick={() => setAuditCursor(audit.data?.next_cursor ?? null)}>
              Load older events
            </button>
          ) : null}
        </div>
      )}
    </section>
  )
}
