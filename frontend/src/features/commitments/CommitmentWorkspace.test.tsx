// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import CommitmentWorkspace from './CommitmentWorkspace'

const commitment = {
  id: 'commitment-1', summary: 'Send the revised forecast', description: null, direction: 'made_by_me',
  counterparty_name: 'Finance lead', status: 'detected', importance: 'high', due_date: null, due_at: null,
  version: 2, archived_at: null,
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(<QueryClientProvider client={client}><CommitmentWorkspace /></QueryClientProvider>)
  return { client, ...utils }
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=commitment-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'commitment-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('CommitmentWorkspace', () => {
  it('invalidates the dashboard and morning brief caches on mutation success', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
      .mockImplementationOnce(() => response(commitment, 201))
      .mockImplementationOnce(() => response({ items: [commitment], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    const { client } = renderWorkspace()
    const invalidateSpy = vi.spyOn(client, 'invalidateQueries')

    fireEvent.change(screen.getByLabelText('Commitment summary'), { target: { value: 'Send the revised forecast' } })
    fireEvent.change(screen.getByLabelText('Direction'), { target: { value: 'made_to_me' } })
    fireEvent.change(screen.getByLabelText('Counterparty name'), { target: { value: 'Finance lead' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create commitment' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const invalidatedKeys = invalidateSpy.mock.calls.map((call) => call[0]?.queryKey)
    expect(invalidatedKeys).toContainEqual(['dashboard', 'today'])
    expect(invalidatedKeys).toContainEqual(['brief', 'morning'])
  })

  it('creates with direction and permitted counterparty but no owner fields', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
      .mockImplementationOnce(() => response(commitment, 201))
      .mockImplementationOnce(() => response({ items: [commitment], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    fireEvent.change(screen.getByLabelText('Commitment summary'), { target: { value: 'Send the revised forecast' } })
    fireEvent.change(screen.getByLabelText('Direction'), { target: { value: 'made_to_me' } })
    fireEvent.change(screen.getByLabelText('Counterparty name'), { target: { value: 'Finance lead' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create commitment' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const init = fetch.mock.calls[1][1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({ summary: 'Send the revised forecast', direction: 'made_to_me', counterparty_name: 'Finance lead', status: 'confirmed' })
    expect(String(init.body)).not.toMatch(/owner|workspace|actor/)
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('commitment-token')
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('commitment-request-id')
  })

  it('offers lifecycle actions valid for detected, active, terminal, and archived states', async () => {
    const confirmed = { ...commitment, id: 'confirmed', summary: 'Confirmed promise', status: 'confirmed' }
    const active = { ...commitment, id: 'active', summary: 'Active promise', status: 'active' }
    const fulfilled = { ...commitment, id: 'fulfilled', summary: 'Finished promise', status: 'fulfilled' }
    const archived = { ...fulfilled, id: 'archived', summary: 'Archived promise', archived_at: '2026-07-16T10:00:00Z' }
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [commitment, confirmed, active, fulfilled, archived], next_cursor: null })))
    renderWorkspace()

    await screen.findByText('Send the revised forecast')
    expect(screen.getByRole('button', { name: 'Confirm Send the revised forecast' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Confirm Confirmed promise' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Fulfil Send the revised forecast' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Fulfil Active promise' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Cancel Finished promise' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Archive Finished promise' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Restore Archived promise' })).toBeTruthy()
  })

  it('preserves an edit and retries with the conflict current version', async () => {
    const current = { ...commitment, version: 3, summary: 'Changed elsewhere' }
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: {} } }
    const saved = { ...current, version: 4, summary: 'My revised promise' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [commitment], next_cursor: null }))
      .mockImplementationOnce(() => response(conflict, 409))
      .mockImplementationOnce(() => response(current))
      .mockImplementationOnce(() => response(saved))
      .mockImplementationOnce(() => response({ items: [saved], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Send the revised forecast')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Send the revised forecast' }))
    fireEvent.change(screen.getByLabelText('Edit commitment summary'), { target: { value: 'My revised promise' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save commitment' }))
    await screen.findByText(/changed while you were editing/i)
    expect((screen.getByLabelText('Edit commitment summary') as HTMLInputElement).value).toBe('My revised promise')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(fetch.mock.calls[2][0]).toContain('/api/v1/commitments/commitment-1')
    fireEvent.click(screen.getByRole('button', { name: 'Retry with latest version' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(JSON.parse(String((fetch.mock.calls[3][1] as RequestInit).body))).toMatchObject({ expected_version: 3, summary: 'My revised promise' })
  })

  it('edits and clears mutually exclusive commitment due precision', async () => {
    const dated = { ...commitment, status: 'confirmed', due_date: '2026-08-01' }
    const fetch = vi.fn().mockImplementationOnce(() => response({ items: [dated], next_cursor: null })).mockImplementationOnce(() => response({ ...dated, due_date: null, version: 3 })).mockImplementationOnce(() => response({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await screen.findByText('Send the revised forecast')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Send the revised forecast' }))
    const dueDate = screen.getByLabelText('Edit commitment due date') as HTMLInputElement
    const dueTime = screen.getByLabelText('Edit commitment due time') as HTMLInputElement
    expect(dueDate.value).toBe('2026-08-01')
    expect(dueTime.disabled).toBe(true)
    fireEvent.change(dueDate, { target: { value: '' } })
    expect(dueTime.disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Save commitment' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toMatchObject({ expected_version: 2, due_date: null, due_at: null })
  })

  it('confirms a confirmed commitment to activate it', async () => {
    const confirmed = { ...commitment, status: 'confirmed' }
    const active = { ...confirmed, status: 'active', version: 3 }
    const fetch = vi.fn().mockImplementationOnce(() => response({ items: [confirmed], next_cursor: null })).mockImplementationOnce(() => response(active)).mockImplementationOnce(() => response({ items: [active], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await screen.findByText('Send the revised forecast')
    fireEvent.click(screen.getByRole('button', { name: 'Confirm Send the revised forecast' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(fetch.mock.calls[1][0]).toContain('/api/v1/commitments/commitment-1/confirm')
    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toEqual({ expected_version: 2 })
  })

  it('disables create submission while the request is pending', async () => {
    let resolveCreate!: (value: Response) => void
    const pending = new Promise<Response>((resolve) => { resolveCreate = resolve })
    const fetch = vi.fn().mockImplementationOnce(() => response({ items: [], next_cursor: null })).mockImplementationOnce(() => pending).mockImplementationOnce(() => response({ items: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    fireEvent.change(screen.getByLabelText('Commitment summary'), { target: { value: 'Only once' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create commitment' }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create commitment' }) as HTMLButtonElement).disabled).toBe(true))
    fireEvent.click(screen.getByRole('button', { name: 'Create commitment' }))
    expect(fetch).toHaveBeenCalledTimes(2)
    resolveCreate(new Response(JSON.stringify(commitment), { status: 201, headers: { 'Content-Type': 'application/json' } }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
  })

  it('blocks stale retry when conflict reload fails and recovers with an explicit reload', async () => {
    const current = { ...commitment, version: 3 }
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: {} } }
    const fetch = vi.fn().mockImplementationOnce(() => response({ items: [commitment], next_cursor: null })).mockImplementationOnce(() => response(conflict, 409)).mockRejectedValueOnce(new TypeError('network')).mockImplementationOnce(() => response(current))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await screen.findByText('Send the revised forecast')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Send the revised forecast' }))
    fireEvent.change(screen.getByLabelText('Edit commitment summary'), { target: { value: 'Kept promise' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save commitment' }))
    await screen.findByText(/could not reload the latest commitment/i)
    expect(screen.queryByRole('button', { name: 'Retry with latest version' })).toBeNull()
    expect((screen.getByLabelText('Edit commitment summary') as HTMLInputElement).value).toBe('Kept promise')
    fireEvent.click(screen.getByRole('button', { name: 'Reload latest commitment' }))
    await screen.findByRole('button', { name: 'Retry with latest version' })
  })
})
