// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

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
    value: 2.5,
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

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ReliabilityPanel', () => {
  it('only shows time_to_restore, not delivery/flow metrics', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      metrics: [metric(), { ...metric(), id: 'm2', metric_key: 'delivery_frequency' }],
    })))
    renderPanel()
    expect(await screen.findByText('Time to restore')).toBeTruthy()
    expect(screen.queryByText('Delivery frequency')).toBeNull()
  })

  it('shows the metric value in days with its own coverage', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ metrics: [metric({ value: 2.5 })] })))
    renderPanel()
    await screen.findByText('Time to restore')
    expect(screen.getByText('2.5 days')).toBeTruthy()
    expect(screen.getByText(/Coverage: complete/)).toBeTruthy()
  })

  it('discloses the still-blocked delivery metrics rather than hiding the gap', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ metrics: [] })))
    renderPanel()
    expect(await screen.findByText(/still require a deployments source/)).toBeTruthy()
  })
})
