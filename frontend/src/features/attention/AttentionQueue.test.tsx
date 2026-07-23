// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AttentionQueue, { groupOf, type AttentionItem } from './AttentionQueue'

function item(overrides: Partial<AttentionItem>): AttentionItem {
  return {
    id: 'item-1',
    entity_type: 'task',
    entity_id: 'task-1',
    source_entity_version: 1,
    score: 60,
    confidence: 0.9,
    factors: [{ code: 'overdue', label: 'Overdue by 2 days', points: 35 }],
    explanation: 'Finish the board memo',
    generated_at: '2026-07-20T00:00:00Z',
    expires_at: '2026-07-21T00:00:00Z',
    pinned: false,
    dismissed_at: null,
    dismissed_entity_version: null,
    deferred_until: null,
    policy_version: 1,
    override_reason: null,
    ...overrides,
  }
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderQueue() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(<QueryClientProvider client={client}><AttentionQueue /></QueryClientProvider>)
  return { client, ...utils }
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=attention-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'attention-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('groupOf', () => {
  it('groups deferred items as safely deferred regardless of entity_type', () => {
    expect(groupOf(item({ deferred_until: '2026-08-01T00:00:00Z', entity_type: 'risk' }))).toBe('safely_deferred')
  })
  it('groups waiting_link items as waiting on others', () => {
    expect(groupOf(item({ entity_type: 'waiting_link' }))).toBe('waiting_on_others')
  })
  it('groups risk and risk_review items as risks', () => {
    expect(groupOf(item({ entity_type: 'risk' }))).toBe('risks')
    expect(groupOf(item({ entity_type: 'risk_review' }))).toBe('risks')
  })
  it('groups meeting items as upcoming meetings', () => {
    expect(groupOf(item({ entity_type: 'meeting' }))).toBe('upcoming_meetings')
  })
  it('groups task and commitment items as needs action', () => {
    expect(groupOf(item({ entity_type: 'task' }))).toBe('needs_action')
    expect(groupOf(item({ entity_type: 'commitment' }))).toBe('needs_action')
  })
})

describe('AttentionQueue', () => {
  it('shows the loading state, then groups items and renders reason/confidence/freshness/evidence', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [item({})] })))
    renderQueue()

    expect(screen.getByRole('status').textContent).toMatch(/Loading attention queue/)
    await waitFor(() => expect(screen.getByText('Finish the board memo')).toBeTruthy())
    expect(screen.getByText(/confidence 90%/)).toBeTruthy()
    expect(screen.getByText(/1 evidence factor/)).toBeTruthy()
    expect(screen.getByLabelText('Score (secondary to the reason above)').textContent).toBe('60')
  })

  it('shows a distinct error state, never the empty-state text, when the fetch fails', async () => {
    // The query passes retry: 1, overriding the QueryClient's own retry:
    // false default -- mockImplementation (not Once) so the retried attempt
    // also fails. React Query's default backoff delays that retry by ~1s,
    // so this needs a longer-than-default findByRole timeout (matching
    // ResolutionInbox.test.tsx's identical retry:1 pattern).
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'REQUEST_FAILED', message: 'boom' } }, 500)))
    renderQueue()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toMatch(/boom/)
    expect(screen.queryByText('Nothing in this group right now.')).toBeNull()
  })

  it('dismisses an item and invalidates the dashboard and morning brief caches', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [item({})] }))
      .mockImplementationOnce(() => response(item({ dismissed_at: '2026-07-21T00:00:00Z' })))
      .mockImplementationOnce(() => response({ items: [item({ dismissed_at: '2026-07-21T00:00:00Z' })] }))
    vi.stubGlobal('fetch', fetch)
    const { client } = renderQueue()
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries')

    await waitFor(() => expect(screen.getByText('Finish the board memo')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss Finish the board memo' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey)
    expect(invalidatedKeys).toContainEqual(['attention'])
    expect(invalidatedKeys).toContainEqual(['dashboard', 'today'])
    expect(invalidatedKeys).toContainEqual(['brief', 'morning'])
  })

  it('offers a restore action for dismissed items with their override reason visible', async () => {
    vi.stubGlobal('fetch', vi.fn(() =>
      response({ items: [item({ dismissed_at: '2026-07-21T00:00:00Z', override_reason: 'Already resolved offline' })] }),
    ))
    renderQueue()

    await waitFor(() => expect(screen.getByText('Already resolved offline')).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Restore Finish the board memo' })).toBeTruthy()
  })

  it('offers a working restore action for deferred-but-not-dismissed items too', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [item({ deferred_until: '2026-08-01T00:00:00Z' })] }))
      .mockImplementationOnce(() => response(item({})))
      .mockImplementationOnce(() => response({ items: [item({})] }))
    vi.stubGlobal('fetch', fetch)
    renderQueue()

    await waitFor(() => expect(screen.getByText('Dismissed or deferred (reversible)')).toBeTruthy())
    const restoreButton = screen.getByRole('button', { name: 'Restore Finish the board memo' })
    expect(restoreButton).toBeTruthy()

    fireEvent.click(restoreButton)

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(fetch.mock.calls[1][0]).toContain('/restore')
  })
})
