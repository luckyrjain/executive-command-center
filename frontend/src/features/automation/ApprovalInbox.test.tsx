// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ApprovalInbox from './ApprovalInbox'
import type { Approval, RunDetail } from './types'

const pendingApproval: Approval = {
  id: 'approval-1',
  run_id: 'run-1',
  step_index: 0,
  action_digest: 'digest-abc123',
  high_impact_categories: ['destructive'],
  status: 'pending',
  requested_at: '2026-07-25T00:00:00Z',
  expires_at: '2026-07-27T00:00:00Z',
  decided_at: null,
  decision: null,
  decided_by: null,
  created_at: '2026-07-25T00:00:00Z',
  updated_at: '2026-07-25T00:00:00Z',
}

const runDetail: RunDetail = {
  id: 'run-1',
  workflow_id: 'weekly-digest',
  workflow_version: 1,
  policy_id: 'policy-1',
  trigger_ref: 'manual:user-1',
  status: 'waiting_approval',
  current_step_index: 0,
  queued_at: '2026-07-25T00:00:00Z',
  started_at: null,
  finished_at: null,
  created_at: '2026-07-25T00:00:00Z',
  updated_at: '2026-07-25T00:00:00Z',
  steps: [{ step_index: 0, step_type: 'action', status: 'pending', action_digest: 'digest-abc123', attempt_count: 0, action_ref: 'local.create_note', input: { note_title: 'Board memo' }, output: null, started_at: null, finished_at: null, error_class: null }],
  compensation_steps: [],
}

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

function renderInbox() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><ApprovalInbox /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=approval-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'approval-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ApprovalInbox', () => {
  it('shows the target, payload summary, high-impact category and expiry before any decision is reachable', async () => {
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/approvals': { approvals: [pendingApproval] },
      '/api/v1/automations/runs/run-1': runDetail,
    }))
    renderInbox()

    await waitFor(() => expect(screen.getByText(/Run run-1 · step 0/)).toBeTruthy())
    expect(screen.getByText('destructive')).toBeTruthy()
    expect(screen.getByText(/expires 7\/27\/2026|expires.*2026/)).toBeTruthy()
    await waitFor(() => expect(screen.getByText('local.create_note')).toBeTruthy())
    await waitFor(() => expect(screen.getByText('Payload summary (redacted)')).toBeTruthy())
    fireEvent.click(screen.getByText('Payload summary (redacted)'))
    expect(screen.getByText(/Board memo/)).toBeTruthy()
    expect(screen.getByText('digest-abc123')).toBeTruthy()
  })

  it('never pre-fills the digest field and requires it before Approve is effective', async () => {
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/approvals': { approvals: [pendingApproval] },
      '/api/v1/automations/runs/run-1': runDetail,
    }))
    renderInbox()

    await waitFor(() => expect(screen.getByText(/Run run-1 · step 0/)).toBeTruthy())
    const digestInput = screen.getByLabelText('Echo action digest for run run-1 step 0') as HTMLInputElement
    expect(digestInput.value).toBe('')

    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))
    expect(await screen.findByText('Enter the exact action digest before approving.')).toBeTruthy()
  })

  it('approves with the correct echoed digest and refreshes the inbox', async () => {
    // onDecided invalidates both ['automation','approvals'] and
    // ['automation','run'] (a prefix match against this card's own run-
    // detail query) -- five real requests total: initial approvals list,
    // initial run detail, the approve POST, then both invalidated queries
    // refetching.
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ approvals: [pendingApproval] }))
      .mockImplementationOnce(() => response(runDetail))
      .mockImplementationOnce(() => response({ ...pendingApproval, status: 'approved', decision: 'approved', decided_at: '2026-07-25T01:00:00Z', decided_by: 'user-1' }))
      .mockImplementationOnce(() => response({ approvals: [] }))
      .mockImplementationOnce(() => response(runDetail))
    vi.stubGlobal('fetch', fetch)
    renderInbox()

    await waitFor(() => expect(screen.getByText(/Run run-1 · step 0/)).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Echo action digest for run run-1 step 0'), { target: { value: 'digest-abc123' } })
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    const approveCall = fetch.mock.calls[2]
    expect(approveCall[0]).toContain('/approvals/approval-1/approve')
    expect(JSON.parse(String(approveCall[1]?.body))).toEqual({ action_digest: 'digest-abc123' })
  })

  it('surfaces digest_mismatch as a distinct, specific state, not a generic error', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ approvals: [pendingApproval] }))
      .mockImplementationOnce(() => response(runDetail))
      .mockImplementationOnce(() => response({ error: { code: 'DIGEST_MISMATCH', message: 'Digest Mismatch', details: { expected_digest: 'digest-abc123', provided_digest: 'wrong-digest' } } }, 409))
    vi.stubGlobal('fetch', fetch)
    renderInbox()

    await waitFor(() => expect(screen.getByText(/Run run-1 · step 0/)).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Echo action digest for run run-1 step 0'), { target: { value: 'wrong-digest' } })
    fireEvent.click(screen.getByRole('button', { name: 'Approve' }))

    expect(await screen.findByText(/does not match this approval's current action digest/)).toBeTruthy()
  })

  it('surfaces approval_expired as a distinct state and disables both decision actions', async () => {
    const expiredApproval: Approval = { ...pendingApproval, expires_at: '2020-01-01T00:00:00Z' }
    vi.stubGlobal('fetch', mockFetchByPath({
      '/api/v1/automations/approvals': { approvals: [expiredApproval] },
      '/api/v1/automations/runs/run-1': runDetail,
    }))
    renderInbox()

    await waitFor(() => expect(screen.getByText(/EXPIRED/)).toBeTruthy())
    expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Reject' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('rejects without requiring a digest', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ approvals: [pendingApproval] }))
      .mockImplementationOnce(() => response(runDetail))
      .mockImplementationOnce(() => response({ ...pendingApproval, status: 'rejected', decision: 'rejected', decided_at: '2026-07-25T01:00:00Z', decided_by: 'user-1' }))
      .mockImplementationOnce(() => response({ approvals: [] }))
      .mockImplementationOnce(() => response(runDetail))
    vi.stubGlobal('fetch', fetch)
    renderInbox()

    await waitFor(() => expect(screen.getByText(/Run run-1 · step 0/)).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Reject' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(fetch.mock.calls[2][0]).toContain('/approvals/approval-1/reject')
  })

  it('shows the empty state when nothing is pending', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ approvals: [] })))
    renderInbox()
    await waitFor(() => expect(screen.getByText('No approvals are waiting on you.')).toBeTruthy())
  })
})
