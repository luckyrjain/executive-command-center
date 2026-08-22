// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DelegationsPanel from './DelegationsPanel'
import type { Delegation } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

const ME = { account_id: 'account-me', users_id: 'user-me', workspace_id: 'workspace-1', email: 'me@example.test', display_name: 'Me' }

function delegation(overrides: Partial<Delegation> = {}): Delegation {
  return {
    id: 'delegation-1',
    delegator_account_id: 'account-me',
    recipient_account_id: 'account-other',
    obligation_type: 'incidents',
    obligation_resource_id: 'incident-1',
    expected_outcome: 'Resolve the incident',
    due_at: '2099-01-01T00:00:00Z',
    status: 'proposed',
    evidence: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><DelegationsPanel /></QueryClientProvider>)
}

function stubFetch(handlers: {
  delegations?: Delegation[]
  onPost?: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | undefined
}) {
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method === 'POST') {
      const scripted = handlers.onPost?.(input, init)
      if (scripted) return scripted
    }
    if (url.endsWith('/identity/me')) return response(ME)
    if (url.endsWith('/delegations')) return response({ delegations: handlers.delegations ?? [] })
    return response({})
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=delegations-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'delegations-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('DelegationsPanel', () => {
  it('shows the empty state when there are no delegations', async () => {
    stubFetch({ delegations: [] })
    renderPanel()
    expect(await screen.findByText('No delegations yet.')).toBeTruthy()
  })

  it('shows accept/reject for the recipient of a proposed delegation, not the delegator', async () => {
    stubFetch({ delegations: [delegation({ recipient_account_id: 'account-me', delegator_account_id: 'account-other' })] })
    renderPanel()
    expect(await screen.findByRole('button', { name: 'Accept' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Reject' })).toBeTruthy()
  })

  it('discloses that a delegator cannot withdraw a still-proposed delegation', async () => {
    stubFetch({ delegations: [delegation({ delegator_account_id: 'account-me', recipient_account_id: 'account-other' })] })
    renderPanel()
    expect(await screen.findByText(/no way to withdraw a still-pending delegation/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull()
  })

  it('shows Revoke for the delegator of an accepted delegation', async () => {
    stubFetch({ delegations: [delegation({ status: 'accepted', delegator_account_id: 'account-me', recipient_account_id: 'account-other' })] })
    renderPanel()
    expect(await screen.findByRole('button', { name: 'Revoke' })).toBeTruthy()
  })

  it('shows Mark complete for the recipient of an accepted delegation', async () => {
    stubFetch({ delegations: [delegation({ status: 'accepted', recipient_account_id: 'account-me', delegator_account_id: 'account-other' })] })
    renderPanel()
    expect(await screen.findByRole('button', { name: 'Mark complete' })).toBeTruthy()
  })

  it('accepts a proposed delegation', async () => {
    const fetch = stubFetch({
      delegations: [delegation({ recipient_account_id: 'account-me', delegator_account_id: 'account-other' })],
      onPost: (input) => (String(input).includes('/delegations/delegation-1/accept') ? response(delegation({ status: 'accepted' })) : undefined),
    })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/delegations/delegation-1/accept'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('maps DELEGATION_NOT_PROPOSED to a readable sentence, never the raw code, and refetches so the stale row and its now-invalid action buttons don\'t linger', async () => {
    // Round-trip fixture, not just an error-message assertion: an earlier
    // draft of this handler had no onError at all, so this row's
    // Accept/Reject buttons stayed enabled against the stale (still
    // "proposed") row after a 409 -- a retry click would just 409 again in
    // a loop. The GET after the 409 returns the row already accepted by
    // someone else, proving the panel actually re-reads current state.
    let delegationsGetCount = 0
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method === 'POST' && url.includes('/delegations/delegation-1/accept')) {
        return response({ error: { code: 'DELEGATION_NOT_PROPOSED', message: 'Delegation Not Proposed' } }, 409)
      }
      if (url.endsWith('/identity/me')) return response(ME)
      if (url.endsWith('/delegations')) {
        delegationsGetCount += 1
        if (delegationsGetCount === 1) {
          return response({ delegations: [delegation({ recipient_account_id: 'account-me', delegator_account_id: 'account-other' })] })
        }
        return response({ delegations: [delegation({ recipient_account_id: 'account-me', delegator_account_id: 'account-other', status: 'accepted' })] })
      }
      return response({})
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: 'Accept' }))
    expect(await screen.findByText('This delegation is no longer awaiting a response.')).toBeTruthy()
    expect(screen.queryByText('Delegation Not Proposed')).toBeNull()

    await waitFor(() => expect(delegationsGetCount).toBe(2))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull())
  })

  it('disables Propose delegation until the form is filled in', async () => {
    stubFetch({ delegations: [] })
    renderPanel()
    await screen.findByText('No delegations yet.')
    expect((screen.getByRole('button', { name: 'Propose delegation' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('proposes a delegation', async () => {
    const fetch = stubFetch({
      delegations: [],
      onPost: (input) => (String(input).endsWith('/api/v1/delegations') ? response(delegation(), 201) : undefined),
    })
    renderPanel()
    await screen.findByText('No delegations yet.')
    fireEvent.change(screen.getByLabelText('Recipient account ID'), { target: { value: 'account-other' } })
    fireEvent.change(screen.getByLabelText('Obligation resource type'), { target: { value: 'incidents' } })
    fireEvent.change(screen.getByLabelText('Obligation resource ID'), { target: { value: 'incident-1' } })
    fireEvent.change(screen.getByLabelText('Expected outcome'), { target: { value: 'Resolve it' } })
    fireEvent.change(screen.getByLabelText('Due date'), { target: { value: '2099-01-01T00:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Propose delegation' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/delegations'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })
})
