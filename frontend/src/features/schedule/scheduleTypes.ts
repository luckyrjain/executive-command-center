export type EventStatus = 'confirmed' | 'tentative' | 'cancelled'
export type MeetingStatus = 'planned' | 'in_progress' | 'completed' | 'cancelled'

export type CalendarEvent = {
  id: string
  title: string
  starts_at: string
  ends_at: string
  all_day: boolean
  timezone: string
  location: string | null
  description: string | null
  status: EventStatus
  external_source: string
  external_id: string | null
  source_authoritative: boolean
  created_at: string
  updated_at: string
  version: number
  archived_at: string | null
  pre_archive_status: string | null
}

export type Meeting = {
  id: string
  calendar_event_id: string | null
  title: string
  starts_at: string
  ends_at: string
  timezone: string
  status: MeetingStatus
  agenda: string | null
  preparation: string | null
  notes_summary: string | null
  created_at: string
  updated_at: string
  version: number
  archived_at: string | null
  pre_archive_status: string | null
}

export type EntityList<T> = { items: T[]; next_cursor: string | null }

export type TimingDraft = { startsAt: string; endsAt: string; timezone: string }
export type EventDraft = TimingDraft & {
  title: string; allDay: boolean; location: string; description: string; status: EventStatus
}
export type MeetingDraft = TimingDraft & {
  calendarEventId: string; title: string; status: MeetingStatus; agenda: string; preparation: string; notesSummary: string
}
