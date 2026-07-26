import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import type { KillSwitchStatus, TriggerListResponse, WorkflowVersion } from './types'
import SimulationView from './SimulationView'

/** Real, distinct, readable feedback for every documented publish/disable
 * error code (API-SCHEMAS.md) -- never raw JSON. */
function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  const details = error.current as { code?: string; violations?: string[]; status?: string } | undefined
  if (error.code === 'WORKFLOW_VERSION_NOT_DRAFT') return `This version is already ${details?.status ?? 'not a draft'} and cannot be published again from here.`
  if (error.code === 'WORKFLOW_VERSION_ACTIVE_CONFLICT') return 'Another version became active for this workflow at the same time. Reload and retry.'
  if (error.code === 'ACTION_REF_NOT_REGISTERED') {
    const violations = details?.violations
    return violations?.length ? `This graph references an unregistered action: ${violations.join('; ')}` : 'This graph references an action that is not registered.'
  }
  if (error.code === 'WORKFLOW_NOT_ACTIVE') return 'This version is not currently active, so it cannot be disabled.'
  if (error.code === 'WORKFLOW_NOT_FOUND') return 'This workflow version no longer exists in this workspace.'
  return error.message
}

function formatSchedule(trigger: TriggerListResponse['triggers'][number]): string {
  if (trigger.trigger_type === 'manual') return 'Manual trigger only.'
  if (trigger.trigger_type === 'event') return `Fires on event: ${trigger.event_type_filter ?? 'unspecified'}`
  const lastFired = trigger.last_fired_at ? new Date(trigger.last_fired_at).toLocaleString() : 'never'
  const skip = trigger.skip_missed ? 'skips missed windows' : 'catches up once on a missed window'
  return `${trigger.schedule_expression ?? 'unspecified schedule'} (${trigger.timezone ?? 'no timezone'}) · last fired ${lastFired} · ${skip}`
}

