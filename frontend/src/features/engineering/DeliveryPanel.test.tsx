// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DeliveryPanel from './DeliveryPanel'
import type { MetricSnapshot } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function metric(overrides: Partial<MetricSnapshot> = {}): MetricSnapshot {
  return {
    id: 'metric-1',
    metric_key: 'delivery_frequency',
    window_label: '30d',
    population: 10,
    numerator: 5,
    denominator: 30,
    value: 0.17,
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
  return render(<QueryClientProvider client={client}><DeliveryPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=delivery-panel-token; Secure; SameSite=Strict'
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('DeliveryPanel', () => {
  it('sends the CSRF token even though this is a GET request', async () => {
    // GET /engineering/metrics has a real server-side side effect (writes
    // fresh snapshot rows every call) and requires CsrfDep on the backend
    // despite being a safe HTTP method -- confirms the frontend actually
    // attaches the header this GET needs, not just that the panel renders.
    const fetch = vi.fn(() => response({ metrics: [metric()] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()
    await screen.findByText('Delivery frequency')
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/engineering/metrics'),
      expect.objectContaining({ headers: expect.objectContaining({ 'X-CSRF-Token': 'delivery-panel-token' }) }),
    )
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
  })

  it('converts review_latency\'s raw-seconds value into days, never showing the raw seconds figure', async () => {
    // `_compute_review_latency` (backend/ecc/domains/engineering/metrics.py)
    // stores a raw `.total_seconds()` median with no day conversion --
    // 14400 seconds is a real 4-hour review latency, which must render as
    // "0.2 days", never as "14400.0 days" (the bug this test guards).
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [metric({ metric_key: 'review_latency', value: 14400 })],
    })))
    renderPanel()
    expect(await screen.findByText('0.2 days')).toBeTruthy()
    expect(screen.queryByText(/14400/)).toBeNull()
  })

  it('renders work_ageing\'s value as a median age in days, not an item count', async () => {
    // `_compute_work_ageing`'s `value` is a median AGE IN DAYS (already
    // divided by 86400 on the backend) -- distinct from `blocked_work`'s
    // `value`, which is a plain count of blocked items. The two must not
    // be formatted identically.
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [metric({ metric_key: 'work_ageing', value: 5.5, population: 12 })],
    })))
    renderPanel()
    expect(await screen.findByText('5.5 days')).toBeTruthy()
  })

  it('renders blocked_work\'s value as an item count, not a duration', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [metric({ metric_key: 'blocked_work', value: 3, population: 10 })],
    })))
    renderPanel()
    expect(await screen.findByText('3 items')).toBeTruthy()
  })

  it('renders change_failure_rate as a percentage, converting the raw 0-1 fraction', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [metric({ metric_key: 'change_failure_rate', value: 0.235, population: 20 })],
    })))
    renderPanel()
    expect(await screen.findByText('23.5%')).toBeTruthy()
  })

  it('only shows delivery/flow metrics, not time_to_restore', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [
        metric({ id: 'm1', metric_key: 'delivery_frequency' }),
        metric({ id: 'm2', metric_key: 'time_to_restore' }),
      ],
    })))
    renderPanel()
    expect(await screen.findByText('Delivery frequency')).toBeTruthy()
    expect(screen.queryByText('Time to restore')).toBeNull()
  })

  it('always shows the metric definition, window, and coverage together', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ metrics: [metric()] })))
    renderPanel()
    await screen.findByText('Delivery frequency')
    expect(screen.getByText('30d')).toBeTruthy()
    expect(screen.getByText(/Count of successful deployments/)).toBeTruthy()
    expect(screen.getByText(/Coverage: complete \(100%\)/)).toBeTruthy()
  })

  it('shows the coverage gap description for a partial-coverage metric', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [metric({ coverage_status: 'partial', coverage_percentage: 62, coverage_gap_description: 'GitLab backfill 62% complete' })],
    })))
    renderPanel()
    expect(await screen.findByText(/Coverage: partial \(62%\)/)).toBeTruthy()
    expect(screen.getByText(/GitLab backfill 62% complete/)).toBeTruthy()
  })

  it('shows insufficient-coverage metrics without a numeric value, never a misleading number', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [metric({ value: null, coverage_status: 'insufficient_coverage', coverage_percentage: 20 })],
    })))
    renderPanel()
    await screen.findByText('Delivery frequency')
    expect(screen.getByText('not yet available')).toBeTruthy()
    expect(screen.getByText(/no number is shown to avoid a misleading value/)).toBeTruthy()
  })

  it('exposes evidence (population/numerator/denominator) behind a drill-down', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ metrics: [metric({ population: 42, numerator: 7, denominator: 30 })] })))
    renderPanel()
    fireEvent.click(await screen.findByText('Evidence'))
    expect(screen.getByText('42')).toBeTruthy()
    expect(screen.getByText('7')).toBeTruthy()
  })

  it('never renders a per-person breakdown anywhere in a metric card', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ metrics: [metric()] })))
    renderPanel()
    await screen.findByText('Delivery frequency')
    expect(screen.queryByText(/person|engineer|leaderboard|rank/i)).toBeNull()
  })
})
