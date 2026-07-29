// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import EngineeringWorkspace from './EngineeringWorkspace'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function emptyResponseFor(url: string) {
  if (url.includes('/connectors')) return response({ connectors: [] })
  if (url.includes('/sync-runs')) return response({ sync_runs: [] })
  if (url.includes('/repositories')) return response({ repositories: [] })
  if (url.includes('/work-items')) return response({ work_items: [] })
  if (url.includes('/incidents')) return response({ incidents: [] })
  if (url.includes('/decisions')) return response({ decisions: [] })
  if (url.includes('/metrics')) return response({ metrics: [] })
  return response({}, 404)
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><EngineeringWorkspace /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=engineering-workspace-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'engineering-workspace-request-id') })
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => emptyResponseFor(String(input))))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('EngineeringWorkspace', () => {
  it('defaults to the Overview tab', () => {
    renderWorkspace()
    expect(screen.getByRole('tab', { name: 'Overview', selected: true })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Engineering overview' })).toBeTruthy()
  })

  it('switches tabs on click, showing exactly one panel at a time', async () => {
    renderWorkspace()
    fireEvent.click(screen.getByRole('tab', { name: 'Connector health' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Connector health' })).toBeTruthy())
    expect(screen.queryByRole('heading', { name: 'Engineering overview' })).toBeNull()

    fireEvent.click(screen.getByRole('tab', { name: 'Source coverage' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Source coverage' })).toBeTruthy())
    expect(screen.queryByRole('heading', { name: 'Connector health' })).toBeNull()
  })

  it('renders every required surface named in UX-STATES.md, plus Work items', async () => {
    renderWorkspace()
    // 'Work items' is a later addition beyond UX-STATES.md's original
    // eight-surface list -- see `EngineeringWorkspace.tsx`'s own
    // docstring for why it was added.
    for (const label of ['Overview', 'Delivery', 'Reliability', 'Repositories', 'Work items', 'Incidents', 'Decisions', 'Connector health', 'Source coverage']) {
      expect(screen.getByRole('tab', { name: label })).toBeTruthy()
    }
  })

  it('supports roving-tabindex keyboard navigation across every engineering tab', async () => {
    renderWorkspace()
    const overviewTab = screen.getByRole('tab', { name: 'Overview' })
    overviewTab.focus()

    fireEvent.keyDown(overviewTab, { key: 'End' })
    expect(document.activeElement?.textContent).toBe('Source coverage')
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Source coverage' })).toBeTruthy())

    fireEvent.keyDown(document.activeElement as Element, { key: 'Home' })
    expect(document.activeElement?.textContent).toBe('Overview')
  })

  it('navigates from Overview to another tab via its own quick-jump buttons', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/incidents')) return response({ incidents: [{ id: 'i1', title: 'Outage', description: null, severity: 'high', status: 'open', detected_at: '2026-07-01T00:00:00Z', resolved_at: null, change_ids: [], version: 1, created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z' }] })
      return emptyResponseFor(url)
    }))
    renderWorkspace()

    await waitFor(() => expect(screen.getByText('1 open.')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'View incidents' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Incidents' })).toBeTruthy())
    expect(screen.getByRole('tab', { name: 'Incidents', selected: true })).toBeTruthy()
  })
})
