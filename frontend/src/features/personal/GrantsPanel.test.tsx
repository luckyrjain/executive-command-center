// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import GrantsPanel from './GrantsPanel'
import type { Domain, Grant } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function domain(overrides: Partial<Domain> = {}): Domain {
  return {
    id: 'domain-1',
    domain_key: 'habits',
    classification: 'standard',
    enabled: true,
    enabled_at: '2026-07-01T00:00:00Z',
    version: 1,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
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

// Every enabled domain a test needs -- 'habits' is the panel's own default
// `sourceDomainKey` selection, 'health' is the domain most tests exercise
// the create/list/revoke flow against.
function stubFetch(handlers: {
  grants?: Grant[]
  domains?: Domain[]
  onPost?: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | undefined
}) {
  const domains = handlers.domains ?? [domain({ domain_key: 'habits' }), domain({ domain_key: 'health', classification: 'high_stakes' })]
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
      const scripted = handlers.onPost?.(input, init)
      if (scripted) return scripted
    }
    if (url.includes('/personal/domains')) return response({ domains })
    return response({ grants: handlers.grants ?? [] })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=personal-grants-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'personal-grants-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('GrantsPanel', () => {
  it('shows the empty state when no grants exist', async () => {
    stubFetch({ grants: [] })
    renderPanel()
    expect(await screen.findByText('No cross-domain grants yet.')).toBeTruthy()
  })

  it('shows an active grant', async () => {
    stubFetch({ grants: [grant()] })
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
    stubFetch({ grants: [grant({ expires_at: past })] })
    renderPanel()
    expect(await screen.findByText(/Expired/)).toBeTruthy()
  })

  it('shows a revoked grant with no Revoke action', async () => {
    stubFetch({ grants: [grant({ revoked_at: '2026-07-15T00:00:00Z' })] })
    renderPanel()
    expect(await screen.findByText(/Revoked/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
  })

  it('warns and disables Create grant when the selected source domain is not enabled', async () => {
    stubFetch({ grants: [], domains: [domain({ domain_key: 'habits', enabled: false })] })
    renderPanel()

    await screen.findByText('No cross-domain grants yet.')
    expect(await screen.findByText(/Enable Habits in the Domains tab/)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('Granted categories'), { target: { value: 'note' } })
    expect((screen.getByRole('button', { name: 'Create grant' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('creates a grant with the entered categories', async () => {
    const fetch = stubFetch({
      grants: [],
      onPost: () => response(grant()),
    })
    renderPanel()

    await screen.findByText('No cross-domain grants yet.')
    fireEvent.change(screen.getByLabelText('Source domain'), { target: { value: 'health' } })
    fireEvent.change(screen.getByLabelText('Granted categories'), { target: { value: 'vital_reading, symptom_log' } })
    await waitFor(() => expect((screen.getByRole('button', { name: 'Create grant' }) as HTMLButtonElement).disabled).toBe(false))
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
    const fetch = stubFetch({
      grants: [grant()],
      onPost: (input) => (String(input).includes('/revoke') ? response(grant({ revoked_at: '2026-07-27T00:00:00Z' })) : undefined),
    })
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/grants/grant-1/revoke'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('disables Create grant until at least one category is entered', async () => {
    stubFetch({ grants: [] })
    renderPanel()
    await screen.findByText('No cross-domain grants yet.')
    expect((screen.getByRole('button', { name: 'Create grant' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
