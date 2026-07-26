// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WorkflowDetail from './WorkflowDetail'
import type { KillSwitchStatus, TriggerListResponse, WorkflowVersion } from './types'

const draftVersion: WorkflowVersion = {
  id: 'version-1',
  workflow_id: 'weekly-digest',
  version: 1,
  graph: {
    steps: [
      { step_id: 's1', step_type: 'action', action_ref: 'local.create_note', input_mapping: {}, on_success: 's2', on_failure: 'failed', compensate_ref: null },
      { step_id: 's2', step_type: 'approval_gate', action_ref: null, input_mapping: {}, on_success: 'succeeded', on_failure: 'failed', compensate_ref: null },
    ],
  },
  trigger_refs: ['manual'],
  policy_ref: null,
  definition_hash: 'hash-1',
  status: 'draft',
  created_at: '2026-07-20T00:00:00Z',
  updated_at: '2026-07-20T00:00:00Z',
}

const noSwitch: KillSwitchStatus = { workflow_id: 'weekly-digest', killed: false, active_global: null, active_workflow: null, history: [] }
const noTriggers: TriggerListResponse = { triggers: [] }

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderDetail(versionId = 'version-1') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(<QueryClientProvider client={client}><WorkflowDetail versionId={versionId} /></QueryClientProvider>)
  return { client, ...utils }
}

/** A route's fixture is either a body (200) or a `[body, status]` pair, so a
 * single *secondary* query can be made to fail while every other route still
 * answers normally -- the exact shape the fixed-up error branches need. */
type Route = unknown | [unknown, number]

function mockFetchByPath(routes: Record<string, Route>) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    for (const [path, route] of Object.entries(routes)) {
      if (!url.includes(path)) continue
      return Array.isArray(route) && route.length === 2 && typeof route[1] === 'number'
        ? response(route[0], route[1])
        : response(route)
    }
    return response({ error: { code: 'NOT_FOUND', message: 'no fixture route' } }, 404)
  })
}

const serverError = [{ error: { code: 'REQUEST_FAILED', message: 'Internal Server Error' } }, 500] as [unknown, number]

