// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import EntityExplorer from './EntityExplorer'

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

type Route = { method: string; match: (pathname: string) => boolean; handle: (pathname: string, body: unknown) => Promise<Response> }

function routedFetch(routes: Route[]) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    const url = new URL(String(input), 'http://localhost')
    const body = init?.body ? JSON.parse(String(init.body)) : undefined
    const route = routes.find((candidate) => candidate.method === method && candidate.match(url.pathname))
    if (!route) return Promise.resolve(new Response(JSON.stringify({ error: { code: 'NOT_FOUND' } }), { status: 404 }))
    return route.handle(url.pathname, body)
  })
}

function renderExplorer() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><EntityExplorer /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=knowledge-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'knowledge-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('EntityExplorer', () => {
  it('lists entities returned by GET /api/v1/knowledge/entities', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (path) => path === '/api/v1/knowledge/entities', handle: () => jsonResponse({ items: [personEntity], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderExplorer()

    await screen.findByText('Ada Lovelace')
  })

  it('creates an entity and refetches the entity list', async () => {
    const created = { ...personEntity, id: 'entity-2', canonical_name: 'Grace Hopper' }
    let listCallCount = 0
    const fetch = routedFetch([
      {
        method: 'GET',
        match: (path) => path === '/api/v1/knowledge/entities',
        handle: () => {
          listCallCount += 1
          return jsonResponse({ items: listCallCount > 1 ? [personEntity, created] : [personEntity], next_cursor: null })
        },
      },
      { method: 'POST', match: (path) => path === '/api/v1/knowledge/entities', handle: () => jsonResponse(created, 201) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${created.id}`, handle: () => jsonResponse(created) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${created.id}/aliases`, handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${created.id}/claims`, handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${created.id}/relationships`, handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${created.id}/timeline`, handle: () => jsonResponse({ items: [], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderExplorer()
    await screen.findByText('Ada Lovelace')

    fireEvent.change(screen.getByLabelText('Canonical name'), { target: { value: 'Grace Hopper' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create entity' }))

    await screen.findByText('Grace Hopper')
    const createCall = fetch.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(createCall).toBeTruthy()
    const payload = JSON.parse(String(createCall?.[1]?.body))
    expect(payload).toEqual({ kind: 'person', canonical_name: 'Grace Hopper', summary: null })
  })

  it('searches and opens entity detail from a result', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (path) => path === '/api/v1/knowledge/entities', handle: () => jsonResponse({ items: [], next_cursor: null }) },
      {
        method: 'GET',
        match: (path) => path === '/api/v1/knowledge/retrieve',
        handle: () =>
          jsonResponse({
            items: [{
              entity_type: 'person', entity_id: personEntity.id, title: 'Ada Lovelace',
              snippet: 'Mathematician', score: 0.95, matching_mode: 'exact_name',
              factors: {}, evidence_state: 'unknown', source_version: 1, stale: false,
            }],
            next_cursor: null, mode: 'lexical', degraded: false, degraded_reason: null,
          }),
      },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${personEntity.id}`, handle: () => jsonResponse(personEntity) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${personEntity.id}/aliases`, handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${personEntity.id}/claims`, handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${personEntity.id}/relationships`, handle: () => jsonResponse({ items: [] }) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${personEntity.id}/timeline`, handle: () => jsonResponse({ items: [], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderExplorer()

    fireEvent.change(screen.getByLabelText('Search entities'), { target: { value: 'Ada' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    const resultButton = await screen.findByRole('button', { name: 'Ada Lovelace' })
    fireEvent.click(resultButton)

    await waitFor(() => expect(screen.getByRole('heading', { name: 'Ada Lovelace', level: 2 })).toBeTruthy())
  })
})
