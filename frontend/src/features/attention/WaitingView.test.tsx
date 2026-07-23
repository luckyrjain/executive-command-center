// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WaitingView, { validateDraft, type Draft, type WaitingLink } from './WaitingView'

const link: WaitingLink = {
  id: 'wl-1',
  subject_type: 'task',
  subject_id: 'task-1',
  counterparty_entity_id: 'entity-1',
  direction: 'waiting_on_them',
  status: 'open',
  note: 'Waiting on vendor signature',
  since_at: '2026-07-01T00:00:00Z',
  expected_at: '2026-07-15T00:00:00Z',
  superseded_by: null,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
  version: 1,
}

const validDraft: Draft = {
  subjectType: 'task',
  subjectId: 'task-1',
  counterpartyEntityId: 'entity-1',
  direction: 'waiting_on_them',
  note: '',
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(<QueryClientProvider client={client}><WaitingView /></QueryClientProvider>)
  return { client, ...utils }
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=waiting-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'waiting-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('validateDraft', () => {
  it('requires a subject ID and a counterparty entity ID', () => {
    expect(validateDraft({ ...validDraft, subjectId: ' ' })).toMatch(/subject/i)
    expect(validateDraft({ ...validDraft, counterpartyEntityId: '' })).toMatch(/counterparty/i)
    expect(validateDraft(validDraft)).toBeNull()
  })
})

describe('WaitingView', () => {
  it('renders open waiting items with direction, since, expected and note', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [link], next_cursor: null })))
    renderView()

    await waitFor(() => expect(screen.getByText('Waiting on vendor signature')).toBeTruthy())
    expect(screen.getAllByText('waiting on them').length).toBeGreaterThan(0)
  })

  it('shows the empty state when there are no open items', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [], next_cursor: null })))
    renderView()

    await waitFor(() => expect(screen.getByText('Nothing is currently waiting.')).toBeTruthy())
  })

  it('rejects creating a link that would form a cycle with a plain-language message', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
      .mockImplementationOnce(() => response({ error: { code: 'INVALID_WAITING_DIRECTION', message: 'cycle' } }, 422))
    vi.stubGlobal('fetch', fetch)
    renderView()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Record waiting item' })).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Subject ID'), { target: { value: 'task-1' } })
    fireEvent.change(screen.getByLabelText('Counterparty entity ID'), { target: { value: 'entity-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Record waiting item' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/dependency cycle/)
  })

  it('fulfils an item and invalidates the dashboard and morning brief caches', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [link], next_cursor: null }))
      .mockImplementationOnce(() => response({ ...link, status: 'fulfilled' }))
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    const { client } = renderView()
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries')

    await waitFor(() => expect(screen.getByText('Waiting on vendor signature')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Fulfil waiting item wl-1' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey)
    expect(invalidatedKeys).toContainEqual(['waiting'])
    expect(invalidatedKeys).toContainEqual(['dashboard', 'today'])
    expect(invalidatedKeys).toContainEqual(['brief', 'morning'])
  })
})
