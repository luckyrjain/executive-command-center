import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'

export type RiskStatus = 'identified' | 'assessed' | 'monitoring' | 'mitigating' | 'materialized' | 'closed'

export type RiskFactor = { code: string; label: string; points: number; source_field?: string }

export type Risk = {
  id: string
  description: string
  probability: number
  impact: number
  status: RiskStatus
  owner_id: string
  mitigation: string | null
  trigger: string | null
  review_at: string | null
  project_id: string | null
  pinned: boolean
  priority_impact: number
  score: number
  factors: RiskFactor[]
  explanation: string
  created_at: string
  updated_at: string
  version: number
  archived_at: string | null
  pre_archive_status: string | null
}

type RiskList = { items: Risk[]; next_cursor?: string | null }

export type Draft = {
  description: string
  probability: number
  impact: number
  status: RiskStatus
  mitigation: string
  trigger: string
  reviewAt: string
  projectId: string
  pinned: boolean
}

type EditState = Draft & { risk: Risk; latestVersion: number; conflict: boolean; reloadFailed: boolean }

const emptyDraft: Draft = {
  description: '',
  probability: 3,
  impact: 3,
  status: 'identified',
  mitigation: '',
  trigger: '',
  reviewAt: '',
  projectId: '',
  pinned: false,
}
const filters = { include_archived: true }
const STATUSES: RiskStatus[] = ['identified', 'assessed', 'monitoring', 'mitigating', 'materialized', 'closed']

function pad(value: number): string { return String(value).padStart(2, '0') }
function serverInstantToLocalInput(value: string): string {
  const instant = new Date(value)
  if (Number.isNaN(instant.getTime())) return ''
  return `${instant.getFullYear()}-${pad(instant.getMonth() + 1)}-${pad(instant.getDate())}T${pad(instant.getHours())}:${pad(instant.getMinutes())}`
}

/** Frontend-only requirement: mitigation, trigger and review_at must be present before submit,
 * even though the backend RiskCreate/RiskPatch contract leaves them nullable. */
export function validateDraft(draft: Draft): string | null {
  if (!draft.description.trim()) return 'Description is required.'
  if (!Number.isInteger(draft.probability) || draft.probability < 1 || draft.probability > 5) return 'Probability must be a whole number between 1 and 5.'
  if (!Number.isInteger(draft.impact) || draft.impact < 1 || draft.impact > 5) return 'Impact must be a whole number between 1 and 5.'
  if (!draft.mitigation.trim()) return 'Mitigation is required.'
  if (!draft.trigger.trim()) return 'Trigger is required.'
  if (!draft.reviewAt.trim()) return 'Review at is required.'
  return null
}

function reviewAtPayload(value: string): string | null {
  return value ? new Date(value).toISOString() : null
}

function fromRisk(risk: Risk): Draft {
  return {
    description: risk.description,
    probability: risk.probability,
    impact: risk.impact,
    status: risk.status,
    mitigation: risk.mitigation ?? '',
    trigger: risk.trigger ?? '',
    reviewAt: risk.review_at ? serverInstantToLocalInput(risk.review_at) : '',
    projectId: risk.project_id ?? '',
    pinned: risk.pinned,
  }
}

function createBody(draft: Draft) {
  return {
    description: draft.description.trim(),
    probability: draft.probability,
    impact: draft.impact,
    status: draft.status,
    mitigation: draft.mitigation.trim(),
    trigger: draft.trigger.trim(),
    review_at: reviewAtPayload(draft.reviewAt),
    project_id: draft.projectId.trim() || null,
    pinned: draft.pinned,
  }
}

function patchBody(edit: EditState): Record<string, unknown> {
  const original = fromRisk(edit.risk)
  const result: Record<string, unknown> = {}
  if (edit.description.trim() !== original.description.trim()) result.description = edit.description.trim()
  if (edit.probability !== original.probability) result.probability = edit.probability
  if (edit.impact !== original.impact) result.impact = edit.impact
  if (edit.status !== original.status) result.status = edit.status
  if (edit.mitigation.trim() !== original.mitigation.trim()) result.mitigation = edit.mitigation.trim()
  if (edit.trigger.trim() !== original.trigger.trim()) result.trigger = edit.trigger.trim()
  if (edit.reviewAt !== original.reviewAt) result.review_at = reviewAtPayload(edit.reviewAt)
  if (edit.projectId.trim() !== original.projectId.trim()) result.project_id = edit.projectId.trim() || null
  if (edit.pinned !== original.pinned) result.pinned = edit.pinned
  return result
}

