import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'

type Commitment = {
  id: string; summary: string; description?: string | null; direction: 'made_by_me' | 'made_to_me'
  counterparty_name?: string | null; status: string; importance: 'low' | 'medium' | 'high' | 'critical'
  due_date?: string | null; due_at?: string | null; version: number; archived_at?: string | null
}
type CommitmentList = { items: Commitment[]; next_cursor?: string | null }
type Draft = { summary: string; description: string; direction: Commitment['direction']; counterpartyName: string; importance: Commitment['importance']; dueDate: string; dueAt: string; originalDueAt?: string }
type EditState = Draft & { commitment: Commitment; latestVersion: number; conflict: boolean }
type Action = 'confirm' | 'fulfil' | 'cancel' | 'archive' | 'restore'

const emptyDraft: Draft = { summary: '', description: '', direction: 'made_by_me', counterpartyName: '', importance: 'medium', dueDate: '', dueAt: '' }
const filters = { include_archived: true }

function duePayload(draft: Draft): Record<string, string | null> {
  if (draft.dueDate) return { due_date: draft.dueDate, due_at: null }
  if (draft.dueAt) {
    const dueAt = draft.originalDueAt && serverInstantToLocalInput(draft.originalDueAt) === draft.dueAt ? draft.originalDueAt : new Date(draft.dueAt).toISOString()
    return { due_date: null, due_at: dueAt }
  }
  return { due_date: null, due_at: null }
}

function pad(value: number): string { return String(value).padStart(2, '0') }
function serverInstantToLocalInput(value: string): string {
  const instant = new Date(value)
  if (Number.isNaN(instant.getTime())) return ''
  return `${instant.getFullYear()}-${pad(instant.getMonth() + 1)}-${pad(instant.getDate())}T${pad(instant.getHours())}:${pad(instant.getMinutes())}`
}

function fromCommitment(value: Commitment): Draft {
  return { summary: value.summary, description: value.description ?? '', direction: value.direction, counterpartyName: value.counterparty_name ?? '', importance: value.importance, dueDate: value.due_date ?? '', dueAt: value.due_at ? serverInstantToLocalInput(value.due_at) : '', originalDueAt: value.due_at ?? undefined }
}

