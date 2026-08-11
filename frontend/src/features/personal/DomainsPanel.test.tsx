// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DomainsPanel from './DomainsPanel'
import type { Domain } from './types'

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function domain(overrides: Partial<Domain> = {}): Domain {
  return {
    id: 'domain-1',
    domain_key: 'habits',
    classification: 'standard',
    enabled: true,
    enabled_at: '2026-07-01T00:00:00Z',
    version: 1,
    created_at: '2026-07-01T00:00:00Z',
    updated_at: '2026-07-01T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><DomainsPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=personal-domains-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'personal-domains-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('DomainsPanel', () => {
  it('shows all seven domains as not-enabled when none have ever been enabled', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ domains: [] })))
    renderPanel()
    expect(await screen.findByText('Habits')).toBeTruthy()
    expect(screen.getByText('Learning')).toBeTruthy()
    expect(screen.getByText('Travel')).toBeTruthy()
    expect(screen.getByText('Relationships')).toBeTruthy()
    expect(screen.getByText('Health')).toBeTruthy()
    expect(screen.getByText('Finance')).toBeTruthy()
    expect(screen.getByText('Email')).toBeTruthy()
    expect(screen.getAllByText(/Not enabled -- no data is captured/).length).toBe(7)
  })

  it('shows a high-stakes domain\'s classification and retention note', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ domains: [] })))
    renderPanel()
    await screen.findByText('Health')
    const healthRow = screen.getByText('Health').closest('li')!
    expect(within(healthRow).getByText('High-stakes data')).toBeTruthy()
    expect(within(healthRow).getByText(/confirm you have reviewed how long it is kept/)).toBeTruthy()
  })

  it('shows an enabled domain with its enabled_at timestamp', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ domains: [domain({ domain_key: 'habits', enabled: true })] })))
    renderPanel()
    const habitsRow = (await screen.findByText('Habits')).closest('li')!
    expect(await within(habitsRow).findByText(/Enabled/)).toBeTruthy()
    expect(within(habitsRow).getByRole('button', { name: 'Disable' })).toBeTruthy()
  })

  it('enables a domain', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).includes('/enable')) return response(domain({ domain_key: 'learning', enabled: true }))
      return response({ domains: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    const learningRow = (await screen.findByText('Learning')).closest('li')!
    fireEvent.click(within(learningRow).getByRole('button', { name: 'Enable' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/domains/learning/enable'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('disables a domain', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes('/disable')) return response(domain({ domain_key: 'habits', enabled: false }))
      return response({ domains: [domain({ domain_key: 'habits', enabled: true })] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    const habitsRow = (await screen.findByText('Habits')).closest('li')!
    fireEvent.click(await within(habitsRow).findByRole('button', { name: 'Disable' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/domains/habits/disable'),
      expect.objectContaining({ method: 'POST' }),
    ))
  })

  it('surfaces an enable failure as an alert', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes('/enable')) return response({ error: { code: 'IDEMPOTENCY_CONFLICT', message: 'Idempotency Conflict' } }, 409)
      return response({ domains: [] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    const habitsRow = (await screen.findByText('Habits')).closest('li')!
    fireEvent.click(within(habitsRow).getByRole('button', { name: 'Enable' }))
    expect(await screen.findByText(/A different request was already recorded/)).toBeTruthy()
  })
})
