// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import EntityDetail from './EntityDetail'

const personEntity = {
  id: 'entity-1',
  entity_id: null,
  kind: 'person',
  canonical_name: 'Ada Lovelace',
  summary: 'Mathematician',
  status: 'active',
  confidence: 1,
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
}

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function errorResponse(code: string, status = 500) {
  return Promise.resolve(new Response(JSON.stringify({ error: { code } }), { status, headers: { 'Content-Type': 'application/json' } }))
}

type Route = { method: string; match: (pathname: string) => boolean; handle: (pathname: string) => Promise<Response> }

function routedFetch(routes: Route[]) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    const url = new URL(String(input), 'http://localhost')
    const route = routes.find((candidate) => candidate.method === method && candidate.match(url.pathname))
    if (!route) return Promise.resolve(new Response(JSON.stringify({ error: { code: 'NOT_FOUND' } }), { status: 404 }))
    return route.handle(url.pathname)
  })
}

function renderDetail() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <EntityDetail entityId={personEntity.id} onClose={() => {}} />
    </QueryClientProvider>,
  )
}

const base = (path: string) => `/api/v1/knowledge/entities/${personEntity.id}${path}`

beforeEach(() => {
  document.cookie = 'ecc_csrf=knowledge-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'knowledge-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('EntityDetail', () => {
  it('renders empty states when aliases, claims, relationships and timeline are genuinely empty', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (p) => p === base('/aliases'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/claims'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/relationships'), handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (p) => p === base('/timeline'), handle: () => jsonResponse({ items: [], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await screen.findByText('No aliases recorded for this entity.')
    await screen.findByText('No claims recorded for this entity.')
    await screen.findByText('No relationships recorded for this entity.')
    await screen.findByText('No timeline entries yet.')
    expect(screen.queryAllByRole('alert')).toHaveLength(0)
  })

  it('shows a distinct error state for aliases, claims, relationships and timeline when their fetch fails, never the empty-state text', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (p) => p === base('/aliases'), handle: () => errorResponse('ALIASES_UNAVAILABLE') },
      { method: 'GET', match: (p) => p === base('/claims'), handle: () => errorResponse('CLAIMS_UNAVAILABLE') },
      { method: 'GET', match: (p) => p === base('/relationships'), handle: () => errorResponse('RELATIONSHIPS_UNAVAILABLE') },
      { method: 'GET', match: (p) => p === base('/timeline'), handle: () => errorResponse('TIMELINE_UNAVAILABLE') },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await screen.findByText('Ada Lovelace')
    const alerts = await screen.findAllByRole('alert')
    expect(alerts).toHaveLength(4)

    // The bug this regression-tests: a failed fetch must never render as if the
    // list were genuinely empty -- these strings must never appear.
    expect(screen.queryByText('No aliases recorded for this entity.')).toBeNull()
    expect(screen.queryByText('No claims recorded for this entity.')).toBeNull()
    expect(screen.queryByText('No relationships recorded for this entity.')).toBeNull()
    expect(screen.queryByText('No timeline entries yet.')).toBeNull()
  })

  it('renders aliases, claims, relationships and timeline content when present', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (p) => p === base(''), handle: () => jsonResponse(personEntity) },
      {
        method: 'GET',
        match: (p) => p === base('/aliases'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'alias-1', entity_id: personEntity.id, alias_type: 'nickname', normalized_value: 'ada', source_id: 'src-1', confidence: 0.9, created_at: '2026-01-01T00:00:00Z' }],
          }),
      },
      {
        method: 'GET',
        match: (p) => p === base('/claims'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'claim-1', subject_id: personEntity.id, predicate: 'title', value: { text: 'Mathematician' }, source_id: 'src-1', confidence: 0.9, superseded_by: null, valid_from: '2026-01-01T00:00:00Z', valid_to: null, created_at: '2026-01-01T00:00:00Z' }],
          }),
      },
      {
        method: 'GET',
        match: (p) => p === base('/relationships'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'rel-1', from_entity_id: personEntity.id, to_entity_id: 'entity-2', relationship_type: 'RELATES_TO', status: 'active', confidence: 0.9, evidence_id: 'src-1', valid_from: null, valid_to: null }],
          }),
      },
      {
        method: 'GET',
        match: (p) => p === base('/timeline'),
        handle: () =>
          jsonResponse({
            items: [{ id: 'tl-1', entity_id: personEntity.id, effective_at: '2026-07-01T00:00:00Z', recorded_at: '2026-07-01T00:00:00Z', event_type: 'entity_created', source_id: null, summary: 'Entity created' }],
            next_cursor: null,
          }),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await screen.findByText('nickname', { exact: false })
    await screen.findByText('title', { exact: false })
    await screen.findByText('RELATES_TO → entity-2')
    await screen.findByText('Entity created', { exact: false })
  })
})