export default function CommitmentWorkspace() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['commitments', filters], queryFn: () => apiRequest<CommitmentList>('/api/v1/commitments?include_archived=true&limit=100'), retry: 1 })
  const [create, setCreate] = useState(emptyDraft)
  const [edit, setEdit] = useState<EditState | null>(null)
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['commitments'] })

  const createMutation = useMutation({
    mutationFn: (draft: Draft) => apiRequest<Commitment>('/api/v1/commitments', { method: 'POST', body: { summary: draft.summary.trim(), description: draft.description.trim() || null, direction: draft.direction, counterparty_name: draft.counterpartyName.trim() || null, importance: draft.importance, ...duePayload(draft), status: 'confirmed' } }),
    onSuccess: () => { setCreate(emptyDraft); void refresh() },
  })
  const editMutation = useMutation({
    mutationFn: ({ draft, version }: { draft: Draft; version: number }) => apiRequest<Commitment>(`/api/v1/commitments/${edit?.commitment.id}`, { method: 'PATCH', body: { expected_version: version, summary: draft.summary.trim(), description: draft.description.trim() || null, counterparty_name: draft.counterpartyName.trim() || null, importance: draft.importance, ...duePayload(draft) } }),
    onSuccess: () => { setEdit(null); void refresh() },
    onError: async (error) => {
      if (!(error instanceof ApiError) || error.code !== 'VERSION_CONFLICT') return
      if (!edit) return
      try {
        const current = await apiRequest<Commitment>(`/api/v1/commitments/${edit.commitment.id}`)
        setEdit((value) => value ? { ...value, latestVersion: current.version, conflict: true } : value)
      } catch {
        setEdit((value) => value ? { ...value, conflict: true } : value)
      }
    },
  })
  const actionMutation = useMutation({
    mutationFn: ({ commitment, action }: { commitment: Commitment; action: Action }) => apiRequest<Commitment>(`/api/v1/commitments/${commitment.id}/${action}`, { method: 'POST', body: { expected_version: commitment.version } }),
    onSuccess: () => { void refresh() }, onError: (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') void refresh() },
  })

  function submitCreate(event: FormEvent) { event.preventDefault(); if (create.summary.trim()) createMutation.mutate(create) }
  function submitEdit(event?: FormEvent) { event?.preventDefault(); if (edit?.summary.trim()) editMutation.mutate({ draft: edit, version: edit.latestVersion }) }
  const mutationError = createMutation.error ?? editMutation.error ?? actionMutation.error

  return <section className="work-panel" aria-labelledby="commitments-title">
    <div className="work-heading"><div><p className="eyebrow">WORK</p><h1 id="commitments-title">Commitments</h1><p>Track promises made by you and to you.</p></div></div>
    {mutationError ? <div role="alert" className="inline-status error-panel">{mutationError instanceof ApiError && mutationError.code === 'VERSION_CONFLICT' ? 'This commitment changed while you were editing it. Review your input and retry with the latest version.' : mutationError.message}</div> : null}
    <form onSubmit={submitCreate}>
      <h2>Create commitment</h2>
      <label>Commitment summary<input aria-label="Commitment summary" value={create.summary} onChange={(e) => setCreate({ ...create, summary: e.target.value })} /></label>
      <label>Description<textarea value={create.description} onChange={(e) => setCreate({ ...create, description: e.target.value })} /></label>
      <label>Direction<select aria-label="Direction" value={create.direction} onChange={(e) => setCreate({ ...create, direction: e.target.value as Draft['direction'] })}><option value="made_by_me">Made by me</option><option value="made_to_me">Made to me</option></select></label>
      <label>Counterparty name<input aria-label="Counterparty name" value={create.counterpartyName} onChange={(e) => setCreate({ ...create, counterpartyName: e.target.value })} /></label>
      <label>Importance<select value={create.importance} onChange={(e) => setCreate({ ...create, importance: e.target.value as Draft['importance'] })}><option>low</option><option>medium</option><option>high</option><option>critical</option></select></label>
      <label>Due date<input type="date" value={create.dueDate} disabled={Boolean(create.dueAt)} onChange={(e) => setCreate({ ...create, dueDate: e.target.value })} /></label>
      <label>Due time<input type="datetime-local" value={create.dueAt} disabled={Boolean(create.dueDate)} onChange={(e) => setCreate({ ...create, dueAt: e.target.value })} /></label>
      <button type="submit" disabled={createMutation.isPending}>Create commitment</button>
    </form>
    {query.isLoading ? <p role="status">Loading commitments…</p> : null}
    {query.isError ? <div role="alert">{query.error.message}</div> : null}
    <ol className="work-list">{(query.data?.items ?? []).map((value) => {
      const archived = Boolean(value.archived_at); const terminal = ['fulfilled', 'broken', 'cancelled'].includes(value.status)
      return <li key={value.id}><div><strong>{value.summary}</strong><small>{value.direction.replaceAll('_', ' ')} · {value.counterparty_name ?? 'No counterparty'} · {value.status}</small></div><div className="work-actions" aria-label={`Actions for ${value.summary}`}>
        {!archived && !terminal ? <button type="button" disabled={actionMutation.isPending} aria-label={`Edit ${value.summary}`} onClick={() => setEdit({ commitment: value, ...fromCommitment(value), latestVersion: value.version, conflict: false })}>Edit</button> : null}
        {!archived && ['detected', 'confirmed'].includes(value.status) ? <button type="button" disabled={actionMutation.isPending} aria-label={`Confirm ${value.summary}`} onClick={() => actionMutation.mutate({ commitment: value, action: 'confirm' })}>Confirm</button> : null}
        {!archived && ['confirmed', 'active'].includes(value.status) ? <button type="button" disabled={actionMutation.isPending} aria-label={`Fulfil ${value.summary}`} onClick={() => actionMutation.mutate({ commitment: value, action: 'fulfil' })}>Fulfil</button> : null}
        {!archived && !terminal ? <button type="button" disabled={actionMutation.isPending} aria-label={`Cancel ${value.summary}`} onClick={() => actionMutation.mutate({ commitment: value, action: 'cancel' })}>Cancel</button> : null}
        {!archived ? <button type="button" disabled={actionMutation.isPending} aria-label={`Archive ${value.summary}`} onClick={() => actionMutation.mutate({ commitment: value, action: 'archive' })}>Archive</button> : <button type="button" disabled={actionMutation.isPending} aria-label={`Restore ${value.summary}`} onClick={() => actionMutation.mutate({ commitment: value, action: 'restore' })}>Restore</button>}
      </div></li>
    })}</ol>
    {edit ? <form onSubmit={submitEdit}><h2>Edit commitment</h2>
      <label>Edit commitment summary<input aria-label="Edit commitment summary" value={edit.summary} onChange={(e) => setEdit({ ...edit, summary: e.target.value })} /></label>
      <label>Edit description<textarea value={edit.description} onChange={(e) => setEdit({ ...edit, description: e.target.value })} /></label>
      <label>Edit counterparty<input value={edit.counterpartyName} onChange={(e) => setEdit({ ...edit, counterpartyName: e.target.value })} /></label>
      <label>Edit importance<select value={edit.importance} onChange={(e) => setEdit({ ...edit, importance: e.target.value as Draft['importance'] })}><option>low</option><option>medium</option><option>high</option><option>critical</option></select></label>
      <label>Edit commitment due date<input aria-label="Edit commitment due date" type="date" value={edit.dueDate} disabled={Boolean(edit.dueAt)} onChange={(e) => setEdit({ ...edit, dueDate: e.target.value })} /></label>
      <label>Edit commitment due time<input aria-label="Edit commitment due time" type="datetime-local" value={edit.dueAt} disabled={Boolean(edit.dueDate)} onChange={(e) => setEdit({ ...edit, dueAt: e.target.value })} /></label>
      {edit.conflict ? <button type="button" disabled={editMutation.isPending} onClick={() => submitEdit()}>Retry with latest version</button> : <button type="submit" disabled={editMutation.isPending}>Save commitment</button>}
      <button type="button" disabled={editMutation.isPending} onClick={() => setEdit(null)}>Discard edit</button>
    </form> : null}
  </section>
}
