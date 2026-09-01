import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { apiErrorMessage } from '../../api/errorMessage'
import type { KillSwitchStatus, PolicyListResponse, TriggerListResponse, WorkflowVersion } from './types'
import SimulationView from './SimulationView'

/** Real, distinct, readable feedback for every documented publish/disable
 * error code (API-SCHEMAS.md) -- never raw JSON. */
function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === 'WORKFLOW_VERSION_NOT_DRAFT') {
    const details = error.current as { status?: string } | undefined
    return `This version is already ${details?.status ?? 'not a draft'} and cannot be published again from here.`
  }
  if (error instanceof ApiError && error.code === 'ACTION_REF_NOT_REGISTERED') {
    const violations = (error.current as { violations?: string[] } | undefined)?.violations
    return violations?.length ? `This graph references an unregistered action: ${violations.join('; ')}` : 'This graph references an action that is not registered.'
  }
  return apiErrorMessage(error, {
    WORKFLOW_VERSION_ACTIVE_CONFLICT: 'Another version became active for this workflow at the same time. Reload and retry.',
    WORKFLOW_NOT_ACTIVE: 'This version is not currently active, so it cannot be disabled.',
    WORKFLOW_NOT_FOUND: 'This workflow version no longer exists in this workspace.',
    OFFLINE: 'You are offline, so this could not be read from or sent to the server.',
    NETWORK_ERROR: 'Could not reach the server, so this could not be read or changed.',
    '401': 'Your session is no longer valid. Sign in again to view or change this workflow.',
  })
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
  const policyRef = detail.data?.policy_ref ?? null

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

  /** The attached policy's own lifecycle status, cross-referenced the same
   * way `RunWorkspace.tsx`'s `RunDetailView` resolves a run's `policy_id`
   * against the workflow-filtered policy list -- `version.policy_ref` alone
   * is a bare UUID that says nothing about whether the authority it names is
   * still active, expired or revoked. Policies are only ever fetched as a
   * `workflow_id`-filtered list in this feature (`PolicyPanel.tsx`,
   * `RunWorkspace.tsx`); there is no fetch-one-policy-by-id endpoint. */
  const policies = useQuery({
    queryKey: ['automation', 'policies', workflowId],
    queryFn: () => apiRequest<PolicyListResponse>(`/api/v1/automations/policies?workflow_id=${encodeURIComponent(workflowId ?? '')}`),
    enabled: workflowId !== null && policyRef !== null,
    retry: 1,
  })
  const attachedPolicy = policies.data?.policies.find((policy) => policy.id === policyRef) ?? null

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
      // Prefix, not the exact `workflowId` key -- a global switch toggled from
      // `KillSwitchPanel.tsx` and this workflow's own switch share the same
      // effective state, so every kill-switch query is refetched together.
      void queryClient.invalidateQueries({ queryKey: ['automation', 'kill-switch'] })
    },
  })

  const pending = publishMutation.isPending || disableMutation.isPending || killSwitchMutation.isPending
  /** UX-STATES.md requires a workflow's kill-switch state be visible *before*
   * a user could attempt a blocked action. While that state is still loading
   * or has failed to load it is genuinely unknown, so every action a kill
   * switch would block is disabled rather than offered against an unverified
   * assumption that nothing is blocked. */
  const killSwitchUnknown = killSwitch.isLoading || killSwitch.isError
  const globalKillActive = killSwitch.data?.active_global != null
  const workflowKillActive = killSwitch.data?.active_workflow != null

  if (detail.isLoading) return <p role="status">Loading workflow…</p>
  if (detail.isError) return <div role="alert" className="inline-status error-panel">{errorMessage(detail.error)}</div>
  if (!detail.data) return null
  const version = detail.data

  return (
    <section className="work-panel" aria-labelledby="automation-workflow-detail-title">
      <h2 id="automation-workflow-detail-title">{version.workflow_id} · v{version.version}</h2>
      <dl>
        <div><dt>Status</dt><dd>{version.status}</dd></div>
        <div><dt>Trigger references</dt><dd>{version.trigger_refs.join(', ') || 'none'}</dd></div>
        <div><dt>Policy</dt><dd>
          {version.policy_ref === null ? 'none attached' : (
            <>
              {version.policy_ref}
              {policies.isLoading ? ' · checking this policy\'s current status…' : null}
              {policies.isError ? <span role="alert"> · status could not be confirmed ({errorMessage(policies.error)})</span> : null}
              {attachedPolicy ? ` · currently ${attachedPolicy.status}` : null}
              {policies.data && !attachedPolicy ? ' · not in this workflow\'s policy list, so its status is not knowable here' : null}
            </>
          )}
        </dd></div>
        <div><dt>Definition hash</dt><dd>{version.definition_hash}</dd></div>
      </dl>

      {killSwitch.isLoading ? <p role="status">Loading kill-switch status… actions a kill switch would block are disabled until it loads.</p> : null}
      {killSwitch.isError ? (
        <div role="alert" className="inline-status error-panel">
          <p>Kill-switch status could not be confirmed -- actions are disabled until it loads. This workflow may or may not be blocked from starting new runs; that is not knowable from here right now.</p>
          <p>{errorMessage(killSwitch.error)}</p>
        </div>
      ) : null}
      {killSwitch.data ? (
        <div role="status" className={killSwitch.data.killed ? 'inline-status error-panel' : 'inline-status'} aria-label="Kill switch status">
          {killSwitch.data.killed
            ? `Kill switch active (${killSwitch.data.active_global ? 'global' : ''}${killSwitch.data.active_global && killSwitch.data.active_workflow ? ' and ' : ''}${killSwitch.data.active_workflow ? 'this workflow' : ''}) -- new runs against this workflow will be rejected before starting.`
            : 'No kill switch is active for this workflow. New runs are permitted (subject to policy and approval gates).'}
        </div>
      ) : null}
      {/* A global switch is not addressed by any workflow_id, so a
          per-workflow deactivate returns 200 and changes nothing about the
          block -- a false remedy. With a global switch active, that button is
          only offered when this workflow *also* has its own active switch
          (where it does do something real, just not enough), and the note
          below always points at where the global block is actually cleared. */}
      {globalKillActive ? (
        <p role="status" className="inline-status">
          The active block is global, so no per-workflow action here clears it -- deactivate the global kill switch from the Kill switches tab.
          {workflowKillActive ? ' This workflow also has its own switch active; deactivating it below leaves the global block in place.' : ''}
        </p>
      ) : null}
      <div className="field-form">
        {/* No `aria-label`: the wrapping label's visible text is the accessible
            name (WCAG 2.5.3 -- "Kill switch reason" did not contain the visible
            "Kill switch reason (optional)"), and it is the only reason field on
            this surface. */}
        <label>Kill switch reason (optional)
          <input value={killSwitchReason} onChange={(e) => setKillSwitchReason(e.target.value)} disabled={!workflowId || killSwitchUnknown} />
        </label>
      </div>
      <div className="work-actions">
        <button type="button" className="btn-destructive" disabled={pending || !workflowId || killSwitchUnknown} onClick={() => killSwitchMutation.mutate(true)}>{killSwitchMutation.isPending && killSwitchMutation.variables === true ? 'Activating…' : 'Activate kill switch for this workflow'}</button>
        {globalKillActive && !workflowKillActive ? null : (
          <button type="button" disabled={pending || !workflowId || killSwitchUnknown} onClick={() => killSwitchMutation.mutate(false)}>{killSwitchMutation.isPending && killSwitchMutation.variables === false ? 'Deactivating…' : 'Deactivate kill switch for this workflow'}</button>
        )}
      </div>

      <h3>Schedule &amp; triggers</h3>
      {triggers.isLoading ? <p role="status">Loading trigger configuration…</p> : null}
      {/* Without this branch a failed trigger fetch renders a bare heading and
          an empty list -- visually identical to a manual-only workflow, even
          for one on a real cron schedule. */}
      {triggers.isError ? (
        <div role="alert" className="inline-status error-panel">
          <p>Trigger configuration could not be loaded, so this workflow's schedule is unknown -- it may still be on a schedule that fires automatically. This is not the same as having no triggers.</p>
          <p>{errorMessage(triggers.error)}</p>
        </div>
      ) : null}
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
          <button type="button" disabled={pending || killSwitchUnknown} onClick={() => publishMutation.mutate()}>{publishMutation.isPending ? 'Publishing…' : 'Publish this version'}</button>
        ) : null}
        {version.status === 'active' ? (
          <button type="button" className="btn-destructive" disabled={pending} onClick={() => disableMutation.mutate()}>{disableMutation.isPending ? 'Disabling…' : 'Disable this version'}</button>
        ) : null}
        <button type="button" onClick={() => setShowSimulation((current) => !current)}>
          {showSimulation ? 'Hide simulation' : 'Simulate this version'}
        </button>
      </div>

      {showSimulation ? <SimulationView versionId={versionId} /> : null}
    </section>
  )
}
