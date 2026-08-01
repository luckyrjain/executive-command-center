// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import InsightsPanel from './InsightsPanel'
import type { Insight } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function insight(overrides: Partial<Insight> = {}): Insight {
  return {
    id: 'insight-1',
    domain_key: 'habits',
    kind: 'observation',
    title: 'Streak update',
    evidence: {},
    source_period_start: '2026-07-20T00:00:00Z',
    source_period_end: '2026-07-27T00:00:00Z',
    missing_data: null,
    confidence: 'medium',
    limitations: 'Based only on records shown here.',
    professional_referral_note: null,
    computed_at: '2026-07-27T00:00:00Z',
    dismissed_at: null,
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><InsightsPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=personal-insights-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'personal-insights-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('InsightsPanel', () => {
  it('shows the empty state when no insights exist', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ insights: [] })))
    renderPanel()
    expect(await screen.findByText('No insights yet -- capture a few records first.')).toBeTruthy()
  })

  it('shows a plain insight with no boundary note', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ insights: [insight()] })))
    renderPanel()
    expect(await screen.findByText('Streak update')).toBeTruthy()
    expect(screen.queryByText(/Consider discussing/)).toBeNull()
  })

  it('shows the sensitive-insight boundary note near a high_stakes-sourced insight', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      insights: [insight({
        domain_key: 'health',
        title: 'Resting heart rate trend',
        professional_referral_note: 'Consider discussing this with your doctor.',
      })],
    })))
    renderPanel()
    expect(await screen.findByText('Consider discussing this with your doctor.')).toBeTruthy()
  })

  it('shows missing_data as an incomplete-data notice', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ insights: [insight({ missing_data: 'no check-ins this week' })] })))
    renderPanel()
    expect(await screen.findByText(/Incomplete data: no check-ins this week/)).toBeTruthy()
  })

  it('dismisses an insight', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes('/dismiss')) return response(insight({ dismissed_at: '2026-07-27T01:00:00Z' }))
      return response({ insights: [insight()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/insights/insight-1/dismiss'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('does not offer Dismiss for an already-dismissed insight', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ insights: [insight({ dismissed_at: '2026-07-27T01:00:00Z' })] })))
    renderPanel()
    await screen.findByText('Streak update')
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull()
    expect(screen.getByText(/Dismissed/)).toBeTruthy()
  })

  it('submits feedback for an insight', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/feedback')) return response({ id: 'fb-1', insight_id: 'insight-1', useful: true, comment: null, created_at: '2026-07-27T01:00:00Z' })
      return response({ insights: [insight()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: 'Useful' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/insights/insight-1/feedback'),
      expect.objectContaining({ method: 'POST' }),
    ))
    expect(await screen.findByText('Thanks for the feedback.')).toBeTruthy()
  })

  it('generates an insight from the selected source domains', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/insights/generate')) return response({ available: true, insight: insight({ id: 'insight-2', title: 'New trend' }), error_code: null })
      return response({ insights: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No insights yet -- capture a few records first.')
    fireEvent.click(screen.getByRole('checkbox', { name: /Habits/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Generate insight' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/insights/generate'),
      expect.objectContaining({ method: 'POST' }),
    ))
    const call = fetch.mock.calls.find((c) => String(c[0]).includes('/insights/generate'))
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({ source_domain_keys: ['habits'] })
    expect(await screen.findByText('A new insight was generated below.')).toBeTruthy()
  })

  it('shows the fail-open unavailable message with its error_code', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/insights/generate')) return response({ available: false, insight: null, error_code: 'grounding_failed' })
      return response({ insights: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No insights yet -- capture a few records first.')
    fireEvent.click(screen.getByRole('checkbox', { name: /Health/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Generate insight' }))

    expect(await screen.findByText(/No insight available right now \(grounding_failed\)/)).toBeTruthy()
  })
})
