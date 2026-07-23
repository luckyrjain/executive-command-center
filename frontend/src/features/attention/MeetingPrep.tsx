import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'

export type MeetingPackParticipant = { id: string; entity_id: string; entity_name: string; role: string }
export type MeetingPackTimelineEntry = { id: string; entity_id: string; effective_at: string; event_type: string; summary: string }
export type MeetingPackCommitment = { id: string; direction: 'made_by_me' | 'made_to_me'; summary: string; status: string; due_at: string | null; counterparty_name: string | null }
export type MeetingPackNote = { id: string; title: string | null; body: string; note_type: string; created_at: string }
export type MeetingPackRisk = { id: string; description: string; status: string; probability: number; impact: number; review_at: string | null }
export type MeetingPackDependency = { id: string; direction: string; note: string | null; expected_at: string | null }
export type MeetingPackEvidenceGap = { id: string; source_type: string; evidence_state: 'available' | 'missing' | 'permission_denied' | 'deleted' }
export type MeetingPackEnrichment = { available: boolean; summary: string | null; error_code: string | null }

export type MeetingPack = {
  id: string
  meeting_id: string
  status: 'fresh' | 'stale' | 'refreshed' | 'archived'
  generated_at: string
  stale_at: string
  source_versions: Record<string, string>
  objective: string
  starts_at: string
  ends_at: string
  timezone: string
  participants: MeetingPackParticipant[]
  timeline: MeetingPackTimelineEntry[]
  commitments: MeetingPackCommitment[]
  decisions: MeetingPackNote[]
  open_questions: string[]
  notes: MeetingPackNote[]
  risks: MeetingPackRisk[]
  dependencies: MeetingPackDependency[]
  evidence_gaps: MeetingPackEvidenceGap[]
  enrichment: MeetingPackEnrichment
}

/** UX-STATES.md: missing-evidence copy must stay neutral, never alarming.
 * "gap" names the fact; it does not blame anyone for it. */
const EVIDENCE_GAP_LABEL: Record<Exclude<MeetingPackEvidenceGap['evidence_state'], 'available'>, string> = {
  missing: 'Evidence not yet captured',
  permission_denied: 'Evidence not accessible from this view',
  deleted: 'Evidence source was removed',
}

/** Formats an ISO instant in the given IANA timezone so the displayed time
 * actually matches the `(timezone)` label rendered next to it -- rather
 * than `toLocaleString`/`toLocaleTimeString`'s default of the browser's
 * local zone, which is only truthful when the viewer happens to be in the
 * meeting's timezone. Falls back to local formatting (omitting the
 * `timeZone` option) for an empty or invalid IANA name, instead of
 * throwing a RangeError and crashing the pack. */
function formatInTimeZone(iso: string, timeZone: string, options: Intl.DateTimeFormatOptions): string {
  const instant = new Date(iso)
  if (Number.isNaN(instant.getTime())) return ''
  try {
    return new Intl.DateTimeFormat(undefined, { ...options, timeZone: timeZone || undefined }).format(instant)
  } catch {
    return new Intl.DateTimeFormat(undefined, options).format(instant)
  }
}

function errorMessage(error: Error): string {
  if (error instanceof ApiError && error.code === 'MEETING_NOT_FOUND') return 'No meeting was found with that ID.'
  if (error instanceof ApiError && error.code === 'MEETING_PACK_NOT_FOUND') return 'No preparation pack exists yet for this meeting. Generate one below.'
  if (error instanceof ApiError && error.code === 'MEETING_PACK_EXISTS') return 'A preparation pack already exists. Use Refresh to update it.'
  if (error instanceof ApiError && error.code === 'STALE_MEETING_PACK') return 'The existing pack is stale. Use Refresh to regenerate it.'
  return error.message
}

