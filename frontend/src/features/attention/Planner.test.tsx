// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import Planner, { type Plan } from './Planner'

const proposedPlan: Plan = {
  id: 'plan-1',
  period_start: '2026-07-24',
  period_end: '2026-07-24',
  status: 'proposed',
  policy_version: 1,
  capacity_minutes: 480,
  conflicts: [],
  unscheduled: [],
  superseded_by: null,
  accepted_at: null,
  created_at: '2026-07-23T00:00:00Z',
  updated_at: '2026-07-23T00:00:00Z',
  version: 1,
  blocks: [
    { id: 'block-1', source_type: 'task', source_id: 'task-1', starts_at: '2026-07-24T09:00:00Z', ends_at: '2026-07-24T09:30:00Z', status: 'proposed', rationale: 'Write the board memo', is_default_effort: true },
  ],
  diff: null,
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderPlanner() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const utils = render(<QueryClientProvider client={client}><Planner /></QueryClientProvider>)
  return { client, ...utils }
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=planner-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'planner-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('Planner', () => {
  it('renders a proposed plan with capacity used, blocks and an unmistakable status label', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [proposedPlan], next_cursor: null })))
    renderPlanner()

    await waitFor(() => expect(screen.getByText('Write the board memo', { exact: false })).toBeTruthy())
    expect(screen.getByText(/30 of 480 minutes used/)).toBeTruthy()
    expect(screen.getByText(/proposed/)).toBeTruthy()
  })

  it('shows an over-capacity indicator distinct from color alone when used minutes exceed capacity', async () => {
    const overCapacity: Plan = { ...proposedPlan, capacity_minutes: 10 }
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [overCapacity], next_cursor: null })))
    renderPlanner()

    await waitFor(() => expect(screen.getByText(/over capacity/)).toBeTruthy())
  })

  it('shows unscheduled work and conflicts explicitly, never silently dropped', async () => {
    const withGaps: Plan = {
      ...proposedPlan,
      unscheduled: [{ source_type: 'task', source_id: 'task-2', label: 'Draft the appendix', reason: 'no_capacity' }],
      conflicts: [{ code: 'capacity_exceeded', detail: 'Not enough remaining capacity to place every eligible item' }],
    }
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [withGaps], next_cursor: null })))
    renderPlanner()

    await waitFor(() => expect(screen.getByText(/Draft the appendix/)).toBeTruthy())
    expect(screen.getByText('Not enough remaining capacity to place every eligible item')).toBeTruthy()
  })

  it('requires reviewing a diff before accepting a replan', async () => {
    const diffPlan: Plan = { ...proposedPlan, id: 'plan-2', diff: [{ source_type: 'task', source_id: 'task-1', label: 'Write the board memo', change: 'unchanged' }] }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [proposedPlan], next_cursor: null }))
      .mockImplementationOnce(() => response(diffPlan, 201))
      .mockImplementationOnce(() => response({ items: [proposedPlan, diffPlan], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderPlanner()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Replan 2026-07-24' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Replan 2026-07-24' }))

    await waitFor(() => expect(screen.getByText('Review replan before accepting')).toBeTruthy())
    expect(screen.getByText('unchanged')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Accept new plan' })).toBeTruthy()
  })

  it('moves a block via keyboard-operable datetime inputs, not drag-and-drop', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [proposedPlan], next_cursor: null }))
      .mockImplementationOnce(() => response({ ...proposedPlan, version: 2 }))
      .mockImplementationOnce(() => response({ items: [{ ...proposedPlan, version: 2 }], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderPlanner()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Move Write the board memo' })).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Move Write the board memo' }))
    fireEvent.change(screen.getByLabelText('New start for Write the board memo'), { target: { value: '2026-07-24T11:00' } })
    fireEvent.change(screen.getByLabelText('New end for Write the board memo'), { target: { value: '2026-07-24T11:30' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save new time' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const body = JSON.parse(String(fetch.mock.calls[1][1]?.body))
    expect(body.expected_version).toBe(1)
    expect(new Date(body.starts_at).getUTCHours()).toBe(new Date('2026-07-24T11:00').getUTCHours())
  })
})
