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

  it('removes a field row, and leaves one empty row behind when removing the last one', async () => {
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/records')) return response({ records: [] })
      return response({ domains: [domain({ domain_key: 'habits', enabled: true })] })
    }))
    renderPanel()

    await screen.findByText('No records yet for Habits.')
    fireEvent.click(screen.getByRole('button', { name: 'Add field' }))
    expect(screen.getByLabelText('Field name 2')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Remove field 2' }))
    expect(screen.queryByLabelText('Field name 2')).toBeNull()
    expect(screen.getByLabelText('Field name 1')).toBeTruthy()

    fireEvent.change(screen.getByLabelText('Field name 1'), { target: { value: 'text' } })
    fireEvent.click(screen.getByRole('button', { name: 'Remove field 1' }))
    // Removing the last remaining row resets to one empty row, not zero --
    // the field name/value inputs are still there, just cleared.
    expect((screen.getByLabelText('Field name 1') as HTMLInputElement).value).toBe('')
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

  it('surfaces a domains-fetch failure as an alert, not a misleading "enable this domain" message', async () => {
    vi.stubGlobal('fetch', vi.fn(() => Promise.reject(new TypeError('fetch failed'))))
    renderPanel()

    expect(await screen.findByRole('alert', {}, { timeout: 3000 })).toBeTruthy()
    expect(screen.queryByText(/Enable Habits in the Domains tab/)).toBeNull()
  })

  it('fetches and shows the decrypted payload for a record with a redacted field', async () => {
    const fetch = vi.fn((input: RequestInfo | URL) => {
      const url = String(input)
      if (url.endsWith(`/records/${record().id}`)) {
        return response(record({ payload: { text: 'the real decrypted value' } }))
      }
      if (url.includes('/records')) return response({ records: [record({ payload: { text: '***encrypted***' } })] })
      return response({ domains: [domain({ domain_key: 'habits', enabled: true })] })
    })
    vi.stubGlobal('fetch', fetch)
    renderPanel()

    await screen.findByText('***encrypted***')
    fireEvent.click(screen.getByRole('button', { name: 'View decrypted' }))
    expect(await screen.findByText('the real decrypted value')).toBeTruthy()
  })

  it('invalidates the requesting domain\'s query, and does not clear another domain\'s in-progress draft, when a create request resolves after the user has switched domains', async () => {
    const pending = deferred<Response>()
    const fetch = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') return pending.promise
      if (url.includes('/records')) return response({ records: [] })
      return response({
        domains: [
          domain({ domain_key: 'habits', enabled: true }),
          domain({ domain_key: 'learning', enabled: true }),
        ],
      })
    })
    vi.stubGlobal('fetch', fetch)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    render(<QueryClientProvider client={client}><RecordsPanel /></QueryClientProvider>)

    await screen.findByText('No records yet for Habits.')
    fireEvent.change(screen.getByLabelText('Record type'), { target: { value: 'note-in-progress' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save record' }))

    fireEvent.change(screen.getByLabelText('Domain'), { target: { value: 'learning' } })
    await screen.findByText('No records yet for Learning.')

    pending.resolve(new Response(
      JSON.stringify(record({ domain_key: 'habits' })),
      { status: 201, headers: { 'Content-Type': 'application/json' } },
    ))

    await waitFor(() => expect(
      client.getQueryState(['personal', 'records', 'habits', '', ''])?.isInvalidated,
    ).toBe(true))
    // The Learning draft the user had already started typing over the old
    // Habits text must survive -- the now-stale Habits mutation's success
    // handler must not clear state that belongs to whatever domain is
    // currently selected.
    expect((screen.getByLabelText('Record type') as HTMLInputElement).value).toBe('note-in-progress')
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}