export default function MeetingPrep() {
  const queryClient = useQueryClient()
  const [meetingId, setMeetingId] = useState('')
  const [activeMeetingId, setActiveMeetingId] = useState<string | null>(null)

  const query = useQuery({
    queryKey: ['meeting-prep', activeMeetingId],
    queryFn: () => apiRequest<MeetingPack>(`/api/v1/meetings/${activeMeetingId}/prep`),
    enabled: Boolean(activeMeetingId),
    retry: 1,
  })

  const refresh = () => { if (activeMeetingId) void queryClient.invalidateQueries({ queryKey: ['meeting-prep', activeMeetingId] }) }

  const createMutation = useMutation({
    mutationFn: (id: string) => apiRequest<MeetingPack>(`/api/v1/meetings/${id}/prep`, { method: 'POST' }),
    onSuccess: () => refresh(),
  })
  const refreshMutation = useMutation({
    mutationFn: (id: string) => apiRequest<MeetingPack>(`/api/v1/meetings/${id}/prep/refresh`, { method: 'POST' }),
    onSuccess: () => refresh(),
  })
  const pending = createMutation.isPending || refreshMutation.isPending

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!meetingId.trim()) return
    setActiveMeetingId(meetingId.trim())
  }

  const pack = query.data
  const mutationError = createMutation.error ?? refreshMutation.error
  const packNotFound = query.isError && query.error instanceof ApiError && query.error.code === 'MEETING_PACK_NOT_FOUND'

  return (
    <section className="work-panel" aria-labelledby="meeting-prep-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">MEETING PREPARATION</p>
          <h1 id="meeting-prep-title">Meeting prep</h1>
          <p>Evidence-backed facts, open questions and suggestions, kept separate.</p>
        </div>
      </div>

      <form onSubmit={submit}>
        <label>Meeting ID<input aria-label="Meeting ID" value={meetingId} onChange={(e) => setMeetingId(e.target.value)} /></label>
        <button type="submit" disabled={pending}>Load meeting prep</button>
      </form>

      {activeMeetingId ? (
        <div className="work-actions" role="group" aria-label="Preparation pack actions">
          <button type="button" disabled={pending} onClick={() => createMutation.mutate(activeMeetingId)}>Generate pack</button>
          <button type="button" disabled={pending} onClick={() => refreshMutation.mutate(activeMeetingId)}>Refresh pack</button>
        </div>
      ) : null}

      {mutationError ? <div role="alert" className="inline-status error-panel">{errorMessage(mutationError)}</div> : null}
      {query.isLoading ? <p role="status">Loading meeting prep…</p> : null}
      {query.isError && !packNotFound ? <div role="alert" className="inline-status error-panel">{errorMessage(query.error)}</div> : null}
      {packNotFound ? <p className="empty-state">No preparation pack exists yet for this meeting. Generate one above.</p> : null}

      {pack ? (
        <>
          {pack.status === 'stale' ? (
            <div className="inline-status degraded-panel" role="status">
              This pack may be out of date. <button type="button" disabled={pending} onClick={() => refreshMutation.mutate(pack.meeting_id)}>Refresh now</button>
            </div>
          ) : null}
          {!pack.enrichment.available ? (
            <div className="inline-status degraded-panel" role="status">
              AI-assisted suggestions are disabled; showing deterministic results only.
            </div>
          ) : null}

          <section className="dashboard-card" aria-labelledby="prep-objective">
            <h2 id="prep-objective">Objective and timing</h2>
            <p>{pack.objective}</p>
            <p>
              {formatInTimeZone(pack.starts_at, pack.timezone, { dateStyle: 'medium', timeStyle: 'short' })}
              {' – '}
              {formatInTimeZone(pack.ends_at, pack.timezone, { timeStyle: 'short' })}
              {' ('}{pack.timezone}{')'}
            </p>
          </section>

          <section className="dashboard-card" aria-labelledby="prep-participants">
            <h2 id="prep-participants">Participants and known roles</h2>
            {pack.participants.length ? (
              <ul>{pack.participants.map((p) => <li key={p.id}>{p.entity_name} · {p.role}</li>)}</ul>
            ) : <p className="empty-state">No participants linked yet.</p>}
          </section>

          <section className="dashboard-card" aria-labelledby="prep-facts">
            <h2 id="prep-facts">Facts</h2>
            <h3>Relevant recent timeline</h3>
            {pack.timeline.length ? (
              <ul>{pack.timeline.map((entry) => <li key={entry.id}>{entry.summary} <cite>({entry.event_type})</cite></li>)}</ul>
            ) : <p className="empty-state">No recent timeline entries.</p>}
            <h3>Open commitments</h3>
            {pack.commitments.length ? (
              <ul>{pack.commitments.map((c) => <li key={c.id}>{c.summary} · {c.direction.replaceAll('_', ' ')}{c.counterparty_name ? ` · ${c.counterparty_name}` : ''}</li>)}</ul>
            ) : <p className="empty-state">No open commitments.</p>}
            <h3>Prior decisions</h3>
            {pack.decisions.length ? (
              <ul>{pack.decisions.map((d) => <li key={d.id}>{d.title ?? d.body}<cite> [source: note {d.id}]</cite></li>)}</ul>
            ) : <p className="empty-state">No recorded decisions.</p>}
            <h3>Active risks and dependencies</h3>
            {pack.risks.length || pack.dependencies.length ? (
              <ul>
                {pack.risks.map((r) => <li key={r.id}>{r.description} · {r.status}</li>)}
                {pack.dependencies.map((d) => <li key={d.id}>{d.direction.replaceAll('_', ' ')}{d.note ? ` · ${d.note}` : ''}</li>)}
              </ul>
            ) : <p className="empty-state">No active risks or dependencies.</p>}
            <h3>Documents and notes worth reviewing</h3>
            {pack.notes.length ? (
              <ul>{pack.notes.map((n) => <li key={n.id}>{n.title ?? n.body}</li>)}</ul>
            ) : <p className="empty-state">No notes attached.</p>}
          </section>

          <section className="dashboard-card" aria-labelledby="prep-questions">
            <h2 id="prep-questions">Open questions</h2>
            {pack.open_questions.length ? (
              <ul>{pack.open_questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
            ) : <p className="empty-state">No open questions recorded.</p>}
          </section>

          <section className="dashboard-card" aria-labelledby="prep-suggestions">
            <h2 id="prep-suggestions">Suggested agenda (AI-assisted, kept separate from the sections above)</h2>
            {pack.enrichment.available && pack.enrichment.summary ? <p>{pack.enrichment.summary}</p> : <p className="empty-state">No suggestions available.</p>}
          </section>

          <section className="dashboard-card" aria-labelledby="prep-evidence">
            <h2 id="prep-evidence">Evidence gaps and source freshness</h2>
            <p>Generated {new Date(pack.generated_at).toLocaleString()}</p>
            {pack.evidence_gaps.length ? (
              <ul>{pack.evidence_gaps.map((gap) => <li key={gap.id}>{EVIDENCE_GAP_LABEL[gap.evidence_state as Exclude<typeof gap.evidence_state, 'available'>]}</li>)}</ul>
            ) : <p className="empty-state">No evidence gaps.</p>}
          </section>
        </>
      ) : null}
    </section>
  )
}