export default function WorkflowDetail({ versionId }: { versionId: string }) {
  const queryClient = useQueryClient()
  const [showSimulation, setShowSimulation] = useState(false)
  const [killSwitchReason, setKillSwitchReason] = useState('')

  const detail = useQuery({
    queryKey: ['automation', 'workflow', versionId],
    queryFn: () => apiRequest<WorkflowVersion>(`/api/v1/automations/workflows/${versionId}`),
    retry: 1,
  })

  const workflowId = detail.data?.workflow_id ?? null

  const triggers = useQuery({
    queryKey: ['automation', 'triggers', workflowId],
    queryFn: () => apiRequest<TriggerListResponse>(`/api/v1/automations/triggers?workflow_id=${encodeURIComponent(workflowId ?? '')}`),
    enabled: workflowId !== null,
    retry: 1,
  })

  const killSwitch = useQuery({
    queryKey: ['automation', 'kill-switch', workflowId],
    queryFn: () => apiRequest<KillSwitchStatus>(`/api/v1/automations/workflows/${encodeURIComponent(workflowId ?? '')}/kill_switch`),
    enabled: workflowId !== null,
    retry: 1,
  })

  const publishMutation = useMutation({
    mutationFn: () => apiRequest<WorkflowVersion>(`/api/v1/automations/workflows/${versionId}/publish`, { method: 'POST' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['automation', 'workflow', versionId] })
      void queryClient.invalidateQueries({ queryKey: ['automation', 'workflows'] })
    },
  })

  const disableMutation = useMutation({
    mutationFn: () => apiRequest<WorkflowVersion>(`/api/v1/automations/workflows/${versionId}/disable`, { method: 'POST' }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['automation', 'workflow', versionId] })
      void queryClient.invalidateQueries({ queryKey: ['automation', 'workflows'] })
    },
  })

  const killSwitchMutation = useMutation({
    mutationFn: (active: boolean) =>
      apiRequest(`/api/v1/automations/workflows/${encodeURIComponent(workflowId ?? '')}/kill_switch`, {
        method: 'POST',
        body: { active, reason: killSwitchReason.trim() || null },
      }),
    onSuccess: () => {
      setKillSwitchReason('')
      void queryClient.invalidateQueries({ queryKey: ['automation', 'kill-switch', workflowId] })
    },
  })

  const pending = publishMutation.isPending || disableMutation.isPending || killSwitchMutation.isPending

  if (detail.isLoading) return <p role="status">Loading workflow…</p>
  if (detail.isError) return <div role="alert" className="inline-status error-panel">{detail.error.message}</div>
  if (!detail.data) return null
  const version = detail.data

  return (
    <section className="work-panel" aria-labelledby="automation-workflow-detail-title">
      <h2 id="automation-workflow-detail-title">{version.workflow_id} · v{version.version}</h2>
      <dl>
        <div><dt>Status</dt><dd>{version.status}</dd></div>
        <div><dt>Trigger references</dt><dd>{version.trigger_refs.join(', ') || 'none'}</dd></div>
        <div><dt>Policy</dt><dd>{version.policy_ref ?? 'none attached'}</dd></div>
        <div><dt>Definition hash</dt><dd>{version.definition_hash}</dd></div>
      </dl>

      {killSwitch.data ? (
        <div role="status" className={killSwitch.data.killed ? 'inline-status error-panel' : 'inline-status'} aria-label="Kill switch status">
          {killSwitch.data.killed
            ? `Kill switch active (${killSwitch.data.active_global ? 'global' : ''}${killSwitch.data.active_global && killSwitch.data.active_workflow ? ' and ' : ''}${killSwitch.data.active_workflow ? 'this workflow' : ''}) -- new runs against this workflow will be rejected before starting.`
            : 'No kill switch is active for this workflow. New runs are permitted (subject to policy and approval gates).'}
        </div>
      ) : null}
      <div className="work-actions">
        <label>Kill switch reason (optional)
          <input aria-label="Kill switch reason" value={killSwitchReason} onChange={(e) => setKillSwitchReason(e.target.value)} disabled={!workflowId} />
        </label>
        <button type="button" disabled={pending || !workflowId} onClick={() => killSwitchMutation.mutate(true)}>Activate kill switch for this workflow</button>
        <button type="button" disabled={pending || !workflowId} onClick={() => killSwitchMutation.mutate(false)}>Deactivate kill switch for this workflow</button>
      </div>

      <h3>Schedule &amp; triggers</h3>
      {triggers.isLoading ? <p role="status">Loading trigger configuration…</p> : null}
      {triggers.data && triggers.data.triggers.length === 0 ? <p className="empty-state">No configured triggers for this workflow.</p> : null}
      <ul aria-label="Configured triggers">
        {(triggers.data?.triggers ?? []).map((trigger) => (
          <li key={trigger.id}>{trigger.trigger_type}: {formatSchedule(trigger)}</li>
        ))}
      </ul>

      <h3>Steps</h3>
      <ol className="work-list">
        {version.graph.steps.map((step) => (
          <li key={step.step_id}>
            <div>
              <strong>{step.step_id}</strong>
              <small>
                {step.step_type}
                {step.action_ref ? ` · ${step.action_ref}` : ''}
                {step.compensate_ref ? ` · compensated by ${step.compensate_ref}` : ''}
              </small>
            </div>
            <div className="item-meta">
              <span>success → {step.on_success ?? 'n/a'}</span>
              <span>failure → {step.on_failure ?? 'n/a'}</span>
            </div>
          </li>
        ))}
      </ol>

      {publishMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(publishMutation.error)}</div> : null}
      {disableMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(disableMutation.error)}</div> : null}
      {killSwitchMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(killSwitchMutation.error)}</div> : null}

      <div className="work-actions">
        {version.status === 'draft' ? (
          <button type="button" disabled={pending} onClick={() => publishMutation.mutate()}>Publish this version</button>
        ) : null}
        {version.status === 'active' ? (
          <button type="button" disabled={pending} onClick={() => disableMutation.mutate()}>Disable this version</button>
        ) : null}
        <button type="button" onClick={() => setShowSimulation((current) => !current)}>
          {showSimulation ? 'Hide simulation' : 'Simulate this version'}
        </button>
      </div>

      {showSimulation ? <SimulationView versionId={versionId} /> : null}
    </section>
  )
}
