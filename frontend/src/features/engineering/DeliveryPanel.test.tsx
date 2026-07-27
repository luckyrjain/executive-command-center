// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('DeliveryPanel', () => {
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
