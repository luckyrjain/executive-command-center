// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, fireEvent, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from './api/client'
import RecommendationPanel, {
  actionPayload,
  actionSummary,
  confidenceLabel,
  recommendationErrorMessage,
  type Recommendation,
} from './RecommendationPanel'

const recommendation: Recommendation = {
  id: 'rec-1',
  recommendation_type: 'complete_task',
  target_type: 'task',
  target_id: 'task-1',
  proposed_action: { operation: 'complete_task', completed: true },
  expected_version: 7,
  rationale: 'The task is complete.',
  confidence: 0.876,
  status: 'pending_confirmation',
  evidence_ids: [],
  source: 'rule',
  pinned: false,
  version: 3,
}

describe('recommendation action payloads', () => {
  it('binds confirmation to recommendation and target versions', () => {
    expect(actionPayload(recommendation, 'confirm')).toEqual({
      expected_version: 3,
      target_expected_version: 7,
    })
  })

  it('toggles pin state using optimistic versioning', () => {
    expect(actionPayload(recommendation, 'pin')).toEqual({ expected_version: 3, pinned: true })
    expect(actionPayload({ ...recommendation, pinned: true }, 'pin')).toEqual({
      expected_version: 3,
      pinned: false,
    })
  })

  it('defers for approximately 24 hours', () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-07-15T09:00:00.000Z'))
    expect(actionPayload(recommendation, 'defer')).toEqual({
      expected_version: 3,
      defer_until: '2026-07-16T09:00:00.000Z',
    })
    vi.useRealTimers()
  })
})

describe('recommendation presentation', () => {
  it('renders action summaries and confidence consistently', () => {
    expect(actionSummary(recommendation.proposed_action)).toBe('complete task · completed')
    expect(confidenceLabel(recommendation.confidence)).toBe('88% confidence')
  })

  it('turns version conflicts into a reload-safe message', () => {
    const conflict = new ApiError(409, 'TARGET_VERSION_CONFLICT', 'Conflict')
    expect(recommendationErrorMessage(conflict)).toContain('latest version has been reloaded')
    expect(recommendationErrorMessage(new Error('Network unavailable'))).toBe('Network unavailable')
  })
})

const riskRecommendation = {
  id: 'rec-risk-1',
  recommendation_type: 'close_risk',
  target_type: 'risk',
  target_id: 'risk-9',
  proposed_action: { operation: 'update_status', status: 'closed' },
  expected_version: 2,
  rationale: 'The vendor contract renewed on schedule.',
  confidence: 0.91,
  status: 'pending_confirmation',
  evidence_ids: ['evidence-1', 'evidence-2'],
  expires_at: null,
  confirmed_by: null,
  confirmed_at: null,
  execution_result: null,
  source: 'rule',
  pinned: false,
  deferred_until: null,
  version: 1,
}

const riskPreview = {
  id: 'risk-9',
  description: 'Vendor renewal may lapse',
  probability: 4,
  impact: 5,
  status: 'monitoring',
  owner_id: 'owner-1',
  mitigation: 'Confirm renewal terms',
  trigger: 'No signed contract',
  review_at: '2026-08-01T00:00:00Z',
  project_id: null,
  pinned: false,
  priority_impact: 20,
  score: 25,
  factors: [{ code: 'risk_impact', label: 'Risk impact 20', points: 25, source_field: 'probability,impact' }],
  explanation: 'Risk impact 20',
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-10T00:00:00Z',
  version: 2,
  archived_at: null,
  pre_archive_status: null,
}

function jsonResponse(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function fetchRouter(routes: Array<{ test: (url: string, init?: RequestInit) => boolean; respond: () => Promise<Response> }>) {
  return vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const route = routes.find((candidate) => candidate.test(url, init))
    if (!route) throw new Error(`Unhandled fetch: ${init?.method ?? 'GET'} ${url}`)
    return route.respond()
  })
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><RecommendationPanel /></QueryClientProvider>)
}

const isGet = (init?: RequestInit) => !init?.method || init.method === 'GET'

