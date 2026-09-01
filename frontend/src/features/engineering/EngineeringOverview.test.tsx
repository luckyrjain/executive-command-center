// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import EngineeringOverview from './EngineeringOverview'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function emptyResponseFor(url: string) {
  if (url.includes('/connectors')) return response({ connectors: [] })
  if (url.includes('/incidents')) return response({ incidents: [] })
  if (url.includes('/decisions')) return response({ decisions: [] })
  if (url.includes('/metrics')) return response({ metrics: [] })
  return response({}, 404)
}

function renderOverview(onNavigate = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><EngineeringOverview onNavigate={onNavigate} /></QueryClientProvider>)
  return onNavigate
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=overview-token; Secure; SameSite=Strict'
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('EngineeringOverview', () => {
  it('sends the CSRF token on its own metrics fetch even though it is a GET request', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, _init?: RequestInit) => emptyResponseFor(String(input)))
    vi.stubGlobal('fetch', fetch)
    renderOverview()
    await screen.findByText('Headline metrics')
    const metricsCall = fetch.mock.calls.find((c) => String(c[0]).includes('/metrics'))
    expect(metricsCall).toBeTruthy()
    expect((metricsCall?.[1] as RequestInit | undefined)?.headers).toMatchObject({ 'X-CSRF-Token': 'overview-token' })
  })

  it('summarizes connectors, incidents, decisions and headline metrics', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/connectors')) return response({ connectors: [{ id: 'c1', provider: 'github', external_account_id: 'x', display_name: 'Acme', granted_scopes: [], status: 'permission_lost', status_detail: null, last_synced_at: null, last_error: null, disconnected_at: null, version: 1, created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z' }] })
      if (url.includes('/incidents')) return response({ incidents: [{ id: 'i1', title: 'Outage', description: null, severity: 'high', status: 'open', detected_at: '2026-07-01T00:00:00Z', resolved_at: null, change_ids: [], version: 1, created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z' }] })
      if (url.includes('/decisions')) return response({ decisions: [] })
      if (url.includes('/metrics')) return response({ metrics: [{ id: 'm1', metric_key: 'delivery_frequency', window_label: '30d', population: 1, numerator: 1, denominator: 30, value: 0.03, details: null, coverage_status: 'complete', coverage_percentage: 100, coverage_gap_description: null, computed_at: '2026-07-27T00:00:00Z' }] })
      return response({}, 404)
    }))
    renderOverview()

    expect(await screen.findByText('1 connected. 1 need attention.')).toBeTruthy()
    expect(await screen.findByText('1 open.')).toBeTruthy()
    expect(await screen.findByText('0 awaiting a decision.')).toBeTruthy()
    expect(await screen.findByText(/Delivery frequency \(30d\): 0.03/)).toBeTruthy()
  })

  it('renders time_to_restore in the headline metrics converted from raw seconds to days, not a bare seconds count', async () => {
    // `_compute_time_to_restore` stores a raw `.total_seconds()` median with
    // no day conversion -- this headline list must apply the same
    // seconds-to-days conversion as `MetricCard`'s own `formatValue`, not
    // render the raw number directly (the bug round-2 correctness review
    // found in this exact list).
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/metrics')) {
        return response({
          metrics: [
            { id: 'm2', metric_key: 'time_to_restore', window_label: '30d', population: 3, numerator: null, denominator: null, value: 14400, details: null, coverage_status: 'complete', coverage_percentage: 100, coverage_gap_description: null, computed_at: '2026-07-27T00:00:00Z' },
          ],
        })
      }
      return emptyResponseFor(url)
    }))
    renderOverview()
    expect(await screen.findByText(/Time to restore \(30d\): 0\.2 days/)).toBeTruthy()
    expect(screen.queryByText(/14400/)).toBeNull()
  })

  it('calls onNavigate with the correct view when a quick-jump button is clicked', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => emptyResponseFor(String(input))))
    const onNavigate = renderOverview()

    fireEvent.click(await screen.findByRole('button', { name: 'View connector health' }))
    expect(onNavigate).toHaveBeenCalledWith('connector-health')

    fireEvent.click(screen.getByRole('button', { name: 'View incidents' }))
    expect(onNavigate).toHaveBeenCalledWith('incidents')

    fireEvent.click(screen.getByRole('button', { name: 'View decisions' }))
    expect(onNavigate).toHaveBeenCalledWith('decisions')

    fireEvent.click(screen.getByRole('button', { name: 'View delivery' }))
    expect(onNavigate).toHaveBeenCalledWith('delivery')

    fireEvent.click(screen.getByRole('button', { name: 'View reliability' }))
    expect(onNavigate).toHaveBeenCalledWith('reliability')
  })

  it('reports all healthy when no connector needs attention', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/connectors')) return response({ connectors: [{ id: 'c1', provider: 'github', external_account_id: 'x', display_name: 'Acme', granted_scopes: [], status: 'active', status_detail: null, last_synced_at: null, last_error: null, disconnected_at: null, version: 1, created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z' }] })
      return emptyResponseFor(url)
    }))
    renderOverview()
    expect(await screen.findByText('1 connected. All healthy.')).toBeTruthy()
  })

  it('does not count a disconnected connector as connected or needing attention', async () => {
    // disable_connector_endpoint never deletes the row, only flips status
    // to 'disconnected' -- such rows persist in GET /connectors forever,
    // and a deliberately-disconnected account is neither "connected" (it
    // provides no data) nor "needing attention" (nothing is wrong, its
    // owner already acted).
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/connectors')) {
        return response({
          connectors: [
            { id: 'c1', provider: 'github', external_account_id: 'x', display_name: 'Acme', granted_scopes: [], status: 'active', status_detail: null, last_synced_at: null, last_error: null, disconnected_at: null, version: 1, created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z' },
            { id: 'c2', provider: 'gitlab', external_account_id: 'y', display_name: 'Old GitLab', granted_scopes: [], status: 'disconnected', status_detail: null, last_synced_at: null, last_error: null, disconnected_at: '2026-07-20T00:00:00Z', version: 2, created_at: '2026-06-01T00:00:00Z', updated_at: '2026-07-20T00:00:00Z' },
          ],
        })
      }
      return emptyResponseFor(url)
    }))
    renderOverview()
    expect(await screen.findByText('1 connected. All healthy.')).toBeTruthy()
  })

  it('surfaces a connector-list load failure as an alert', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/connectors')) return Promise.reject(new TypeError('fetch failed'))
      return emptyResponseFor(url)
    }))
    renderOverview()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
  })

  // A whole-phase test-coverage review found only the connectors query's
  // own `isError` path was exercised above -- the identical pattern
  // (independent isError block per query) on incidents/decisions/metrics
  // had zero coverage, the same "3 of 4 untested" bug class an earlier
  // review already found and fixed once for a different panel set
  // (ConnectorHealthPanel/IncidentsPanel/DecisionsPanel's own mutations).
  // Each test below asserts the OTHER three sections still render their
  // real data, proving the failure is scoped to its own query, not just
  // that some alert appears somewhere.
  it('surfaces an incidents load failure as its own alert, without hiding connector/decision/metric data', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/incidents')) return Promise.reject(new TypeError('fetch failed'))
      if (url.includes('/decisions')) return response({ decisions: [] })
      return emptyResponseFor(url)
    }))
    renderOverview()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(await screen.findByText('0 connected. All healthy.')).toBeTruthy()
    expect(await screen.findByText('0 awaiting a decision.')).toBeTruthy()
    expect(screen.queryByText(/open\./)).toBeNull()
  })

  it('surfaces a decisions load failure as its own alert, without hiding connector/incident/metric data', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/decisions')) return Promise.reject(new TypeError('fetch failed'))
      return emptyResponseFor(url)
    }))
    renderOverview()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(await screen.findByText('0 connected. All healthy.')).toBeTruthy()
    expect(await screen.findByText('0 open.')).toBeTruthy()
    expect(screen.queryByText(/awaiting a decision\./)).toBeNull()
  })

  it('surfaces a metrics load failure as its own alert, without hiding connector/incident/decision data', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/metrics')) return Promise.reject(new TypeError('fetch failed'))
      return emptyResponseFor(url)
    }))
    renderOverview()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(await screen.findByText('0 connected. All healthy.')).toBeTruthy()
    expect(await screen.findByText('0 open.')).toBeTruthy()
    expect(await screen.findByText('0 awaiting a decision.')).toBeTruthy()
  })
})
