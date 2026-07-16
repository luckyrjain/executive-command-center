// @vitest-environment jsdom

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ScheduleWorkspace, { wallTimeToInstant } from './ScheduleWorkspace'

const event = {
  id: 'event-1', title: 'Board review', starts_at: '2026-07-20T04:30:00Z', ends_at: '2026-07-20T05:30:00Z',
  all_day: false, timezone: 'Asia/Kolkata', location: 'Board room', description: 'Quarterly review', status: 'confirmed',
  external_source: 'local', external_id: null, source_authoritative: true, version: 4, archived_at: null,
  created_at: '2026-07-16T00:00:00Z', updated_at: '2026-07-16T00:00:00Z', pre_archive_status: null,
}
const linkedMeeting = {
  id: 'meeting-1', calendar_event_id: 'event-1', title: 'Board review', starts_at: event.starts_at, ends_at: event.ends_at,
  timezone: event.timezone, status: 'planned', agenda: 'Metrics', preparation: 'Read pack', notes_summary: null,
  version: 2, archived_at: null, created_at: event.created_at, updated_at: event.updated_at, pre_archive_status: null,
}
const standaloneMeeting = { ...linkedMeeting, id: 'meeting-2', calendar_event_id: null, title: 'Coaching session', version: 3 }

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }))
}

function initial(fetch: ReturnType<typeof vi.fn>, events = [event], meetings = [linkedMeeting, standaloneMeeting]) {
  fetch.mockImplementationOnce(() => response({ items: events, next_cursor: null }))
    .mockImplementationOnce(() => response({ items: meetings, next_cursor: null }))
  return fetch
}

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={client}><ScheduleWorkspace /></QueryClientProvider>)
}

