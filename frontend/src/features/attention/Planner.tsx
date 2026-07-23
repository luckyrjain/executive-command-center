import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'

export type PlanBlock = {
  id: string
  source_type: string
  source_id: string | null
  starts_at: string
  ends_at: string
  status: 'proposed' | 'accepted'
  rationale: string
  is_default_effort: boolean
}

export type PlanConflict = { code: string; detail: string; source_type?: string | null; source_id?: string | null }
export type PlanUnscheduled = { source_type: string; source_id: string | null; label: string; reason: string }
export type PlanDiffEntry = { source_type: string; source_id: string | null; label: string; change: 'added' | 'removed' | 'moved' | 'unchanged' | 'newly_conflicted' }

export type Plan = {
  id: string
  period_start: string
  period_end: string
  status: 'draft' | 'proposed' | 'accepted' | 'completed' | 'superseded'
  policy_version: number
  capacity_minutes: number
  conflicts: PlanConflict[]
  unscheduled: PlanUnscheduled[]
  superseded_by: string | null
  accepted_at: string | null
  created_at: string
  updated_at: string
  version: number
  blocks: PlanBlock[]
  diff: PlanDiffEntry[] | null
}

type PlanList = { items: Plan[]; next_cursor?: string | null }

function usedMinutes(blocks: PlanBlock[]): number {
  return blocks.reduce((total, block) => total + (new Date(block.ends_at).getTime() - new Date(block.starts_at).getTime()) / 60000, 0)
}

function errorMessage(error: Error): string {
  if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') return 'This plan changed since it was loaded. Refresh and try again.'
  if (error instanceof ApiError && error.code === 'STALE_PLAN') return 'The plan’s sources have changed. Replan before accepting.'
  if (error instanceof ApiError && error.code === 'CAPACITY_EXCEEDED') return 'This period has no remaining capacity for more work.'
  return error.message
}

function todayIso(offsetDays = 1): string {
  const date = new Date()
  date.setDate(date.getDate() + offsetDays)
  return date.toISOString().slice(0, 10)
}

function pad(value: number): string { return String(value).padStart(2, '0') }
function toLocalInputValue(iso: string): string {
  const instant = new Date(iso)
  if (Number.isNaN(instant.getTime())) return ''
  return `${instant.getFullYear()}-${pad(instant.getMonth() + 1)}-${pad(instant.getDate())}T${pad(instant.getHours())}:${pad(instant.getMinutes())}`
}

