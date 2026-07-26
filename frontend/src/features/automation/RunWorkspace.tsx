import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import type { KillSwitchStatus, PolicyListResponse, Run, RunDetail, RunListResponse, RunStatus } from './types'

// This activation's fixed retry ceiling (`worker.MAX_RETRY_ATTEMPTS = 3`,
// 2s/4s/8s backoff) -- a static, documented backend constant, not
// user-configurable, so it is safe to mirror here for display only.
const MAX_RETRY_ATTEMPTS = 3

const RUN_STATUSES: RunStatus[] = [
  'queued', 'leased', 'running', 'waiting_approval', 'paused', 'needs_review',
  'succeeded', 'failed', 'cancelled', 'compensating', 'compensated', 'compensation_failed',
  'expired', 'rate_limited', 'preview_blocked',
]

/** Terminal statuses -- nothing further will happen to a run in one of
 * these, so neither Pause/Resume nor Cancel is offered. `preview_blocked`
 * belongs here for the same reason `succeeded` does: the run is finished,
 * not stuck (see statusDescription below). */
const TERMINAL_RUN_STATUSES: RunStatus[] = [
  'succeeded', 'failed', 'cancelled', 'compensated', 'compensation_failed',
  'expired', 'rate_limited', 'preview_blocked',
]

/** Plain-language description of every DATA-MODEL.md run status --
 * UX-STATES.md's required states (waiting approval, expired, paused,
 * retrying [step-level, see RunDetailView], unknown/needs_review, partially
 * compensated, kill switch active) all route through this run-status
 * vocabulary plus the step/compensation ledger below. */
function statusDescription(status: RunStatus): string {
  switch (status) {
    case 'queued': return 'Queued -- waiting for a worker to claim it.'
    case 'leased': return 'Claimed by a worker, about to start.'
    case 'running': return 'Actively dispatching steps.'
    case 'waiting_approval': return 'Paused -- a step needs a human approval decision before it can continue.'
    case 'paused': return 'Paused by an operator. Resume to continue from where it left off.'
    case 'needs_review': return 'Stopped for human review -- an ambiguous outcome, a blocked policy, or a kill switch. See below for what is knowable about the cause.'
    case 'succeeded': return 'Completed successfully.'
    case 'failed': return 'Failed -- no further automatic action will be taken.'
    case 'cancelled': return 'Cancelled before its next step could dispatch.'
    case 'compensating': return 'Rolling back already-succeeded steps that declared a compensation action.'
    case 'compensated': return 'Failed, but every declared compensation action succeeded.'
    case 'compensation_failed': return 'Failed, and at least one compensation action itself failed or is unknown -- see the compensation ledger below for exactly which.'
    case 'expired': return 'Expired.'
    case 'rate_limited': return 'Blocked by the policy\'s own rate limit.'
    case 'preview_blocked': return 'Finished without dispatching anything -- this run\'s policy is preview-only, which never authorizes a real action. Nothing failed and nothing needs fixing.'
    default: return status
  }
}

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  const details = error.current as { workflow_id?: string; status?: string; limit?: number } | undefined
  if (error.code === 'WORKFLOW_NOT_ACTIVE') return `Workflow "${details?.workflow_id ?? ''}" has no active version -- publish a version before running it.`
  if (error.code === 'KILL_SWITCH_ACTIVE') return `A kill switch is active for workflow "${details?.workflow_id ?? ''}" -- new runs are rejected until it is deactivated.`
  // Enqueue-time rate limiting (`worker.RunRateLimited`): the policy's own
  // runs-per-workflow-per-hour ceiling is already used up for this trailing
  // hour. Names the limit so an operator can tell "misconfigured policy"
  // apart from "I really did start that many runs".
  if (error.code === 'RATE_LIMITED') return `Workflow "${details?.workflow_id ?? ''}" has already used its policy's limit of ${details?.limit ?? 'allowed'} runs per hour -- the next run is rejected until the trailing hour rolls over.`
  if (error.code === 'RUN_NOT_PAUSED') return `This run is ${details?.status ?? 'not paused'}, so it cannot be resumed.`
  if (error.code === 'RUN_NOT_FOUND') return 'This run no longer exists in this workspace.'
  return error.message
}

