import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'

export type WaitingDirection = 'waiting_on_me' | 'waiting_on_them' | 'blocked_by' | 'delegated'
export type WaitingSubjectType = 'task' | 'commitment' | 'knowledge_entity'

export type WaitingLink = {
  id: string
  subject_type: WaitingSubjectType
  subject_id: string
  counterparty_entity_id: string
  direction: WaitingDirection
  status: 'open' | 'fulfilled' | 'cancelled' | 'superseded'
  note: string | null
  since_at: string
  expected_at: string | null
  superseded_by: string | null
  created_at: string
  updated_at: string
  version: number
}

type WaitingLinkList = { items: WaitingLink[]; next_cursor?: string | null }

export type Draft = {
  subjectType: WaitingSubjectType
  subjectId: string
  counterpartyEntityId: string
  direction: WaitingDirection
  note: string
}

const emptyDraft: Draft = {
  subjectType: 'task',
  subjectId: '',
  counterpartyEntityId: '',
  direction: 'waiting_on_them',
  note: '',
}
const DIRECTIONS: WaitingDirection[] = ['waiting_on_me', 'waiting_on_them', 'blocked_by', 'delegated']
const SUBJECT_TYPES: WaitingSubjectType[] = ['task', 'commitment', 'knowledge_entity']

export function validateDraft(draft: Draft): string | null {
  if (!draft.subjectId.trim()) return 'Subject ID is required.'
  if (!draft.counterpartyEntityId.trim()) return 'Counterparty entity ID is required.'
  return null
}

function errorMessage(error: Error): string {
  if (error instanceof ApiError && error.code === 'INVALID_WAITING_DIRECTION') {
    return 'That direction would create a dependency cycle and was rejected.'
  }
  if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') {
    return 'This waiting item changed since it was loaded. Refresh and try again.'
  }
  return error.message
}

export default function WaitingView() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['waiting'],
    queryFn: () => apiRequest<WaitingLinkList>('/api/v1/waiting?limit=100'),
    retry: 1,
  })
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [formError, setFormError] = useState<string | null>(null)

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['waiting'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard', 'today'] })
    void queryClient.invalidateQueries({ queryKey: ['brief', 'morning'] })
  }

  const createMutation = useMutation({
    mutationFn: (value: Draft) => apiRequest<WaitingLink>('/api/v1/waiting', {
      method: 'POST',
      body: {
        subject_type: value.subjectType,
        subject_id: value.subjectId.trim(),
        counterparty_entity_id: value.counterpartyEntityId.trim(),
        direction: value.direction,
        note: value.note.trim() || null,
      },
    }),
    onSuccess: () => { setDraft(emptyDraft); refresh() },
  })
  const terminalMutation = useMutation({
    mutationFn: ({ link, action }: { link: WaitingLink; action: 'fulfil' | 'cancel' }) =>
      apiRequest<WaitingLink>(`/api/v1/waiting/${link.id}/${action}`, {
        method: 'POST',
        body: { expected_version: link.version },
      }),
    onSuccess: () => refresh(),
    onError: (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') refresh() },
  })
  const pending = createMutation.isPending || terminalMutation.isPending
  const mutationError = createMutation.error ?? terminalMutation.error

  function submit(event: FormEvent) {
    event.preventDefault()
    const problem = validateDraft(draft)
    if (problem) { setFormError(problem); return }
    setFormError(null)
    createMutation.mutate(draft)
  }

  const open = (query.data?.items ?? []).filter((link) => link.status === 'open')

  return (
    <section className="work-panel" aria-labelledby="waiting-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">DEPENDENCIES</p>
          <h1 id="waiting-title">Waiting</h1>
          <p>Obligations and dependencies, in either direction, with their history preserved.</p>
        </div>
      </div>

      {formError ? <div role="alert" className="inline-status error-panel">{formError}</div> : null}
      {mutationError ? <div role="alert" className="inline-status error-panel">{errorMessage(mutationError)}</div> : null}

      <form onSubmit={submit}>
        <h2>Record a waiting item</h2>
        <label>Subject type
          <select aria-label="Subject type" value={draft.subjectType} onChange={(e) => setDraft({ ...draft, subjectType: e.target.value as WaitingSubjectType })}>
            {SUBJECT_TYPES.map((type) => <option key={type} value={type}>{type.replaceAll('_', ' ')}</option>)}
          </select>
        </label>
        <label>Subject ID<input aria-label="Subject ID" value={draft.subjectId} onChange={(e) => setDraft({ ...draft, subjectId: e.target.value })} /></label>
        <label>Counterparty entity ID<input aria-label="Counterparty entity ID" value={draft.counterpartyEntityId} onChange={(e) => setDraft({ ...draft, counterpartyEntityId: e.target.value })} /></label>
        <label>Direction
          <select aria-label="Direction" value={draft.direction} onChange={(e) => setDraft({ ...draft, direction: e.target.value as WaitingDirection })}>
            {DIRECTIONS.map((direction) => <option key={direction} value={direction}>{direction.replaceAll('_', ' ')}</option>)}
          </select>
        </label>
        <label>Note<input aria-label="Waiting note" value={draft.note} onChange={(e) => setDraft({ ...draft, note: e.target.value })} /></label>
        <button type="submit" disabled={pending}>Record waiting item</button>
      </form>

      {query.isLoading ? <p role="status">Loading waiting items…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && open.length === 0 ? <p className="empty-state">Nothing is currently waiting.</p> : null}
      <ol className="work-list">
        {open.map((link) => (
          <li key={link.id}>
            <div>
              <strong>{link.direction.replaceAll('_', ' ')}</strong>
              <small>{link.subject_type.replaceAll('_', ' ')} · since {new Date(link.since_at).toLocaleDateString()}{link.expected_at ? ` · expected ${new Date(link.expected_at).toLocaleDateString()}` : ''}</small>
              {link.note ? <p>{link.note}</p> : null}
            </div>
            <div className="work-actions" role="group" aria-label={`Actions for waiting item ${link.id}`}>
              <button type="button" disabled={pending} aria-label={`Fulfil waiting item ${link.id}`} onClick={() => terminalMutation.mutate({ link, action: 'fulfil' })}>Fulfil</button>
              <button type="button" disabled={pending} aria-label={`Cancel waiting item ${link.id}`} onClick={() => terminalMutation.mutate({ link, action: 'cancel' })}>Cancel</button>
            </div>
          </li>
        ))}
      </ol>
    </section>
  )
}
