// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import AutomationWorkspace from './AutomationWorkspace'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><AutomationWorkspace /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=automation-workspace-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'automation-workspace-request-id') })
  vi.stubGlobal('fetch', vi.fn(() => response({ workflows: [], approvals: [], runs: [], policies: [] })))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('AutomationWorkspace', () => {
  it('defaults to the Workflows tab', async () => {
    renderWorkspace()
    expect(screen.getByRole('tab', { name: 'Workflows', selected: true })).toBeTruthy()
    await waitFor(() => expect(screen.getByText('No workflows yet. Draft one below.')).toBeTruthy())
  })

  it('switches tabs on click, showing exactly one panel at a time', async () => {
    renderWorkspace()
    fireEvent.click(screen.getByRole('tab', { name: 'Approvals' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Approval inbox' })).toBeTruthy())
    expect(screen.queryByText('No workflows yet. Draft one below.')).toBeNull()

    fireEvent.click(screen.getByRole('tab', { name: 'Kill switches' }))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Kill switches' })).toBeTruthy())
    expect(screen.queryByRole('heading', { name: 'Approval inbox' })).toBeNull()
  })

  it('supports roving-tabindex keyboard navigation across every automation tab', async () => {
    renderWorkspace()
    const workflowsTab = screen.getByRole('tab', { name: 'Workflows' })
    workflowsTab.focus()
    expect(document.activeElement?.textContent).toBe('Workflows')

    fireEvent.keyDown(workflowsTab, { key: 'ArrowRight' })
    expect(document.activeElement?.textContent).toBe('Approvals')
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Approval inbox' })).toBeTruthy())

    fireEvent.keyDown(document.activeElement as Element, { key: 'End' })
    expect(document.activeElement?.textContent).toBe('Kill switches')
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Kill switches' })).toBeTruthy())

    fireEvent.keyDown(document.activeElement as Element, { key: 'Home' })
    expect(document.activeElement?.textContent).toBe('Workflows')
  })
})
