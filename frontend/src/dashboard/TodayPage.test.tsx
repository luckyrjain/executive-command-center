// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import TodayPage from './TodayPage'

beforeEach(() => {
  document.cookie = 'ecc_csrf=today-token; Secure; SameSite=Strict'
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><TodayPage /></QueryClientProvider>)
}

describe('TodayPage', () => {
  it('shows the loading state, then the dashboard heading once data resolves', async () => {
    const dashboardResponse = {
      date: '2026-09-11', timezone: 'Asia/Kolkata', generated_at: '2026-09-11T00:00:00Z', stale: false,
      sections: { top_priorities: [], today_schedule: [], overdue_commitments: [], risks: [], waiting_on: [], recently_changed: [] },
    }
    const briefResponse = {
      id: 'brief-1', briefing_date: '2026-09-11', generation_version: 1,
      sections: { top_priorities: [], today_schedule: [], overdue_commitments: [], risks: [] },
      source_versions: {}, evidence_ids: [], generated_at: '2026-09-11T00:00:00Z',
      timezone: 'Asia/Kolkata', algorithm_version: 'v1', ai_status: 'available', stale: false,
    }
    const fetchMock = vi.fn()
      .mockImplementationOnce(() => Promise.resolve(new Response(JSON.stringify(dashboardResponse), { status: 200, headers: { 'Content-Type': 'application/json' } })))
      .mockImplementationOnce(() => Promise.resolve(new Response(JSON.stringify(briefResponse), { status: 200, headers: { 'Content-Type': 'application/json' } })))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'test-uuid') })

    renderPage()
    expect(screen.getByText('Loading today\'s command center…')).toBeTruthy()
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Today' })).toBeTruthy())
  })

  it('renders the page header even when there is an error', async () => {
    const errorResponse = { error: { code: 'INTERNAL', message: 'boom' } }
    vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(new Response(JSON.stringify(errorResponse), { status: 500, headers: { 'Content-Type': 'application/json' } }))))
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'test-uuid') })

    renderPage()
    // Even if queries fail, the page header should still render
    await waitFor(() => {
      expect(screen.getByRole('heading', { name: 'Today' })).toBeTruthy()
    }, { timeout: 500 })
  })
})
