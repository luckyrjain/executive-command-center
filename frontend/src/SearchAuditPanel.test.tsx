// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import SearchAuditPanel from './SearchAuditPanel'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><SearchAuditPanel /></QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('SearchAuditPanel', () => {
  // Both search and audit routed through a hand-rolled fetch wrapper until
  // this file was switched to the shared `apiRequest` client -- that wrapper
  // never caught a fetch-level rejection at all, so a real network failure
  // (offline, DNS failure, CORS block) surfaced as the browser's own raw
  // `TypeError: Failed to fetch` text instead of a readable message. These
  // two tests prove the friendly OFFLINE/NETWORK_ERROR classification
  // `apiRequest` already gives every other feature now applies here too.
  it('classifies a network failure on the audit tab as a readable offline message, not a raw TypeError', async () => {
    vi.stubGlobal('navigator', { onLine: false })
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))))
    renderPanel()

    fireEvent.click(screen.getByRole('tab', { name: 'Audit history' }))

    // { timeout: 3000 }: search/audit's own useQuery hardcodes retry: 1 --
    // overriding this QueryClient's retry: false default (per-query options
    // win) -- so the query only settles into isError after a real retry
    // delay. Matches DecisionsPanel.test.tsx's/IncidentsPanel.test.tsx's
    // identical timeout for the same reason.
    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toBe('You are offline')
    expect(alert.textContent).not.toContain('Failed to fetch')
  })

  it('classifies a network failure on the search tab as a readable message, not a raw TypeError', async () => {
    vi.stubGlobal('navigator', { onLine: true })
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('Failed to fetch'))))
    renderPanel()

    fireEvent.change(screen.getByLabelText('Search tasks, commitments, notes, meetings, events and risks'), { target: { value: 'renewal' } })
    fireEvent.click(screen.getByRole('button', { name: 'Search' }))

    // { timeout: 3000 }: search/audit's own useQuery hardcodes retry: 1 --
    // overriding this QueryClient's retry: false default (per-query options
    // win) -- so the query only settles into isError after a real retry
    // delay. Matches DecisionsPanel.test.tsx's/IncidentsPanel.test.tsx's
    // identical timeout for the same reason.
    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toBe('Network request failed')
    expect(alert.textContent).not.toContain('Failed to fetch')
  })

  it('surfaces the backend error envelope message on a non-2xx response, same as before', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'SEARCH_UNAVAILABLE', message: 'Search index is rebuilding.' } }, 503)))
    renderPanel()

    fireEvent.click(screen.getByRole('tab', { name: 'Audit history' }))

    expect((await screen.findByRole('alert', {}, { timeout: 3000 })).textContent).toBe('Search index is rebuilding.')
  })
})
