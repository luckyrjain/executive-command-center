import { useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { applyWizardFieldInvalidState, useWizardStepFocus } from '../../lib/wizardFocus'
import type { CalendarEvent, EntityList, EventDraft, Meeting, MeetingDraft } from './scheduleTypes'

const emptyEvent: EventDraft = { title: '', startsAt: '', endsAt: '', timezone: 'UTC', allDay: false, location: '', description: '', status: 'confirmed' }
const emptyMeeting: MeetingDraft = { calendarEventId: '', title: '', startsAt: '', endsAt: '', timezone: 'UTC', status: 'planned', agenda: '', preparation: '', notesSummary: '' }
const CREATE_EVENT_STEPS = ['basics', 'details', 'review'] as const
const CREATE_EVENT_STEP_LABELS: Record<(typeof CREATE_EVENT_STEPS)[number], string> = { basics: 'Basics', details: 'Details', review: 'Review' }
const CREATE_MEETING_STEPS = ['basics', 'notes', 'review'] as const
const CREATE_MEETING_STEP_LABELS: Record<(typeof CREATE_MEETING_STEPS)[number], string> = { basics: 'Basics', notes: 'Notes', review: 'Review' }
const SCHEDULE_ERROR_ID = 'schedule-form-error'
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
  const [createEventStepIndex, setCreateEventStepIndex] = useState(0)
  const [createMeetingStepIndex, setCreateMeetingStepIndex] = useState(0)
  const createEventFormRef = useRef<HTMLFormElement>(null)
  const createMeetingFormRef = useRef<HTMLFormElement>(null)
  const createEventStep = CREATE_EVENT_STEPS[createEventStepIndex] ?? 'basics'
  const createMeetingStep = CREATE_MEETING_STEPS[createMeetingStepIndex] ?? 'basics'
  const [invalidEventField, setInvalidEventField] = useState<string | null>(null)
  const [invalidMeetingField, setInvalidMeetingField] = useState<string | null>(null)
  const createEventStepHeadingRef = useWizardStepFocus(
    () => applyWizardFieldInvalidState(createEventFormRef.current, invalidEventField, SCHEDULE_ERROR_ID, createEventStepHeadingRef.current),
    [createEventStep, invalidEventField],
  )
  const createMeetingStepHeadingRef = useWizardStepFocus(
    () => applyWizardFieldInvalidState(createMeetingFormRef.current, invalidMeetingField, SCHEDULE_ERROR_ID, createMeetingStepHeadingRef.current),
    [createMeetingStep, invalidMeetingField],
  )
  const refreshEvents = () => Promise.all([
    client.invalidateQueries({ queryKey: ['calendar-events'] }),
    client.invalidateQueries({ queryKey: ['dashboard', 'today'] }),
    client.invalidateQueries({ queryKey: ['brief', 'morning'] }),
  ])
  const refreshMeetings = () => Promise.all([
    client.invalidateQueries({ queryKey: ['meetings'] }),
    client.invalidateQueries({ queryKey: ['dashboard', 'today'] }),
    client.invalidateQueries({ queryKey: ['brief', 'morning'] }),
  ])

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
    onSuccess: () => { setCreateEvent(emptyEvent); setCreateEventStepIndex(0); setInvalidEventField(null); void refreshEvents() },
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
    onSuccess: () => { setCreateMeeting(emptyMeeting); setCreateMeetingStepIndex(0); setInvalidMeetingField(null); void refreshMeetings() },
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
  function failEvent(message: string, field: string, step: (typeof CREATE_EVENT_STEPS)[number]) {
    setFormError(message)
    setInvalidEventField(field)
    setCreateEventStepIndex(CREATE_EVENT_STEPS.indexOf(step))
  }
  function attemptCreateEvent(event: FormEvent) {
    event.preventDefault()
    if (!createEvent.title.trim()) { failEvent('Event title is required.', 'Event title', 'basics'); return }
    // wallTimeToInstant throws the same "Enter a complete date and time."
    // message for a blank/malformed start or end, so it's called once per
    // field, in order, to know which one is actually invalid -- a single
    // call inside eventBody (as createEventMutation's mutationFn does) can't
    // distinguish start from end from a bad timezone.
    try {
      wallTimeToInstant(createEvent.startsAt, createEvent.timezone)
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Invalid schedule input.'
      failEvent(message, message.toLowerCase().includes('timezone') ? 'Event timezone' : 'Event start', 'basics')
      return
    }
    try {
      wallTimeToInstant(createEvent.endsAt, createEvent.timezone)
    } catch (error) {
      const message = error instanceof Error ? error.message : 'Invalid schedule input.'
      failEvent(message, message.toLowerCase().includes('timezone') ? 'Event timezone' : 'Event end', 'basics')
      return
    }
    setFormError(null)
    setInvalidEventField(null)
    try {
      createEventMutation.mutate(createEvent)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Invalid schedule input.')
    }
  }
  function submitEventEdit(event?: FormEvent) { event?.preventDefault(); if (editEvent?.title.trim() && editEvent.latestVersion > 0) safeSubmit(() => saveEventMutation.mutate({ draft: editEvent, version: editEvent.latestVersion })) }
  function failMeeting(message: string, field: string, step: (typeof CREATE_MEETING_STEPS)[number]) {
    setFormError(message)
    setInvalidMeetingField(field)
    setCreateMeetingStepIndex(CREATE_MEETING_STEPS.indexOf(step))
  }
  function attemptCreateMeeting(event: FormEvent) {
    event.preventDefault()
    if (!createMeeting.title.trim()) { failMeeting('Meeting title is required.', 'Meeting title', 'basics'); return }
    // Timing only applies to a standalone meeting -- a linked meeting's
    // timing is projected from its calendar event and isn't even rendered.
    if (!createMeeting.calendarEventId) {
      try {
        wallTimeToInstant(createMeeting.startsAt, createMeeting.timezone)
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Invalid schedule input.'
        failMeeting(message, message.toLowerCase().includes('timezone') ? 'Meeting timezone' : 'Meeting start', 'basics')
        return
      }
      try {
        wallTimeToInstant(createMeeting.endsAt, createMeeting.timezone)
      } catch (error) {
        const message = error instanceof Error ? error.message : 'Invalid schedule input.'
        failMeeting(message, message.toLowerCase().includes('timezone') ? 'Meeting timezone' : 'Meeting end', 'basics')
        return
      }
    }
    setFormError(null)
    setInvalidMeetingField(null)
    try {
      createMeetingMutation.mutate(createMeeting)
    } catch (error) {
      setFormError(error instanceof Error ? error.message : 'Invalid schedule input.')
    }
  }
  function submitMeetingEdit(event?: FormEvent) { event?.preventDefault(); if (editMeeting?.title.trim() && editMeeting.latestVersion > 0) saveMeetingMutation.mutate({ draft: editMeeting, version: editMeeting.latestVersion }) }
  function goCreateEventNext() { setInvalidEventField(null); setCreateEventStepIndex((i) => Math.min(i + 1, CREATE_EVENT_STEPS.length - 1)) }
  function goCreateEventBack() { setInvalidEventField(null); setCreateEventStepIndex((i) => Math.max(i - 1, 0)) }
  function goCreateMeetingNext() { setInvalidMeetingField(null); setCreateMeetingStepIndex((i) => Math.min(i + 1, CREATE_MEETING_STEPS.length - 1)) }
  function goCreateMeetingBack() { setInvalidMeetingField(null); setCreateMeetingStepIndex((i) => Math.max(i - 1, 0)) }

  return <section className="schedule-workspace" aria-labelledby="schedule-title">
    <div className="work-heading"><div><p className="eyebrow">SCHEDULE</p><h1 id="schedule-title">Calendar & meetings</h1><p>Calendar events own linked timing. Meeting records own agenda, preparation and notes.</p></div></div>
    {formError ? <div id={SCHEDULE_ERROR_ID} role="alert" className="inline-status error-panel">{formError}</div> : null}
    {mutationError ? <div role="alert" className="inline-status error-panel">{mutationError instanceof ApiError && mutationError.code === 'VERSION_CONFLICT' ? 'This schedule item changed while you were editing it. Your input is preserved; retry after the latest version loads.' : mutationError.message}</div> : null}
    <div className="work-grid">
      <section className="work-panel">
        <h2 id="create-event-title">Create calendar event</h2>
        <form ref={createEventFormRef} noValidate onSubmit={attemptCreateEvent} aria-labelledby="create-event-title">
        <ol className="wizard-stepper" aria-label="Create event progress">
          {CREATE_EVENT_STEPS.map((step, i) => (
            <li className="wizard-step-node" key={step} aria-current={i === createEventStepIndex ? 'step' : undefined}>
              <span className={i < createEventStepIndex ? 'wizard-step-circle done' : i === createEventStepIndex ? 'wizard-step-circle active' : 'wizard-step-circle upcoming'}>{i < createEventStepIndex ? '✓' : i + 1}</span>
              <span className={i <= createEventStepIndex ? 'wizard-step-label on' : 'wizard-step-label'}>{CREATE_EVENT_STEP_LABELS[step]}</span>
              {i < CREATE_EVENT_STEPS.length - 1 ? <span className={i < createEventStepIndex ? 'wizard-step-line done' : 'wizard-step-line'} /> : null}
            </li>
          ))}
        </ol>
        {createEventStep === 'basics' ? (
          <div className="field-form">
            <p className="eyebrow">Step {createEventStepIndex + 1} of {CREATE_EVENT_STEPS.length} · Basics</p>
            <h3 ref={createEventStepHeadingRef} tabIndex={-1}>What and when?</h3>
            <label>Event title<input aria-label="Event title" value={createEvent.title} onChange={(e) => setCreateEvent({ ...createEvent, title: e.target.value })} /></label>
            <TimingFields prefix="Event" draft={createEvent} onChange={setCreateEvent} />
            <label className="field-checkbox"><input type="checkbox" checked={createEvent.allDay} onChange={(e) => setCreateEvent({ ...createEvent, allDay: e.target.checked })} /> All day</label>
            <div className="work-actions"><button type="button" onClick={goCreateEventNext}>Continue</button></div>
          </div>
        ) : createEventStep === 'details' ? (
          <div className="field-form">
            <p className="eyebrow">Step {createEventStepIndex + 1} of {CREATE_EVENT_STEPS.length} · Details</p>
            <h3 ref={createEventStepHeadingRef} tabIndex={-1}>Anything else?</h3>
            <label>Location<input value={createEvent.location} onChange={(e) => setCreateEvent({ ...createEvent, location: e.target.value })} /></label>
            <label>Description<textarea value={createEvent.description} onChange={(e) => setCreateEvent({ ...createEvent, description: e.target.value })} /></label>
            <div className="work-actions"><button type="button" onClick={goCreateEventBack}>Back</button><button type="button" onClick={goCreateEventNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createEventStepIndex + 1} of {CREATE_EVENT_STEPS.length} · Review</p>
            <h3 ref={createEventStepHeadingRef} tabIndex={-1}>Review and create</h3>
            <dl>
              <div><dt>Title</dt><dd>{createEvent.title || '—'}</dd></div>
              <div><dt>Start</dt><dd className="is-machine-value">{createEvent.startsAt || '—'}</dd></div>
              <div><dt>End</dt><dd className="is-machine-value">{createEvent.endsAt || '—'}</dd></div>
              <div><dt>Timezone</dt><dd className="is-machine-value">{createEvent.timezone || '—'}</dd></div>
              <div><dt>All day</dt><dd>{createEvent.allDay ? 'Yes' : 'No'}</dd></div>
              <div><dt>Location</dt><dd>{createEvent.location || '—'}</dd></div>
              <div><dt>Description</dt><dd>{createEvent.description || '—'}</dd></div>
            </dl>
            <div className="work-actions"><button type="button" onClick={goCreateEventBack}>Back</button><button type="submit" disabled={pending}>Create event</button></div>
          </div>
        )}
        </form>
      </section>
      <section className="work-panel">
        <h2 id="create-meeting-title">Create meeting</h2>
        <form ref={createMeetingFormRef} noValidate onSubmit={attemptCreateMeeting} aria-labelledby="create-meeting-title">
        <ol className="wizard-stepper" aria-label="Create meeting progress">
          {CREATE_MEETING_STEPS.map((step, i) => (
            <li className="wizard-step-node" key={step} aria-current={i === createMeetingStepIndex ? 'step' : undefined}>
              <span className={i < createMeetingStepIndex ? 'wizard-step-circle done' : i === createMeetingStepIndex ? 'wizard-step-circle active' : 'wizard-step-circle upcoming'}>{i < createMeetingStepIndex ? '✓' : i + 1}</span>
              <span className={i <= createMeetingStepIndex ? 'wizard-step-label on' : 'wizard-step-label'}>{CREATE_MEETING_STEP_LABELS[step]}</span>
              {i < CREATE_MEETING_STEPS.length - 1 ? <span className={i < createMeetingStepIndex ? 'wizard-step-line done' : 'wizard-step-line'} /> : null}
            </li>
          ))}
        </ol>
        {createMeetingStep === 'basics' ? (
          <div className="field-form">
            <p className="eyebrow">Step {createMeetingStepIndex + 1} of {CREATE_MEETING_STEPS.length} · Basics</p>
            <h3 ref={createMeetingStepHeadingRef} tabIndex={-1}>What and when?</h3>
            <label>Linked calendar event<select aria-label="Linked calendar event" value={createMeeting.calendarEventId} onChange={(e) => setCreateMeeting({ ...createMeeting, calendarEventId: e.target.value })}><option value="">Standalone meeting</option>{(events.data?.items ?? []).filter((item) => !item.archived_at).map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}</select></label>
            <label>Meeting title<input aria-label="Meeting title" value={createMeeting.title} onChange={(e) => setCreateMeeting({ ...createMeeting, title: e.target.value })} /></label>
            {createMeeting.calendarEventId ? <p className="inline-status">Timing will be projected from the selected calendar event.</p> : <TimingFields prefix="Meeting" draft={createMeeting} onChange={setCreateMeeting} />}
            <label>Meeting status<select value={createMeeting.status} onChange={(e) => setCreateMeeting({ ...createMeeting, status: e.target.value as MeetingDraft['status'] })}><option value="planned">planned</option><option value="in_progress">in progress</option><option value="completed">completed</option><option value="cancelled">cancelled</option></select></label>
            <div className="work-actions"><button type="button" onClick={goCreateMeetingNext}>Continue</button></div>
          </div>
        ) : createMeetingStep === 'notes' ? (
          <div className="field-form">
            <p className="eyebrow">Step {createMeetingStepIndex + 1} of {CREATE_MEETING_STEPS.length} · Notes</p>
            <h3 ref={createMeetingStepHeadingRef} tabIndex={-1}>Agenda and prep</h3>
            <label>Agenda<textarea aria-label="Meeting agenda" value={createMeeting.agenda} onChange={(e) => setCreateMeeting({ ...createMeeting, agenda: e.target.value })} /></label>
            <label>Preparation<textarea aria-label="Meeting preparation" value={createMeeting.preparation} onChange={(e) => setCreateMeeting({ ...createMeeting, preparation: e.target.value })} /></label>
            <label>Notes summary<textarea aria-label="Meeting notes summary" value={createMeeting.notesSummary} onChange={(e) => setCreateMeeting({ ...createMeeting, notesSummary: e.target.value })} /></label>
            <div className="work-actions"><button type="button" onClick={goCreateMeetingBack}>Back</button><button type="button" onClick={goCreateMeetingNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createMeetingStepIndex + 1} of {CREATE_MEETING_STEPS.length} · Review</p>
            <h3 ref={createMeetingStepHeadingRef} tabIndex={-1}>Review and create</h3>
            <dl>
              <div><dt>Title</dt><dd>{createMeeting.title || '—'}</dd></div>
              <div><dt>Linked event</dt><dd>{createMeeting.calendarEventId ? (events.data?.items ?? []).find((item) => item.id === createMeeting.calendarEventId)?.title ?? createMeeting.calendarEventId : 'Standalone'}</dd></div>
              {!createMeeting.calendarEventId ? <>
                <div><dt>Start</dt><dd className="is-machine-value">{createMeeting.startsAt || '—'}</dd></div>
                <div><dt>End</dt><dd className="is-machine-value">{createMeeting.endsAt || '—'}</dd></div>
                <div><dt>Timezone</dt><dd className="is-machine-value">{createMeeting.timezone || '—'}</dd></div>
              </> : null}
              <div><dt>Status</dt><dd>{createMeeting.status}</dd></div>
              <div><dt>Agenda</dt><dd>{createMeeting.agenda || '—'}</dd></div>
              <div><dt>Preparation</dt><dd>{createMeeting.preparation || '—'}</dd></div>
              <div><dt>Notes summary</dt><dd>{createMeeting.notesSummary || '—'}</dd></div>
            </dl>
            <div className="work-actions"><button type="button" onClick={goCreateMeetingBack}>Back</button><button type="submit" disabled={pending}>{createMeeting.calendarEventId ? 'Create linked meeting' : 'Create standalone meeting'}</button></div>
          </div>
        )}
        </form>
      </section>
    </div>
    <div className="work-grid">
      <section className="work-panel"><h2>Calendar events</h2>{events.isLoading ? <p role="status">Loading calendar events…</p> : null}{events.isError ? <div className="inline-status error-panel" role="alert">{events.error.message}</div> : null}
        {!events.isLoading && !(events.data?.items.length) ? <p className="empty-state">No calendar events.</p> : null}
        <ol className="work-list">{(events.data?.items ?? []).map((record) => <li key={record.id}><div><strong>{record.title}</strong><small>{instantToWallTime(record.starts_at, record.timezone)} · {record.timezone} · {record.status}{record.source_authoritative ? ' · authoritative event' : ''}</small></div><div className="work-actions">
          {!record.archived_at ? <><button type="button" disabled={pending} aria-label={`Edit event ${record.title}`} onClick={() => { setEditMeeting(null); setEditEvent({ record, ...eventDraft(record), latestVersion: record.version, conflict: false, reloadFailed: false }) }}>Edit</button><button type="button" disabled={pending} aria-label={`Archive event ${record.title}`} onClick={() => eventAction.mutate({ record, action: 'archive' })}>Archive</button></> : <button type="button" disabled={pending} aria-label={`Restore event ${record.title}`} onClick={() => eventAction.mutate({ record, action: 'restore' })}>Restore</button>}
        </div></li>)}</ol>
      </section>
      <section className="work-panel"><h2>Meetings</h2>{meetings.isLoading ? <p role="status">Loading meetings…</p> : null}{meetings.isError ? <div className="inline-status error-panel" role="alert">{meetings.error.message}</div> : null}
        {!meetings.isLoading && !(meetings.data?.items.length) ? <p className="empty-state">No meetings.</p> : null}
        <ol className="work-list">{(meetings.data?.items ?? []).map((record) => <li key={record.id}><div><strong>{record.title}</strong><small>{instantToWallTime(record.starts_at, record.timezone)} · {record.timezone} · {record.calendar_event_id ? 'timing from calendar event' : 'standalone timing'} · {record.status}</small></div><div className="work-actions">
          {!record.archived_at ? <><button type="button" disabled={pending} aria-label={`Edit meeting ${record.title}`} onClick={() => { setEditEvent(null); setEditMeeting(meetingContent(record)) }}>Edit</button><button type="button" disabled={pending} aria-label={`Archive meeting ${record.title}`} onClick={() => meetingAction.mutate({ record, action: 'archive' })}>Archive</button></> : <button type="button" disabled={pending} aria-label={`Restore meeting ${record.title}`} onClick={() => meetingAction.mutate({ record, action: 'restore' })}>Restore</button>}
        </div></li>)}</ol>
      </section>
    </div>
    {editEvent ? <section className="work-panel"><form className="field-form" onSubmit={submitEventEdit}><h2>Edit calendar event</h2><p>This calendar event is the authoritative timing record.</p>
      <label>Edit event title<input aria-label="Edit event title" value={editEvent.title} onChange={(e) => setEditEvent({ ...editEvent, title: e.target.value })} /></label><TimingFields prefix="Edit event" draft={editEvent} onChange={(value) => setEditEvent({ ...editEvent, ...value })} />
      <label className="field-checkbox"><input aria-label="Edit event all day" type="checkbox" checked={editEvent.allDay} onChange={(e) => setEditEvent({ ...editEvent, allDay: e.target.checked })} /> All day</label>
      <label>Edit event location<input aria-label="Edit event location" value={editEvent.location} onChange={(e) => setEditEvent({ ...editEvent, location: e.target.value })} /></label>
      <label>Edit event description<textarea aria-label="Edit event description" value={editEvent.description} onChange={(e) => setEditEvent({ ...editEvent, description: e.target.value })} /></label>
      <label>Edit event status<select aria-label="Edit event status" value={editEvent.status} onChange={(e) => setEditEvent({ ...editEvent, status: e.target.value as EventDraft['status'] })}><option value="confirmed">confirmed</option><option value="tentative">tentative</option><option value="cancelled">cancelled</option></select></label>
      {editEvent.reloadFailed ? <><p role="alert">Could not reload the latest event. Your edits are preserved.</p><button type="button" disabled={pending} onClick={() => void reloadEvent(editEvent.record.id)}>Reload latest event</button></> : editEvent.conflict ? <button type="button" disabled={pending} onClick={() => submitEventEdit()}>Retry event with latest version</button> : <button type="submit" disabled={pending}>Save event</button>}
      <button type="button" disabled={pending} onClick={() => setEditEvent(null)}>Discard event edit</button>
    </form></section> : null}
    {editMeeting ? <section className="work-panel"><form className="field-form" onSubmit={submitMeetingEdit}><h2>Edit meeting</h2>
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
