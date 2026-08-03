// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SharingReview from './SharingReview'
import type { Grant, GrantPreview } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function grant(overrides: Partial<Grant> = {}): Grant {
  return {
    id: 'grant-1',
    resource_type: 'incidents',
    resource_id: 'incident-1',
    grantee_account_id: 'account-2',
    actions: ['read'],
    granted_by: 'account-1',
    expires_at: null,
    revoked_at: null,
    created_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function preview(overrides: Partial<GrantPreview> = {}): GrantPreview {
  return {
    resource_type: 'incidents',
    resource_id: 'incident-1',
    grantee_account_id: 'account-2',
    current_visibility: 'workspace',
    proposed_visibility: 'shared_explicitly',
    requires_narrow_visibility_confirmation: true,
    members_losing_default_access: ['account-3', 'account-4'],
    grantee_already_has_access: true,
    grantee_gains_actions: [],
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><SharingReview /></QueryClientProvider>)
}

function stubFetch(handlers: {
  grants?: Grant[]
  onPost?: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | undefined
  onDelete?: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | undefined
}) {
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method === 'POST') {
      const scripted = handlers.onPost?.(input, init)
      if (scripted) return scripted
    }
    if (method === 'DELETE') {
      const scripted = handlers.onDelete?.(input, init)
      if (scripted) return scripted
    }
    return response({ grants: handlers.grants ?? [] })
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

function fillForm() {
  fireEvent.change(screen.getByLabelText('Resource type'), { target: { value: 'incidents' } })
  fireEvent.change(screen.getByLabelText('Resource ID'), { target: { value: 'incident-1' } })
  fireEvent.change(screen.getByLabelText('Grantee account ID'), { target: { value: 'account-2' } })
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=sharing-review-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'sharing-review-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('SharingReview', () => {
  it('shows the empty state when no grants exist', async () => {
    stubFetch({ grants: [] })
    renderPanel()
    expect(await screen.findByText('No grants shared yet.')).toBeTruthy()
  })

  it('disables Preview sharing until the form is filled in', async () => {
    stubFetch({ grants: [] })
    renderPanel()
    await screen.findByText('No grants shared yet.')
    expect((screen.getByRole('button', { name: 'Preview sharing' }) as HTMLButtonElement).disabled).toBe(true)
    fillForm()
    expect((screen.getByRole('button', { name: 'Preview sharing' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('previews a narrowing grant and warns how many members lose default access', async () => {
    stubFetch({
      grants: [],
      onPost: (input) => (String(input).includes('/grants/preview') ? response(preview()) : undefined),
    })
    renderPanel()
    await screen.findByText('No grants shared yet.')
    fillForm()
    fireEvent.click(screen.getByRole('button', { name: 'Preview sharing' }))

    expect(await screen.findByText(/2 other members will lose their current default access/)).toBeTruthy()
    expect(screen.getByText(/already has this access today/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Confirm and share' })).toBeTruthy()
  })

  it('does not warn when the preview requires no narrowing', async () => {
    stubFetch({
      grants: [],
      onPost: (input) =>
        String(input).includes('/grants/preview')
          ? response(preview({ current_visibility: 'private', requires_narrow_visibility_confirmation: false, members_losing_default_access: [], grantee_already_has_access: false, grantee_gains_actions: ['read'] }))
          : undefined,
    })
    renderPanel()
    await screen.findByText('No grants shared yet.')
    fillForm()
    fireEvent.click(screen.getByRole('button', { name: 'Preview sharing' }))

    await screen.findByText(/gains: read/)
    expect(screen.queryByText(/will lose their current default access/)).toBeNull()
  })

  it('hides the stale preview and disables sharing once the form is edited', async () => {
    stubFetch({
      grants: [],
      onPost: (input) => (String(input).includes('/grants/preview') ? response(preview()) : undefined),
    })
    renderPanel()
    await screen.findByText('No grants shared yet.')
    fillForm()
    fireEvent.click(screen.getByRole('button', { name: 'Preview sharing' }))
    await screen.findByRole('button', { name: 'Confirm and share' })

    fireEvent.change(screen.getByLabelText('Resource ID'), { target: { value: 'incident-2' } })
    expect(screen.queryByRole('button', { name: 'Confirm and share' })).toBeNull()
  })

  it('shares after a matching preview and resets the form', async () => {
    const fetch = stubFetch({
      grants: [],
      onPost: (input) => {
        if (String(input).includes('/grants/preview')) return response(preview())
        if (String(input).endsWith('/api/v1/sharing/grants')) return response(grant(), 201)
        return undefined
      },
    })
    renderPanel()
    await screen.findByText('No grants shared yet.')
    fillForm()
    fireEvent.click(screen.getByRole('button', { name: 'Preview sharing' }))
    await screen.findByRole('button', { name: 'Confirm and share' })
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and share' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/sharing/grants'),
      expect.objectContaining({ method: 'POST' }),
    ))
    const call = fetch.mock.calls.find((c) => {
      const init = c[1] as RequestInit | undefined
      return init?.method === 'POST' && String(c[0]).endsWith('/api/v1/sharing/grants')
    })
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      resource_type: 'incidents',
      resource_id: 'incident-1',
      grantee_account_id: 'account-2',
      actions: ['read'],
      narrow_visibility: true,
    })

    await waitFor(() => expect((screen.getByLabelText('Resource type') as HTMLInputElement).value).toBe(''))
  })

  it('revokes a grant', async () => {
    const fetch = stubFetch({
      grants: [grant()],
      onDelete: (input) => (String(input).includes('/api/v1/sharing/grants/grant-1') ? response(grant({ revoked_at: '2026-07-27T00:00:00Z' })) : undefined),
    })
    renderPanel()

    fireEvent.click(await screen.findByRole('button', { name: 'Revoke' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/sharing/grants/grant-1'),
      expect.objectContaining({ method: 'DELETE' }),
    ))
  })

  it('shows a revoked grant with no Revoke action', async () => {
    stubFetch({ grants: [grant({ revoked_at: '2026-07-15T00:00:00Z' })] })
    renderPanel()
    expect(await screen.findByText(/Revoked/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Revoke' })).toBeNull()
  })

  it('surfaces a preview failure as an alert', async () => {
    stubFetch({
      grants: [],
      onPost: (input) => (String(input).includes('/grants/preview') ? Promise.resolve(new Response(JSON.stringify({ error: { code: 'GRANTEE_NOT_FOUND' } }), { status: 404, headers: { 'Content-Type': 'application/json' } })) : undefined),
    })
    renderPanel()
    await screen.findByText('No grants shared yet.')
    fillForm()
    fireEvent.click(screen.getByRole('button', { name: 'Preview sharing' }))
    expect(await screen.findByRole('alert')).toBeTruthy()
  })
})