beforeEach(() => {
  document.cookie = 'ecc_csrf=recommendation-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'recommendation-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('recommendation preview (rendered)', () => {
  it('shows all reachable evidence states and risk-target factors before confirmation', async () => {
    const fetch = fetchRouter([
      { test: (url, init) => url.includes('/api/v1/recommendations?') && isGet(init), respond: () => jsonResponse({ items: [riskRecommendation], next_cursor: null }) },
      { test: (url) => url.includes('/api/v1/evidence?'), respond: () => jsonResponse({
        items: [
          { id: 'evidence-1', status: 'available', source_type: 'document', label: 'Renewal memo', captured_at: '2026-07-01T00:00:00Z' },
          { id: 'evidence-2', status: 'missing', source_type: null, label: null, captured_at: null },
        ],
      }) },
      { test: (url) => url.includes('/api/v1/risks/risk-9'), respond: () => jsonResponse(riskPreview) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('close risk')
    const evidence = await screen.findByLabelText('Evidence for close risk')
    expect(evidence.textContent).toContain('Renewal memo')
    expect(evidence.textContent).toContain('available')
    expect(evidence.textContent).toContain('missing')

    const factors = await screen.findByLabelText('Risk factors for close risk')
    expect(factors.textContent).toContain('Risk impact 20')
  })

  it('does not show factors or fetch the risk endpoint for non-risk targets', async () => {
    const taskRecommendation = { ...riskRecommendation, id: 'rec-task-1', target_type: 'task', target_id: 'task-1', evidence_ids: [] }
    const fetch = fetchRouter([
      { test: (url, init) => url.includes('/api/v1/recommendations?') && isGet(init), respond: () => jsonResponse({ items: [taskRecommendation], next_cursor: null }) },
    ])
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('close risk')
    expect(screen.queryByLabelText('Risk factors for close risk')).toBeNull()
    expect(fetch.mock.calls.some((call) => String(call[0]).includes('/api/v1/risks/'))).toBe(false)
  })

  it('suppresses all lifecycle actions once a recommendation reaches a terminal status', async () => {
    const executed = { ...riskRecommendation, id: 'rec-executed', status: 'executed', evidence_ids: [] }
    vi.stubGlobal('fetch', vi.fn(() => jsonResponse({ items: [executed], next_cursor: null })))
    renderPanel()

    await screen.findByText('close risk')
    expect(screen.queryByRole('button', { name: 'Publish for confirmation' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Confirm and execute' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reject' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Defer 24h' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Pin' })).toBeNull()
  })

  it('reloads the recommendation list after a target version conflict on confirm', async () => {
    const pending = { ...riskRecommendation, evidence_ids: [] }
    const conflictBody = { error: { code: 'TARGET_VERSION_CONFLICT', message: 'Target changed', details: {} } }
    let listCalls = 0
    const fetch = fetchRouter([
      {
        test: (url, init) => url.includes('/api/v1/recommendations?') && isGet(init),
        respond: () => { listCalls += 1; return jsonResponse({ items: [pending], next_cursor: null }) },
      },
      {
        test: (url, init) => url.includes('/confirm') && init?.method === 'POST',
        respond: () => jsonResponse(conflictBody, 409),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('close risk')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and execute' }))
    await waitFor(() => expect(listCalls).toBeGreaterThanOrEqual(2))
  })

  it('invalidates the dashboard and morning brief caches after confirming a recommendation', async () => {
    const pending = { ...riskRecommendation, evidence_ids: [] }
    const confirmed = { ...pending, status: 'executed', execution_result: { applied: true } }
    const fetch = fetchRouter([
      {
        test: (url, init) => url.includes('/api/v1/recommendations?') && isGet(init),
        respond: () => jsonResponse({ items: [pending], next_cursor: null }),
      },
      {
        test: (url, init) => url.includes('/confirm') && init?.method === 'POST',
        respond: () => jsonResponse(confirmed),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><RecommendationPanel /></QueryClientProvider>)

    await screen.findByText('close risk')
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and execute' }))

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalled())
    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey)
    expect(invalidatedKeys).toContainEqual(['recommendations', 'review'])
    expect(invalidatedKeys).toContainEqual(['dashboard', 'today'])
    expect(invalidatedKeys).toContainEqual(['brief', 'morning'])
  })

  it('does not invalidate the dashboard or morning brief caches for non-executing actions', async () => {
    const pending = { ...riskRecommendation, evidence_ids: [] }
    const rejected = { ...pending, status: 'rejected' }
    const fetch = fetchRouter([
      {
        test: (url, init) => url.includes('/api/v1/recommendations?') && isGet(init),
        respond: () => jsonResponse({ items: [pending], next_cursor: null }),
      },
      {
        test: (url, init) => url.includes('/reject') && init?.method === 'POST',
        respond: () => jsonResponse(rejected),
      },
    ])
    vi.stubGlobal('fetch', fetch)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><RecommendationPanel /></QueryClientProvider>)

    await screen.findByText('close risk')
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries')
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))

    await waitFor(() => expect(invalidateSpy).toHaveBeenCalled())
    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey)
    expect(invalidatedKeys).toContainEqual(['recommendations', 'review'])
    expect(invalidatedKeys).not.toContainEqual(['dashboard', 'today'])
    expect(invalidatedKeys).not.toContainEqual(['brief', 'morning'])
  })
})
