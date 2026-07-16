// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MorningBrief from './MorningBrief'

const baseBrief = {
  id: 'brief-1',
  briefing_date: '2026-07-16',
  generation_version: 1,
  sections: {
    today_schedule: [{ id: 'evt-1', title: 'Board sync', starts_at: '2026-07-16T14:00:00Z' }],
    top_priorities: [],
    overdue_commitments: [],
    risks: [],
  },
  source_versions: {},
  evidence_ids: [],
  generated_at: '2026-07-16T06:00:00Z',
  timezone: 'UTC',
  algorithm_version: 'phase1-deterministic-v1',
  ai_status: 'available',
  stale: false,
  stale_reason: null,
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderBrief() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><MorningBrief /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=brief-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'brief-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('MorningBrief', () => {
  it('refreshes via POST with CSRF and idempotency headers', async () => {
    const refreshed = { ...baseBrief, generation_version: 2 }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response(baseBrief))
      .mockImplementationOnce(() => response(refreshed))
    vi.stubGlobal('fetch', fetch)
    renderBrief()

    await screen.findByText('Board sync')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh brief' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2))
    const [url, init] = fetch.mock.calls[1] as [string, RequestInit]
    expect(String(url)).toContain('/api/v1/briefs/morning')
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('brief-token')
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('brief-request-id')
    await screen.findByText(/Generation 2/)
  })

  it('replaces a stale brief with fresh sections after refresh', async () => {
    const stale = { ...baseBrief, stale: true, stale_reason: 'source_data_changed' }
    const fresh = {
      ...baseBrief,
      stale: false,
      generation_version: 2,
      sections: { ...baseBrief.sections, today_schedule: [{ id: 'evt-2', title: 'Investor call', starts_at: '2026-07-16T15:00:00Z' }] },
    }
    const fetch = vi.fn().mockImplementationOnce(() => response(stale)).mockImplementationOnce(() => response(fresh))
    vi.stubGlobal('fetch', fetch)
    renderBrief()

    await screen.findByText(/this brief is stale/i)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh brief' }))

    await waitFor(() => expect(screen.queryByText(/this brief is stale/i)).toBeNull())
    await screen.findByText('Investor call')
    expect(screen.queryByText('Board sync')).toBeNull()
  })

  it('shows an AI-disabled notice distinct from the stale/error states', async () => {
    const disabled = { ...baseBrief, ai_status: 'disabled' }
    vi.stubGlobal('fetch', vi.fn(() => response(disabled)))
    renderBrief()

    await screen.findByText(/AI-assisted sections are disabled/i)
  })

  it('does not show the AI-disabled notice when AI is available', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(baseBrief)))
    renderBrief()

    await screen.findByText('Board sync')
    expect(screen.queryByText(/AI-assisted sections are disabled/i)).toBeNull()
  })

  it('keeps the current brief visible and reports a recoverable error when refresh fails', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response(baseBrief))
      .mockImplementationOnce(() => response({ error: { code: 'REQUEST_FAILED', message: 'Refresh temporarily unavailable' } }, 500))
    vi.stubGlobal('fetch', fetch)
    renderBrief()

    await screen.findByText('Board sync')
    fireEvent.click(screen.getByRole('button', { name: 'Refresh brief' }))

    await screen.findByRole('alert')
    expect(screen.getByRole('alert').textContent).toContain('Refresh temporarily unavailable')
    expect(screen.getByText('Board sync')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Refresh brief' }) as HTMLButtonElement).disabled).toBe(false)
  })
})
