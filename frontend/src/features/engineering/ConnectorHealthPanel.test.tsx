// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ConnectorHealthPanel from './ConnectorHealthPanel'
import type { ConnectorAccount, SyncRun } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function connector(overrides: Partial<ConnectorAccount> = {}): ConnectorAccount {
  return {
    id: 'connector-1',
    provider: 'github',
    external_account_id: 'gh-1',
    display_name: 'Acme GitHub',
    granted_scopes: ['repo'],
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

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><ConnectorHealthPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=connector-health-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'connector-health-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function stubFetch(connectors: ConnectorAccount[], syncRuns: SyncRun[] = []) {
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/sync-runs')) return response({ sync_runs: syncRuns })
    if (url.includes('/connectors')) return response({ connectors })
    return response({}, 404)
  }))
}

describe('ConnectorHealthPanel', () => {
  it('shows an empty state when no connectors exist', async () => {
    stubFetch([])
    renderPanel()
    expect(await screen.findByText('No connectors are configured for this workspace yet.')).toBeTruthy()
  })

  it('surfaces a connector-list load failure as an alert, not a silent empty list', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()
    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByText('No connectors are configured for this workspace yet.')).toBeNull()
  })

  // --- UX-STATES.md required degraded states -----------------------------

  it('shows "first sync" state for a pending connector with no sync history', async () => {
    stubFetch([connector({ status: 'pending', last_synced_at: null })])
    renderPanel()
    expect(await screen.findByText(/first sync not yet run/)).toBeTruthy()
    expect(screen.getByText('No sync has ever run for this connector -- data will appear once a backfill completes.')).toBeTruthy()
  })

  it('shows the backfill state from sync history while a backfill is running', async () => {
    stubFetch(
      [connector({ status: 'active' })],
      [{ id: 'run-1', connector_account_id: 'connector-1', run_type: 'backfill', status: 'running', items_processed: 3, error_summary: null, started_at: '2026-07-27T00:00:00Z', completed_at: null }],
    )
    renderPanel()
    fireEvent.click(await screen.findByText(/Sync history/))
    expect(await screen.findByText(/in progress/)).toBeTruthy()
  })

  it('shows a partial sync run with its own status and no error alert (a rate-limit page-bound resume, not a failure)', async () => {
    stubFetch(
      [connector({ status: 'active' })],
      [{ id: 'run-partial', connector_account_id: 'connector-1', run_type: 'incremental', status: 'partial', items_processed: 12, error_summary: null, started_at: '2026-07-27T00:00:00Z', completed_at: '2026-07-27T00:05:00Z' }],
    )
    renderPanel()
    fireEvent.click(await screen.findByText(/Sync history/))
    expect(await screen.findByText(/partial/)).toBeTruthy()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('shows a failed sync run with its real error_summary as an alert', async () => {
    stubFetch(
      [connector({ status: 'active' })],
      [{ id: 'run-failed', connector_account_id: 'connector-1', run_type: 'backfill', status: 'failed', items_processed: 0, error_summary: 'GitHub returned 500 Internal Server Error', started_at: '2026-07-27T00:00:00Z', completed_at: '2026-07-27T00:01:00Z' }],
    )
    renderPanel()
    fireEvent.click(await screen.findByText(/Sync history/))
    expect(await screen.findByText(/failed/)).toBeTruthy()
    expect(screen.getByRole('alert').textContent).toContain('GitHub returned 500 Internal Server Error')
  })

  it('shows partial-permissions state distinctly from a plain active connector', async () => {
    stubFetch([connector({ status: 'permission_lost', status_detail: 'scope revoked upstream' })])
    renderPanel()
    expect(await screen.findByText(/partial permissions -- provider revoked some access/)).toBeTruthy()
    expect(screen.getByText(/scope revoked upstream/)).toBeTruthy()
  })

  it('shows a stale-connector banner derived from last_synced_at age', async () => {
    stubFetch([connector({ status: 'active', last_synced_at: '2020-01-01T00:00:00Z' })])
    renderPanel()
    expect(await screen.findByText(/this connector may be showing stale data/)).toBeTruthy()
  })

  it('does not show a stale banner for a recently synced connector', async () => {
    stubFetch([connector({ status: 'active', last_synced_at: new Date().toISOString() })])
    renderPanel()
    await screen.findByText('Acme GitHub')
    expect(screen.queryByText(/this connector may be showing stale data/)).toBeNull()
  })

  it('shows the rate-limited state', async () => {
    stubFetch([connector({ status: 'rate_limited' })])
    renderPanel()
    expect(await screen.findByText(/rate limited by the provider/)).toBeTruthy()
  })

  it('shows the disconnected state and disables its own actions', async () => {
    stubFetch([connector({ status: 'disconnected' })])
    renderPanel()
    expect(await screen.findByText('disconnected')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Start sync' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Disconnect' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows the provider-unavailable state with the real error detail', async () => {
    stubFetch([connector({ status: 'error', last_error: 'GitHub returned 503' })])
    renderPanel()
    expect(await screen.findByText(/provider unavailable/)).toBeTruthy()
    expect(screen.getByText('GitHub returned 503')).toBeTruthy()
  })

  // --- Actions -------------------------------------------------------------

  it('creates a connector with the entered provider and credential', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(connector())
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    fireEvent.change(screen.getByLabelText('Credential'), { target: { value: 'ghp_secret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/engineering/connectors'), expect.objectContaining({ method: 'POST' })))
    const call = fetch.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({ provider: 'github', credential: 'ghp_secret' })
  })

  it('offers Datadog as a connectable provider, alongside every other real adapter', async () => {
    // A team-concept whole-phase review found this list was correctly
    // extended when GitLab/Jira were each added, but the Datadog connector
    // (PR #85) never got the same treatment -- a workspace admin had no UI
    // path to connect one at all despite full backend support.
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    const options = [...(screen.getByLabelText('Provider') as HTMLSelectElement).options].map((option) => option.value)
    expect(options).toEqual(['github', 'gitlab', 'jira', 'datadog', 'sandbox'])
  })

  it('offers the Datadog resource types (monitor/service_definition/dashboard) for starting a sync', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [connector()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Acme GitHub')
    const options = [...(screen.getByLabelText('Resource type for Acme GitHub') as HTMLSelectElement).options].map(
      (option) => option.value,
    )
    expect(options).toEqual([
      'repository', 'work_item', 'change', 'review', 'deployment', 'incident',
      'monitor', 'service_definition', 'dashboard',
    ])
  })

  it('maps CONNECTOR_AUTHORIZATION_FAILED to a readable sentence, never the raw code', async () => {
    // The real backend's detail dict is `{"code": ..., "error": <message>}`
    // (`create_connector_endpoint`'s `AdapterAuthorizationError` handler),
    // and `main.py`'s `_error_payload` strips only `code`/`message` into
    // `details` -- so `error` (not `reason`) is the field name that
    // actually survives onto the client's `ApiError.current`.
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return response({ error: { code: 'CONNECTOR_AUTHORIZATION_FAILED', message: 'Connector Authorization Failed', details: { error: 'bad token' } } }, 422)
      }
      return response({ connectors: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    fireEvent.change(screen.getByLabelText('Credential'), { target: { value: 'bad' } })
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }))

    expect(await screen.findByText(/The provider rejected this credential: bad token/)).toBeTruthy()
    expect(screen.queryByText('Connector Authorization Failed')).toBeNull()
  })

  it('starts a sync with the selected run type and resource type', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/sync') && (init?.method ?? '').toUpperCase() === 'POST') return response({ id: 'run-2', connector_account_id: 'connector-1', run_type: 'incremental', status: 'succeeded', items_processed: 1, error_summary: null, started_at: '2026-07-27T00:00:00Z', completed_at: '2026-07-27T00:01:00Z' })
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [connector()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Acme GitHub')
    fireEvent.change(screen.getByLabelText('Run type for Acme GitHub'), { target: { value: 'incremental' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start sync' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/connectors/connector-1/sync'), expect.objectContaining({ method: 'POST' })))
    const call = fetch.mock.calls.find((c) => String(c[0]).includes('/connectors/connector-1/sync'))
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({ run_type: 'incremental', resource_type: 'repository' })
  })

  it('maps CONNECTOR_SYNC_IN_PROGRESS to a readable sentence', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/sync') && (init?.method ?? '').toUpperCase() === 'POST') return response({ error: { code: 'CONNECTOR_SYNC_IN_PROGRESS', message: 'Connector Sync In Progress' } }, 409)
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [connector()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Acme GitHub')
    fireEvent.click(screen.getByRole('button', { name: 'Start sync' }))
    expect(await screen.findByText(/A sync is already running for this connector/)).toBeTruthy()
  })

  it('disconnects a connector', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/disable')) return response(connector({ status: 'disconnected' }))
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [connector()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Acme GitHub')
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/connectors/connector-1/disable'), expect.objectContaining({ method: 'POST' })))
  })

  it('surfaces a failed disconnect as an alert, mapping CONNECTOR_NOT_FOUND to a readable sentence', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.includes('/disable')) return response({ error: { code: 'CONNECTOR_NOT_FOUND', message: 'Connector Not Found' } }, 404)
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [connector()] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('Acme GitHub')
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect' }))
    expect(await screen.findByText('This connector no longer exists in this workspace.')).toBeTruthy()
  })
})
