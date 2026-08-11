// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import GmailPanel from './GmailPanel'
import type { Domain, GmailThreadContent, GmailThreadSummary } from './types'
import type { ConnectorAccount, SyncRun } from '../engineering/types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function domain(overrides: Partial<Domain> = {}): Domain {
  return {
    id: 'domain-1',
    domain_key: 'email',
    classification: 'high_stakes',
    enabled: true,
    enabled_at: '2026-08-01T00:00:00Z',
    version: 1,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-01T00:00:00Z',
    ...overrides,
  }
}

function connector(overrides: Partial<ConnectorAccount> = {}): ConnectorAccount {
  return {
    id: 'connector-1',
    provider: 'gmail',
    external_account_id: 'owner@example.test',
    display_name: 'owner@example.test',
    granted_scopes: ['https://www.googleapis.com/auth/gmail.readonly'],
    status: 'active',
    status_detail: null,
    last_synced_at: '2026-08-10T00:00:00Z',
    last_error: null,
    disconnected_at: null,
    version: 1,
    created_at: '2026-08-01T00:00:00Z',
    updated_at: '2026-08-10T00:00:00Z',
    ...overrides,
  }
}

function thread(overrides: Partial<GmailThreadSummary> = {}): GmailThreadSummary {
  return {
    id: 'thread-1',
    subject: 'Signed contract needed by Friday',
    last_message_at: '2026-08-10T00:00:00Z',
    last_sender: 'priya@partner-co.test',
    last_direction: 'inbound',
    message_count: 1,
    body_cached: true,
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><GmailPanel /></QueryClientProvider>)
}