export default function Planner() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['plans'],
    queryFn: () => apiRequest<PlanList>('/api/v1/plans?limit=20'),
    retry: 1,
  })
  const [periodStart, setPeriodStart] = useState(todayIso())
  const [periodEnd, setPeriodEnd] = useState(todayIso())
  const [pendingDiff, setPendingDiff] = useState<Plan | null>(null)
  const [editingBlock, setEditingBlock] = useState<{ blockId: string; startsAt: string; endsAt: string } | null>(null)

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['plans'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard', 'today'] })
    void queryClient.invalidateQueries({ queryKey: ['brief', 'morning'] })
  }

  // A VERSION_CONFLICT means the cached plan (and its version) is stale --
  // refetching here (rather than only on success) is what lets the next
  // user action retry against the current version, instead of failing the
  // same way again against data that's still stale in the query cache.
  const onVersionConflict = (error: Error) => {
    if (error instanceof ApiError && error.code === 'VERSION_CONFLICT') refresh()
  }

  const createMutation = useMutation({
    mutationFn: () => apiRequest<Plan>('/api/v1/plans', { method: 'POST', body: { period_start: periodStart, period_end: periodEnd } }),
    onSuccess: () => refresh(),
  })
  const acceptMutation = useMutation({
    mutationFn: (plan: Plan) => apiRequest<Plan>(`/api/v1/plans/${plan.id}/accept`, { method: 'POST', body: { expected_version: plan.version } }),
    onSuccess: () => refresh(),
    onError: onVersionConflict,
  })
  const supersedeMutation = useMutation({
    mutationFn: (plan: Plan) => apiRequest<Plan>(`/api/v1/plans/${plan.id}/supersede`, { method: 'POST', body: { expected_version: plan.version } }),
    onSuccess: () => refresh(),
    onError: onVersionConflict,
  })
  // Replanning always presents the diff before acceptance (UX-STATES.md).
  // The replan call itself just proposes; a separate, explicit accept step
  // is required once the operator has reviewed the diff.
  const replanMutation = useMutation({
    mutationFn: (plan: Plan) => apiRequest<Plan>(`/api/v1/plans/${plan.id}/propose`, { method: 'POST', body: { expected_version: plan.version } }),
    onSuccess: (newPlan) => { setPendingDiff(newPlan); refresh() },
    onError: onVersionConflict,
  })
  // Block editing is keyboard-only (plain numeric/datetime inputs, no
  // drag-and-drop) -- UX-STATES.md requires DnD to have a full keyboard
  // equivalent, so this is the first-class flow, not a fallback.
  const moveBlockMutation = useMutation({
    mutationFn: ({ plan, block, startsAt, endsAt }: { plan: Plan; block: PlanBlock; startsAt: string; endsAt: string }) =>
      apiRequest<Plan>(`/api/v1/plans/${plan.id}/blocks/${block.id}/move`, {
        method: 'POST',
        body: { expected_version: plan.version, starts_at: new Date(startsAt).toISOString(), ends_at: new Date(endsAt).toISOString() },
      }),
    onSuccess: () => refresh(),
    onError: onVersionConflict,
  })
  const removeBlockMutation = useMutation({
    mutationFn: ({ plan, block }: { plan: Plan; block: PlanBlock }) =>
      apiRequest<Plan>(`/api/v1/plans/${plan.id}/blocks/${block.id}/remove`, {
        method: 'POST',
        body: { expected_version: plan.version },
      }),
    onSuccess: () => refresh(),
    onError: onVersionConflict,
  })
  const pending = createMutation.isPending || acceptMutation.isPending || supersedeMutation.isPending
    || replanMutation.isPending || moveBlockMutation.isPending || removeBlockMutation.isPending
  const mutationError = createMutation.error ?? acceptMutation.error ?? supersedeMutation.error
    ?? replanMutation.error ?? moveBlockMutation.error ?? removeBlockMutation.error

  function submitCreate(event: FormEvent) {
    event.preventDefault()
    createMutation.mutate()
  }

  const plans = (query.data?.items ?? []).filter((plan) => plan.status !== 'superseded')

  return (
    <section className="work-panel" aria-labelledby="planner-title">
      <div className="work-heading">
        <div>
          <p className="eyebrow">PLANNING</p>
          <h1 id="planner-title">Planner</h1>
          <p>Deterministic daily and weekly plans, with capacity, conflicts, and unscheduled work always visible.</p>
        </div>
      </div>

      {mutationError ? <div role="alert" className="inline-status error-panel">{errorMessage(mutationError)}</div> : null}

      <form onSubmit={submitCreate}>
        <h2>Propose a plan</h2>
        <label>Period start<input aria-label="Period start" type="date" value={periodStart} onChange={(e) => setPeriodStart(e.target.value)} /></label>
        <label>Period end<input aria-label="Period end" type="date" value={periodEnd} onChange={(e) => setPeriodEnd(e.target.value)} /></label>
        <button type="submit" disabled={pending}>Propose plan</button>
      </form>

      {query.isLoading ? <p role="status">Loading plans…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && plans.length === 0 ? <p className="empty-state">No active plans for this period.</p> : null}

      {pendingDiff ? (
        <section className="dashboard-card" aria-labelledby="replan-diff-title">
          <h2 id="replan-diff-title">Review replan before accepting</h2>
          <ol className="item-list">
            {(pendingDiff.diff ?? []).map((entry, index) => (
              <li key={`${entry.source_type}-${entry.source_id ?? index}`}>
                <strong>{entry.label}</strong>
                <span className="item-meta"><span>{entry.change.replaceAll('_', ' ')}</span></span>
              </li>
            ))}
          </ol>
          <button type="button" disabled={pending} onClick={() => { acceptMutation.mutate(pendingDiff); setPendingDiff(null) }}>Accept new plan</button>
          <button type="button" disabled={pending} onClick={() => setPendingDiff(null)}>Keep reviewing</button>
        </section>
      ) : null}

      <ol className="work-list">
        {plans.map((plan) => {
          const used = usedMinutes(plan.blocks)
          const overCapacity = used > plan.capacity_minutes
          const noCapacity = plan.capacity_minutes === 0
          return (
            <li key={plan.id}>
              <div>
                <strong data-plan-status={plan.status}>{plan.period_start} – {plan.period_end} · {plan.status}</strong>
                <small>
                  {Math.round(used)} of {plan.capacity_minutes} minutes used
                  {noCapacity ? ' · no capacity configured' : overCapacity ? ' · over capacity' : ''}
                </small>
              </div>
              {plan.unscheduled.length ? (
                <p>{plan.unscheduled.length} item{plan.unscheduled.length === 1 ? '' : 's'} unscheduled: {plan.unscheduled.map((u) => u.label).join(', ')}</p>
              ) : null}
              {plan.conflicts.length ? (
                <ul aria-label={`Conflicts for plan ${plan.period_start}`}>
                  {plan.conflicts.map((conflict, index) => <li key={index}>{conflict.detail}</li>)}
                </ul>
              ) : null}
              <ol aria-label={`Blocks for plan ${plan.period_start}`}>
                {plan.blocks.map((block) => (
                  <li key={block.id}>
                    {block.rationale} · {new Date(block.starts_at).toLocaleString()} – {new Date(block.ends_at).toLocaleTimeString()}{block.is_default_effort ? ' · default estimate' : ''}
                    {plan.status === 'proposed' ? (
                      editingBlock?.blockId === block.id ? (
                        <span className="work-actions" role="group" aria-label={`Edit time for ${block.rationale}`}>
                          <label>New start<input aria-label={`New start for ${block.rationale}`} type="datetime-local" value={editingBlock.startsAt} onChange={(e) => setEditingBlock({ ...editingBlock, startsAt: e.target.value })} /></label>
                          <label>New end<input aria-label={`New end for ${block.rationale}`} type="datetime-local" value={editingBlock.endsAt} onChange={(e) => setEditingBlock({ ...editingBlock, endsAt: e.target.value })} /></label>
                          <button
                            type="button"
                            disabled={pending}
                            onClick={() => { moveBlockMutation.mutate({ plan, block, startsAt: editingBlock.startsAt, endsAt: editingBlock.endsAt }); setEditingBlock(null) }}
                          >
                            Save new time
                          </button>
                          <button type="button" disabled={pending} onClick={() => setEditingBlock(null)}>Cancel</button>
                        </span>
                      ) : (
                        <span className="work-actions" role="group" aria-label={`Actions for ${block.rationale}`}>
                          <button
                            type="button"
                            disabled={pending}
                            aria-label={`Move ${block.rationale}`}
                            onClick={() => setEditingBlock({ blockId: block.id, startsAt: toLocalInputValue(block.starts_at), endsAt: toLocalInputValue(block.ends_at) })}
                          >
                            Move
                          </button>
                          <button type="button" disabled={pending} aria-label={`Remove ${block.rationale}`} onClick={() => removeBlockMutation.mutate({ plan, block })}>Remove</button>
                        </span>
                      )
                    ) : null}
                  </li>
                ))}
              </ol>
              <div className="work-actions" role="group" aria-label={`Actions for plan ${plan.period_start}`}>
                {plan.status === 'proposed' ? <button type="button" disabled={pending} aria-label={`Accept plan ${plan.period_start}`} onClick={() => acceptMutation.mutate(plan)}>Accept</button> : null}
                {plan.status === 'proposed' || plan.status === 'accepted' ? <button type="button" disabled={pending} aria-label={`Replan ${plan.period_start}`} onClick={() => replanMutation.mutate(plan)}>Replan</button> : null}
                {plan.status === 'proposed' || plan.status === 'accepted' ? <button type="button" disabled={pending} aria-label={`Supersede plan ${plan.period_start}`} onClick={() => supersedeMutation.mutate(plan)}>Supersede</button> : null}
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}
