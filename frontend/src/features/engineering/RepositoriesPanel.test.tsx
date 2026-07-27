// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import RepositoriesPanel from './RepositoriesPanel'
import type { Repository } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function repository(overrides: Partial<Repository> = {}): Repository {
  return {
    id: 'repo-1',
    connector_account_id: 'connector-1',
    provider: 'github',
    external_id: 'gh-repo-1',
    name: 'acme/widgets',
    source_url: 'https://github.com/acme/widgets',
    default_branch: 'main',
    permission_state: 'active',
    freshness_state: 'fresh',
    provider_updated_at: '2026-07-20T00:00:00Z',
    observed_at: '2026-07-26T00:00:00Z',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-26T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><RepositoriesPanel /></QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('RepositoriesPanel', () => {
  it('shows an empty state ("first sync") when nothing has synced yet', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ repositories: [] })))
    renderPanel()
    expect(await screen.findByText(/No repositories have synced yet/)).toBeTruthy()
  })

  it('lists a repository with its provider and a link to view it', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ repositories: [repository()] })))
    renderPanel()
    expect(await screen.findByText('acme/widgets')).toBeTruthy()
    const link = screen.getByRole('link', { name: 'View on github' })
    expect(link.getAttribute('href')).toBe('https://github.com/acme/widgets')
  })

  it('shows the partial-permissions state for a repository that lost permission', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ repositories: [repository({ permission_state: 'permission_lost' })] })))
    renderPanel()
    expect(await screen.findByText('permission lost')).toBeTruthy()
  })

  it('shows the stale-connector state for a repository whose content has gone stale', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ repositories: [repository({ freshness_state: 'stale' })] })))
    renderPanel()
    expect(await screen.findByText('stale')).toBeTruthy()
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
  })
})
