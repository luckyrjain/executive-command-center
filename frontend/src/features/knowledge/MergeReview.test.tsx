// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MergeReview from './MergeReview'

const candidate = {
  id: 'candidate-1',
  left_entity_id: 'entity-left',
  right_entity_id: 'entity-right',
  score: 0.91,
  factors: {},
  resolver_version: 'phase2-resolution-v1',
  status: 'confirmed',
  created_at: '2026-07-01T00:00:00Z',
  resolved_at: '2026-07-02T00:00:00Z',
  resolved_by: 'user-1',
  reason: 'verified duplicate',
}

const leftEntity = {
  id: 'entity-left', entity_id: null, kind: 'person', canonical_name: 'Ada Lovelace',
  summary: null, status: 'active', confidence: 1, version: 3,
  created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z',
}
const rightEntity = {
  id: 'entity-right', entity_id: null, kind: 'person', canonical_name: 'Ada Lovelase',
  summary: null, status: 'active', confidence: 1, version: 1,
  created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z',
}

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

type Route = { method: string; match: (pathname: string) => boolean; handle: () => Promise<Response> }

function routedFetch(routes: Route[]) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    const url = new URL(String(input), 'http://localhost')
    const route = routes.find((candidateRoute) => candidateRoute.method === method && candidateRoute.match(url.pathname))
    if (!route) return Promise.resolve(new Response(JSON.stringify({ error: { code: 'NOT_FOUND' } }), { status: 404 }))
    return route.handle()
  })
}

function renderMergeReview() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><MergeReview /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=knowledge-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'knowledge-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('MergeReview', () => {
  it('lists confirmed candidates with both entity names', async () => {
    const fetch = routedFetch([
      { method: 'GET', match: (path) => path === '/api/v1/knowledge/resolution/candidates', handle: () => jsonResponse({ items: [candidate], next_cursor: null }) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${leftEntity.id}`, handle: () => jsonResponse(leftEntity) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${rightEntity.id}`, handle: () => jsonResponse(rightEntity) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderMergeReview()

    await screen.findByText('Ada Lovelace')
    await screen.findByText('Ada Lovelase')
    expect(fetch.mock.calls[0][0]).toContain('status=confirmed')
  })

  it('merges into the chosen target with correct expected versions and offers a reverse action', async () => {
    const mergeOperation = {
      id: 'operation-1', operation_type: 'merge', status: 'active',
      source_entity_id: rightEntity.id, target_entity_id: leftEntity.id,
      actor_id: 'user-1', reason: 'confirmed duplicate', reverses_operation_id: null,
      created_at: '2026-07-03T00:00:00Z',
    }
    const fetch = routedFetch([
      { method: 'GET', match: (path) => path === '/api/v1/knowledge/resolution/candidates', handle: () => jsonResponse({ items: [candidate], next_cursor: null }) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${leftEntity.id}`, handle: () => jsonResponse(leftEntity) },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${rightEntity.id}`, handle: () => jsonResponse(rightEntity) },
      { method: 'POST', match: (path) => path === '/api/v1/knowledge/entities/merge', handle: () => jsonResponse(mergeOperation, 201) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderMergeReview()

    await screen.findByText('Ada Lovelace')
    fireEvent.change(screen.getByLabelText(`Merge reason for ${candidate.id}`), { target: { value: 'confirmed duplicate' } })
    fireEvent.click(screen.getByRole('button', { name: 'Merge into Ada Lovelace' }))

    await waitFor(() => {
      const mergeCall = fetch.mock.calls.find(([, init]) => init?.method === 'POST')
      expect(mergeCall).toBeTruthy()
    })
    const mergeCall = fetch.mock.calls.find(([, init]) => init?.method === 'POST')
    const payload = JSON.parse(String(mergeCall?.[1]?.body))
    expect(payload).toEqual({
      candidate_id: candidate.id,
      target_entity_id: leftEntity.id,
      expected_target_version: leftEntity.version,
      expected_source_version: rightEntity.version,
      reason: 'confirmed duplicate',
    })

    await screen.findByText(`Merged ${rightEntity.id} into ${leftEntity.id}`)
    expect(screen.getByLabelText(`Reversal reason for ${mergeOperation.id}`)).toBeTruthy()
  })

  it('on a version conflict, shows a dedicated message and refetches the latest entity versions', async () => {
    let leftCallCount = 0
    const fetch = routedFetch([
      { method: 'GET', match: (path) => path === '/api/v1/knowledge/resolution/candidates', handle: () => jsonResponse({ items: [candidate], next_cursor: null }) },
      {
        method: 'GET',
        match: (path) => path === `/api/v1/knowledge/entities/${leftEntity.id}`,
        handle: () => {
          leftCallCount += 1
          // The entity changed (version bumped) by the time of refetch --
          // simulating another user's concurrent edit.
          return jsonResponse(leftCallCount > 1 ? { ...leftEntity, version: 4 } : leftEntity)
        },
      },
      { method: 'GET', match: (path) => path === `/api/v1/knowledge/entities/${rightEntity.id}`, handle: () => jsonResponse(rightEntity) },
      {
        method: 'POST',
        match: (path) => path === '/api/v1/knowledge/entities/merge',
        handle: () => Promise.resolve(new Response(JSON.stringify({ error: { code: 'VERSION_CONFLICT', message: 'Version Conflict' } }), { status: 409, headers: { 'Content-Type': 'application/json' } })),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderMergeReview()

    await screen.findByText('Ada Lovelace')
    fireEvent.change(screen.getByLabelText(`Merge reason for ${candidate.id}`), { target: { value: 'confirmed duplicate' } })
    fireEvent.click(screen.getByRole('button', { name: 'Merge into Ada Lovelace' }))

    await screen.findByText('One of these entities changed since this page loaded. Refreshing the latest version below.')
    await screen.findByRole('button', { name: 'Retry: merge into Ada Lovelace' })
    expect(leftCallCount).toBeGreaterThan(1)
  })
})
