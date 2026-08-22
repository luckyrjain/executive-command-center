// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import IncidentsPanel from './IncidentsPanel'
import type { Incident } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function incident(overrides: Partial<Incident> = {}): Incident {
  return {
    id: 'incident-1',
    title: 'Checkout outage',
    description: 'Payments failing',
    severity: 'high',
    status: 'open',
    detected_at: '2026-07-26T12:00:00Z',
    resolved_at: null,
    change_ids: [],
    version: 1,
    created_at: '2026-07-26T12:00:00Z',
    updated_at: '2026-07-26T12:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><IncidentsPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=incidents-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'incidents-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('IncidentsPanel', () => {
  it('shows an empty state matching the default open filter', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ incidents: [] })))
    renderPanel()
    expect(await screen.findByText('No incidents match this filter.')).toBeTruthy()
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByText('No incidents match this filter.')).toBeNull()
  })

  it('lists an open incident with a resolve action', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ incidents: [incident()] })))
    renderPanel()
    expect(await screen.findByText('Checkout outage')).toBeTruthy()
    expect(screen.getByText('Payments failing')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Resolve now' })).toBeTruthy()
  })

  it('does not show a resolve action for an already-resolved incident', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ incidents: [incident({ status: 'resolved', resolved_at: '2026-07-27T00:00:00Z' })] })))
    renderPanel()
    await screen.findByText('Checkout outage')
    expect(screen.queryByRole('button', { name: 'Resolve now' })).toBeNull()
  })

  it('captures a new incident with the entered title and severity', async () => {
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(incident())
      return response({ incidents: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No incidents match this filter.')
    fireEvent.change(screen.getByLabelText('Incident title'), { target: { value: 'Checkout outage' } })
    fireEvent.change(screen.getByLabelText('Severity'), { target: { value: 'critical' } })
    fireEvent.click(screen.getByRole('button', { name: 'Capture incident' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/engineering/incidents', expect.objectContaining({ method: 'POST' })))
    const call = fetch.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    const body = JSON.parse(String((call?.[1] as RequestInit).body))
    expect(body.title).toBe('Checkout outage')
    expect(body.severity).toBe('critical')
  })

  it('surfaces a failed incident capture as an alert', async () => {
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response({ error: { code: 'VALIDATION_ERROR', message: 'title is required' } }, 422)
      return response({ incidents: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No incidents match this filter.')
    fireEvent.change(screen.getByLabelText('Incident title'), { target: { value: 'Checkout outage' } })
    fireEvent.click(screen.getByRole('button', { name: 'Capture incident' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
  })

  it('maps a stale CSRF token to a reload instruction, never the generic "not permitted" 403 fallback', async () => {
    // Both CSRF_TOKEN_REQUIRED/CSRF_TOKEN_INVALID carry HTTP 403, same as a
    // real permission failure -- final Phase 6 review found this panel (and
    // DecisionsPanel) fell through to the generic 403 fallback instead of
    // the CSRF-specific message ConnectorHealthPanel already handles.
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return response({ error: { code: 'CSRF_TOKEN_INVALID', message: 'Csrf Token Invalid' } }, 403)
      }
      return response({ incidents: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No incidents match this filter.')
    fireEvent.change(screen.getByLabelText('Incident title'), { target: { value: 'Checkout outage' } })
    fireEvent.click(screen.getByRole('button', { name: 'Capture incident' }))
    expect(await screen.findByText(/security token is missing or stale/)).toBeTruthy()
    expect(screen.queryByText('You are not permitted to manage incidents in this workspace.')).toBeNull()
  })

  it('resolves an open incident', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/resolve')) return response(incident({ status: 'resolved', resolved_at: '2026-07-27T00:00:00Z' }))
      return response({ incidents: [incident()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Checkout outage')
    fireEvent.click(screen.getByRole('button', { name: 'Resolve now' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/incidents/incident-1/resolve'), expect.objectContaining({ method: 'POST' })))
  })

  it('maps INCIDENT_ALREADY_RESOLVED to a readable sentence, never the raw code, and refetches so the stale row and its now-invalid action button don\'t linger', async () => {
    // Round-trip fixture, not just an error-message assertion: an earlier
    // draft of this handler had no onError refetch at all, so this row's
    // "Resolve now" button stayed enabled against the stale (still-open)
    // row after a 409 -- a retry click would just 409 again in a loop. The
    // GET after the 409 returns the row already resolved by someone else,
    // proving the panel actually re-reads current state.
    let getCount = 0
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/resolve')) return response({ error: { code: 'INCIDENT_ALREADY_RESOLVED', message: 'Incident Already Resolved' } }, 409)
      getCount += 1
      if (getCount === 1) return response({ incidents: [incident()] })
      return response({ incidents: [incident({ status: 'resolved', resolved_at: '2026-07-27T00:00:00Z' })] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Checkout outage')
    fireEvent.click(screen.getByRole('button', { name: 'Resolve now' }))
    expect(await screen.findByText(/This incident was already resolved/)).toBeTruthy()
    expect(screen.queryByText('Incident Already Resolved')).toBeNull()

    await waitFor(() => expect(getCount).toBe(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Resolve now' })).toBeNull())
  })

  it('switches the status filter and refetches accordingly', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('status=resolved')) return response({ incidents: [incident({ status: 'resolved', resolved_at: '2026-07-27T00:00:00Z' })] })
      return response({ incidents: [incident()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Checkout outage')
    fireEvent.click(screen.getByRole('button', { name: 'resolved' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('status=resolved'), expect.anything()))
  })
})
