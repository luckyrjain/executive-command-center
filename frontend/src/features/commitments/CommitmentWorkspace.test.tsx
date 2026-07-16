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
  return render(<QueryClientProvider client={client}><CommitmentWorkspace /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=commitment-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'commitment-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('CommitmentWorkspace', () => {
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
    const active = { ...commitment, id: 'active', summary: 'Active promise', status: 'active' }
    const fulfilled = { ...commitment, id: 'fulfilled', summary: 'Finished promise', status: 'fulfilled' }
    const archived = { ...fulfilled, id: 'archived', summary: 'Archived promise', archived_at: '2026-07-16T10:00:00Z' }
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [commitment, active, fulfilled, archived], next_cursor: null })))
    renderWorkspace()

    await screen.findByText('Send the revised forecast')
    expect(screen.getByRole('button', { name: 'Confirm Send the revised forecast' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Fulfil Send the revised forecast' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Fulfil Active promise' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Cancel Finished promise' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Archive Finished promise' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Restore Archived promise' })).toBeTruthy()
  })

  it('preserves an edit and retries with the conflict current version', async () => {
    const current = { ...commitment, version: 3, summary: 'Changed elsewhere' }
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: { current } } }
    const saved = { ...current, version: 4, summary: 'My revised promise' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [commitment], next_cursor: null }))
      .mockImplementationOnce(() => response(conflict, 409))
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
    fireEvent.click(screen.getByRole('button', { name: 'Retry with latest version' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4))
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).toMatchObject({ expected_version: 3, summary: 'My revised promise' })
  })
})
