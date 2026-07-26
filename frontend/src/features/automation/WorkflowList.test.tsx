// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WorkflowList from './WorkflowList'
import type { WorkflowSummary, WorkflowVersion } from './types'

const summary: WorkflowSummary = {
  workflow_id: 'weekly-digest',
  latest_version: 2,
  latest_status: 'draft',
  active_version: 1,
  latest_version_id: 'version-2',
  active_version_id: 'version-1',
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderList(onSelect = vi.fn()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(<QueryClientProvider client={client}><WorkflowList onSelect={onSelect} /></QueryClientProvider>)
  return { client, onSelect, ...utils }
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=workflow-list-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'workflow-list-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('WorkflowList', () => {
  it('renders workflow summaries with latest and active version labels', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ workflows: [summary] })))
    renderList()

    await waitFor(() => expect(screen.getByText('weekly-digest')).toBeTruthy())
    expect(screen.getByText(/latest v2 \(draft\)/)).toBeTruthy()
    expect(screen.getByText(/active v1/)).toBeTruthy()
  })

  it('shows the empty state when no workflows exist', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ workflows: [] })))
    renderList()

    await waitFor(() => expect(screen.getByText('No workflows yet. Draft one below.')).toBeTruthy())
  })

  it('offers separate View latest / View active buttons using real version-row UUIDs, never a bare version number', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ workflows: [summary] })))
    const onSelect = vi.fn()
    renderList(onSelect)

    await waitFor(() => expect(screen.getByText('weekly-digest')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'View latest (v2)' }))
    expect(onSelect).toHaveBeenCalledWith('version-2')

    fireEvent.click(screen.getByRole('button', { name: 'View active (v1)' }))
    expect(onSelect).toHaveBeenCalledWith('version-1')
  })

  it('creates a draft workflow and selects the created version', async () => {
    const created: WorkflowVersion = {
      id: 'new-version-1',
      workflow_id: 'new-flow',
      version: 1,
      graph: { steps: [{ step_id: 's1', step_type: 'action', action_ref: 'local.create_note', input_mapping: {}, on_success: 'succeeded', on_failure: 'failed', compensate_ref: null }] },
      trigger_refs: ['manual'],
      policy_ref: null,
      definition_hash: 'hash',
      status: 'draft',
      created_at: '2026-07-20T00:00:00Z',
      updated_at: '2026-07-20T00:00:00Z',
    }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ workflows: [] }))
      .mockImplementationOnce(() => response(created, 201))
      .mockImplementationOnce(() => response({ workflows: [summary] }))
    vi.stubGlobal('fetch', fetch)
    const onSelect = vi.fn()
    renderList(onSelect)

    await waitFor(() => expect(screen.getByText('No workflows yet. Draft one below.')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Workflow ID'), { target: { value: 'new-flow' } })
    fireEvent.change(screen.getByLabelText('Step ID for step 1'), { target: { value: 's1' } })
    fireEvent.change(screen.getByLabelText('Action reference for step 1'), { target: { value: 'local.create_note' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }))

    await waitFor(() => expect(onSelect).toHaveBeenCalledWith('new-version-1'))
    const createCall = fetch.mock.calls[1]
    expect(createCall[0]).toContain('/api/v1/automations/workflows')
    const body = JSON.parse(String(createCall[1]?.body))
    expect(body.workflow_id).toBe('new-flow')
    expect(body.graph.steps[0].step_id).toBe('s1')
    expect(body.trigger_refs).toEqual(['manual'])
  })

  it('rejects invalid JSON in a step input mapping before submitting', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ workflows: [] })))
    renderList()
    await waitFor(() => expect(screen.getByText('No workflows yet. Draft one below.')).toBeTruthy())

    fireEvent.change(screen.getByLabelText('Workflow ID'), { target: { value: 'bad-json-flow' } })
    fireEvent.change(screen.getByLabelText('Step ID for step 1'), { target: { value: 's1' } })
    fireEvent.change(screen.getByLabelText('Input mapping (JSON object) for step 1'), { target: { value: '{not valid json' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }))

    expect(await screen.findByText(/input mapping must be valid JSON/)).toBeTruthy()
  })

  it('names the visible label text in every control\'s accessible name (WCAG 2.5.3 Label in Name)', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ workflows: [] })))
    renderList()
    await waitFor(() => expect(screen.getByText('No workflows yet. Draft one below.')).toBeTruthy())

    // Visible label text is the whole accessible name where no
    // disambiguation is needed...
    expect(screen.getByLabelText('Trigger references (comma separated)')).toBeTruthy()
    expect(screen.getByLabelText('Policy ID (optional -- attach after creating one from the Policies tab)')).toBeTruthy()
    // ...and is the *prefix* of it where a step number must disambiguate one
    // of several identical fields. The old "Step 1 ID"/"Step 1 type" names did
    // not contain their own visible label text at all.
    for (const [visible, accessible] of [
      ['Step ID', 'Step ID for step 1'],
      ['Step type', 'Step type for step 1'],
      ['Input mapping (JSON object)', 'Input mapping (JSON object) for step 1'],
      ['Action reference', 'Action reference for step 1'],
      ['On success', 'On success for step 1'],
      ['On failure', 'On failure for step 1'],
      ['Compensate reference (action steps only)', 'Compensate reference (action steps only) for step 1'],
    ]) {
      const control = screen.getByLabelText(accessible)
      expect(control.getAttribute('aria-label')?.startsWith(visible)).toBe(true)
    }
  })

  it('maps a failed workflow-list fetch through errorMessage(), never the raw backend message', async () => {
    // retry: 1 on the list query overrides the client default, so the mock
    // keeps failing and the wait outlasts React Query's ~1s backoff.
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'POLICY_NOT_FOUND', message: 'Policy Not Found' } }, 404)))
    renderList()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('That policy ID does not exist in this workspace.')
    expect(alert.textContent).not.toContain('Policy Not Found')
    expect(screen.queryByText('No workflows yet. Draft one below.')).toBeNull()
  })

  it('maps an unreachable server on the workflow list to a readable sentence', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderList()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('Could not reach the server')
  })

  it('surfaces SCHEMA_INVALID violations as readable text, never raw JSON', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ workflows: [] }))
      .mockImplementationOnce(() => response({ error: { code: 'SCHEMA_INVALID', message: 'Schema Invalid', details: { violations: ["step 's1': action_ref required for action"] } } }, 422))
    vi.stubGlobal('fetch', fetch)
    renderList()
    await waitFor(() => expect(screen.getByText('No workflows yet. Draft one below.')).toBeTruthy())

    fireEvent.change(screen.getByLabelText('Workflow ID'), { target: { value: 'invalid-flow' } })
    fireEvent.change(screen.getByLabelText('Step ID for step 1'), { target: { value: 's1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create draft' }))

    expect(await screen.findByText(/action_ref required for action/)).toBeTruthy()
  })
})
