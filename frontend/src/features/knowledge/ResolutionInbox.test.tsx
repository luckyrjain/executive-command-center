// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ResolutionInbox from './ResolutionInbox'

const candidate = {
  id: 'candidate-1',
  left_entity_id: 'entity-left',
  right_entity_id: 'entity-right',
  score: 0.82,
  factors: { name_similarity: 0.9, alias_overlap: 0 },
  resolver_version: 'phase2-resolution-v1',
  status: 'open',
  created_at: '2026-07-01T00:00:00Z',
  resolved_at: null,
  resolved_by: null,
  reason: null,
  deferred_until: null,
}

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderInbox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><ResolutionInbox /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=knowledge-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'knowledge-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ResolutionInbox', () => {
  it('lists open candidates with their score and factors', async () => {
    const fetch = vi.fn().mockImplementationOnce(() => jsonResponse({ items: [candidate], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderInbox()

    await screen.findByText('entity-left', { exact: false })
    expect(fetch.mock.calls[0][0]).toContain('status=open')
  })

  it('confirms a candidate with the entered reason', async () => {
    const confirmed = { ...candidate, status: 'confirmed', resolved_at: '2026-07-02T00:00:00Z', reason: 'same identity' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => jsonResponse({ items: [candidate], next_cursor: null }))
      .mockImplementationOnce(() => jsonResponse(confirmed))
      .mockImplementationOnce(() => jsonResponse({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderInbox()

    await screen.findByText('entity-left', { exact: false })
    fireEvent.change(screen.getByLabelText(`Reason for ${candidate.id}`), { target: { value: 'same identity' } })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm match' }))

    const confirmCall = await new Promise<[string, RequestInit]>((resolve) => {
      const check = () => {
        const call = fetch.mock.calls.find((c) => String(c[0]).includes('/confirm'))
        if (call) resolve(call as [string, RequestInit])
        else setTimeout(check, 5)
      }
      check()
    })
    expect(confirmCall[0]).toContain(`/resolution/candidates/${candidate.id}/confirm`)
    expect(JSON.parse(String(confirmCall[1].body))).toEqual({ reason: 'same identity' })
  })

  it('shows an empty state when there are no open candidates', async () => {
    const fetch = vi.fn().mockImplementationOnce(() => jsonResponse({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderInbox()

    await screen.findByText('No resolution candidates awaiting review.')
  })

  it('shows a distinct error state when the candidates fetch fails, never the empty-state text', async () => {
    // Regression test: the empty-state paragraph used to be gated on
    // `!query.isLoading`, which is also true while a fetch has failed --
    // so an error rendered the "No resolution candidates..." text
    // alongside the alert, exactly the bug EntityDetail.tsx already fixed
    // for its own sections but this component missed.
    // ResolutionInbox's query passes retry: 1, overriding the QueryClient's
    // own retry: false default -- mockImplementation (not Once) so the
    // retried attempt also fails, rather than hanging on an unconfigured
    // mock call. React Query's default backoff delays that retry by ~1s,
    // so the assertion needs a longer-than-default findByRole timeout.
    const fetch = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ error: { code: 'CANDIDATES_UNAVAILABLE' } }), {
          status: 500,
          headers: { 'Content-Type': 'application/json' },
        }),
      ),
    )
    vi.stubGlobal('fetch', fetch)
    renderInbox()

    await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(screen.queryByText('No resolution candidates awaiting review.')).toBeNull()
  })

  it('defers a candidate with a future deferred_until, without requiring a reason', async () => {
    const deferred = { ...candidate, deferred_until: '2026-07-03T00:00:00Z' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => jsonResponse({ items: [candidate], next_cursor: null }))
      .mockImplementationOnce(() => jsonResponse(deferred))
      .mockImplementationOnce(() => jsonResponse({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderInbox()

    await screen.findByText('entity-left', { exact: false })
    fireEvent.click(screen.getByRole('button', { name: 'Defer' }))

    const deferCall = await new Promise<[string, RequestInit]>((resolve) => {
      const check = () => {
        const call = fetch.mock.calls.find((c) => String(c[0]).includes('/defer'))
        if (call) resolve(call as [string, RequestInit])
        else setTimeout(check, 5)
      }
      check()
    })
    expect(deferCall[0]).toContain(`/resolution/candidates/${candidate.id}/defer`)
    const body = JSON.parse(String(deferCall[1].body))
    expect(new Date(body.deferred_until).getTime()).toBeGreaterThan(Date.now())
  })
})
