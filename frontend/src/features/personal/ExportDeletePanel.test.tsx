// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ExportDeletePanel from './ExportDeletePanel'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><ExportDeletePanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=personal-export-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'personal-export-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ExportDeletePanel', () => {
  it('exports a domain and shows its item count', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      domain_key: 'habits',
      exported_at: '2026-07-27T00:00:00Z',
      goals: [{}],
      routines: [],
      check_ins: [{}, {}],
      domain_records: [],
    })))
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Export Habits' }))
    expect(await screen.findByText(/Exported 3 item\(s\)/)).toBeTruthy()
    fireEvent.click(screen.getByText('Raw export data'))
    expect(screen.getByText(/"domain_key": "habits"/)).toBeTruthy()
  })

  it('shows "Export running…" while the export request is in flight', async () => {
    const pending = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn(() => pending.promise))
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Export Habits' }))
    expect(await screen.findByText('Export running…')).toBeTruthy()

    pending.resolve(new Response(JSON.stringify({
      domain_key: 'habits', exported_at: '2026-07-27T00:00:00Z', goals: [], routines: [], check_ins: [], domain_records: [],
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await waitFor(() => expect(screen.queryByText('Export running…')).toBeNull())
  })

  it('surfaces an export failure as an alert', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'DOMAIN_NOT_FOUND', message: 'Domain Not Found' } }, 404)))
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Export Habits' }))
    expect(await screen.findByText('This domain has not been enabled yet.')).toBeTruthy()
  })

  it('disables Delete until the confirmation checkbox is checked', () => {
    vi.stubGlobal('fetch', vi.fn(() => response({})))
    renderPanel()
    expect((screen.getByRole('button', { name: 'Delete Habits' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.click(screen.getByRole('checkbox'))
    expect((screen.getByRole('button', { name: 'Delete Habits' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('deletes a domain and shows its completed status', async () => {
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return response({ id: 'job-1', domain_key: 'habits', status: 'completed', requested_at: '2026-07-27T00:00:00Z', completed_at: '2026-07-27T00:00:01Z' })
      }
      return response({})
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete Habits' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/domains/habits/delete'),
      expect.objectContaining({ method: 'POST' }),
    ))
    expect(await screen.findByText(/Deletion completed at/)).toBeTruthy()
  })

  it('resets the confirmation and any previous result when switching domains', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({
      domain_key: 'habits', exported_at: '2026-07-27T00:00:00Z', goals: [], routines: [], check_ins: [], domain_records: [],
    })))
    renderPanel()

    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Export Habits' }))
    await screen.findByText(/Exported 0 item\(s\)/)

    fireEvent.change(screen.getByLabelText('Export domain'), { target: { value: 'learning' } })
    expect(screen.queryByText(/Exported 0 item\(s\)/)).toBeNull()
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(false)
  })

  it('shows "Deletion pending…" as a status live region while the delete request is in flight', async () => {
    const pending = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn(() => pending.promise))
    renderPanel()

    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete Habits' }))
    // Both the button's own label and a dedicated live region read
    // "Deletion pending…" -- `getAllByText` scoped to the status role
    // confirms the live region specifically exists, not just the button.
    await waitFor(() => expect(
      screen.getAllByText('Deletion pending…', { selector: '[role="status"]' }).length,
    ).toBeGreaterThan(0))

    pending.resolve(new Response(JSON.stringify({
      id: 'job-1', domain_key: 'habits', status: 'completed', requested_at: '2026-07-27T00:00:00Z', completed_at: '2026-07-27T00:00:01Z',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await waitFor(() => expect(screen.queryAllByText('Deletion pending…', { selector: '[role="status"]' })).toHaveLength(0))
  })

  it('does not uncheck a different domain\'s confirmation when a stale delete request resolves after switching domains', async () => {
    const pending = deferred<Response>()
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return pending.promise
      return response({})
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Delete Habits' }))

    fireEvent.change(screen.getByLabelText('Export domain'), { target: { value: 'learning' } })
    fireEvent.click(screen.getByRole('checkbox'))
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true)

    pending.resolve(new Response(JSON.stringify({
      id: 'job-1', domain_key: 'habits', status: 'completed', requested_at: '2026-07-27T00:00:00Z', completed_at: '2026-07-27T00:00:01Z',
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await waitFor(() => expect(fetch).toHaveBeenCalled())
    // The now-stale Habits deletion resolving must not silently uncheck the
    // confirmation the user has since given for Learning.
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true)
  })
})