function RunDetailView({ run }: { run: RunDetail }) {
  const queryClient = useQueryClient()

  const killSwitch = useQuery({
    queryKey: ['automation', 'kill-switch', run.workflow_id],
    queryFn: () => apiRequest<KillSwitchStatus>(`/api/v1/automations/workflows/${encodeURIComponent(run.workflow_id)}/kill_switch`),
    retry: 1,
  })
  const policies = useQuery({
    queryKey: ['automation', 'policies', run.workflow_id],
    queryFn: () => apiRequest<PolicyListResponse>(`/api/v1/automations/policies?workflow_id=${encodeURIComponent(run.workflow_id)}`),
    enabled: run.status === 'needs_review',
    retry: 1,
  })

  const mutate = useMutation({
    mutationFn: (action: 'pause' | 'resume' | 'cancel') => apiRequest<Run>(`/api/v1/automations/runs/${run.id}/${action}`, { method: 'POST' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['automation', 'run', run.id] })
      void queryClient.invalidateQueries({ queryKey: ['automation', 'runs'] })
    },
  })
  const pending = mutate.isPending

  const runningPolicy = policies.data?.policies.find((policy) => policy.id === run.policy_id) ?? null
  const lastStep = run.steps.length ? run.steps[run.steps.length - 1] : null
  const retryingStep = run.steps.find((step) => step.status === 'retrying')
  const mixedCompensation = run.status === 'compensation_failed' && run.compensation_steps.length > 0
    && run.compensation_steps.some((c) => c.status === 'succeeded') && run.compensation_steps.some((c) => c.status !== 'succeeded')

  return (
    <section className="work-panel" aria-labelledby="automation-run-detail-title">
      <h3 id="automation-run-detail-title">Run {run.id}</h3>
      <p role="status">{run.workflow_id} v{run.workflow_version} · <strong>{run.status.replaceAll('_', ' ')}</strong> -- {statusDescription(run.status)}</p>

      {retryingStep ? (
        <div role="status" className="inline-status">
          Step {retryingStep.step_index} is retrying (attempt {retryingStep.attempt_count} of {MAX_RETRY_ATTEMPTS}) after a classified transient failure. This run is otherwise queued and will be reclaimed automatically once its backoff elapses.
        </div>
      ) : null}

      {run.status === 'needs_review' ? (
        <div role="alert" className="inline-status error-panel">
          <strong>This run needs human review before it can proceed.</strong>
          {killSwitch.data?.killed ? <p>A kill switch is currently active for this workflow ({killSwitch.data.active_global ? 'global' : ''}{killSwitch.data.active_global && killSwitch.data.active_workflow ? ' and ' : ''}{killSwitch.data.active_workflow ? 'per-workflow' : ''}) -- this may be why this run stopped, though the timing is not a guarantee of cause.</p> : null}
          {runningPolicy && runningPolicy.status !== 'active' ? <p>This run's own policy is currently {runningPolicy.status} -- a likely, directly attributable cause.</p> : null}
          {lastStep?.status === 'unknown' ? <p>The last dispatched step's outcome is unknown -- the underlying action may or may not have happened. Inspect the target system directly before resolving this manually (see the recovery runbook).</p> : null}
          {!killSwitch.data?.killed && (!runningPolicy || runningPolicy.status === 'active') && lastStep?.status !== 'unknown' ? <p>No further cause is determinable from data available to this view.</p> : null}
        </div>
      ) : null}

      {run.status === 'preview_blocked' ? (
        // role="status", not role="alert", and no error-panel class: a
        // preview-only run reaching its end without dispatching is the
        // mode working exactly as designed, not a fault an operator has to
        // react to. Rendering it as an error would misreport the one
        // guarantee `docs/runbooks/PHASE-5-DOGFOOD.md`'s Stage 1 depends on.
        <div role="status" className="inline-status">
          <strong>Preview only -- no real action was taken.</strong>
          <p>
            This run&apos;s policy is in <code>preview_only</code> mode, which never authorizes a real
            side effect: every step was gated, any approval below was recorded for real, and no
            adapter was ever executed. Switch the workflow&apos;s policy to <code>per_run</code> or{' '}
            <code>bounded_recurring</code> and start a new run to dispatch for real.
          </p>
        </div>
      ) : null}

      {run.status === 'compensation_failed' ? (
        <div role="alert" className="inline-status error-panel">
          <strong>{mixedCompensation ? 'Partially compensated' : 'Compensation failed'}</strong> -- see the compensation ledger below for exactly which rollback actions succeeded and which did not.
        </div>
      ) : null}

      {mutate.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(mutate.error)}</div> : null}
      <div className="work-actions">
        {['queued', 'leased', 'running', 'waiting_approval'].includes(run.status) ? <button type="button" disabled={pending} onClick={() => mutate.mutate('pause')}>Pause</button> : null}
        {run.status === 'paused' ? <button type="button" disabled={pending} onClick={() => mutate.mutate('resume')}>Resume</button> : null}
        {!TERMINAL_RUN_STATUSES.includes(run.status) ? <button type="button" disabled={pending} onClick={() => mutate.mutate('cancel')}>Cancel</button> : null}
      </div>

      <h4>Steps</h4>
      <ol className="work-list">
        {run.steps.map((step) => (
          <li key={step.step_index}>
            <div>
              <strong>Step {step.step_index} · {step.step_type}</strong>
              <small>{step.status}{step.status === 'retrying' ? ` (attempt ${step.attempt_count} of ${MAX_RETRY_ATTEMPTS})` : ''}{step.error_class ? ` · ${step.error_class}` : ''}</small>
            </div>
          </li>
        ))}
        {run.steps.length === 0 ? <li><p className="empty-state">No steps have dispatched yet.</p></li> : null}
      </ol>

      <h4>Compensation ledger</h4>
      {run.compensation_steps.length === 0 ? <p className="empty-state">No compensation has been dispatched for this run.</p> : (
        <ol className="work-list" aria-label="Compensation ledger">
          {run.compensation_steps.map((step) => (
            <li key={step.compensates_step_index}>
              <div>
                <strong>Compensates step {step.compensates_step_index}</strong>
                <small>{step.status}{step.error_class ? ` · ${step.error_class}` : ''}</small>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

export default function RunWorkspace() {
  const queryClient = useQueryClient()
  const [statusFilter, setStatusFilter] = useState<RunStatus | ''>('')
  const [workflowId, setWorkflowId] = useState('')
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null)

  const runsQuery = useQuery({
    queryKey: ['automation', 'runs', statusFilter],
    queryFn: () => apiRequest<RunListResponse>(`/api/v1/automations/runs${statusFilter ? `?status=${statusFilter}` : ''}`),
    retry: 1,
  })

  const runDetailQuery = useQuery({
    queryKey: ['automation', 'run', selectedRunId],
    queryFn: () => apiRequest<RunDetail>(`/api/v1/automations/runs/${selectedRunId}`),
    enabled: selectedRunId !== null,
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: () => apiRequest<Run>('/api/v1/automations/runs', { method: 'POST', body: { workflow_id: workflowId.trim() } }),
    onSuccess: (created) => {
      setWorkflowId('')
      void queryClient.invalidateQueries({ queryKey: ['automation', 'runs'] })
      setSelectedRunId(created.id)
    },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    if (workflowId.trim()) createMutation.mutate()
  }

  const runs = runsQuery.data?.runs ?? []

  return (
    <div className="schedule-workspace">
      <section className="work-panel" aria-labelledby="automation-runs-title">
        <h2 id="automation-runs-title">Run history</h2>
        <form onSubmit={submit}>
          <label>Run a workflow (manual trigger)
            <input aria-label="Workflow ID to run" value={workflowId} onChange={(e) => setWorkflowId(e.target.value)} placeholder="workflow ID" />
          </label>
          <button type="submit" disabled={createMutation.isPending || !workflowId.trim()}>{createMutation.isPending ? 'Starting…' : 'Start run'}</button>
        </form>
        {createMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(createMutation.error)}</div> : null}

        <label>Filter by status
          <select aria-label="Filter runs by status" value={statusFilter} onChange={(e) => setStatusFilter(e.target.value as RunStatus | '')}>
            <option value="">All statuses</option>
            {RUN_STATUSES.map((status) => <option key={status} value={status}>{status.replaceAll('_', ' ')}</option>)}
          </select>
        </label>

        {runsQuery.isLoading ? <p role="status">Loading runs…</p> : null}
        {runsQuery.isError ? <div role="alert" className="inline-status error-panel">{runsQuery.error.message}</div> : null}
        {runsQuery.data && runs.length === 0 ? <p className="empty-state">No runs match this filter.</p> : null}

        <ol className="work-list">
          {runs.map((run) => (
            <li key={run.id}>
              <div>
                <strong>{run.workflow_id} v{run.workflow_version}</strong>
                <small>{run.status.replaceAll('_', ' ')} · queued {new Date(run.queued_at).toLocaleString()}</small>
              </div>
              <div className="work-actions">
                <button type="button" onClick={() => setSelectedRunId(run.id)}>View</button>
              </div>
            </li>
          ))}
        </ol>
      </section>

      {selectedRunId && runDetailQuery.isLoading ? <p role="status">Loading run detail…</p> : null}
      {selectedRunId && runDetailQuery.isError ? <div role="alert" className="inline-status error-panel">{runDetailQuery.error.message}</div> : null}
      {runDetailQuery.data ? <RunDetailView run={runDetailQuery.data} /> : null}
    </div>
  )
}
