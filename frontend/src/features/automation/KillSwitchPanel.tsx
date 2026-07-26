import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { KillSwitch, KillSwitchStatus } from './types'

/**
 * Global and per-workflow kill-switch controls (`POST /workflows/{id}/
 * kill_switch`, `POST /kill_switch`). UX-STATES.md requires a workflow's
 * kill-switch state be visible *before* a user attempts a new run against
 * it, not only discovered via a 409 after trying -- `WorkflowDetail.tsx`
 * already shows a workflow-scoped view of this at the point a user would
 * act on one specific workflow; this tab is the administrative surface for
 * global control and for checking any workflow_id without first navigating
 * to its own detail view.
 *
 * There is no dedicated "read only the current global state" endpoint in
 * this activation -- `GET /workflows/{id}/kill_switch` reports
 * `active_global` alongside a specific workflow's own state, since a
 * global switch is not itself addressed by any workflow_id. This panel
 * is honest about that: the global toggle buttons always work
 * (idempotent per the backend's own contract), but the *current* global
 * state display only appears once a workflow_id has been looked up here
 * -- never fabricated from a made-up probe id.
 */
export default function KillSwitchPanel() {
  const queryClient = useQueryClient()
  const [workflowId, setWorkflowId] = useState('')
  const [lookupId, setLookupId] = useState<string | null>(null)
  const [reason, setReason] = useState('')

  const status = useQuery({
    queryKey: ['automation', 'kill-switch', lookupId],
    queryFn: () => apiRequest<KillSwitchStatus>(`/api/v1/automations/workflows/${encodeURIComponent(lookupId ?? '')}/kill_switch`),
    enabled: lookupId !== null,
    retry: 1,
  })

  const globalMutation = useMutation({
    mutationFn: (active: boolean) => apiRequest<KillSwitch>('/api/v1/automations/kill_switch', { method: 'POST', body: { active, reason: reason.trim() || null } }),
    onSuccess: () => {
      setReason('')
      if (lookupId) void queryClient.invalidateQueries({ queryKey: ['automation', 'kill-switch', lookupId] })
    },
  })

  const workflowMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      apiRequest<KillSwitch>(`/api/v1/automations/workflows/${encodeURIComponent(id)}/kill_switch`, { method: 'POST', body: { active, reason: reason.trim() || null } }),
    onSuccess: (_result, variables) => {
      setReason('')
      void queryClient.invalidateQueries({ queryKey: ['automation', 'kill-switch', variables.id] })
    },
  })

  const pending = globalMutation.isPending || workflowMutation.isPending

  return (
    <section className="work-panel" aria-labelledby="automation-kill-switch-title">
      <h2 id="automation-kill-switch-title">Kill switches</h2>
      <p>A global kill switch stops every workflow in this workspace from starting a new run. A per-workflow switch stops only that workflow. Both take effect before the next not-yet-started step.</p>

      <label>Reason (optional, recorded on the switch)
        <input aria-label="Kill switch reason" value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>

      {globalMutation.isError ? <div role="alert" className="inline-status error-panel">{globalMutation.error instanceof Error ? globalMutation.error.message : 'Request failed.'}</div> : null}
      <div className="work-actions">
        <button type="button" disabled={pending} onClick={() => globalMutation.mutate(true)}>Activate global kill switch</button>
        <button type="button" disabled={pending} onClick={() => globalMutation.mutate(false)}>Deactivate global kill switch</button>
      </div>

      <h3>Check or toggle a specific workflow</h3>
      <label>Workflow ID
        <input aria-label="Workflow ID to check" value={workflowId} onChange={(e) => setWorkflowId(e.target.value)} />
      </label>
      <div className="work-actions">
        <button type="button" onClick={() => setLookupId(workflowId.trim() || null)} disabled={!workflowId.trim()}>Check current status</button>
        <button type="button" disabled={pending || !workflowId.trim()} onClick={() => workflowMutation.mutate({ id: workflowId.trim(), active: true })}>Activate for this workflow</button>
        <button type="button" disabled={pending || !workflowId.trim()} onClick={() => workflowMutation.mutate({ id: workflowId.trim(), active: false })}>Deactivate for this workflow</button>
      </div>
      {workflowMutation.isError ? <div role="alert" className="inline-status error-panel">{workflowMutation.error instanceof Error ? workflowMutation.error.message : 'Request failed.'}</div> : null}

      {status.isLoading ? <p role="status">Loading kill-switch status…</p> : null}
      {status.data ? (
        <div role="status" className={status.data.killed ? 'inline-status error-panel' : 'inline-status'}>
          <p><strong>{lookupId}</strong>: {status.data.killed ? 'blocked from starting new runs' : 'not blocked'}</p>
          <p>Global: {status.data.active_global ? `active${status.data.active_global.reason ? ` (${status.data.active_global.reason})` : ''}` : 'inactive'}</p>
          <p>This workflow: {status.data.active_workflow ? `active${status.data.active_workflow.reason ? ` (${status.data.active_workflow.reason})` : ''}` : 'inactive'}</p>
          {status.data.history.length ? (
            <details>
              <summary>Per-workflow activation history</summary>
              <ul>
                {status.data.history.map((entry, index) => (
                  <li key={index}>{entry.active ? 'activated' : 'deactivated'}{entry.reason ? `: ${entry.reason}` : ''} at {entry.activated_at ? new Date(entry.activated_at).toLocaleString() : 'unknown time'}</li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
