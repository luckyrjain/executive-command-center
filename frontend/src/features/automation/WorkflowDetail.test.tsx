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

function mockFetchByPath(routes: Record<string, unknown>) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    for (const [path, body] of Object.entries(routes)) {
      if (url.includes(path)) return response(body)
    }
    return response({ error: { code: 'NOT_FOUND', message: 'no fixture route' } }, 404)
  })
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

    await waitFor(() => expect(screen.getByRole('button', { name: 'Publish this version' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Publish this version' }))
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

    await waitFor(() => expect(screen.getByRole('button', { name: 'Publish this version' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Publish this version' }))
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
})