// One dispatcher covering every endpoint `GmailPanel` calls -- domains
// (consent gate), the shared engineering connector/sync-runs endpoints
// (connect status, history), and the Gmail-specific thread endpoints.
// Overridable per test via `handlers`, matching `GrantsPanel.test.tsx`'s
// own `stubFetch` shape.
function stubFetch(handlers: {
  domains?: Domain[]
  connectors?: ConnectorAccount[]
  syncRuns?: SyncRun[]
  threads?: GmailThreadSummary[]
  threadContent?: GmailThreadContent
  onPost?: (url: string) => Promise<Response> | undefined
}) {
  const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const method = (init?.method ?? 'GET').toUpperCase()
    if (method === 'POST') {
      const scripted = handlers.onPost?.(url)
      if (scripted) return scripted
      if (url.includes('/oauth/start')) return response({ authorization_url: 'https://accounts.google.com/o/oauth2/v2/auth?state=abc' })
      if (url.includes('/forget')) return response({ id: 'job-1', thread_id: 'thread-1', status: 'completed', requested_at: '2026-08-11T00:00:00Z', completed_at: '2026-08-11T00:00:00Z' })
      if (/\/connectors\/[^/]+\/sync$/.test(url)) return response(connector({ status: 'active' }))
      if (/\/connectors\/[^/]+\/disable$/.test(url)) return response(connector({ status: 'disconnected' }))
      if (url.includes('/personal/domains/email/disable')) return response(domain({ enabled: false }))
      return response({}, 404)
    }
    if (url.includes('/engineering/sync-runs')) return response({ sync_runs: handlers.syncRuns ?? [] })
    if (url.includes('/engineering/connectors')) return response({ connectors: handlers.connectors ?? [] })
    if (url.endsWith('/personal/gmail/threads')) return response({ threads: handlers.threads ?? [] })
    if (url.includes('/personal/gmail/threads/')) return response(handlers.threadContent ?? { subject: 'Signed contract needed by Friday', messages: [] })
    if (url.includes('/personal/domains')) return response({ domains: handlers.domains ?? [] })
    return response({}, 404)
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=gmail-panel-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'gmail-panel-request-id') })
  Object.defineProperty(window, 'location', { value: { ...window.location, href: '' }, writable: true })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('GmailPanel', () => {
  it('shows a consent-missing hint and a connect action when nothing is set up yet', async () => {
    stubFetch({ domains: [], connectors: [] })
    renderPanel()
    expect(await screen.findByText(/Enable Email in the Domains tab/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Connect Gmail' })).toBeTruthy()
  })

  it('starts the OAuth flow and redirects the browser to the returned authorization_url', async () => {
    const fetch = stubFetch({ domains: [], connectors: [] })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Gmail' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/gmail/oauth/start'),
      expect.objectContaining({ method: 'POST' }),
    ))
    await waitFor(() => expect(window.location.href).toBe('https://accounts.google.com/o/oauth2/v2/auth?state=abc'))
  })

  it('surfaces an allowlist rejection without offering a bypass', async () => {
    stubFetch({
      domains: [], connectors: [],
      onPost: (url) => url.includes('/oauth/start')
        ? response({ error: { code: 'GMAIL_ACCOUNT_NOT_ALLOWLISTED', message: 'not allowlisted' } }, 403)
        : undefined,
    })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Connect Gmail' }))
    expect(await screen.findByText(/not on the internal allowlist/)).toBeTruthy()
  })

  it('shows connector status, last-sync time, and a working sync action for a connected account', async () => {
    const fetch = stubFetch({
      domains: [domain()], connectors: [connector()], threads: [],
      syncRuns: [{ id: 'run-1', connector_account_id: 'connector-1', run_type: 'backfill', status: 'succeeded', items_processed: 3, error_summary: null, started_at: '2026-08-01T00:00:00Z', completed_at: '2026-08-01T00:05:00Z' }],
    })
    renderPanel()
    expect(await screen.findByText('owner@example.test')).toBeTruthy()
    expect(screen.getByText(/last synced/)).toBeTruthy()

    fireEvent.click(await screen.findByRole('button', { name: 'Sync now' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/engineering/connectors/connector-1/sync'),
      expect.objectContaining({ method: 'POST' }),
    ))
    const call = fetch.mock.calls.find(([input, init]) => String(input).endsWith('/sync') && (init?.method ?? 'GET') === 'POST')!
    const body = JSON.parse(String((call[1] as RequestInit).body))
    expect(body).toEqual({ run_type: 'incremental', resource_type: 'message', since: null })
  })

  it('runs a first-time backfill (not incremental) when no sync has ever run', async () => {
    const fetch = stubFetch({ domains: [domain()], connectors: [connector({ status: 'pending', last_synced_at: null })], syncRuns: [], threads: [] })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Run first sync' }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    const call = fetch.mock.calls.find(([input, init]) => String(input).endsWith('/sync') && (init?.method ?? 'GET') === 'POST')!
    const body = JSON.parse(String((call[1] as RequestInit).body))
    expect(body.run_type).toBe('backfill')
  })

  it('disables sync and explains reauthorization is required when permission is lost', async () => {
    stubFetch({
      domains: [domain()], connectors: [connector({ status: 'permission_lost' })], threads: [],
      syncRuns: [{ id: 'run-1', connector_account_id: 'connector-1', run_type: 'backfill', status: 'succeeded', items_processed: 3, error_summary: null, started_at: '2026-08-01T00:00:00Z', completed_at: '2026-08-01T00:05:00Z' }],
    })
    renderPanel()
    expect(await screen.findByText(/permission lost -- reconnect required/)).toBeTruthy()
    // Waits (not a plain `getByRole`): the button reads "Run first sync"
    // until the separate `sync-runs` query resolves and `neverSynced`
    // flips false, a real async gap from the `permission lost` text above
    // resolving off the faster `connectors` query alone.
    expect(await screen.findByRole('button', { name: 'Sync now' })).toHaveProperty('disabled', true)
    expect(screen.getByText(/Reconnect below before syncing/)).toBeTruthy()
  })

  it('sends an expand-history sync with the chosen since date', async () => {
    const fetch = stubFetch({ domains: [domain()], connectors: [connector()], threads: [] })
    renderPanel()
    await screen.findByText('owner@example.test')
    fireEvent.change(screen.getByLabelText('Sync history from date'), { target: { value: '2026-01-01' } })
    fireEvent.click(screen.getByRole('button', { name: 'Sync from this date' }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    const call = fetch.mock.calls.find(([input, init]) => String(input).endsWith('/sync') && (init?.method ?? 'GET') === 'POST')!
    const body = JSON.parse(String((call[1] as RequestInit).body))
    expect(body.run_type).toBe('backfill')
    expect(body.since).toBe(new Date('2026-01-01').toISOString())
  })

  it('lists threads newest-first with a summary line, and shows an empty state distinct from "never synced"', async () => {
    stubFetch({ domains: [domain()], connectors: [connector()], syncRuns: [{ id: 'run-1', connector_account_id: 'connector-1', run_type: 'backfill', status: 'succeeded', items_processed: 1, error_summary: null, started_at: '2026-08-01T00:00:00Z', completed_at: '2026-08-01T00:05:00Z' }], threads: [] })
    renderPanel()
    expect(await screen.findByText('No messages in the synced window.')).toBeTruthy()
  })

  it('opens a thread and forgets its cached content', async () => {
    const fetch = stubFetch({
      domains: [domain()], connectors: [connector()], threads: [thread()],
      threadContent: { subject: 'Signed contract needed by Friday', messages: [{ id: 'msg-1', sender: 'priya@partner-co.test', sent_at: '2026-08-10T00:00:00Z', direction: 'inbound', body: 'Please sign and return.' }] },
    })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Signed contract needed by Friday' }))
    expect(await screen.findByText('Please sign and return.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Forget cached content for this thread' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/gmail/threads/thread-1/forget'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('disconnects through the domain-level endpoint (not the generic connector endpoint) once consent is active', async () => {
    const fetch = stubFetch({ domains: [domain()], connectors: [connector()], threads: [] })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/domains/email/disable'),
      expect.objectContaining({ method: 'POST' }),
    ))
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/connectors/connector-1/disable'), expect.anything())
  })

  it('disconnects through the generic connector endpoint when the email domain was never enabled', async () => {
    const fetch = stubFetch({ domains: [], connectors: [connector()], threads: [] })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/engineering/connectors/connector-1/disable'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('disconnects through the domain-level endpoint when a disabled email domain row still exists (reconnect-without-re-enable)', async () => {
    // A `personal_domains` row for `email` survives a prior disable (it only
    // flips `enabled` to false); the backend's own disconnect gate keys off
    // row existence, not `enabled`, so the frontend must too -- otherwise a
    // reconnected-but-not-re-enabled account 409s on disconnect.
    const fetch = stubFetch({ domains: [domain({ enabled: false })], connectors: [connector()], threads: [] })
    renderPanel()
    fireEvent.click(await screen.findByRole('button', { name: 'Disconnect' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/domains/email/disable'),
      expect.objectContaining({ method: 'POST' }),
    ))
    expect(fetch).not.toHaveBeenCalledWith(expect.stringContaining('/connectors/connector-1/disable'), expect.anything())
  })

  it('surfaces a thread-list load failure as an alert', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response({}, 404)
      if (url.includes('/engineering/sync-runs')) return response({ sync_runs: [] })
      if (url.includes('/engineering/connectors')) return response({ connectors: [connector()] })
      if (url.endsWith('/personal/gmail/threads')) return response({ error: { code: 'EMAIL_CONSENT_NOT_ACTIVE', message: 'not active' } }, 403)
      if (url.includes('/personal/domains')) return response({ domains: [domain()] })
      return response({}, 404)
    }))
    renderPanel()
    expect(await screen.findByText(/Email consent is not active/, {}, { timeout: 3000 })).toBeTruthy()
  })
})
