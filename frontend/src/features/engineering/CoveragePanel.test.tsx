// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import CoveragePanel from './CoveragePanel'
import type { ConnectorAccount, Repository, WorkItem } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function connector(overrides: Partial<ConnectorAccount> = {}): ConnectorAccount {
  return {
    id: 'connector-1',
    provider: 'github',
    external_account_id: 'gh-1',
    display_name: 'Acme GitHub',
    granted_scopes: [],
    status: 'active',
    status_detail: null,
    last_synced_at: '2026-07-27T00:00:00Z',
    last_error: null,
    disconnected_at: null,
    version: 1,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-27T00:00:00Z',
    ...overrides,
  }
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

function workItem(overrides: Partial<WorkItem> = {}): WorkItem {
  return {
    id: 'item-1',
    connector_account_id: 'connector-1',
    provider: 'jira',
    external_id: 'jira-1',
    title: 'Fix bug',
    source_url: 'https://acme.atlassian.net/browse/PROJ-1',
    item_type: 'Bug',
    status: 'To Do',
    reporter_external_id: 'jira-user-9001',
    assignee_external_id: null,
    permission_state: 'active',
    freshness_state: 'fresh',
    provider_created_at: '2026-07-20T00:00:00Z',
    observed_at: '2026-07-26T00:00:00Z',
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-26T00:00:00Z',
    ...overrides,
  }
}

function stubFetch({ connectors = [], repositories = [], workItems = [] }: { connectors?: ConnectorAccount[]; repositories?: Repository[]; workItems?: WorkItem[] }) {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/repositories')) return response({ repositories })
    if (url.includes('/work-items')) return response({ work_items: workItems })
    if (url.includes('/connectors')) return response({ connectors })
    return response({}, 404)
  }))
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><CoveragePanel /></QueryClientProvider>)
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('CoveragePanel', () => {
  it('shows an empty state when no connectors are configured', async () => {
    stubFetch({})
    renderPanel()
    expect(await screen.findByText(/No connectors are configured yet/)).toBeTruthy()
  })

  it('surfaces a load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByText(/No connectors are configured yet/)).toBeNull()
  })

  it('rolls up repository and work-item counts per connector', async () => {
    stubFetch({
      connectors: [connector()],
      repositories: [repository(), repository({ id: 'repo-2', freshness_state: 'stale' })],
      workItems: [workItem()],
    })
    renderPanel()
    expect(await screen.findByText(/2 repositories/)).toBeTruthy()
    expect(screen.getByText(/1 fresh, 1 stale/)).toBeTruthy()
    expect(screen.getByText(/1 work item/)).toBeTruthy()
  })

  it('shows a degraded banner when any synced content is stale or permission-affected', async () => {
    stubFetch({
      connectors: [connector()],
      repositories: [repository({ permission_state: 'permission_lost' })],
      workItems: [],
    })
    renderPanel()
    expect(await screen.findByText(/record.*stale, disconnected, or missing permission/)).toBeTruthy()
  })

  it('shows an all-healthy state when nothing is degraded', async () => {
    stubFetch({ connectors: [connector()], repositories: [repository()], workItems: [] })
    renderPanel()
    expect(await screen.findByText(/All synced content from this connector is fresh with active permissions/)).toBeTruthy()
  })

  // --- "Conflicting identities" disclosure --------------------------------

  it('discloses unresolved reporter/assignee identities as a sample list, not an interactive merge flow', async () => {
    stubFetch({
      connectors: [connector()],
      repositories: [],
      workItems: [
        workItem({ reporter_external_id: 'jira-user-9001', assignee_external_id: null }),
        workItem({ id: 'item-2', reporter_external_id: 'jira-user-9002', assignee_external_id: 'jira-user-9001' }),
      ],
    })
    renderPanel()

    fireEvent.click(await screen.findByText(/Unresolved identities \(2\)/))
    expect(screen.getByText(/no identity-resolution mechanism for/)).toBeTruthy()
    expect(screen.getByText('jira-user-9001')).toBeTruthy()
    expect(screen.getByText('jira-user-9002')).toBeTruthy()
    // Explicitly not an interactive merge/resolve control.
    expect(screen.queryByRole('button', { name: /merge|resolve/i })).toBeNull()
  })

  it('does not show the unresolved-identities disclosure when no work items have synced', async () => {
    stubFetch({ connectors: [connector()], repositories: [repository()], workItems: [] })
    renderPanel()
    await screen.findByText(/1 repositor/)
    expect(screen.queryByText(/Unresolved identities/)).toBeNull()
  })
})
