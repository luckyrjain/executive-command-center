// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import RecordsPanel from './RecordsPanel'
import type { Domain, PersonalRecord } from './types'

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

function record(overrides: Partial<PersonalRecord> = {}): PersonalRecord {
  return {
    id: 'record-1',
    domain_key: 'habits',
    record_type: 'note',
    payload: { text: 'hello' },
    domain_source_id: null,
    effective_at: '2026-07-27T00:00:00Z',
    retention_acknowledged_at: null,
    version: 1,
    created_at: '2026-07-27T00:00:00Z',
    updated_at: '2026-07-27T00:00:00Z',
    ...overrides,
  }
}

function renderPanel() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><RecordsPanel /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=personal-records-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'personal-records-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('RecordsPanel', () => {
  it('shows the disabled state when the selected domain has never been enabled', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ domains: [] })))
    renderPanel()
    expect(await screen.findByText(/Enable Habits in the Domains tab/)).toBeTruthy()
    expect(screen.queryByLabelText('Record type')).toBeNull()
  })

  it('shows the empty state for an enabled domain with no records yet', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/records')) return response({ records: [] })
      return response({ domains: [domain({ domain_key: 'habits', enabled: true })] })
    }))
    renderPanel()
    expect(await screen.findByText('No records yet for Habits.')).toBeTruthy()
  })

  it('lists existing records with their payload fields', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/records')) return response({ records: [record()] })
      return response({ domains: [domain({ domain_key: 'habits', enabled: true })] })
    }))
    renderPanel()
    expect(await screen.findByText('note')).toBeTruthy()
    expect(screen.getByText('text')).toBeTruthy()
    expect(screen.getByText('hello')).toBeTruthy()
    expect(screen.getByText('No retention acknowledgement recorded for this record.')).toBeTruthy()
  })

  it('requires retention acknowledgement before Save is enabled for a high_stakes domain', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/records')) return response({ records: [] })
      return response({ domains: [domain({ domain_key: 'health', classification: 'high_stakes', enabled: true })] })
    }))
    renderPanel()

    fireEvent.change(await screen.findByLabelText('Domain'), { target: { value: 'health' } })
    await screen.findByText('No records yet for Health.')
    fireEvent.change(screen.getByLabelText('Record type'), { target: { value: 'vital_reading' } })

    const saveButton = screen.getByRole('button', { name: 'Save record' }) as HTMLButtonElement
    expect(saveButton.disabled).toBe(true)

    fireEvent.click(screen.getByRole('checkbox'))
    expect(saveButton.disabled).toBe(false)
  })

  it('does not require the retention checkbox for a standard domain', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/records')) return response({ records: [] })
      return response({ domains: [domain({ domain_key: 'habits', enabled: true })] })
    }))
    renderPanel()

    await screen.findByText('No records yet for Habits.')
    expect(screen.queryByRole('checkbox')).toBeNull()
    fireEvent.change(screen.getByLabelText('Record type'), { target: { value: 'note' } })
    expect((screen.getByRole('button', { name: 'Save record' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('creates a record with the entered fields', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return response(record())
      if (url.includes('/records')) return response({ records: [] })
      return response({ domains: [domain({ domain_key: 'habits', enabled: true })] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('No records yet for Habits.')
    fireEvent.change(screen.getByLabelText('Record type'), { target: { value: 'note' } })
    fireEvent.change(screen.getByLabelText('Field name 1'), { target: { value: 'text' } })
    fireEvent.change(screen.getByLabelText('Field value 1'), { target: { value: 'hello' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save record' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/v1/personal/records'),
      expect.objectContaining({ method: 'POST' }),
    ))
    const call = fetch.mock.calls.find((c) => (c[1] as RequestInit | undefined)?.method === 'POST')
    expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
      domain_key: 'habits',
      record_type: 'note',
      payload: { text: 'hello' },
      retention_acknowledged: false,
    })
  })

  it('surfaces RETENTION_ACKNOWLEDGEMENT_REQUIRED as a readable sentence', async () => {
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return response({ error: { code: 'RETENTION_ACKNOWLEDGEMENT_REQUIRED', message: 'Retention Acknowledgement Required' } }, 422)
      }
      if (url.includes('/records')) return response({ records: [] })
      return response({ domains: [domain({ domain_key: 'health', classification: 'high_stakes', enabled: true })] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    fireEvent.change(await screen.findByLabelText('Domain'), { target: { value: 'health' } })
    await screen.findByText('No records yet for Health.')
    fireEvent.change(screen.getByLabelText('Record type'), { target: { value: 'vital_reading' } })
    fireEvent.click(screen.getByRole('checkbox'))
    fireEvent.click(screen.getByRole('button', { name: 'Save record' }))

    expect(await screen.findByText(/requires you to acknowledge its retention terms/)).toBeTruthy()
  })
})
