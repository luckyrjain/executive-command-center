// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import GrantsPanel from './GrantsPanel'
import type { Grant } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function grant(overrides: Partial<Grant> = {}): Grant {
  return {
    id: 'grant-1',
    source_domain_key: 'health',
    purpose: 'insight_generation',
    granted_categories: ['vital_reading'],
    granted_at: '2026-07-01T00:00:00Z',
    expires_at: null,
    revoked_at: null,
    created_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><GrantsPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=personal-grants-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'personal-grants-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('GrantsPanel', () => {
  it('shows the empty state when no grants exist', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ grants: [] })))
    renderPanel()
    expect(await screen.findByText('No cross-domain grants yet.')).toBeTruthy()
  })

  it('shows an active grant', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ grants: [grant()] })))
    renderPanel()
    // Scoped to the grants list, not the create-form's domain <select> --
    // "Health" is also one of that form's always-rendered <option> labels.
    const list = screen.getByRole('list')
    expect(await within(list).findByText('Health')).toBeTruthy()
    expect(within(list).getByText(/Active/)).toBeTruthy()
    expect(within(list).getByText(/no expiry/)).toBeTruthy()
  })

  it('shows an expired grant distinctly from a revoked one', async () => {
    const past = new Date(Date.now() - 60_000).toISOString()
    vi.stubGlobal('fetch', vi.fn(() => response({ grants: [grant({ expires_at: past })] })))
    renderPanel()
    expect(await screen.findByText(/Expired/)).toBeTruthy()
  })

  it('shows a revoked grant with no Revoke action', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ grants: [grant({ revoked_at: '2026-07-15T00:00:00Z' })] })))
    renderPanel()
    expect(await screen.findByText(/Revoked/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
  })

  it('creates a grant with the entered categories', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(grant())
      return response({ grants: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No cross-domain grants yet.')
    fireEvent.change(screen.getByLabelText('Source domain'), { target: { value: 'health' } })
    fireEvent.change(screen.getByLabelText('Granted categories'), { target: { value: 'vital_reading, symptom_log' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create grant' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/grants'),
      expect.objectContaining({ method: 'POST' }),
    ))
    const call = fetch.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      source_domain_key: 'health',
      purpose: 'insight_generation',
      granted_categories: ['vital_reading', 'symptom_log'],
      expires_at: null,
    })
  })

  it('revokes a grant', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes('/revoke')) return response(grant({ revoked_at: '2026-07-27T00:00:00Z' }))
      return response({ grants: [grant()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/grants/grant-1/revoke'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('disables Create grant until at least one category is entered', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ grants: [] })))
    renderPanel()
    await screen.findByText('No cross-domain grants yet.')
    expect((screen.getByRole('button', { name: 'Create grant' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
