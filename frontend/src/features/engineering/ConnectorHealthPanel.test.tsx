// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

// Wizard navigation helpers -- the "Connect an integration" flow is one
// field per step (provider tile -> one field step per credential part ->
// review), so every create-flow test below drives it step by step rather
// than filling one big form.
function providerGroup() {
  return screen.getByRole('group', { name: 'Provider' })
}
function pickProvider(label: string) {
  fireEvent.click(within(providerGroup()).getByRole('button', { name: label }))
}
function clickContinue() {
  fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
}
function clickBack() {
  fireEvent.click(screen.getByRole('button', { name: 'Back' }))
}
function clickConnect() {
  fireEvent.click(screen.getByRole('button', { name: 'Connect' }))
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

  it('creates a GitHub connector by continuing through the single-step Personal access token wizard', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(connector())
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    clickContinue() // provider (github, default) -> token
    fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'ghp_secret' } })
    clickContinue() // token -> review
    clickConnect()

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/engineering/connectors'), expect.objectContaining({ method: 'POST' })))
    const call = fetch.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({ provider: 'github', credential: 'ghp_secret' })
  })

  it('creates a GitLab connector through the Host-then-token wizard steps, joined as host|token', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(connector({ provider: 'gitlab' }))
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('GitLab')
    clickContinue() // provider -> host
    expect((screen.getByLabelText('Host') as HTMLInputElement).value).toBe('gitlab.com')
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'gitlab-ee.example.com' } })
    clickContinue() // host -> token
    fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'glpat-xxxx' } })
    clickContinue() // token -> review
    clickConnect()

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/engineering/connectors'), expect.objectContaining({ method: 'POST' })))
    const call = fetch.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      provider: 'gitlab',
      credential: 'gitlab-ee.example.com|glpat-xxxx',
    })
  })

  it('creates a Jira connector through the Site/Email/API token wizard steps, joined as site|email|token', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(connector({ provider: 'jira' }))
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('Jira')
    clickContinue() // provider -> site
    fireEvent.change(screen.getByLabelText('Site'), { target: { value: 'acme.atlassian.net' } })
    clickContinue() // site -> email
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ops@acme.com' } })
    clickContinue() // email -> token
    fireEvent.change(screen.getByLabelText('API token'), { target: { value: 'jira-token' } })
    clickContinue() // token -> review
    clickConnect()

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/engineering/connectors'), expect.objectContaining({ method: 'POST' })))
    const call = fetch.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      provider: 'jira',
      credential: 'acme.atlassian.net|ops@acme.com|jira-token',
    })
  })

  it('creates a Datadog connector through the Site/API key/Application key wizard steps, joined as site|api_key|app_key', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(connector({ provider: 'datadog' }))
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('Datadog')
    clickContinue() // provider -> site
    fireEvent.change(screen.getByLabelText('Site'), { target: { value: 'api.datadoghq.eu' } })
    clickContinue() // site -> API key
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'dd-api-key' } })
    clickContinue() // API key -> Application key
    fireEvent.change(screen.getByLabelText('Application key'), { target: { value: 'dd-app-key' } })
    clickContinue() // Application key -> review
    clickConnect()

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v1/engineering/connectors'), expect.objectContaining({ method: 'POST' })))
    const call = fetch.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      provider: 'datadog',
      credential: 'api.datadoghq.eu|dd-api-key|dd-app-key',
    })
  })

  it('offers Datadog\'s real regional sites in the Site select, not free text', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('Datadog')
    clickContinue() // provider -> site
    const options = [...(screen.getByLabelText('Site') as HTMLSelectElement).options].map((option) => option.value)
    expect(options).toEqual([
      'api.datadoghq.com',
      'api.us3.datadoghq.com',
      'api.us5.datadoghq.com',
      'api.datadoghq.eu',
      'api.ap1.datadoghq.com',
      'api.ddog-gov.com',
    ])
  })

  it('resets provider-specific fields when switching provider mid-wizard, so a stale value never leaks into a different shape', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('Jira')
    clickContinue() // provider -> site
    fireEvent.change(screen.getByLabelText('Site'), { target: { value: 'acme.atlassian.net' } })
    clickContinue() // site -> email
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ops@acme.com' } })
    clickBack() // email -> site
    clickBack() // site -> provider

    pickProvider('GitHub')
    clickContinue() // provider -> token
    expect((screen.getByLabelText('Personal access token') as HTMLInputElement).value).toBe('')

    clickBack() // token -> provider
    pickProvider('Jira')
    clickContinue() // provider -> site
    expect((screen.getByLabelText('Site') as HTMLInputElement).value).toBe('')
    fireEvent.change(screen.getByLabelText('Site'), { target: { value: 'temp.atlassian.net' } }) // fill just enough to advance
    clickContinue() // site -> email
    expect((screen.getByLabelText('Email') as HTMLInputElement).value).toBe('')
  })

  it('disables Continue at each wizard step until that step\'s own field is filled, then enables Connect on review', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('Jira')
    const continueButton = () => screen.getByRole('button', { name: 'Continue' })
    expect(continueButton().hasAttribute('disabled')).toBe(false) // provider always pre-selected

    clickContinue() // -> site
    expect(continueButton().hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Site'), { target: { value: 'acme.atlassian.net' } })
    expect(continueButton().hasAttribute('disabled')).toBe(false)

    clickContinue() // -> email
    expect(continueButton().hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ops@acme.com' } })
    expect(continueButton().hasAttribute('disabled')).toBe(false)

    clickContinue() // -> token
    expect(continueButton().hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('API token'), { target: { value: 'jira-token' } })
    expect(continueButton().hasAttribute('disabled')).toBe(false)

    clickContinue() // -> review
    expect(screen.getByRole('button', { name: 'Connect' }).hasAttribute('disabled')).toBe(false)
  })

  it('disables Continue on Datadog\'s Application key step until that field is filled too, not just API key', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('Datadog')
    clickContinue() // provider -> site
    clickContinue() // site -> API key
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'dd-api-key' } })
    clickContinue() // API key -> Application key

    const continueButton = screen.getByRole('button', { name: 'Continue' })
    expect(continueButton.hasAttribute('disabled')).toBe(true)
    fireEvent.change(screen.getByLabelText('Application key'), { target: { value: 'dd-app-key' } })
    expect(continueButton.hasAttribute('disabled')).toBe(false)
  })

  it('re-clicking the already-selected provider tile is a no-op, not a reset of fields already typed for it', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('GitHub') // already the default -- re-selecting it must not touch anything
    clickContinue() // provider -> token
    fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'ghp_secret' } })
    clickBack() // token -> provider

    pickProvider('GitHub') // re-click the tile we're already on
    clickContinue() // provider -> token
    expect((screen.getByLabelText('Personal access token') as HTMLInputElement).value).toBe('ghp_secret')
  })

  it('offers every real adapter as a clickable, readably-labeled provider tile, alongside dev-only Sandbox', async () => {
    // A team-concept whole-phase review found this list was correctly
    // extended when GitLab/Jira were each added, but the Datadog connector
    // (PR #85) never got the same treatment -- a workspace admin had no UI
    // path to connect one at all despite full backend support.
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    const labels = within(providerGroup()).getAllByRole('button').map((button) => button.textContent)
    expect(labels).toEqual(['GitHub', 'GitLab', 'Jira', 'Datadog', 'Sandbox (developer testing only)'])
  })

  it('shows GitLab\'s Host and Personal access token as separate wizard steps, never combined on one screen', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    pickProvider('GitLab')
    clickContinue() // provider -> host
    expect(screen.getByLabelText('Host')).toBeTruthy()
    expect(screen.queryByLabelText('Personal access token')).toBeNull()
    clickContinue() // host -> token
    expect(screen.getByLabelText('Personal access token')).toBeTruthy()
    expect(screen.queryByLabelText('Host')).toBeNull()
  })

  it('shows a single Personal access token step for GitHub, no host/site step at all', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    clickContinue() // provider (github, default) -> token
    expect(screen.getByLabelText('Personal access token')).toBeTruthy()
    expect(screen.queryByLabelText('Host')).toBeNull()
    expect(screen.queryByLabelText('Site')).toBeNull()
    fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'ghp_secret' } })
    clickContinue() // token -> review (the very next step, nothing in between)
    expect(screen.getByRole('button', { name: 'Connect' })).toBeTruthy()
  })

  it('shows a success step after connecting, then returns to the provider step on "Connect another integration"', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(connector({ display_name: 'Acme GitHub' }))
      if (url.includes('/sync-runs')) return response({ sync_runs: [] })
      return response({ connectors: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    clickContinue() // provider -> token
    fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'ghp_secret' } })
    clickContinue() // token -> review
    clickConnect()

    expect(await screen.findByText('Acme GitHub is connected')).toBeTruthy()
    expect(screen.queryByRole('group', { name: 'Provider' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Connect another integration' }))
    expect(screen.getByRole('group', { name: 'Provider' })).toBeTruthy()
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
    clickContinue() // provider -> token
    fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'bad' } })
    clickContinue() // token -> review
    clickConnect()

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

  it('splits the create wizard and the connected-accounts list under their own headings', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    expect(screen.getByRole('heading', { name: 'Connect an integration' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Connected integrations' })).toBeTruthy()
  })

  it('shows the wizard stepper with the current step marked active and completed steps checked off', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    const stepper = () => screen.getByRole('list', { name: 'Setup progress' })

    pickProvider('GitLab')
    expect(within(stepper()).getByText('Provider').className).toContain('on')
    clickContinue() // -> host
    expect(within(stepper()).getByText('Host').className).toContain('on')
    clickContinue() // -> token
    fireEvent.change(screen.getByLabelText('Personal access token'), { target: { value: 'glpat-xxxx' } })
    clickContinue() // -> review
    expect(within(stepper()).getByText('Review').className).toContain('on')
    // the two steps already passed both read as done (checkmark), not the
    // upcoming step's own bare number
    expect(within(stepper()).getAllByText('✓')).toHaveLength(3)
  })

  it('marks the current stepper node aria-current="step", not only via color', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    const stepper = () => screen.getByRole('list', { name: 'Setup progress' })

    expect(within(stepper()).getByText('Provider').closest('[aria-current="step"]')).toBeTruthy()
    clickContinue() // -> token
    expect(within(stepper()).getByText('Provider').closest('[aria-current="step"]')).toBeNull()
    expect(within(stepper()).getByText('Token').closest('[aria-current="step"]')).toBeTruthy()
  })

  it('moves focus to the new step\'s own heading on Continue/Back, so it is never dropped to the page body', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    clickContinue() // provider -> token
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Token' }))

    clickBack() // token -> provider
    expect(document.activeElement).toBe(screen.getByRole('heading', { name: 'Choose a provider' }))
  })

  it('does not steal focus on an ordinary page load -- only a real step transition moves it', async () => {
    stubFetch([])
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')
    expect(screen.getByRole('heading', { name: 'Choose a provider' })).toBeTruthy()
    expect(document.activeElement).not.toBe(screen.getByRole('heading', { name: 'Choose a provider' }))
    expect(document.activeElement).toBe(document.body)
  })

  it('links to the real token-creation page for each credential-based provider, on the step that field appears', async () => {
    const fetch = vi.fn(() => response({ connectors: [] }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No connectors are configured for this workspace yet.')

    // GitHub: the token step is the only field step
    clickContinue() // provider -> token
    expect(screen.getByRole('link', { name: 'Create a GitHub token' }).getAttribute('href')).toBe(
      'https://github.com/settings/tokens/new?scopes=repo&description=Executive+Command+Center',
    )
    clickBack()

    // GitLab: no help on the host step; the token step's link depends on
    // whatever host was set on the step before it
    pickProvider('GitLab')
    clickContinue() // provider -> host
    clickContinue() // host -> token (still gitlab.com)
    expect(screen.getByRole('link', { name: 'Create a GitLab token' }).getAttribute('href')).toBe(
      'https://gitlab.com/-/user_settings/personal_access_tokens',
    )
    clickBack() // token -> host
    fireEvent.change(screen.getByLabelText('Host'), { target: { value: 'gitlab.example.com' } })
    clickContinue() // host -> token
    expect(screen.queryByRole('link', { name: 'Create a GitLab token' })).toBeNull()
    expect(screen.getByText('Find it under Settings → Access Tokens on gitlab.example.com.')).toBeTruthy()
    clickBack() // token -> host
    clickBack() // host -> provider

    // Jira
    pickProvider('Jira')
    clickContinue() // provider -> site
    fireEvent.change(screen.getByLabelText('Site'), { target: { value: 'acme.atlassian.net' } })
    clickContinue() // site -> email
    fireEvent.change(screen.getByLabelText('Email'), { target: { value: 'ops@acme.com' } })
    clickContinue() // email -> token
    expect(screen.getByRole('link', { name: 'Create a Jira API token' }).getAttribute('href')).toBe(
      'https://id.atlassian.com/manage-profile/security/api-tokens',
    )
    clickBack()
    clickBack()
    clickBack() // -> provider

    // Datadog: the API key and Application key steps each link to their
    // own real settings page, built from whatever site was picked
    pickProvider('Datadog')
    clickContinue() // provider -> site
    clickContinue() // site -> API key
    expect(screen.getByRole('link', { name: 'Open API keys' }).getAttribute('href')).toBe(
      'https://app.datadoghq.com/organization-settings/api-keys',
    )
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'dd-api-key' } })
    clickContinue() // API key -> Application key
    expect(screen.getByRole('link', { name: 'Open Application keys' }).getAttribute('href')).toBe(
      'https://app.datadoghq.com/organization-settings/application-keys',
    )

    clickBack() // Application key -> API key
    clickBack() // API key -> site
    fireEvent.change(screen.getByLabelText('Site'), { target: { value: 'api.us3.datadoghq.com' } })
    clickContinue() // site -> API key
    expect(screen.getByRole('link', { name: 'Open API keys' }).getAttribute('href')).toBe(
      'https://us3.datadoghq.com/organization-settings/api-keys',
    )
  })
})