beforeEach(() => {
  document.cookie = 'ecc_csrf=test-token; Secure; SameSite=Strict'
  vi.stubGlobal('crypto', { randomUUID: vi.fn(() => 'request-id') })
})

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('ScheduleWorkspace', () => {
  it('creates an event by converting IANA wall times and submitting only frozen schema fields', async () => {
    const fetch = initial(vi.fn(), [], [])
      .mockImplementationOnce(() => response(event, 201))
      .mockImplementationOnce(() => response({ items: [event], next_cursor: null }))
    vi.stubGlobal('fetch', fetch)
    renderWorkspace()
    await screen.findByText('No calendar events.')

    fireEvent.change(screen.getByLabelText('Event title'), { target: { value: 'Board review' } })
    fireEvent.change(screen.getByLabelText('Event start'), { target: { value: '2026-07-20T10:00' } })
    fireEvent.change(screen.getByLabelText('Event end'), { target: { value: '2026-07-20T11:00' } })
    fireEvent.change(screen.getByLabelText('Event timezone'), { target: { value: 'Asia/Kolkata' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create event' }))

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4))
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).toEqual({
      title: 'Board review', starts_at: '2026-07-20T04:30:00.000Z', ends_at: '2026-07-20T05:30:00.000Z',
      all_day: false, timezone: 'Asia/Kolkata', location: null, description: null, status: 'confirmed', external_id: null,
    })
  })

  it('archives and restores an event using the displayed versions', async () => {
    const archived = { ...event, archived_at: '2026-07-16T10:00:00Z', version: 5 }
    const restored = { ...event, version: 6 }
    const fetch = initial(vi.fn()).mockImplementationOnce(() => response(archived))
      .mockImplementationOnce(() => response({ items: [archived], next_cursor: null }))
      .mockImplementationOnce(() => response({ items: [linkedMeeting, standaloneMeeting], next_cursor: null }))
      .mockImplementationOnce(() => response(restored))
      .mockImplementationOnce(() => response({ items: [restored], next_cursor: null }))
      .mockImplementationOnce(() => response({ items: [linkedMeeting, standaloneMeeting], next_cursor: null }))
    vi.stubGlobal('fetch', fetch); renderWorkspace()
    await screen.findByRole('button', { name: 'Archive event Board review' })
    fireEvent.click(screen.getByRole('button', { name: 'Archive event Board review' }))
    await screen.findByRole('button', { name: 'Restore event Board review' })
    fireEvent.click(screen.getByRole('button', { name: 'Restore event Board review' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(8))
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).toEqual({ expected_version: 4 })
    expect(JSON.parse(String((fetch.mock.calls[5][1] as RequestInit).body))).toEqual({ expected_version: 5 })
  })

  it('locks linked meeting timing and reschedules it through the authoritative event PATCH', async () => {
    const changed = { ...event, starts_at: '2026-07-20T06:30:00Z', ends_at: '2026-07-20T07:30:00Z', version: 5 }
    const fetch = initial(vi.fn()).mockImplementationOnce(() => response(changed))
      .mockImplementationOnce(() => response({ items: [changed], next_cursor: null }))
      .mockImplementationOnce(() => response({ items: [{ ...linkedMeeting, starts_at: changed.starts_at, ends_at: changed.ends_at }, standaloneMeeting], next_cursor: null }))
    vi.stubGlobal('fetch', fetch); renderWorkspace()
    await screen.findByRole('button', { name: 'Edit meeting Board review' })
    fireEvent.click(screen.getByRole('button', { name: 'Edit meeting Board review' }))
    expect(screen.queryByLabelText('Edit meeting start')).toBeNull()
    expect(screen.getByText(/timing is controlled by its calendar event/i)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Reschedule Board review' }))
    fireEvent.change(screen.getByLabelText('Edit event start'), { target: { value: '2026-07-20T12:00' } })
    fireEvent.change(screen.getByLabelText('Edit event end'), { target: { value: '2026-07-20T13:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save event' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(fetch.mock.calls[2][0]).toContain('/api/v1/calendar/events/event-1')
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).toMatchObject({
      expected_version: 4, starts_at: '2026-07-20T06:30:00.000Z', ends_at: '2026-07-20T07:30:00.000Z',
    })
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).not.toHaveProperty('timezone')
  })

  it('creates a standalone meeting with exact API timing and content field mappings', async () => {
    const fetch = initial(vi.fn(), [], []).mockImplementationOnce(() => response(standaloneMeeting, 201))
      .mockImplementationOnce(() => response({ items: [standaloneMeeting], next_cursor: null }))
    vi.stubGlobal('fetch', fetch); renderWorkspace(); await screen.findByText('No meetings.')
    fireEvent.change(screen.getByLabelText('Meeting title'), { target: { value: 'Coaching session' } })
    fireEvent.change(screen.getByLabelText('Meeting start'), { target: { value: '2026-07-20T10:00' } })
    fireEvent.change(screen.getByLabelText('Meeting end'), { target: { value: '2026-07-20T11:00' } })
    fireEvent.change(screen.getByLabelText('Meeting timezone'), { target: { value: 'Asia/Kolkata' } })
    fireEvent.change(screen.getByLabelText('Meeting agenda'), { target: { value: 'Goals' } })
    fireEvent.change(screen.getByLabelText('Meeting preparation'), { target: { value: 'Reflect' } })
    fireEvent.change(screen.getByLabelText('Meeting notes summary'), { target: { value: 'Next steps' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create standalone meeting' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4))
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).toEqual({
      calendar_event_id: null, title: 'Coaching session', starts_at: '2026-07-20T04:30:00.000Z', ends_at: '2026-07-20T05:30:00.000Z',
      timezone: 'Asia/Kolkata', status: 'planned', agenda: 'Goals', preparation: 'Reflect', notes_summary: 'Next steps',
    })
  })

  it('preserves event edits across a conflict and retries only after loading the current version', async () => {
    const conflict = { error: { code: 'VERSION_CONFLICT', message: 'changed', details: { current_version: 5 } } }
    const current = { ...event, title: 'Changed elsewhere', version: 5 }
    const saved = { ...current, title: 'My board review', version: 6 }
    const fetch = initial(vi.fn()).mockImplementationOnce(() => response(conflict, 409))
      .mockImplementationOnce(() => response(current)).mockImplementationOnce(() => response(saved))
      .mockImplementationOnce(() => response({ items: [saved], next_cursor: null }))
      .mockImplementationOnce(() => response({ items: [linkedMeeting, standaloneMeeting], next_cursor: null }))
    vi.stubGlobal('fetch', fetch); renderWorkspace(); await screen.findByRole('button', { name: 'Edit event Board review' })
    fireEvent.click(screen.getByRole('button', { name: 'Edit event Board review' }))
    fireEvent.change(screen.getByLabelText('Edit event title'), { target: { value: 'My board review' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save event' }))
    await screen.findByText(/changed while you were editing/i)
    expect((screen.getByLabelText('Edit event title') as HTMLInputElement).value).toBe('My board review')
    fireEvent.click(screen.getByRole('button', { name: 'Retry event with latest version' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(7))
    expect(JSON.parse(String((fetch.mock.calls[4][1] as RequestInit).body))).toEqual({ expected_version: 5, title: 'My board review' })
  })

  it('reschedules a standalone meeting with all three exact timing fields', async () => {
    const changed = { ...standaloneMeeting, starts_at: '2026-07-20T06:30:00Z', ends_at: '2026-07-20T07:30:00Z', timezone: 'Asia/Kolkata', version: 4 }
    const fetch = initial(vi.fn()).mockImplementationOnce(() => response(changed))
      .mockImplementationOnce(() => response({ items: [linkedMeeting, changed], next_cursor: null }))
    vi.stubGlobal('fetch', fetch); renderWorkspace()
    await screen.findByRole('button', { name: 'Edit meeting Coaching session' })
    fireEvent.click(screen.getByRole('button', { name: 'Edit meeting Coaching session' }))
    fireEvent.change(screen.getByLabelText('Edit meeting start'), { target: { value: '2026-07-20T12:00' } })
    fireEvent.change(screen.getByLabelText('Edit meeting end'), { target: { value: '2026-07-20T13:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save meeting' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(4))
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).toEqual({
      expected_version: 3, starts_at: '2026-07-20T06:30:00.000Z', ends_at: '2026-07-20T07:30:00.000Z', timezone: 'Asia/Kolkata',
    })
  })

  it('refreshes projected meetings after event rescheduling', async () => {
    const changedEvent = { ...event, starts_at: '2026-07-20T06:30:00Z', ends_at: '2026-07-20T07:30:00Z', version: 5 }
    const projected = { ...linkedMeeting, starts_at: changedEvent.starts_at, ends_at: changedEvent.ends_at }
    const fetch = initial(vi.fn()).mockImplementationOnce(() => response(changedEvent))
      .mockImplementationOnce(() => response({ items: [changedEvent], next_cursor: null }))
      .mockImplementationOnce(() => response({ items: [projected, standaloneMeeting], next_cursor: null }))
    vi.stubGlobal('fetch', fetch); renderWorkspace(); await screen.findByRole('button', { name: 'Edit event Board review' })
    fireEvent.click(screen.getByRole('button', { name: 'Edit event Board review' }))
    fireEvent.change(screen.getByLabelText('Edit event start'), { target: { value: '2026-07-20T12:00' } })
    fireEvent.change(screen.getByLabelText('Edit event end'), { target: { value: '2026-07-20T13:00' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save event' }))
    await waitFor(() => expect(screen.getAllByText(/2026-07-20T12:00/).length).toBeGreaterThanOrEqual(2))
    expect(fetch).toHaveBeenCalledTimes(5)
  })

  it('loads an off-page authoritative event before linked rescheduling', async () => {
    const fetch = initial(vi.fn(), [], [linkedMeeting]).mockImplementationOnce(() => response(event))
    vi.stubGlobal('fetch', fetch); renderWorkspace(); await screen.findByRole('button', { name: 'Edit meeting Board review' })
    fireEvent.click(screen.getByRole('button', { name: 'Edit meeting Board review' }))
    fireEvent.click(screen.getByRole('button', { name: 'Reschedule Board review' }))
    await screen.findByLabelText('Edit event start')
    expect(fetch.mock.calls[2][0]).toContain('/api/v1/calendar/events/event-1')
  })

  it('rejects DST gaps and folds instead of silently choosing an instant', () => {
    expect(() => wallTimeToInstant('2026-03-08T02:30', 'America/New_York')).toThrow(/does not exist/i)
    expect(() => wallTimeToInstant('2026-11-01T01:30', 'America/New_York')).toThrow(/ambiguous/i)
    expect(() => wallTimeToInstant('2026-07-20T10:00', 'Mars/Olympus_Mons')).toThrow(/timezone/i)
  })

  it('edits and clears all mutable event metadata with a sparse PATCH', async () => {
    const changed = { ...event, all_day: true, location: null, description: null, status: 'tentative', version: 5 }
    const fetch = initial(vi.fn()).mockImplementationOnce(() => response(changed))
      .mockImplementationOnce(() => response({ items: [changed], next_cursor: null }))
      .mockImplementationOnce(() => response({ items: [linkedMeeting, standaloneMeeting], next_cursor: null }))
    vi.stubGlobal('fetch', fetch); renderWorkspace(); await screen.findByRole('button', { name: 'Edit event Board review' })
    fireEvent.click(screen.getByRole('button', { name: 'Edit event Board review' }))
    fireEvent.click(screen.getByLabelText('Edit event all day'))
    fireEvent.change(screen.getByLabelText('Edit event location'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Edit event description'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('Edit event status'), { target: { value: 'tentative' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save event' }))
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(5))
    expect(JSON.parse(String((fetch.mock.calls[2][1] as RequestInit).body))).toEqual({
      expected_version: 4, all_day: true, location: null, description: null, status: 'tentative',
    })
  })
})
