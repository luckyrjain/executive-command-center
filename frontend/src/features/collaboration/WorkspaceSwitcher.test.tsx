// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import WorkspaceSwitcher from './WorkspaceSwitcher'
import type { Workspace } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function workspace(overrides: Partial<Workspace> = {}): Workspace {
  return { id: 'workspace-1', name: 'Acme', timezone: 'UTC', role: 'owner', created_at: '2026-01-01T00:00:00Z', current: true, ...overrides }
}

function renderSwitcher() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><WorkspaceSwitcher /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=switcher-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'switcher-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('WorkspaceSwitcher', () => {
  it('renders nothing when the account belongs to only one workspace', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ workspaces: [workspace()] })))
    const { container } = renderSwitcher()
    await waitFor(() => expect(container.querySelector('.workspace-switcher')).toBeNull())
  })

  it('lists every workspace and switches on selection', async () => {
    const reload = vi.fn()
    Object.defineProperty(window, 'location', { value: { ...window.location, reload }, writable: true })

    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const method = (init?.method ?? 'GET').toUpperCase()
      if (method === 'POST' && String(input).includes('/auth/select-workspace')) {
        return response({ status: 'authenticated', workspace_id: 'workspace-2' })
      }
      return response({ workspaces: [workspace(), workspace({ id: 'workspace-2', name: 'Beta Co', current: false })] })
    })
    vi.stubGlobal('fetch', fetch)
    renderSwitcher()

    const select = await screen.findByLabelText('Switch workspace') as HTMLSelectElement
    expect(select.value).toBe('workspace-1')
    expect(screen.getByRole('option', { name: 'Beta Co' })).toBeTruthy()

    fireEvent.change(select, { target: { value: 'workspace-2' } })
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/auth/select-workspace'),
      expect.objectContaining({ method: 'POST' }),
    ))
    await waitFor(() => expect(reload).toHaveBeenCalled())
  })
})
