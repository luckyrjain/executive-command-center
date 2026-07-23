// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import MeetingPrep, { type MeetingPack } from './MeetingPrep'

const pack: MeetingPack = {
  id: 'pack-1',
  meeting_id: 'meeting-1',
  status: 'fresh',
  generated_at: '2026-07-23T00:00:00Z',
  stale_at: '2026-07-24T00:00:00Z',
  source_versions: {},
  objective: 'Review Q3 numbers',
  starts_at: '2026-07-24T09:00:00Z',
  ends_at: '2026-07-24T10:00:00Z',
  timezone: 'UTC',
  participants: [{ id: 'p-1', entity_id: 'entity-1', entity_name: 'Jordan Lee', role: 'organizer' }],
  timeline: [{ id: 't-1', entity_id: 'entity-1', effective_at: '2026-07-20T00:00:00Z', event_type: 'note_created', summary: 'Prior sync' }],
  commitments: [{ id: 'c-1', direction: 'made_to_me', summary: 'Send the report', status: 'active', due_at: null, counterparty_name: 'Jordan Lee' }],
  decisions: [{ id: 'd-1', title: 'Chose vendor', body: 'We picked Acme', note_type: 'decision', created_at: '2026-07-20T00:00:00Z' }],
  open_questions: [],
  notes: [],
  risks: [{ id: 'r-1', description: 'Vendor concentration', status: 'monitoring', probability: 3, impact: 4, review_at: null }],
  dependencies: [],
  evidence_gaps: [{ id: 'e-1', source_type: 'email', evidence_state: 'missing' }],
  enrichment: { available: false, summary: null, error_code: 'feature_disabled' },
}

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function renderPrep() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><MeetingPrep /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=meeting-prep-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'meeting-prep-request-id') })
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('MeetingPrep', () => {
  it('separates facts, open questions and suggestions into distinct sections with inline citations', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(pack)))
    renderPrep()

    fireEvent.change(screen.getByLabelText('Meeting ID'), { target: { value: 'meeting-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load meeting prep' }))

    await waitFor(() => expect(screen.getByText('Review Q3 numbers')).toBeTruthy())
    expect(screen.getByRole('heading', { name: 'Facts' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: 'Open questions' })).toBeTruthy()
    expect(screen.getByRole('heading', { name: /Suggested agenda/ })).toBeTruthy()
    expect(screen.getByText(/source: note d-1/)).toBeTruthy()
  })

  it('shows neutral evidence-gap copy, never alarming language, for missing evidence', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(pack)))
    renderPrep()

    fireEvent.change(screen.getByLabelText('Meeting ID'), { target: { value: 'meeting-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load meeting prep' }))

    await waitFor(() => expect(screen.getByText('Evidence not yet captured')).toBeTruthy())
  })

  it('shows the degraded/AI-unavailable state while keeping deterministic sections usable', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response(pack)))
    renderPrep()

    fireEvent.change(screen.getByLabelText('Meeting ID'), { target: { value: 'meeting-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load meeting prep' }))

    await waitFor(() => expect(screen.getByText(/AI-assisted suggestions are disabled/)).toBeTruthy())
    expect(screen.getByText('Review Q3 numbers')).toBeTruthy()
    expect(screen.getByText('Send the report', { exact: false })).toBeTruthy()
  })

  it('shows the stale state with a visible refresh action while keeping the pack readable', async () => {
    vi.stubGlobal('fetch', vi.fn(() => response({ ...pack, status: 'stale' })))
    renderPrep()

    fireEvent.change(screen.getByLabelText('Meeting ID'), { target: { value: 'meeting-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load meeting prep' }))

    await waitFor(() => expect(screen.getByText(/may be out of date/)).toBeTruthy())
    expect(screen.getByRole('button', { name: 'Refresh now' })).toBeTruthy()
    expect(screen.getByText('Review Q3 numbers')).toBeTruthy()
  })

  it('shows a distinct not-found state guiding the operator to generate a pack', async () => {
    // The query passes retry: 1, overriding the QueryClient's own retry:
    // false default, and the mock fails on the retried attempt too --
    // React Query's default backoff delays that retry by ~1s, so this
    // needs a longer-than-default findByText timeout (matching
    // ResolutionInbox.test.tsx's identical retry:1 pattern).
    vi.stubGlobal('fetch', vi.fn(() => response({ error: { code: 'MEETING_PACK_NOT_FOUND', message: 'not found' } }, 404)))
    renderPrep()

    fireEvent.change(screen.getByLabelText('Meeting ID'), { target: { value: 'meeting-1' } })
    fireEvent.click(screen.getByRole('button', { name: 'Load meeting prep' }))

    await screen.findByText(/No preparation pack exists yet/, {}, { timeout: 3000 })
  })
})
