// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import SimulationView from './SimulationView'
import type { SimulateResponse } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderView() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><SimulationView versionId="version-1" /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=simulation-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'simulation-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('SimulationView', () => {
  it('always renders a persistent SIMULATION banner distinguishing it from real run history', () => {
    renderView()
    expect(screen.getByText(/SIMULATION -- preview only/)).toBeTruthy()
  })

  it('runs a simulation and renders each step\'s dispatch gate, reversibility and high-impact categories without ever calling execute-shaped endpoints', async () => {
    const simulateResult: SimulateResponse = {
      workflow_id: 'weekly-digest',
      version: 1,
      steps: [
        {
          step_index: 0,
          step_id: 's1',
          step_type: 'action',
          action_ref: 'local.create_note',
          compensate_ref: null,
          preview: { title: 'preview note' },
          reversible: true,
          high_impact_categories: [],
          dispatch_gate: 'clear',
          policy_block_reason: null,
          error: null,
        },
        {
          step_index: 1,
          step_id: 's2',
          step_type: 'action',
          action_ref: 'local.send_test_notification',
          compensate_ref: null,
          preview: { acknowledged_at: '2026-07-20T00:00:00Z' },
          reversible: false,
          high_impact_categories: ['person_directed'],
          dispatch_gate: 'requires_approval',
          policy_block_reason: null,
          error: null,
        },
      ],
    }
    const fetch = vi.fn((_input: RequestInfo | URL, _init?: RequestInit) => response(simulateResult))
    vi.stubGlobal('fetch', fetch)
    renderView()

    fireEvent.click(screen.getByRole('button', { name: 'Run simulation' }))

    await waitFor(() => expect(screen.getByText('[SIMULATED] s1')).toBeTruthy())
    expect(screen.getByText('[SIMULATED] s2')).toBeTruthy()
    expect(screen.getByText(/Clear -- would dispatch/)).toBeTruthy()
    expect(screen.getByText(/Requires approval before dispatch/)).toBeTruthy()
    expect(screen.getByText('reversible')).toBeTruthy()
    expect(screen.getByText('irreversible')).toBeTruthy()
    expect(screen.getByText('person directed')).toBeTruthy()

    // Only one network call ever happens -- POST .../simulate -- proving no
    // second, execute-shaped request is made from this view.
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(String(fetch.mock.calls[0][0])).toContain('/simulate')
    expect(String(fetch.mock.calls[0][1]?.method ?? fetch.mock.calls[0][0])).not.toMatch(/execute/)
  })

  it('shows a policy_block_reason distinctly from a generic error', async () => {
    const blocked: SimulateResponse = {
      workflow_id: 'weekly-digest',
      version: 1,
      steps: [{
        step_index: 0,
        step_id: 's1',
        step_type: 'action',
        action_ref: 'local.create_note',
        compensate_ref: null,
        preview: null,
        reversible: null,
        high_impact_categories: [],
        dispatch_gate: 'policy_blocked',
        policy_block_reason: 'policy_revoked',
        error: null,
      }],
    }
    vi.stubGlobal('fetch', vi.fn(() => response(blocked)))
    renderView()

    fireEvent.click(screen.getByRole('button', { name: 'Run simulation' }))
    await waitFor(() => expect(screen.getByText('Blocked by policy')).toBeTruthy())
    expect(screen.getByText('policy revoked')).toBeTruthy()
  })
})
