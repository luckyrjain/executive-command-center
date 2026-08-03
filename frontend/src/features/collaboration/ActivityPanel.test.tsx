// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ActivityPanel from './ActivityPanel'
import type { ActivityItem } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function item(overrides: Partial<ActivityItem> = {}): ActivityItem {
  return {
    id: 'event-1',
    event_type: 'incident.created',
    aggregate_type: 'incident',
    aggregate_id: 'incident-1',
    actor_id: 'user-1',
    occurred_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><ActivityPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=activity-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'activity-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ActivityPanel', () => {
  it('shows the empty state when there is no visible activity', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [], next_cursor: null })))
    renderPanel()
    expect(await screen.findByText('No visible activity yet.')).toBeTruthy()
  })

  it('lists activity items', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [item()], next_cursor: null })))
    renderPanel()
    expect(await screen.findByText('incident.created')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('loads the next page via the cursor and offers Back to latest', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('cursor=next-page')) return response({ items: [item({ id: 'event-2', event_type: 'incident.resolved' })], next_cursor: null })
      return response({ items: [item()], next_cursor: 'next-page' })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('incident.created')
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('cursor=next-page'),
      expect.anything(),
    ))
    expect(await screen.findByText('incident.resolved')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Back to latest' })).toBeTruthy()
  })

  it('surfaces a fetch failure as an alert', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify({ error: { code: 'REQUEST_FAILED' } }), { status: 500, headers: { 'Content-Type': 'application/json' } }))))
    renderPanel()
    // The query itself carries `retry: 1` (a real resilience choice, kept
    // as-is rather than weakened for this test) -- TanStack Query's default
    // retry delay means the error state lands slightly after testing-
    // library's own default 1000ms `findBy*` timeout, so this one needs a
    // longer explicit wait rather than a component change.
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
  })
})
