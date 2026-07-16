import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import type { CalendarEvent, EntityList, EventDraft, Meeting, MeetingDraft } from './scheduleTypes'

const emptyEvent: EventDraft = { title: '', startsAt: '', endsAt: '', timezone: 'UTC', allDay: false, location: '', description: '', status: 'confirmed' }
const emptyMeeting: MeetingDraft = { calendarEventId: '', title: '', startsAt: '', endsAt: '', timezone: 'UTC', status: 'planned', agenda: '', preparation: '', notesSummary: '' }
type EventEdit = EventDraft & { record: CalendarEvent; latestVersion: number; conflict: boolean; reloadFailed: boolean }
type MeetingEdit = Pick<MeetingDraft, 'title' | 'status' | 'agenda' | 'preparation' | 'notesSummary' | 'startsAt' | 'endsAt' | 'timezone'> & { record: Meeting; latestVersion: number; conflict: boolean; reloadFailed: boolean }

/** Formats an instant as a datetime-local value in the record's authoritative IANA zone. */
export function instantToWallTime(value: string, timezone: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: timezone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(date)
  const get = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}`
}

/** Converts an IANA-zone wall time without relying on the browser's own timezone. */
export function wallTimeToInstant(value: string, timezone: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value)
  if (!match) throw new Error('Enter a complete date and time.')
  const [, year, month, day, hour, minute] = match
  const desiredUtc = Date.UTC(+year, +month - 1, +day, +hour, +minute)
  const offsets = new Set<number>()
  try {
    for (const deltaHours of [-36, -12, 0, 12, 36]) {
      const sample = desiredUtc + deltaHours * 60 * 60 * 1000
      const rendered = instantToWallTime(new Date(sample).toISOString(), timezone)
      const renderedMatch = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(rendered)
      if (!renderedMatch) throw new Error('Invalid IANA timezone.')
      const renderedUtc = Date.UTC(+renderedMatch[1], +renderedMatch[2] - 1, +renderedMatch[3], +renderedMatch[4], +renderedMatch[5])
      offsets.add(renderedUtc - sample)
    }
  } catch (error) {
    if (error instanceof RangeError) throw new Error('Enter a valid IANA timezone.')
    throw error
  }
  const candidates = [...offsets].map((offset) => desiredUtc - offset)
    .filter((candidate) => instantToWallTime(new Date(candidate).toISOString(), timezone) === value)
  const unique = [...new Set(candidates)]
  if (!unique.length) throw new Error('That local time does not exist in the selected timezone.')
  if (unique.length > 1) throw new Error('That local time is ambiguous in the selected timezone. Choose a time outside the daylight-saving fold.')
  return new Date(unique[0]).toISOString()
}

function eventDraft(record: CalendarEvent): EventDraft {
  return { title: record.title, startsAt: instantToWallTime(record.starts_at, record.timezone), endsAt: instantToWallTime(record.ends_at, record.timezone), timezone: record.timezone, allDay: record.all_day, location: record.location ?? '', description: record.description ?? '', status: record.status }
}
function meetingContent(record: Meeting): MeetingEdit {
  return { record, title: record.title, startsAt: instantToWallTime(record.starts_at, record.timezone), endsAt: instantToWallTime(record.ends_at, record.timezone), timezone: record.timezone, status: record.status, agenda: record.agenda ?? '', preparation: record.preparation ?? '', notesSummary: record.notes_summary ?? '', latestVersion: record.version, conflict: false, reloadFailed: false }
}
function eventBody(draft: EventDraft) {
  return { title: draft.title.trim(), starts_at: wallTimeToInstant(draft.startsAt, draft.timezone), ends_at: wallTimeToInstant(draft.endsAt, draft.timezone), all_day: draft.allDay, timezone: draft.timezone.trim(), location: draft.location.trim() || null, description: draft.description.trim() || null, status: draft.status }
}
function eventPatchBody(draft: EventEdit) {
  const original = eventDraft(draft.record)
  const current = eventBody(draft)
  const result: Partial<ReturnType<typeof eventBody>> = {}
  if (draft.title.trim() !== original.title.trim()) result.title = current.title
  if (draft.startsAt !== original.startsAt || draft.timezone !== original.timezone) result.starts_at = current.starts_at
  if (draft.endsAt !== original.endsAt || draft.timezone !== original.timezone) result.ends_at = current.ends_at
  if (draft.allDay !== original.allDay) result.all_day = current.all_day
  if (draft.timezone.trim() !== original.timezone) result.timezone = current.timezone
  if (draft.location.trim() !== original.location.trim()) result.location = current.location
  if (draft.description.trim() !== original.description.trim()) result.description = current.description
  if (draft.status !== original.status) result.status = current.status
  return result
}
function meetingContentBody(draft: MeetingEdit) {
  const result: Record<string, string | null> = {}
  if (draft.title.trim() !== draft.record.title.trim()) result.title = draft.title.trim()
  if (draft.status !== draft.record.status) result.status = draft.status
  if (draft.agenda.trim() !== (draft.record.agenda ?? '').trim()) result.agenda = draft.agenda.trim() || null
  if (draft.preparation.trim() !== (draft.record.preparation ?? '').trim()) result.preparation = draft.preparation.trim() || null
  if (draft.notesSummary.trim() !== (draft.record.notes_summary ?? '').trim()) result.notes_summary = draft.notesSummary.trim() || null
  const originalStart = instantToWallTime(draft.record.starts_at, draft.record.timezone)
  const originalEnd = instantToWallTime(draft.record.ends_at, draft.record.timezone)
  if (draft.startsAt !== originalStart || draft.endsAt !== originalEnd || draft.timezone !== draft.record.timezone) {
    result.starts_at = wallTimeToInstant(draft.startsAt, draft.timezone)
    result.ends_at = wallTimeToInstant(draft.endsAt, draft.timezone)
    result.timezone = draft.timezone.trim()
  }
  return result
}

export default function ScheduleWorkspace() {
  const client = useQueryClient()
  const events = useQuery({ queryKey: ['calendar-events'], queryFn: () => apiRequest<EntityList<CalendarEvent>>('/api/v1/calendar/events?include_archived=true&limit=100'), retry: 1 })
  const meetings = useQuery({ queryKey: ['meetings'], queryFn: () => apiRequest<EntityList<Meeting>>('/api/v1/meetings?include_archived=true&limit=100'), retry: 1 })
  const [createEvent, setCreateEvent] = useState(emptyEvent)
  const [createMeeting, setCreateMeeting] = useState(emptyMeeting)
  const [editEvent, setEditEvent] = useState<EventEdit | null>(null)
  const [editMeeting, setEditMeeting] = useState<MeetingEdit | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const refreshEvents = () => client.invalidateQueries({ queryKey: ['calendar-events'] })
  const refreshMeetings = () => client.invalidateQueries({ queryKey: ['meetings'] })

  async function reloadEvent(id: string) {
    try {
      const current = await apiRequest<CalendarEvent>(`/api/v1/calendar/events/${id}`)
      setEditEvent((draft) => draft?.record.id === id ? { ...draft, latestVersion: current.version, conflict: true, reloadFailed: false } : draft)
    } catch { setEditEvent((draft) => draft?.record.id === id ? { ...draft, latestVersion: 0, conflict: false, reloadFailed: true } : draft) }
  }
  async function reloadMeeting(id: string) {
    try {
      const current = await apiRequest<Meeting>(`/api/v1/meetings/${id}`)
      setEditMeeting((draft) => draft?.record.id === id ? { ...draft, latestVersion: current.version, conflict: true, reloadFailed: false } : draft)
    } catch { setEditMeeting((draft) => draft?.record.id === id ? { ...draft, latestVersion: 0, conflict: false, reloadFailed: true } : draft) }
  }

  const createEventMutation = useMutation({
    mutationFn: (draft: EventDraft) => apiRequest<CalendarEvent>('/api/v1/calendar/events', { method: 'POST', body: { ...eventBody(draft), external_id: null } }),
    onSuccess: () => { setCreateEvent(emptyEvent); void refreshEvents() },
  })
  const saveEventMutation = useMutation({
    mutationFn: ({ draft, version }: { draft: EventEdit; version: number }) => apiRequest<CalendarEvent>(`/api/v1/calendar/events/${draft.record.id}`, { method: 'PATCH', body: { expected_version: version, ...eventPatchBody(draft) } }),
    onSuccess: () => { setEditEvent(null); void Promise.all([refreshEvents(), refreshMeetings()]) },
    onError: async (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT' && editEvent) await reloadEvent(editEvent.record.id) },
  })
  const eventAction = useMutation({
    mutationFn: ({ record, action }: { record: CalendarEvent; action: 'archive' | 'restore' }) => apiRequest<CalendarEvent>(`/api/v1/calendar/events/${record.id}/${action}`, { method: 'POST', body: { expected_version: record.version } }),
    onSuccess: () => { void Promise.all([refreshEvents(), refreshMeetings()]) }, onError: (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') void Promise.all([refreshEvents(), refreshMeetings()]) },
  })
  const createMeetingMutation = useMutation({
    mutationFn: (draft: MeetingDraft) => apiRequest<Meeting>('/api/v1/meetings', { method: 'POST', body: draft.calendarEventId ? {
      calendar_event_id: draft.calendarEventId, title: draft.title.trim(), status: draft.status, agenda: draft.agenda.trim() || null, preparation: draft.preparation.trim() || null, notes_summary: draft.notesSummary.trim() || null,
    } : {
      calendar_event_id: null, title: draft.title.trim(), starts_at: wallTimeToInstant(draft.startsAt, draft.timezone), ends_at: wallTimeToInstant(draft.endsAt, draft.timezone), timezone: draft.timezone.trim(), status: draft.status, agenda: draft.agenda.trim() || null, preparation: draft.preparation.trim() || null, notes_summary: draft.notesSummary.trim() || null,
    } }),
    onSuccess: () => { setCreateMeeting(emptyMeeting); void refreshMeetings() },
  })
  const saveMeetingMutation = useMutation({
    mutationFn: ({ draft, version }: { draft: MeetingEdit; version: number }) => apiRequest<Meeting>(`/api/v1/meetings/${draft.record.id}`, { method: 'PATCH', body: { expected_version: version, ...meetingContentBody(draft) } }),
    onSuccess: () => { setEditMeeting(null); void refreshMeetings() },
    onError: async (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT' && editMeeting) await reloadMeeting(editMeeting.record.id) },
  })
  const meetingAction = useMutation({
    mutationFn: ({ record, action }: { record: Meeting; action: 'archive' | 'restore' }) => apiRequest<Meeting>(`/api/v1/meetings/${record.id}/${action}`, { method: 'POST', body: { expected_version: record.version } }),
    onSuccess: () => { void refreshMeetings() }, onError: (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') void refreshMeetings() },
  })
  const eventLookup = useMutation({
    mutationFn: (eventId: string) => apiRequest<CalendarEvent>(`/api/v1/calendar/events/${eventId}`),
    onSuccess: (record) => setEditEvent({ record, ...eventDraft(record), latestVersion: record.version, conflict: false, reloadFailed: false }),
  })
  const pending = createEventMutation.isPending || saveEventMutation.isPending || eventAction.isPending || createMeetingMutation.isPending || saveMeetingMutation.isPending || meetingAction.isPending || eventLookup.isPending
  const mutationError = createEventMutation.error ?? saveEventMutation.error ?? eventAction.error ?? createMeetingMutation.error ?? saveMeetingMutation.error ?? meetingAction.error ?? eventLookup.error

  function safeSubmit(work: () => void) { setFormError(null); try { work() } catch (error) { setFormError(error instanceof Error ? error.message : 'Invalid schedule input.') } }
  function submitEvent(event: FormEvent) { event.preventDefault(); if (createEvent.title.trim()) safeSubmit(() => createEventMutation.mutate(createEvent)) }
  function submitEventEdit(event?: FormEvent) { event?.preventDefault(); if (editEvent?.title.trim() && editEvent.latestVersion > 0) safeSubmit(() => saveEventMutation.mutate({ draft: editEvent, version: editEvent.latestVersion })) }
  function submitMeeting(event: FormEvent) { event.preventDefault(); if (createMeeting.title.trim()) safeSubmit(() => createMeetingMutation.mutate(createMeeting)) }
  function submitMeetingEdit(event?: FormEvent) { event?.preventDefault(); if (editMeeting?.title.trim() && editMeeting.latestVersion > 0) saveMeetingMutation.mutate({ draft: editMeeting, version: editMeeting.latestVersion }) }

  return <section className="schedule-workspace" aria-labelledby="schedule-title">
    <div className="work-heading"><div><p className="eyebrow">SCHEDULE</p><h1 id="schedule-title">Calendar & meetings</h1><p>Calendar events own linked timing. Meeting records own agenda, preparation and notes.</p></div></div>
    {formError ? <div role="alert" className="inline-status error-panel">{formError}</div> : null}
    {mutationError ? <div role="alert" className="inline-status error-panel">{mutationError instanceof ApiError && mutationError.code === 'VERSION_CONFLICT' ? 'This schedule item changed while you were editing it. Your input is preserved; retry after the latest version loads.' : mutationError.message}</div> : null}
    <div className="work-grid">
      <section className="work-panel"><form onSubmit={submitEvent}><h2>Create calendar event</h2>
        <label>Event title<input aria-label="Event title" required value={createEvent.title} onChange={(e) => setCreateEvent({ ...createEvent, title: e.target.value })} /></label>
        <TimingFields prefix="Event" draft={createEvent} onChange={setCreateEvent} />
        <label><input type="checkbox" checked={createEvent.allDay} onChange={(e) => setCreateEvent({ ...createEvent, allDay: e.target.checked })} /> All day</label>
        <label>Location<input value={createEvent.location} onChange={(e) => setCreateEvent({ ...createEvent, location: e.target.value })} /></label>
        <label>Description<textarea value={createEvent.description} onChange={(e) => setCreateEvent({ ...createEvent, description: e.target.value })} /></label>
        <button type="submit" disabled={pending}>Create event</button>
      </form></section>
      <section className="work-panel"><form onSubmit={submitMeeting}><h2>Create meeting</h2>
        <label>Linked calendar event<select aria-label="Linked calendar event" value={createMeeting.calendarEventId} onChange={(e) => setCreateMeeting({ ...createMeeting, calendarEventId: e.target.value })}><option value="">Standalone meeting</option>{(events.data?.items ?? []).filter((item) => !item.archived_at).map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
        <label>Meeting title<input aria-label="Meeting title" required value={createMeeting.title} onChange={(e) => setCreateMeeting({ ...createMeeting, title: e.target.value })} /></label>
        {createMeeting.calendarEventId ? <p className="inline-status">Timing will be projected from the selected calendar event.</p> : <TimingFields prefix="Meeting" draft={createMeeting} onChange={setCreateMeeting} />}
        <MeetingFields draft={createMeeting} onChange={setCreateMeeting} />
        <button type="submit" disabled={pending}>{createMeeting.calendarEventId ? 'Create linked meeting' : 'Create standalone meeting'}</button>
      </form></section>
    </div>
    <div className="work-grid">
      <section className="work-panel"><h2>Calendar events</h2>{events.isLoading ? <p role="status">Loading calendar events…</p> : null}{events.isError ? <div role="alert">{events.error.message}</div> : null}
        {!events.isLoading && !(events.data?.items.length) ? <p className="empty-state">No calendar events.</p> : null}
        <ol className="work-list">{(events.data?.items ?? []).map((record) => <li key={record.id}><div><strong>{record.title}</strong><small>{instantToWallTime(record.starts_at, record.timezone)} · {record.timezone} · {record.status}{record.source_authoritative ? ' · authoritative event' : ''}</small></div><div className="work-actions">
          {!record.archived_at ? <><button type="button" disabled={pending} aria-label={`Edit event ${record.title}`} onClick={() => { setEditMeeting(null); setEditEvent({ record, ...eventDraft(record), latestVersion: record.version, conflict: false, reloadFailed: false }) }}>Edit</button><button type="button" disabled={pending} aria-label={`Archive event ${record.title}`} onClick={() => eventAction.mutate({ record, action: 'archive' })}>Archive</button></> : <button type="button" disabled={pending} aria-label={`Restore event ${record.title}`} onClick={() => eventAction.mutate({ record, action: 'restore' })}>Restore</button>}
        </div></li>)}</ol>
      </section>
      <section className="work-panel"><h2>Meetings</h2>{meetings.isLoading ? <p role="status">Loading meetings…</p> : null}{meetings.isError ? <div role="alert">{meetings.error.message}</div> : null}
        {!meetings.isLoading && !(meetings.data?.items.length) ? <p className="empty-state">No meetings.</p> : null}
        <ol className="work-list">{(meetings.data?.items ?? []).map((record) => <li key={record.id}><div><strong>{record.title}</strong><small>{instantToWallTime(record.starts_at, record.timezone)} · {record.timezone} · {record.calendar_event_id ? 'timing from calendar event' : 'standalone timing'} · {record.status}</small></div><div className="work-actions">
          {!record.archived_at ? <><button type="button" disabled={pending} aria-label={`Edit meeting ${record.title}`} onClick={() => { setEditEvent(null); setEditMeeting(meetingContent(record)) }}>Edit</button><button type="button" disabled={pending} aria-label={`Archive meeting ${record.title}`} onClick={() => meetingAction.mutate({ record, action: 'archive' })}>Archive</button></> : <button type="button" disabled={pending} aria-label={`Restore meeting ${record.title}`} onClick={() => meetingAction.mutate({ record, action: 'restore' })}>Restore</button>}
        </div></li>)}</ol>
      </section>
    </div>
    {editEvent ? <section className="work-panel"><form onSubmit={submitEventEdit}><h2>Edit calendar event</h2><p>This calendar event is the authoritative timing record.</p>
      <label>Edit event title<input aria-label="Edit event title" value={editEvent.title} onChange={(e) => setEditEvent({ ...editEvent, title: e.target.value })} /></label><TimingFields prefix="Edit event" draft={editEvent} onChange={(value) => setEditEvent({ ...editEvent, ...value })} />
      <label><input aria-label="Edit event all day" type="checkbox" checked={editEvent.allDay} onChange={(e) => setEditEvent({ ...editEvent, allDay: e.target.checked })} /> All day</label>
      <label>Edit event location<input aria-label="Edit event location" value={editEvent.location} onChange={(e) => setEditEvent({ ...editEvent, location: e.target.value })} /></label>
      <label>Edit event description<textarea aria-label="Edit event description" value={editEvent.description} onChange={(e) => setEditEvent({ ...editEvent, description: e.target.value })} /></label>
      <label>Edit event status<select aria-label="Edit event status" value={editEvent.status} onChange={(e) => setEditEvent({ ...editEvent, status: e.target.value as EventDraft['status'] })}><option value="confirmed">confirmed</option><option value="tentative">tentative</option><option value="cancelled">cancelled</option></select></label>
      {editEvent.reloadFailed ? <><p role="alert">Could not reload the latest event. Your edits are preserved.</p><button type="button" disabled={pending} onClick={() => void reloadEvent(editEvent.record.id)}>Reload latest event</button></> : editEvent.conflict ? <button type="button" disabled={pending} onClick={() => submitEventEdit()}>Retry event with latest version</button> : <button type="submit" disabled={pending}>Save event</button>}
      <button type="button" disabled={pending} onClick={() => setEditEvent(null)}>Discard event edit</button>
    </form></section> : null}
    {editMeeting ? <section className="work-panel"><form onSubmit={submitMeetingEdit}><h2>Edit meeting</h2>
      {editMeeting.record.calendar_event_id ? <><p className="inline-status">Linked meeting timing is controlled by its calendar event and is display-only here.</p><button type="button" disabled={pending} onClick={() => { const eventId = editMeeting.record.calendar_event_id; if (!eventId) return; const authoritative = events.data?.items.find((item) => item.id === eventId); if (authoritative) setEditEvent({ record: authoritative, ...eventDraft(authoritative), latestVersion: authoritative.version, conflict: false, reloadFailed: false }); else eventLookup.mutate(eventId) }}>Reschedule {editMeeting.record.title}</button></> : <TimingFields prefix="Edit meeting" draft={editMeeting} onChange={(value) => setEditMeeting({ ...editMeeting, ...value })} />}
      <label>Edit meeting title<input value={editMeeting.title} onChange={(e) => setEditMeeting({ ...editMeeting, title: e.target.value })} /></label><MeetingFields draft={editMeeting} onChange={(value) => setEditMeeting({ ...editMeeting, ...value })} />
      {editMeeting.reloadFailed ? <><p role="alert">Could not reload the latest meeting. Your edits are preserved.</p><button type="button" disabled={pending} onClick={() => void reloadMeeting(editMeeting.record.id)}>Reload latest meeting</button></> : editMeeting.conflict ? <button type="button" disabled={pending} onClick={() => submitMeetingEdit()}>Retry meeting with latest version</button> : <button type="submit" disabled={pending}>Save meeting</button>}
      <button type="button" disabled={pending} onClick={() => setEditMeeting(null)}>Discard meeting edit</button>
    </form></section> : null}
  </section>
}

function TimingFields<T extends { startsAt: string; endsAt: string; timezone: string }>({ prefix, draft, onChange }: { prefix: string; draft: T; onChange: (value: T) => void }) {
  return <><label>{prefix} start<input aria-label={`${prefix} start`} type="datetime-local" required value={draft.startsAt} onChange={(e) => onChange({ ...draft, startsAt: e.target.value })} /></label><label>{prefix} end<input aria-label={`${prefix} end`} type="datetime-local" required value={draft.endsAt} onChange={(e) => onChange({ ...draft, endsAt: e.target.value })} /></label><label>{prefix} timezone<input aria-label={`${prefix} timezone`} required value={draft.timezone} onChange={(e) => onChange({ ...draft, timezone: e.target.value })} /></label></>
}

function MeetingFields<T extends { status: MeetingDraft['status']; agenda: string; preparation: string; notesSummary: string }>({ draft, onChange }: { draft: T; onChange: (value: T) => void }) {
  return <><label>Meeting status<select value={draft.status} onChange={(e) => onChange({ ...draft, status: e.target.value as MeetingDraft['status'] })}><option value="planned">planned</option><option value="in_progress">in progress</option><option value="completed">completed</option><option value="cancelled">cancelled</option></select></label><label>Agenda<textarea aria-label="Meeting agenda" value={draft.agenda} onChange={(e) => onChange({ ...draft, agenda: e.target.value })} /></label><label>Preparation<textarea aria-label="Meeting preparation" value={draft.preparation} onChange={(e) => onChange({ ...draft, preparation: e.target.value })} /></label><label>Notes summary<textarea aria-label="Meeting notes summary" value={draft.notesSummary} onChange={(e) => onChange({ ...draft, notesSummary: e.target.value })} /></label></>
}
