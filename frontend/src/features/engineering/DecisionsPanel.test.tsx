// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DecisionsPanel from './DecisionsPanel'
import type { Decision } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function decision(overrides: Partial<Decision> = {}): Decision {
  return {
    id: 'decision-1',
    title: 'Adopt trunk-based development',
    description: 'Reduce merge conflicts',
    rationale: null,
    status: 'proposed',
    decided_at: null,
    change_ids: [],
    version: 1,
    created_at: '2026-07-26T12:00:00Z',
    updated_at: '2026-07-26T12:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><DecisionsPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=decisions-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'decisions-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('DecisionsPanel', () => {
  it('shows an empty state matching the default proposed filter', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ decisions: [] })))
    renderPanel()
    expect(await screen.findByText('No decisions match this filter.')).toBeTruthy()
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByText('No decisions match this filter.')).toBeNull()
  })

  it('lists a proposed decision with a decide action', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ decisions: [decision()] })))
    renderPanel()
    expect(await screen.findByText('Adopt trunk-based development')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Record decision' })).toBeTruthy()
  })

  it('does not show a decide action for an already-decided decision', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ decisions: [decision({ status: 'decided', decided_at: '2026-07-27T00:00:00Z', rationale: 'Team agreed' })] })))
    renderPanel()
    await screen.findByText('Adopt trunk-based development')
    expect(screen.queryByRole('button', { name: 'Record decision' })).toBeNull()
    expect(screen.getByText('Rationale: Team agreed')).toBeTruthy()
  })

  it('proposes a new decision with the entered title and description', async () => {
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(decision())
      return response({ decisions: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No decisions match this filter.')
    fireEvent.change(screen.getByLabelText('Decision title'), { target: { value: 'Adopt trunk-based development' } })
    fireEvent.click(screen.getByRole('button', { name: 'Propose decision' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/v1/engineering/decisions', expect.objectContaining({ method: 'POST' })))
  })

  it('surfaces a failed decision proposal as an alert', async () => {
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response({ error: { code: 'VALIDATION_ERROR', message: 'title is required' } }, 422)
      return response({ decisions: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No decisions match this filter.')
    fireEvent.change(screen.getByLabelText('Decision title'), { target: { value: 'Adopt trunk-based development' } })
    fireEvent.click(screen.getByRole('button', { name: 'Propose decision' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
  })

  it('maps a stale CSRF token to a reload instruction, never the generic "not permitted" 403 fallback', async () => {
    // Both CSRF_TOKEN_REQUIRED/CSRF_TOKEN_INVALID carry HTTP 403, same as a
    // real permission failure -- final Phase 6 review found this panel (and
    // IncidentsPanel) fell through to the generic 403 fallback instead of
    // the CSRF-specific message ConnectorHealthPanel already handles.
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return response({ error: { code: 'CSRF_TOKEN_INVALID', message: 'Csrf Token Invalid' } }, 403)
      }
      return response({ decisions: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No decisions match this filter.')
    fireEvent.change(screen.getByLabelText('Decision title'), { target: { value: 'Adopt trunk-based development' } })
    fireEvent.click(screen.getByRole('button', { name: 'Propose decision' }))
    expect(await screen.findByText(/security token is missing or stale/)).toBeTruthy()
    expect(screen.queryByText('You are not permitted to manage decisions in this workspace.')).toBeNull()
  })

  it('decides a proposed decision with an optional rationale', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/decide')) return response(decision({ status: 'decided', decided_at: '2026-07-27T00:00:00Z', rationale: 'Consensus reached' }))
      return response({ decisions: [decision()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Adopt trunk-based development')
    fireEvent.change(screen.getByLabelText('Rationale for Adopt trunk-based development'), { target: { value: 'Consensus reached' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record decision' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/decisions/decision-1/decide'), expect.objectContaining({ method: 'POST' })))
    const call = fetch.mock.calls.find((c) => String(c[0]).includes('/decide'))
    expect(JSON.parse(String((call?.[1] as RequestInit).body)).rationale).toBe('Consensus reached')
  })

  it('maps DECISION_NOT_PROPOSED to a readable sentence, never the raw code', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/decide')) return response({ error: { code: 'DECISION_NOT_PROPOSED', message: 'Decision Not Proposed' } }, 409)
      return response({ decisions: [decision()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Adopt trunk-based development')
    fireEvent.click(screen.getByRole('button', { name: 'Record decision' }))
    expect(await screen.findByText(/This decision has already been decided/)).toBeTruthy()
    expect(screen.queryByText('Decision Not Proposed')).toBeNull()
  })

  it('maps DECIDED_AT_BEFORE_CREATED_AT to a readable sentence, never the raw code', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/decide')) return response({ error: { code: 'DECIDED_AT_BEFORE_CREATED_AT', message: 'Decided At Before Created At' } }, 422)
      return response({ decisions: [decision()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Adopt trunk-based development')
    fireEvent.click(screen.getByRole('button', { name: 'Record decision' }))
    expect(await screen.findByText(/The decided time cannot be before the decision was created/)).toBeTruthy()
    expect(screen.queryByText('Decided At Before Created At')).toBeNull()
  })
})