function errorMessage(error: Error): string {
  if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') return 'This risk changed while you were editing it. Review your input and retry with the latest version.'
  if (error instanceof ApiError && error.code === 'RISK_TERMINAL') return 'This risk is closed and cannot change status again. Other fields can still be edited.'
  return error.message
}

export default function RiskWorkspace() {
  const queryClient = useQueryClient()
  const query = useQuery({ queryKey: ['risks', filters], queryFn: () => apiRequest<RiskList>('/api/v1/risks?include_archived=true&limit=100'), retry: 1 })
  const [create, setCreate] = useState<Draft>(emptyDraft)
  const [edit, setEdit] = useState<EditState | null>(null)
  const [formError, setFormError] = useState<string | null>(null)

  async function reloadLatestRisk(id: string) {
    try {
      const current = await apiRequest<Risk>(`/api/v1/risks/${id}`)
      setEdit((value) => value?.risk.id === id ? { ...value, latestVersion: current.version, conflict: true, reloadFailed: false } : value)
    } catch {
      setEdit((value) => value?.risk.id === id ? { ...value, latestVersion: 0, conflict: false, reloadFailed: true } : value)
    }
  }
  const refresh = () => queryClient.invalidateQueries({ queryKey: ['risks'] })

  const createMutation = useMutation({
    mutationFn: (draft: Draft) => apiRequest<Risk>('/api/v1/risks', { method: 'POST', body: createBody(draft) }),
    onSuccess: () => { setCreate(emptyDraft); void refresh() },
  })
  const editMutation = useMutation({
    mutationFn: ({ draft, version }: { draft: EditState; version: number }) => apiRequest<Risk>(`/api/v1/risks/${draft.risk.id}`, { method: 'PATCH', body: { expected_version: version, ...patchBody(draft) } }),
    onSuccess: (_data, variables) => { setEdit((value) => value?.risk.id === variables.draft.risk.id ? null : value); void refresh() },
    onError: async (error) => {
      if (!(error instanceof ApiError) || error.code !== 'VERSION_CONFLICT') return
      if (edit) await reloadLatestRisk(edit.risk.id)
    },
  })
  const actionMutation = useMutation({
    mutationFn: ({ risk, action }: { risk: Risk; action: 'archive' | 'restore' }) => apiRequest<Risk>(`/api/v1/risks/${risk.id}/${action}`, { method: 'POST', body: { expected_version: risk.version } }),
    onSuccess: () => { void refresh() },
    onError: (error) => { if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') void refresh() },
  })
  const pending = createMutation.isPending || editMutation.isPending || actionMutation.isPending

  function submitCreate(event: FormEvent) {
    event.preventDefault()
    const problem = validateDraft(create)
    if (problem) { setFormError(problem); return }
    setFormError(null)
    createMutation.mutate(create)
  }
  function submitEdit(event?: FormEvent) {
    event?.preventDefault()
    if (!edit) return
    const problem = validateDraft(edit)
    if (problem) { setFormError(problem); return }
    if (edit.latestVersion <= 0) return
    setFormError(null)
    editMutation.mutate({ draft: edit, version: edit.latestVersion })
  }
  const mutationError = createMutation.error ?? editMutation.error ?? actionMutation.error

  return <section className="work-panel" aria-labelledby="risks-title">
    <div className="work-heading"><div><p className="eyebrow">GOVERNANCE</p><h1 id="risks-title">Risks</h1><p>Track exposure, mitigation, and review cadence.</p></div></div>
    {formError ? <div role="alert" className="inline-status error-panel">{formError}</div> : null}
    {mutationError ? <div role="alert" className="inline-status error-panel">{errorMessage(mutationError)}</div> : null}
    <form onSubmit={submitCreate}>
      <h2>Create risk</h2>
      <label>Risk description<textarea aria-label="Risk description" value={create.description} onChange={(e) => setCreate({ ...create, description: e.target.value })} /></label>
      <label>Probability (1-5)<input aria-label="Probability" type="number" min={1} max={5} value={create.probability} onChange={(e) => setCreate({ ...create, probability: Number(e.target.value) })} /></label>
      <label>Impact (1-5)<input aria-label="Impact" type="number" min={1} max={5} value={create.impact} onChange={(e) => setCreate({ ...create, impact: Number(e.target.value) })} /></label>
      <label>Status<select aria-label="Status" value={create.status} onChange={(e) => setCreate({ ...create, status: e.target.value as RiskStatus })}>{STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}</select></label>
      <label>Mitigation<textarea aria-label="Mitigation" value={create.mitigation} onChange={(e) => setCreate({ ...create, mitigation: e.target.value })} /></label>
      <label>Trigger<input aria-label="Trigger" value={create.trigger} onChange={(e) => setCreate({ ...create, trigger: e.target.value })} /></label>
      <label>Review at<input aria-label="Review at" type="datetime-local" value={create.reviewAt} onChange={(e) => setCreate({ ...create, reviewAt: e.target.value })} /></label>
      <label>Project ID<input aria-label="Project ID" value={create.projectId} onChange={(e) => setCreate({ ...create, projectId: e.target.value })} /></label>
      <label><input type="checkbox" checked={create.pinned} onChange={(e) => setCreate({ ...create, pinned: e.target.checked })} /> Pinned</label>
      <button type="submit" disabled={pending}>Create risk</button>
    </form>
    {query.isLoading ? <p role="status">Loading risks…</p> : null}
    {query.isError ? <div role="alert">{query.error.message}</div> : null}
    <ol className="work-list">{(query.data?.items ?? []).map((value) => {
      const archived = Boolean(value.archived_at)
      return <li key={value.id}>
        <div>
          <strong>{value.description}</strong>
          <small>{value.status.replaceAll('_', ' ')} · P{value.probability}×I{value.impact} · score {value.score}{archived ? ' · archived' : ''}</small>
        </div>
        {value.factors.length ? <ul className="risk-factors" aria-label={`Factors for ${value.description}`}>{value.factors.map((factor) => <li key={factor.code}>{factor.label} (+{factor.points})</li>)}</ul> : null}
        <div className="work-actions" aria-label={`Actions for ${value.description}`}>
          {!archived ? <button type="button" disabled={pending} aria-label={`Edit ${value.description}`} onClick={() => { setFormError(null); setEdit({ risk: value, ...fromRisk(value), latestVersion: value.version, conflict: false, reloadFailed: false }) }}>Edit</button> : null}
          {!archived ? <button type="button" disabled={pending} aria-label={`Archive ${value.description}`} onClick={() => actionMutation.mutate({ risk: value, action: 'archive' })}>Archive</button> : <button type="button" disabled={pending} aria-label={`Restore ${value.description}`} onClick={() => actionMutation.mutate({ risk: value, action: 'restore' })}>Restore</button>}
        </div>
      </li>
    })}</ol>
    {edit ? <form onSubmit={submitEdit}><h2>Edit risk</h2>
      <label>Edit risk description<textarea aria-label="Edit risk description" value={edit.description} onChange={(e) => setEdit({ ...edit, description: e.target.value })} /></label>
      <label>Edit probability<input aria-label="Edit probability" type="number" min={1} max={5} value={edit.probability} onChange={(e) => setEdit({ ...edit, probability: Number(e.target.value) })} /></label>
      <label>Edit impact<input aria-label="Edit impact" type="number" min={1} max={5} value={edit.impact} onChange={(e) => setEdit({ ...edit, impact: Number(e.target.value) })} /></label>
      <label>Edit risk status<select aria-label="Edit risk status" disabled={edit.risk.status === 'closed'} value={edit.status} onChange={(e) => setEdit({ ...edit, status: e.target.value as RiskStatus })}>{STATUSES.map((status) => <option key={status} value={status}>{status}</option>)}</select></label>
      <label>Edit mitigation<textarea aria-label="Edit mitigation" value={edit.mitigation} onChange={(e) => setEdit({ ...edit, mitigation: e.target.value })} /></label>
      <label>Edit trigger<input aria-label="Edit trigger" value={edit.trigger} onChange={(e) => setEdit({ ...edit, trigger: e.target.value })} /></label>
      <label>Edit review at<input aria-label="Edit review at" type="datetime-local" value={edit.reviewAt} onChange={(e) => setEdit({ ...edit, reviewAt: e.target.value })} /></label>
      <label>Edit project ID<input aria-label="Edit project ID" value={edit.projectId} onChange={(e) => setEdit({ ...edit, projectId: e.target.value })} /></label>
      <label><input aria-label="Edit pinned" type="checkbox" checked={edit.pinned} onChange={(e) => setEdit({ ...edit, pinned: e.target.checked })} /> Pinned</label>
      {edit.reloadFailed ? <><p role="alert">Could not reload the latest risk. Your edits are preserved.</p><button type="button" disabled={pending} onClick={() => void reloadLatestRisk(edit.risk.id)}>Reload latest risk</button></> : edit.conflict ? <button type="button" disabled={pending} onClick={() => submitEdit()}>Retry with latest version</button> : <button type="submit" disabled={pending}>Save risk</button>}
      <button type="button" disabled={pending} onClick={() => setEdit(null)}>Discard edit</button>
    </form> : null}
  </section>
}
