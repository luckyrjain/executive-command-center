// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import KillSwitchPanel from './KillSwitchPanel'
import type { KillSwitchStatus } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><KillSwitchPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=kill-switch-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'kill-switch-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('KillSwitchPanel', () => {
  it('never shows current global/per-workflow state until a workflow ID has been looked up', () => {
    renderPanel()
    expect(screen.queryByText(/blocked from starting new runs|not blocked/)).toBeNull()
  })

  it('checks a workflow\'s current kill-switch status before any run would be attempted', async () => {
    const status: KillSwitchStatus = {
      workflow_id: 'weekly-digest',
      killed: true,
      active_global: null,
      active_workflow: { workflow_id: 'weekly-digest', active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-20T00:00:00Z', deactivated_by: null, deactivated_at: null },
      history: [{ workflow_id: 'weekly-digest', active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-20T00:00:00Z', deactivated_by: null, deactivated_at: null }],
    }
    vi.stubGlobal('fetch', vi.fn(() => response(status)))
    renderPanel()

    fireEvent.change(screen.getByLabelText('Workflow ID to check'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check current status' }))

    await waitFor(() => expect(screen.getByText(/blocked from starting new runs/)).toBeTruthy())
    expect(screen.getByText(/This workflow: active \(incident\)/)).toBeTruthy()
  })

  it('activates the global kill switch with the entered reason', async () => {
    const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => response({ workflow_id: null, active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-25T00:00:00Z', deactivated_by: null, deactivated_at: null }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.change(screen.getByLabelText(/^Reason \(optional/), { target: { value: 'incident' } })
    fireEvent.click(screen.getByRole('button', { name: 'Activate global kill switch' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    const call = fetch.mock.calls[0]
    expect(call[0]).toContain('/api/v1/automations/kill_switch')
    expect(JSON.parse(String(call[1]?.body))).toEqual({ active: true, reason: 'incident' })
  })

  it('activates a per-workflow kill switch for the entered workflow ID', async () => {
    const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => response({ workflow_id: 'weekly-digest', active: true, reason: null, activated_by: 'user-1', activated_at: '2026-07-25T00:00:00Z', deactivated_by: null, deactivated_at: null }))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.change(screen.getByLabelText('Workflow ID to check'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Activate for this workflow' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1))
    expect(fetch.mock.calls[0][0]).toContain('/api/v1/automations/workflows/weekly-digest/kill_switch')
  })

  // --- Failure paths (the states an empty panel used to silently fake) ----

  it('reports an unreadable kill-switch lookup as unknown, never as an empty (implicitly "not blocked") panel', async () => {
    // The status query passes retry: 1, overriding the QueryClient's retry:
    // false default -- mockImplementation (not Once) so the retried attempt
    // fails too, and a longer-than-default timeout to cover React Query's
    // ~1s backoff (AttentionQueue.test.tsx's identical retry:1 convention).
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()

    fireEvent.change(screen.getByLabelText('Workflow ID to check'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check current status' }))

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('kill-switch status could not be confirmed')
    expect(alert.textContent).toContain('treat this workflow\'s block state as unknown')
    expect(alert.textContent).toContain('Could not reach the server')
    // No `role="status"` state panel at all while the state is unknown: the
    // only thing rendered is the alert above, so nothing reads as "not
    // blocked" (the alert says so in as many words).
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('maps a kill-switch lookup 500 to a readable sentence, not a raw backend message', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'REQUEST_FAILED', message: 'Internal Server Error' } }, 500)))
    renderPanel()

    fireEvent.change(screen.getByLabelText('Workflow ID to check'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check current status' }))

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('kill-switch status could not be confirmed')
  })

  it('maps a per-workflow mutation IDEMPOTENCY_CONFLICT to a readable sentence, never the raw code', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'IDEMPOTENCY_CONFLICT', message: 'Idempotency Conflict' } }, 409)))
    renderPanel()

    fireEvent.change(screen.getByLabelText('Workflow ID to check'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Activate for this workflow' }))

    expect(await screen.findByText(/Nothing was changed by this attempt/)).toBeTruthy()
    expect(screen.queryByText('Idempotency Conflict')).toBeNull()
  })

  it('maps a global mutation CSRF rejection to a readable sentence', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'CSRF_TOKEN_INVALID', message: 'Csrf Token Invalid' } }, 403)))
    renderPanel()

    fireEvent.click(screen.getByRole('button', { name: 'Activate global kill switch' }))

    expect(await screen.findByText(/security token is missing or stale/)).toBeTruthy()
  })

  // --- Visible confirmation after a mutation ------------------------------

  it('shows the resulting per-workflow state after activating, without a second manual status check', async () => {
    const killed: KillSwitchStatus = {
      workflow_id: 'weekly-digest',
      killed: true,
      active_global: null,
      active_workflow: { workflow_id: 'weekly-digest', active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-25T00:00:00Z', deactivated_by: null, deactivated_at: null },
      history: [],
    }
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => (
      (init?.method ?? 'GET').toUpperCase() === 'POST'
        ? response({ workflow_id: 'weekly-digest', active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-25T00:00:00Z', deactivated_by: null, deactivated_at: null })
        : response(killed)
    ))
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.change(screen.getByLabelText('Workflow ID to check'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Activate for this workflow' }))

    // No click on "Check current status" anywhere in this test.
    await waitFor(() => expect(screen.getByText(/blocked from starting new runs/)).toBeTruthy())
    expect(screen.getByText(/This workflow: active \(incident\)/)).toBeTruthy()
  })

  it('refetches the displayed lookup after a global toggle, and confirms the global result', async () => {
    const before: KillSwitchStatus = { workflow_id: 'weekly-digest', killed: false, active_global: null, active_workflow: null, history: [] }
    const after: KillSwitchStatus = {
      workflow_id: 'weekly-digest',
      killed: true,
      active_global: { workflow_id: null, active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-25T00:00:00Z', deactivated_by: null, deactivated_at: null },
      active_workflow: null,
      history: [],
    }
    let toggled = false
    const fetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        toggled = true
        return response({ workflow_id: null, active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-25T00:00:00Z', deactivated_by: null, deactivated_at: null })
      }
      return response(toggled ? after : before)
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.change(screen.getByLabelText('Workflow ID to check'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check current status' }))
    await waitFor(() => expect(screen.getByText(/not blocked/)).toBeTruthy())

    fireEvent.change(screen.getByLabelText(/^Reason \(optional/), { target: { value: 'incident' } })
    fireEvent.click(screen.getByRole('button', { name: 'Activate global kill switch' }))

    await waitFor(() => expect(screen.getByText(/server recorded the global kill switch as active \(incident\)/)).toBeTruthy())
    // The already-displayed lookup reflects the new global state too.
    await waitFor(() => expect(screen.getByText(/blocked from starting new runs/)).toBeTruthy())
    expect(screen.getByText(/Global: active \(incident\)/)).toBeTruthy()
  })

  // --- History rows ------------------------------------------------------

  it('shows each history row\'s own timestamp and actor: deactivated rows never report the activation time as the event time', async () => {
    const status: KillSwitchStatus = {
      workflow_id: 'weekly-digest',
      killed: false,
      active_global: null,
      active_workflow: null,
      history: [
        { workflow_id: 'weekly-digest', active: false, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-20T00:00:00Z', deactivated_by: 'user-2', deactivated_at: '2026-07-22T00:00:00Z' },
      ],
    }
    vi.stubGlobal('fetch', vi.fn(() => response(status)))
    renderPanel()

    fireEvent.change(screen.getByLabelText('Workflow ID to check'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Check current status' }))

    const row = await screen.findByText(/deactivated: incident/)
    expect(row.textContent).toContain(new Date('2026-07-22T00:00:00Z').toLocaleString())
    expect(row.textContent).toContain('by user-2')
    // The activation time is still shown, but only as the window's start --
    // never as the deactivation event's own timestamp.
    expect(row.textContent).toContain(`(activated at ${new Date('2026-07-20T00:00:00Z').toLocaleString()})`)
    expect(row.textContent?.indexOf(new Date('2026-07-22T00:00:00Z').toLocaleString()))
      .toBeLessThan(row.textContent?.indexOf('(activated at') ?? -1)
  })
})
