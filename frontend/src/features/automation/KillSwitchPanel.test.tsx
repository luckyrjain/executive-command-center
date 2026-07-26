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

    fireEvent.change(screen.getByLabelText('Kill switch reason'), { target: { value: 'incident' } })
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
})
