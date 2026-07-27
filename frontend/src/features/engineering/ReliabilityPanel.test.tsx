// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ReliabilityPanel from './ReliabilityPanel'
import type { MetricSnapshot } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function metric(overrides: Partial<MetricSnapshot> = {}): MetricSnapshot {
  return {
    id: 'metric-1',
    metric_key: 'time_to_restore',
    window_label: '30d',
    population: 4,
    numerator: 4,
    denominator: null,
    // `_compute_time_to_restore` (backend/ecc/domains/engineering/metrics.py)
    // stores a raw `.total_seconds()` median, never pre-converted to days --
    // 14400 seconds is a real 4-hour time-to-restore.
    value: 14400,
    details: null,
    coverage_status: 'complete',
    coverage_percentage: 100,
    coverage_gap_description: null,
    computed_at: '2026-07-27T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><ReliabilityPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=reliability-panel-token; Secure; SameSite=Strict'
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ReliabilityPanel', () => {
  it('sends the CSRF token even though this is a GET request', async () => {
    const fetch = vi.fn(() => response({ metrics: [metric()] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()
    await screen.findByText('Time to restore')
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/engineering/metrics'),
      expect.objectContaining({ headers: expect.objectContaining({ 'X-CSRF-Token': 'reliability-panel-token' }) }),
    )
  })

  it('only shows time_to_restore, not delivery/flow metrics', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [metric(), { ...metric(), id: 'm2', metric_key: 'delivery_frequency' }],
    })))
    renderPanel()
    expect(await screen.findByText('Time to restore')).toBeTruthy()
    expect(screen.queryByText('Delivery frequency')).toBeNull()
  })

  it('converts the raw-seconds value into days, never showing the raw seconds figure', async () => {
    // 14400 seconds (a real 4-hour time-to-restore) must render as "0.2
    // days", never as "14400.0 days" -- the bug this test guards.
    vi.stubGlobal('fetch', vi.fn(() => response({ metrics: [metric({ value: 14400 })] })))
    renderPanel()
    await screen.findByText('Time to restore')
    expect(screen.getByText('0.2 days')).toBeTruthy()
    expect(screen.queryByText(/14400/)).toBeNull()
    expect(screen.getByText(/Coverage: complete/)).toBeTruthy()
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
  })

  it('discloses the still-blocked delivery metrics rather than hiding the gap', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ metrics: [] })))
    renderPanel()
    expect(await screen.findByText(/still require a deployments source/)).toBeTruthy()
  })
})
