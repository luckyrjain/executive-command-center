// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import RiskReviewQueue, { type ReviewQueueItem } from './RiskReviewQueue'

const overdueItem: ReviewQueueItem = {
  risk_id: 'risk-1',
  description: 'Vendor concentration risk',
  status: 'monitoring',
  review_at: '2026-07-01T00:00:00Z',
  urgency: 'overdue',
  version: 3,
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderQueue() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(<QueryClientProvider client={client}><RiskReviewQueue /></QueryClientProvider>)
  return { client, ...utils }
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=risk-review-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'risk-review-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('RiskReviewQueue', () => {
  it('renders queued risks with neutral, non-alarming urgency copy, never a shame-toned label', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [overdueItem] })))
    renderQueue()

    await waitFor(() => expect(screen.getByText('Vendor concentration risk')).toBeTruthy())
    expect(screen.getByText(/Review overdue/)).toBeTruthy()
    expect(screen.queryByText(/you failed/i)).toBeNull()
    expect(screen.queryByText(/you are behind/i)).toBeNull()
  })

  it('shows the empty state when nothing is due for review', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [] })))
    renderQueue()

    await waitFor(() => expect(screen.getByText('No risks are due for review.')).toBeTruthy())
  })

  it('records a review and invalidates the risks and dashboard caches', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [overdueItem] }))
      .mockImplementationOnce(() => response({ id: 'review-1', risk_id: 'risk-1', outcome: 'mitigated', notes: null, evidence_refs: [], reviewed_at: '2026-07-23T00:00:00Z', next_review_at: null, actor_id: 'user-1' }))
      .mockImplementationOnce(() => response({ items: [] }))
    vi.stubGlobal('fetch', fetch)
    const { client } = renderQueue()
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries')

    await waitFor(() => expect(screen.getByText('Vendor concentration risk')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Record review for Vendor concentration risk' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey)
    expect(invalidatedKeys).toContainEqual(['risk-review-queue'])
    expect(invalidatedKeys).toContainEqual(['risks'])
    expect(invalidatedKeys).toContainEqual(['dashboard', 'today'])
    expect(invalidatedKeys).toContainEqual(['brief', 'morning'])
  })

  it('submits the queue item\'s real version as expected_version, never a hardcoded or user-edited value', async () => {
    const highVersionItem: ReviewQueueItem = { ...overdueItem, risk_id: 'risk-2', description: 'Key-person dependency risk', version: 17 }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [highVersionItem] }))
      .mockImplementationOnce(() => response({ id: 'review-2', risk_id: 'risk-2', outcome: 'no_change', notes: null, evidence_refs: [], reviewed_at: '2026-07-23T00:00:00Z', next_review_at: null, actor_id: 'user-1' }))
      .mockImplementationOnce(() => response({ items: [] }))
    vi.stubGlobal('fetch', fetch)
    renderQueue()

    await waitFor(() => expect(screen.getByText('Key-person dependency risk')).toBeTruthy())
    // The form must not expose an editable "Expected version" field -- the
    // version is carried from the queue item in state and submitted as-is.
    expect(screen.queryByLabelText('Expected version')).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Record review for Key-person dependency risk' }))
    expect(screen.queryByLabelText('Expected version')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Save review' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(JSON.parse(String(fetch.mock.calls[1][1]?.body))).toMatchObject({ expected_version: 17, outcome: 'no_change' })
  })
})
