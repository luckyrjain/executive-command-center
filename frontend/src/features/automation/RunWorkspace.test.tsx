// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import RunWorkspace from './RunWorkspace'
import type { KillSwitchStatus, PolicyListResponse, Run, RunDetail } from './types'

const baseRun: Run = {
  id: 'run-1',
  workflow_id: 'weekly-digest',
  workflow_version: 1,
  policy_id: 'policy-1',
  trigger_ref: 'manual:user-1',
  status: 'succeeded',
  current_step_index: 1,
  queued_at: '2026-07-25T00:00:00Z',
  started_at: '2026-07-25T00:00:01Z',
  finished_at: '2026-07-25T00:00:05Z',
  created_at: '2026-07-25T00:00:00Z',
  updated_at: '2026-07-25T00:00:05Z',
}

const noSwitch: KillSwitchStatus = { workflow_id: 'weekly-digest', killed: false, active_global: null, active_workflow: null, history: [] }
const noPolicies: PolicyListResponse = { policies: [] }

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
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

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><RunWorkspace /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=run-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'run-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('RunWorkspace', () => {
  it('lists runs and shows the empty state when there are none', async () => {
    vi.stubGlobal('fetch', mockFetchByPath({ '/api/v1/automations/runs': { runs: [] } }))
    renderWorkspace()
    await waitFor(() => expect(screen.getByText('No runs match this filter.')).toBeTruthy())
  })

  it('starts a manual run against the entered workflow ID', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ runs: [] }))
      .mockImplementationOnce(() => response(baseRun, 201))
      .mockImplementationOnce(() => response({ runs: [baseRun] }))
      .mockImplementationOnce(() => response({ ...baseRun, steps: [], compensation_steps: [] }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await waitFor(() => expect(screen.getByText('No runs match this filter.')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Workflow ID to run'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThanOrEqual(2))
    const createCall = fetch.mock.calls[1]
    expect(createCall[0]).toContain('/api/v1/automations/runs')
    expect(JSON.parse(String(createCall[1]?.body))).toEqual({ workflow_id: 'weekly-digest' })
  })

  it('shows a readable message for KILL_SWITCH_ACTIVE, never raw JSON', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ runs: [] }))
      .mockImplementationOnce(() => response({ error: { code: 'KILL_SWITCH_ACTIVE', message: 'Kill Switch Active', details: { workflow_id: 'weekly-digest' } } }, 409))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await waitFor(() => expect(screen.getByText('No runs match this filter.')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Workflow ID to run'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    expect(await screen.findByText(/A kill switch is active for workflow "weekly-digest"/)).toBeTruthy()
  })

  it('shows a run\'s detail including status description, steps and compensation ledger', async () => {
    const detail: RunDetail = {
      ...baseRun,
      status: 'compensation_failed',
      steps: [{ step_index: 0, step_type: 'action', status: 'succeeded', action_digest: 'd1', attempt_count: 0, action_ref: 'local.create_note', input: {}, output: {}, started_at: null, finished_at: null, error_class: null }],
      compensation_steps: [
        { compensates_step_index: 0, status: 'succeeded', action_digest: 'c1', started_at: null, finished_at: null, error_class: null },
        { compensates_step_index: 1, status: 'failed', action_digest: 'c2', started_at: null, finished_at: null, error_class: 'RuntimeError' },
      ],
    }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/runs/run-1': detail,
      '/api/v1/automations/runs': { runs: [{ ...baseRun, status: 'compensation_failed' }] },
      '/kill_switch': noSwitch,
      '/policies': noPolicies,
    }))
    renderWorkspace()

    await waitFor(() => expect(screen.getByRole('button', { name: 'View' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'View' }))

    await waitFor(() => expect(screen.getByText('Partially compensated')).toBeTruthy())
    expect(screen.getByText(/Compensates step 0/)).toBeTruthy()
    expect(screen.getByText(/Compensates step 1/)).toBeTruthy()
  })

  it('shows a step-level retrying badge with attempt count, never a fabricated run-level retrying status', async () => {
    const detail: RunDetail = {
      ...baseRun,
      status: 'queued',
      steps: [{ step_index: 0, step_type: 'action', status: 'retrying', action_digest: 'd1', attempt_count: 2, action_ref: 'local.send_test_notification', input: {}, output: null, started_at: null, finished_at: null, error_class: 'TransientAdapterError' }],
      compensation_steps: [],
    }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/runs/run-1': detail,
      '/api/v1/automations/runs': { runs: [{ ...baseRun, status: 'queued' }] },
      '/kill_switch': noSwitch,
      '/policies': noPolicies,
    }))
    const { container } = renderWorkspace()

    await waitFor(() => expect(screen.getByRole('button', { name: 'View' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'View' }))

    await waitFor(() => expect(screen.getByText(/is retrying \(attempt 2 of 3\)/)).toBeTruthy())
    // The run's own status line must say "queued" -- there is no run-level
    // "retrying" status in DATA-MODEL.md; "retrying" is only ever a
    // step-level fact, asserted above.
    const statusLine = container.querySelector('#automation-run-detail-title + p[role="status"]')
    expect(statusLine?.textContent).toMatch(/queued/)
    expect(statusLine?.textContent).not.toMatch(/retrying/)
  })

  it('shows a needs_review run with the strongest available cause: its own revoked policy', async () => {
    const detail: RunDetail = {
      ...baseRun,
      status: 'needs_review',
      policy_id: 'policy-1',
      steps: [],
      compensation_steps: [],
    }
    const revokedPolicy: PolicyListResponse = {
      policies: [{ id: 'policy-1', workflow_id: 'weekly-digest', action_types: [], data_classes: [], value_limit: '0', count_limit: 10, rate_limit: {}, schedule: null, approval_mode: 'per_run', expires_at: '2026-10-01T00:00:00Z', revoked_at: '2026-07-24T00:00:00Z', status: 'revoked', version: 2, created_at: '2026-07-01T00:00:00Z', updated_at: '2026-07-24T00:00:00Z' }],
    }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/runs/run-1': detail,
      '/api/v1/automations/runs': { runs: [{ ...baseRun, status: 'needs_review' }] },
      '/kill_switch': noSwitch,
      '/policies': revokedPolicy,
    }))
    renderWorkspace()

    await waitFor(() => expect(screen.getByRole('button', { name: 'View' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'View' }))

    await waitFor(() => expect(screen.getByText(/This run's own policy is currently revoked/)).toBeTruthy())
  })

  it('shows a preview_blocked run as a normal finished outcome, not an error, and offers no stop actions', async () => {
    // The docs-vs-code fix for `preview_only`: a run under a preview-only
    // policy terminates in `preview_blocked` having dispatched nothing. This
    // asserts the UI presents that honestly -- an operator in Stage 1 of
    // `docs/runbooks/PHASE-5-DOGFOOD.md` must not read the mode working
    // correctly as a bug, and must not be offered Pause/Resume/Cancel
    // buttons for a run that is already finished.
    const detail: RunDetail = {
      ...baseRun,
      status: 'preview_blocked',
      steps: [],
      compensation_steps: [],
    }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/runs/run-1': detail,
      '/api/v1/automations/runs': { runs: [{ ...baseRun, status: 'preview_blocked' }] },
      '/kill_switch': noSwitch,
      '/policies': noPolicies,
    }))
    const { container } = renderWorkspace()

    await waitFor(() => expect(screen.getByRole('button', { name: 'View' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'View' }))

    await waitFor(() => expect(screen.getByText('Preview only -- no real action was taken.')).toBeTruthy())
    expect(screen.getByText(/never authorizes a real\s+side effect/)).toBeTruthy()
    // Not an error: no role="alert" anywhere on this run's detail, and no
    // error-panel styling on the explanation.
    expect(container.querySelectorAll('[role="alert"]').length).toBe(0)
    expect(container.querySelectorAll('.error-panel').length).toBe(0)
    // Terminal: no stop/resume action is offered.
    expect(screen.queryByRole('button', { name: 'Cancel' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Pause' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull()
  })

  it('explains an enqueue-time rate_limited rejection with the policy\'s own limit', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ runs: [] }))
      .mockImplementationOnce(() => response({ error: { code: 'RATE_LIMITED', message: 'Rate Limited', details: { workflow_id: 'weekly-digest', limit: 10, runs_in_window: 10 } } }, 409))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await waitFor(() => expect(screen.getByText('No runs match this filter.')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Workflow ID to run'), { target: { value: 'weekly-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start run' }))

    expect(await screen.findByText(/already used its policy's limit of 10 runs per hour/)).toBeTruthy()
  })

  it('pauses an in-flight run', async () => {
    const runningRun: Run = { ...baseRun, status: 'running' }
    const runningDetail: RunDetail = { ...runningRun, steps: [], compensation_steps: [] }
    const pausedRun: Run = { ...runningRun, status: 'paused' }
    const pausedDetail: RunDetail = { ...pausedRun, steps: [], compensation_steps: [] }
    // Call order: initial list, initial detail, kill-switch status, the
    // pause POST itself, then the two invalidated queries refetching in
    // the order RunDetailView's own onSuccess calls invalidateQueries --
    // run detail first, then the runs list.
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ runs: [runningRun] }))
      .mockImplementationOnce(() => response(runningDetail))
      .mockImplementationOnce(() => response(noSwitch))
      .mockImplementationOnce(() => response(pausedRun))
      .mockImplementationOnce(() => response(pausedDetail))
      .mockImplementationOnce(() => response({ runs: [pausedRun] }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await waitFor(() => expect(screen.getByRole('button', { name: 'View' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'View' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Pause' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy())
  })

  it('shows a readable message when Resume is refused with RUN_NOT_PAUSED', async () => {
    // UX-STATES.md's Paused row requires Pause/Resume to be offered "per
    // `RUN_NOT_PAUSED`'s own error surface, mapped to a readable message".
    // `errorMessage`'s RUN_NOT_PAUSED branch was implemented but had no test:
    // every existing mutation-error test in this file goes through the
    // *create-run* mutation (KILL_SWITCH_ACTIVE, RATE_LIMITED), which is a
    // different `useMutation` in a different component (`RunWorkspace`'s own,
    // not `RunDetailView`'s). So the run-control error panel -- a separate
    // `mutate.isError` render site -- was never exercised at all.
    //
    // The scenario is real and not rare: this view renders whatever the last
    // fetch returned, so a run that was `paused` when the detail loaded can be
    // reclaimed and back to `running` by the time a human clicks Resume. The
    // mapped message must name the status the server actually found
    // (`details.status`), because "cannot be resumed" alone leaves the
    // operator unable to tell "it already restarted on its own" from "it
    // reached a terminal state and my rollback plan needs to change".
    const pausedRun: Run = { ...baseRun, status: 'paused' }
    const pausedDetail: RunDetail = { ...pausedRun, steps: [], compensation_steps: [] }
    // Call order mirrors the pause test above: initial list, initial detail,
    // kill-switch status (the policies query stays disabled -- it is gated on
    // `needs_review`), then the resume POST that fails.
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ runs: [pausedRun] }))
      .mockImplementationOnce(() => response(pausedDetail))
      .mockImplementationOnce(() => response(noSwitch))
      .mockImplementationOnce(() => response({ error: { code: 'RUN_NOT_PAUSED', message: 'Run Not Paused', details: { status: 'running' } } }, 409))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await waitFor(() => expect(screen.getByRole('button', { name: 'View' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'View' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Resume' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))

    expect(await screen.findByText('This run is running, so it cannot be resumed.')).toBeTruthy()
    // Never the backend's own title-cased fallback message.
    expect(screen.queryByText('Run Not Paused')).toBeNull()
  })
})
