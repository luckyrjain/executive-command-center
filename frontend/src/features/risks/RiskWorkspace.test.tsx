// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import RiskWorkspace, { validateDraft, type Draft } from './RiskWorkspace'

const risk = {
  id: 'risk-1',
  description: 'Vendor renewal may lapse',
  probability: 3,
  impact: 4,
  status: 'identified',
  owner_id: 'owner-1',
  mitigation: 'Confirm renewal terms with vendor',
  trigger: 'No signed contract by review date',
  review_at: '2026-08-01T00:00:00Z',
  project_id: null,
  pinned: false,
  priority_impact: 12,
  score: 15,
  factors: [{ code: 'risk_impact', label: 'Risk impact 12', points: 15, source_field: 'probability,impact' }],
  explanation: 'Risk impact 12',
  created_at: '2026-07-01T00:00:00Z',
  updated_at: '2026-07-10T00:00:00Z',
  version: 2,
  archived_at: null,
  pre_archive_status: null,
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><RiskWorkspace /></QueryClientProvider>)
}

const validDraft: Draft = {
  description: 'Vendor renewal may lapse',
  probability: 3,
  impact: 4,
  status: 'identified',
  mitigation: 'Confirm renewal terms',
  trigger: 'No signed contract',
  reviewAt: '2026-08-01T00:00',
  projectId: '',
  pinned: false,
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=risk-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'risk-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('validateDraft', () => {
  it('requires numeric probability and impact within 1-5', () => {
    expect(validateDraft({ ...validDraft, probability: 0 })).toMatch(/probability/i)
    expect(validateDraft({ ...validDraft, probability: 6 })).toMatch(/probability/i)
    expect(validateDraft({ ...validDraft, impact: 0 })).toMatch(/impact/i)
    expect(validateDraft({ ...validDraft, impact: 6 })).toMatch(/impact/i)
    expect(validateDraft(validDraft)).toBeNull()
  })

  it('requires non-empty mitigation, trigger, and review fields on the frontend only', () => {
    expect(validateDraft({ ...validDraft, mitigation: '  ' })).toMatch(/mitigation/i)
    expect(validateDraft({ ...validDraft, trigger: '' })).toMatch(/trigger/i)
    expect(validateDraft({ ...validDraft, reviewAt: '' })).toMatch(/review/i)
  })

  it('requires a non-empty description', () => {
    expect(validateDraft({ ...validDraft, description: '  ' })).toMatch(/description/i)
  })
})

describe('RiskWorkspace', () => {
  it('creates a risk with owner-derived fields absent from the request', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [], next_cursor: null }))
      .mockImplementationOnce(() => response(risk, 201))
      .mockImplementationOnce(() => response({ items: [risk], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    fireEvent.change(screen.getByLabelText('Risk description'), { target: { value: 'Vendor renewal may lapse' } })
    fireEvent.change(screen.getByLabelText('Mitigation'), { target: { value: 'Confirm renewal terms' } })
    fireEvent.change(screen.getByLabelText('Trigger'), { target: { value: 'No signed contract' } })
    fireEvent.change(screen.getByLabelText('Review at'), { target: { value: '2026-08-01T00:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create risk' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    const init = fetch.mock.calls[1][1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({
      description: 'Vendor renewal may lapse',
      mitigation: 'Confirm renewal terms',
      trigger: 'No signed contract',
    })
    expect(String(init.body)).not.toMatch(/owner|workspace|actor/)
    expect(new Headers(init.headers).get('X-CSRF-Token')).toBe('risk-token')
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('risk-request-id')
  })

  it('blocks create submission with a client-side error when a required field is missing', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [], next_cursor: null })))
    renderWorkspace()

    fireEvent.change(screen.getByLabelText('Risk description'), { target: { value: 'Missing mitigation' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create risk' }))

    await screen.findByRole('alert')
    expect(screen.getByRole('alert').textContent).toMatch(/mitigation/i)
  })

  it('disables the status control once a risk is closed and shows its factors', async () => {
    const closed = { ...risk, id: 'closed-risk', description: 'Lapsed contract', status: 'closed', factors: [] }
    vi.stubGlobal('fetch', vi.fn(() => response({ items: [risk, closed], next_cursor: null })))
    renderWorkspace()

    await screen.findByText('Vendor renewal may lapse')
    expect(screen.getByLabelText('Factors for Vendor renewal may lapse').textContent).toContain('Risk impact 12')

    fireEvent.click(screen.getByRole('button', { name: 'Edit Lapsed contract' }))
    expect((screen.getByLabelText('Edit risk status') as HTMLSelectElement).disabled).toBe(true)
  })

  it('archives and restores risks with expected_version', async () => {
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [risk], next_cursor: null }))
      .mockImplementationOnce(() => response({ ...risk, archived_at: '2026-07-16T00:00:00Z', version: 3 }))
      .mockImplementationOnce(() => response({ items: [{ ...risk, archived_at: '2026-07-16T00:00:00Z', version: 3 }], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Vendor renewal may lapse')
    fireEvent.click(screen.getByRole('button', { name: 'Archive Vendor renewal may lapse' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    expect(fetch.mock.calls[1][0]).toContain('/api/v1/risks/risk-1/archive')
    expect(JSON.parse(String((fetch.mock.calls[1][1] as RequestInit).body))).toEqual({ expected_version: 2 })
    await screen.findByRole('button', { name: 'Restore Vendor renewal may lapse' })
  })

  it('preserves an edit and retries with the conflict current version', async () => {
    const current = { ...risk, version: 3, description: 'Changed elsewhere' }
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: {} } }
    const saved = { ...current, version: 4, description: 'My revised assessment' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [risk], next_cursor: null }))
      .mockImplementationOnce(() => response(conflict, 409))
      .mockImplementationOnce(() => response(current))
      .mockImplementationOnce(() => response(saved))
      .mockImplementationOnce(() => response({ items: [saved], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Vendor renewal may lapse')
    fireEvent.click(screen.getByRole('button', { name: 'Edit Vendor renewal may lapse' }))
    fireEvent.change(screen.getByLabelText('Edit risk description'), { target: { value: 'My revised assessment' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save risk' }))
    await screen.findByText(/changed while you were editing/i)
    expect((screen.getByLabelText('Edit risk description') as HTMLTextAreaElement).value).toBe('My revised assessment')
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
    fireEvent.click(screen.getByRole('button', { name: 'Retry with latest version' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(JSON.parse(String((fetch.mock.calls[3][1] as RequestInit).body))).toMatchObject({ expected_version: 3, description: 'My revised assessment' })
  })

  it('disables cross-row actions while any mutation is in flight', async () => {
    let resolveArchive!: (value: Response) => void
    const pendingArchive = new Promise<Response>((resolve) => { resolveArchive = resolve })
    const other = { ...risk, id: 'risk-2', description: 'Second risk' }
    const fetch = vi.fn()
      .mockImplementationOnce(() => response({ items: [risk, other], next_cursor: null }))
      .mockImplementationOnce(() => pendingArchive)
      .mockImplementationOnce(() => response({ items: [risk, other], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()

    await screen.findByText('Vendor renewal may lapse')
    fireEvent.click(screen.getByRole('button', { name: 'Archive Vendor renewal may lapse' }))
    await waitFor(() => expect((screen.getByRole('button', { name: 'Archive Second risk' }) as HTMLButtonElement).disabled).toBe(true))
    resolveArchive(new Response(JSON.stringify({ ...risk, archived_at: '2026-07-16T00:00:00Z', version: 3 }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(3))
  })
})