function publishButton() {
  return screen.getByRole('button', { name: 'Publish this version' }) as HTMLButtonElement
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=workflow-detail-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'workflow-detail-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('WorkflowDetail', () => {
  it('renders the graph, status and kill-switch state before any run could be attempted', async () => {
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': noTriggers,
      '/kill_switch': noSwitch,
    }))
    renderDetail()

    await waitFor(() => expect(screen.getByText('weekly-digest · v1')).toBeTruthy())
    expect(screen.getByText('s1')).toBeTruthy()
    expect(screen.getByText('s2')).toBeTruthy()
    await waitFor(() => expect(screen.getByText(/No kill switch is active/)).toBeTruthy())
  })

  it('shows the kill switch is active before a user would attempt a new run', async () => {
    const killed: KillSwitchStatus = {
      workflow_id: 'weekly-digest',
      killed: true,
      active_global: null,
      active_workflow: { workflow_id: 'weekly-digest', active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-20T00:00:00Z', deactivated_by: null, deactivated_at: null },
      history: [],
    }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': noTriggers,
      '/kill_switch': killed,
    }))
    renderDetail()

    await waitFor(() => expect(screen.getByText(/Kill switch active/)).toBeTruthy())
  })

  it('shows real schedule trigger fields, not just a bare trigger_refs string', async () => {
    const scheduled: TriggerListResponse = {
      triggers: [{ id: 't1', workflow_id: 'weekly-digest', trigger_type: 'schedule', event_type_filter: null, schedule_expression: '0 9 * * MON-FRI', timezone: 'America/New_York', skip_missed: true, last_fired_at: null, created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-01T00:00:00Z' }],
    }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': scheduled,
      '/kill_switch': noSwitch,
    }))
    renderDetail()

    await waitFor(() => expect(screen.getByText(/0 9 \* \* MON-FRI/)).toBeTruthy())
    expect(screen.getByText(/America\/New_York/)).toBeTruthy()
    expect(screen.getByText(/skips missed windows/)).toBeTruthy()
  })

  it('publishes a draft version and reflects the new status', async () => {
    const published = { ...draftVersion, status: 'active' as const }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response(draftVersion))
      .mockImplementationOnce(() => response(noTriggers))
      .mockImplementationOnce(() => response(noSwitch))
      .mockImplementationOnce(() => response(published))
      .mockImplementationOnce(() => response(published))
      .mockImplementationOnce(() => response(noTriggers))
      .mockImplementationOnce(() => response(noSwitch))
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    // Publish stays disabled until the kill-switch state is actually known.
    await waitFor(() => expect(publishButton().disabled).toBe(false))
    fireEvent.click(publishButton())
    await waitFor(() => expect(screen.getByText('active', { exact: true })).toBeTruthy())
  })

  it('shows a readable message for ACTION_REF_NOT_REGISTERED, never raw JSON', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response(draftVersion))
      .mockImplementationOnce(() => response(noTriggers))
      .mockImplementationOnce(() => response(noSwitch))
      .mockImplementationOnce(() => response({ error: { code: 'ACTION_REF_NOT_REGISTERED', message: 'Action Ref Not Registered', details: { violations: ["step 's1': action_ref 'x' is not a registered adapter"] } } }, 422))
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await waitFor(() => expect(publishButton().disabled).toBe(false))
    fireEvent.click(publishButton())
    expect(await screen.findByText(/references an unregistered action/)).toBeTruthy()
  })

  it('toggles the simulation view without calling the real dispatch endpoint', async () => {
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': noTriggers,
      '/kill_switch': noSwitch,
    }))
    renderDetail()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Simulate this version' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Simulate this version' }))
    expect(screen.getByText(/SIMULATION -- preview only/)).toBeTruthy()
  })

  // --- Kill-switch state unknown: never silently "not blocked" -----------

  it('says the kill-switch state could not be confirmed and disables every blocked action when its fetch fails', async () => {
    // retry: 1 on the kill-switch query overrides the client's retry: false,
    // so the mock must keep failing and the wait must outlast the ~1s backoff
    // (AttentionQueue.test.tsx's convention for the same shape).
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': noTriggers,
      '/kill_switch': serverError,
    }))
    renderDetail()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('Kill-switch status could not be confirmed')
    expect(alert.textContent).toContain('actions are disabled until it loads')
    // Never the reassuring reading while the truth is unknown.
    expect(screen.queryByText(/No kill switch is active/)).toBeNull()
    expect(publishButton().disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Activate kill switch for this workflow' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Deactivate kill switch for this workflow' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('disables publish and the kill-switch toggles while the kill-switch state is still loading', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/kill_switch')) return new Promise<Response>(() => {}) // never settles
      if (url.includes('/api/v1/automations/triggers')) return response(noTriggers)
      return response(draftVersion)
    }))
    renderDetail()

    await waitFor(() => expect(screen.getByText('weekly-digest · v1')).toBeTruthy())
    expect(screen.getByText(/Loading kill-switch status/)).toBeTruthy()
    expect(publishButton().disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Activate kill switch for this workflow' }) as HTMLButtonElement).disabled).toBe(true)
  })

  // --- A global block is not clearable per workflow -----------------------

  it('does not offer a per-workflow deactivate as the remedy for a purely global kill switch', async () => {
    const globalOnly: KillSwitchStatus = {
      workflow_id: 'weekly-digest',
      killed: true,
      active_global: { workflow_id: null, active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-20T00:00:00Z', deactivated_by: null, deactivated_at: null },
      active_workflow: null,
      history: [],
    }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': noTriggers,
      '/kill_switch': globalOnly,
    }))
    renderDetail()

    await waitFor(() => expect(screen.getByText(/Kill switch active \(global\)/)).toBeTruthy())
    expect(screen.getByText(/deactivate the global kill switch from the Kill switches tab/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Deactivate kill switch for this workflow' })).toBeNull()
  })

  it('keeps the per-workflow deactivate when this workflow also has its own switch, but says it will not clear the global block', async () => {
    const both: KillSwitchStatus = {
      workflow_id: 'weekly-digest',
      killed: true,
      active_global: { workflow_id: null, active: true, reason: 'incident', activated_by: 'user-1', activated_at: '2026-07-20T00:00:00Z', deactivated_by: null, deactivated_at: null },
      active_workflow: { workflow_id: 'weekly-digest', active: true, reason: 'local', activated_by: 'user-1', activated_at: '2026-07-21T00:00:00Z', deactivated_by: null, deactivated_at: null },
      history: [],
    }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': noTriggers,
      '/kill_switch': both,
    }))
    renderDetail()

    await waitFor(() => expect(screen.getByText(/Kill switch active \(global and this workflow\)/)).toBeTruthy())
    expect(screen.getByText(/leaves the global block in place/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Deactivate kill switch for this workflow' })).toBeTruthy()
  })

  // --- Triggers: a failed fetch is not "no triggers" ----------------------

  it('says the schedule is unknown when the trigger fetch fails, never the no-triggers empty state', async () => {
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': serverError,
      '/kill_switch': noSwitch,
    }))
    renderDetail()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('Trigger configuration could not be loaded')
    expect(alert.textContent).toContain('may still be on a schedule')
    expect(screen.queryByText('No configured triggers for this workflow.')).toBeNull()
  })

  // --- Policy: a bare UUID is not a lifecycle status ----------------------

  it('resolves the attached policy_ref to its real lifecycle status, not just the bare id', async () => {
    const withPolicy = { ...draftVersion, policy_ref: 'policy-1' }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': withPolicy,
      '/api/v1/automations/triggers': noTriggers,
      '/api/v1/automations/policies': { policies: [{ id: 'policy-1', workflow_id: 'weekly-digest', action_types: [], data_classes: [], value_limit: '0', count_limit: 10, rate_limit: {}, schedule: null, approval_mode: 'per_run', expires_at: '2026-10-01T00:00:00Z', revoked_at: '2026-07-22T00:00:00Z', status: 'revoked', version: 1, created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-22T00:00:00Z' }] },
      '/kill_switch': noSwitch,
    }))
    renderDetail()

    await waitFor(() => expect(screen.getByText(/policy-1 · currently revoked/)).toBeTruthy())
  })

  it('says the attached policy\'s status could not be confirmed when the policy fetch fails, never implying it is fine', async () => {
    const withPolicy = { ...draftVersion, policy_ref: 'policy-1' }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/workflows/version-1': withPolicy,
      '/api/v1/automations/triggers': noTriggers,
      '/api/v1/automations/policies': serverError,
      '/kill_switch': noSwitch,
    }))
    renderDetail()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('status could not be confirmed')
  })

  it('does not fetch policies at all for a version with no policy attached', async () => {
    const fetch = mockFetchByPath({
      '/api/v1/automations/workflows/version-1': draftVersion,
      '/api/v1/automations/triggers': noTriggers,
      '/kill_switch': noSwitch,
    })
    vi.stubGlobal('fetch', fetch)
    renderDetail()

    await waitFor(() => expect(screen.getByText(/No kill switch is active/)).toBeTruthy())
    expect(screen.getByText('none attached')).toBeTruthy()
    expect(fetch.mock.calls.some((call) => String(call[0]).includes('/api/v1/automations/policies'))).toBe(false)
  })

  // --- The main detail fetch goes through errorMessage() too --------------

  it('maps a failed detail fetch through errorMessage(), never the raw backend message', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'WORKFLOW_NOT_FOUND', message: 'Workflow Not Found' } }, 404)))
    renderDetail()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('no longer exists in this workspace')
    expect(alert.textContent).not.toContain('Workflow Not Found')
  })
})
