// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PolicyPanel from './PolicyPanel'
import type { AdapterListResponse, Policy } from './types'

const activePolicy: Policy = {
  id: 'policy-1',
  workflow_id: 'weekly-digest',
  action_types: ['create_note'],
  data_classes: [],
  value_limit: '0',
  count_limit: 10,
  rate_limit: { runs_per_workflow_per_hour: 10 },
  schedule: null,
  approval_mode: 'per_run',
  expires_at: '2026-10-01T00:00:00Z',
  revoked_at: null,
  status: 'active',
  version: 1,
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-01T00:00:00Z',
  scope_enforced: true,
}

const adapters: AdapterListResponse = {
  adapters: [
    { adapter_id: 'local.create_note', action_type: 'note.create', data_class: 'sensitive', reversible: true, high_impact_categories: [], has_dispatch_value: false },
    { adapter_id: 'github.add_issue_comment', action_type: 'comment.create', data_class: 'sensitive', reversible: true, high_impact_categories: ['public'], has_dispatch_value: false },
    { adapter_id: 'gitlab.add_note', action_type: 'comment.create', data_class: 'sensitive', reversible: true, high_impact_categories: ['public'], has_dispatch_value: false },
  ],
  action_types: ['comment.create', 'note.create'],
  data_classes: ['public', 'internal', 'sensitive', 'restricted'],
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

/** Answers the adapters vocabulary request itself and passes every other
 * call to `inner`, so tests keep their ordered policy-endpoint mocks and
 * their call-count assertions. */
function withAdapters(inner: ReturnType<typeof vi.fn>) {
  const routed = vi.fn((url: string, init?: RequestInit) =>
    String(url).includes('/api/v1/automations/adapters') ? response(adapters) : inner(url, init),
  )
  vi.stubGlobal('fetch', routed)
  return inner
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><PolicyPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=policy-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'policy-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('PolicyPanel', () => {
  it('renders the policy scope a human needs to trust a workflow', async () => {
    withAdapters(vi.fn(() => response({ policies: [activePolicy] })))
    renderPanel()

    await waitFor(() => expect(screen.getByText('weekly-digest')).toBeTruthy())
    expect(screen.getByText(/per run · active/)).toBeTruthy()
    expect(screen.getByText(/action types: create_note/)).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Revoke policy for weekly-digest' })).toBeTruthy()
  })

  it('shows the empty state when no policies exist', async () => {
    withAdapters(vi.fn(() => response({ policies: [] })))
    renderPanel()
    await waitFor(() => expect(screen.getByText('No policies recorded yet.')).toBeTruthy())
  })

  it('does not offer a revoke action for an already-revoked policy', async () => {
    const revoked = { ...activePolicy, status: 'revoked' as const, revoked_at: '2026-07-15T00:00:00Z' }
    withAdapters(vi.fn(() => response({ policies: [revoked] })))
    renderPanel()

    await waitFor(() => expect(screen.getByText('weekly-digest')).toBeTruthy())
    expect(screen.queryByRole('button', { name: 'Revoke policy for weekly-digest' })).toBeNull()
  })

  it('revokes a policy and refreshes the list', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ policies: [activePolicy] }))
      .mockImplementationOnce(() => response({ ...activePolicy, status: 'revoked', revoked_at: '2026-07-20T00:00:00Z' }))
      .mockImplementationOnce(() => response({ policies: [{ ...activePolicy, status: 'revoked', revoked_at: '2026-07-20T00:00:00Z' }] }))
    withAdapters(fetch)
    renderPanel()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Revoke policy for weekly-digest' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Revoke policy for weekly-digest' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(fetch.mock.calls[1][0]).toContain('/policies/policy-1/revoke')
  })

  it('shows a readable message when revoking an already-expired policy, never raw JSON', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ policies: [activePolicy] }))
      .mockImplementationOnce(() => response({ error: { code: 'POLICY_EXPIRED', message: 'Policy Expired', details: { expires_at: '2026-07-01T00:00:00Z' } } }, 409))
    withAdapters(fetch)
    renderPanel()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Revoke policy for weekly-digest' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Revoke policy for weekly-digest' }))

    expect(await screen.findByText(/already expired/)).toBeTruthy()
  })

  it('takes every control\'s accessible name from its own visible label text (WCAG 2.5.3 Label in Name)', async () => {
    withAdapters(vi.fn(() => response({ policies: [] })))
    renderPanel()
    await waitFor(() => expect(screen.getByText('No policies recorded yet.')).toBeTruthy())

    // The old accessible names ("Filter policies by workflow ID", "Policy
    // value limit", …) did not contain the visible label text a speech-input
    // user reads off the screen.
    await waitFor(() => expect(screen.getByLabelText('note.create')).toBeTruthy())
    for (const visible of ['Filter by workflow ID', 'Workflow ID', 'note.create', 'Highest data class allowed']) {
      expect(screen.getByLabelText(visible).hasAttribute('aria-label')).toBe(false)
    }
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    for (const visible of ['Value limit', 'Count limit', 'Approval mode', 'Schedule note (optional)']) {
      expect(screen.getByLabelText(visible).hasAttribute('aria-label')).toBe(false)
    }
  })

  it('maps a failed policy-list fetch through errorMessage(), never the raw backend message', async () => {
    // retry: 1 on the list query overrides the client default, so the mock
    // keeps failing and the wait outlasts React Query's ~1s backoff.
    withAdapters(vi.fn(() => response({ error: { code: 'WORKFLOW_NOT_FOUND', message: 'Workflow Not Found' } }, 404)))
    renderPanel()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('does not exist in this workspace yet')
    expect(alert.textContent).not.toContain('Workflow Not Found')
    // A failed list is never the "no policies" empty state.
    expect(screen.queryByText('No policies recorded yet.')).toBeNull()
  })

  it('maps an unreachable server on the policy list to a readable sentence', async () => {
    withAdapters(vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()

    const alert = await screen.findByRole('alert', {}, { timeout: 3000 })
    expect(alert.textContent).toContain('Could not reach the server')
  })

  it('creates a policy with the exact numeric limits entered, not a hardcoded default', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ policies: [] }))
      .mockImplementationOnce(() => response({ ...activePolicy, id: 'policy-2', count_limit: 25 }, 201))
      .mockImplementationOnce(() => response({ policies: [{ ...activePolicy, id: 'policy-2', count_limit: 25 }] }))
    withAdapters(fetch)
    renderPanel()

    await waitFor(() => expect(screen.getByText('No policies recorded yet.')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Workflow ID'), { target: { value: 'weekly-digest' } })
    fireEvent.click(await screen.findByLabelText('note.create'))
    fireEvent.change(screen.getByLabelText('Highest data class allowed'), { target: { value: 'internal' } })
    // Scope step's Continue is the one dominant forward action in its row.
    expect(screen.getByRole('button', { name: 'Continue' }).className).toBe('btn-primary')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.change(screen.getByLabelText('Count limit'), { target: { value: '25' } })
    // Limits step's Continue, beside an unstyled Back.
    expect(screen.getByRole('button', { name: 'Continue' }).className).toBe('btn-primary')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    // Review step's submit, beside an unstyled Back.
    expect(screen.getByRole('button', { name: 'Create policy' }).className).toBe('btn-primary')
    fireEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const body = JSON.parse(String(fetch.mock.calls[1][1]?.body))
    expect(body.count_limit).toBe(25)
    expect(body.workflow_id).toBe('weekly-digest')
    expect(body.action_types).toEqual(['note.create'])
    expect(body.data_classes).toEqual(['internal'])
  })

  it('offers the closed action types and data classes, naming the adapters each type covers', async () => {
    withAdapters(vi.fn(() => response({ policies: [] })))
    renderPanel()

    const comment = await screen.findByLabelText('comment.create')
    expect(comment.getAttribute('type')).toBe('checkbox')
    expect(screen.getByText('github.add_issue_comment, gitlab.add_note')).toBeTruthy()
    const ceiling = screen.getByLabelText('Highest data class allowed') as HTMLSelectElement
    expect(Array.from(ceiling.options).map((o) => o.value)).toEqual(['', 'public', 'internal', 'sensitive', 'restricted'])
  })

  it('refuses to submit without an action type, returning to Scope and focusing the first choice', async () => {
    withAdapters(vi.fn(() => response({ policies: [] })))
    renderPanel()

    fireEvent.change(await screen.findByLabelText('Workflow ID'), { target: { value: 'weekly-digest' } })
    await screen.findByLabelText('comment.create')
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/at least one action type/i)
    const first = await screen.findByLabelText('comment.create')
    expect(document.activeElement).toBe(first)
  })

  it('refuses to submit without a data-class ceiling', async () => {
    withAdapters(vi.fn(() => response({ policies: [] })))
    renderPanel()

    fireEvent.change(await screen.findByLabelText('Workflow ID'), { target: { value: 'weekly-digest' } })
    fireEvent.click(await screen.findByLabelText('note.create'))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    expect((await screen.findByRole('alert')).textContent).toMatch(/highest data class/i)
    expect(document.activeElement).toBe(screen.getByLabelText('Highest data class allowed'))
  })

  it('marks a legacy policy whose scope is not enforced', async () => {
    withAdapters(vi.fn(() => response({ policies: [{ ...activePolicy, scope_enforced: false }] })))
    renderPanel()
    expect(await screen.findByText(/Legacy scope: not enforced, expires/)).toBeTruthy()
  })

  it('shows a readable message for a refused scope, never the raw code', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ policies: [] }))
      .mockImplementationOnce(() => response({ error: { code: 'POLICY_SCOPE_UNKNOWN_VALUE', message: 'x', details: { field: 'action_types', values: ['bogus'], allowed: [] } } }, 422))
    withAdapters(fetch)
    renderPanel()

    fireEvent.change(await screen.findByLabelText('Workflow ID'), { target: { value: 'weekly-digest' } })
    fireEvent.click(await screen.findByLabelText('note.create'))
    fireEvent.change(screen.getByLabelText('Highest data class allowed'), { target: { value: 'internal' } })
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    expect(await screen.findByText('Unknown action type: bogus.')).toBeTruthy()
  })

  it('on failed final submit, navigates back to Scope (where the missing Workflow ID lives) and focuses it', async () => {
    withAdapters(vi.fn(() => response({ policies: [] })))
    renderPanel()

    // Reach Review without ever filling Workflow ID.
    await waitFor(() => expect(screen.getByText('No policies recorded yet.')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toMatch(/workflow id is required/i)
    const workflowIdField = await screen.findByLabelText('Workflow ID')
    expect(document.activeElement).toBe(workflowIdField)
    expect(workflowIdField.getAttribute('aria-invalid')).toBe('true')
    expect(workflowIdField.getAttribute('aria-describedby')).toBe(alert.id)
    expect(screen.queryByLabelText('Value limit')).toBeNull()
  })
})
