import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import type { ApprovalMode, Policy, PolicyListResponse } from './types'

const APPROVAL_MODES: ApprovalMode[] = ['preview_only', 'per_run', 'bounded_recurring']

type Draft = {
  workflowId: string
  actionTypes: string
  dataClasses: string
  valueLimit: string
  countLimit: string
  approvalMode: ApprovalMode
  schedule: string
}

const emptyDraft: Draft = { workflowId: '', actionTypes: '', dataClasses: '', valueLimit: '0', countLimit: '10', approvalMode: 'per_run', schedule: '' }

function errorMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  const details = error.current as { revoked_at?: string; expires_at?: string } | undefined
  if (error.code === 'WORKFLOW_NOT_FOUND') return 'That workflow ID does not exist in this workspace yet -- draft the workflow first.'
  if (error.code === 'POLICY_REVOKED') return `This policy was already revoked${details?.revoked_at ? ` at ${new Date(details.revoked_at).toLocaleString()}` : ''}.`
  if (error.code === 'POLICY_EXPIRED') return `This policy already expired${details?.expires_at ? ` at ${new Date(details.expires_at).toLocaleString()}` : ''} and cannot be revoked further.`
  if (error.code === 'POLICY_NOT_FOUND') return 'That policy no longer exists in this workspace.'
  return error.message
}

/**
 * Authority/policy review -- the scope a human needs to read and trust
 * before a workflow's steps can dispatch (`action_types`/`data_classes`/
 * `value_limit`/`count_limit`/`rate_limit`/`approval_mode`/`expires_at`/
 * `revoked_at`, per `PHASE-005-automation.md`'s Frontend changes line).
 */
export default function PolicyPanel() {
  const queryClient = useQueryClient()
  const [workflowFilter, setWorkflowFilter] = useState('')
  const [draft, setDraft] = useState<Draft>(emptyDraft)
  const [formError, setFormError] = useState<string | null>(null)

  const query = useQuery({
    queryKey: ['automation', 'policies', workflowFilter],
    queryFn: () => apiRequest<PolicyListResponse>(`/api/v1/automations/policies${workflowFilter ? `?workflow_id=${encodeURIComponent(workflowFilter)}` : ''}`),
    retry: 1,
  })

  const createMutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => apiRequest<Policy>('/api/v1/automations/policies', { method: 'POST', body }),
    onSuccess: () => {
      setDraft(emptyDraft)
      void queryClient.invalidateQueries({ queryKey: ['automation', 'policies'] })
    },
  })

  const revokeMutation = useMutation({
    mutationFn: (policyId: string) => apiRequest<Policy>(`/api/v1/automations/policies/${policyId}/revoke`, { method: 'POST' }),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['automation', 'policies'] }) },
  })

  function submit(event: FormEvent) {
    event.preventDefault()
    setFormError(null)
    if (!draft.workflowId.trim()) { setFormError('Workflow ID is required.'); return }
    const valueLimit = Number(draft.valueLimit)
    const countLimit = Number(draft.countLimit)
    if (!Number.isFinite(valueLimit) || valueLimit < 0) { setFormError('Value limit must be zero or a positive number.'); return }
    if (!Number.isInteger(countLimit) || countLimit < 0) { setFormError('Count limit must be zero or a positive whole number.'); return }
    createMutation.mutate({
      workflow_id: draft.workflowId.trim(),
      action_types: draft.actionTypes.split(',').map((v) => v.trim()).filter(Boolean),
      data_classes: draft.dataClasses.split(',').map((v) => v.trim()).filter(Boolean),
      value_limit: valueLimit,
      count_limit: countLimit,
      approval_mode: draft.approvalMode,
      schedule: draft.schedule.trim() || null,
      rate_limit: null,
    })
  }

  const pending = createMutation.isPending || revokeMutation.isPending
  const policies = query.data?.policies ?? []

  return (
    <section className="work-panel" aria-labelledby="automation-policy-title">
      <h2 id="automation-policy-title">Authority &amp; policy review</h2>

      <label>Filter by workflow ID
        <input aria-label="Filter policies by workflow ID" value={workflowFilter} onChange={(e) => setWorkflowFilter(e.target.value)} />
      </label>

      {query.isLoading ? <p role="status">Loading policies…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && policies.length === 0 ? <p className="empty-state">No policies recorded yet.</p> : null}
      {revokeMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(revokeMutation.error)}</div> : null}

      <ol className="work-list">
        {policies.map((policy) => (
          <li key={policy.id}>
            <div>
              <strong>{policy.workflow_id}</strong>
              <small>
                {policy.approval_mode.replaceAll('_', ' ')} · {policy.status}
                {' · value limit '}{policy.value_limit}{' · count limit '}{policy.count_limit}
              </small>
              <small>
                action types: {policy.action_types.join(', ') || 'none'} · data classes: {policy.data_classes.join(', ') || 'none'}
              </small>
              <small>expires {new Date(policy.expires_at).toLocaleString()}{policy.revoked_at ? ` · revoked ${new Date(policy.revoked_at).toLocaleString()}` : ''}</small>
            </div>
            <div className="work-actions">
              {policy.status === 'active' ? (
                <button type="button" disabled={pending} aria-label={`Revoke policy for ${policy.workflow_id}`} onClick={() => revokeMutation.mutate(policy.id)}>
                  Revoke
                </button>
              ) : null}
            </div>
          </li>
        ))}
      </ol>

      <form onSubmit={submit}>
        <h3>Create a policy</h3>
        {formError ? <div role="alert" className="inline-status error-panel">{formError}</div> : null}
        {createMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(createMutation.error)}</div> : null}

        <label>Workflow ID
          <input aria-label="Policy workflow ID" required value={draft.workflowId} onChange={(e) => setDraft({ ...draft, workflowId: e.target.value })} />
        </label>
        <label>Action types (comma separated)
          <input aria-label="Policy action types" value={draft.actionTypes} onChange={(e) => setDraft({ ...draft, actionTypes: e.target.value })} />
        </label>
        <label>Data classes (comma separated)
          <input aria-label="Policy data classes" value={draft.dataClasses} onChange={(e) => setDraft({ ...draft, dataClasses: e.target.value })} />
        </label>
        <label>Value limit
          <input aria-label="Policy value limit" type="number" min={0} step="0.01" value={draft.valueLimit} onChange={(e) => setDraft({ ...draft, valueLimit: e.target.value })} />
        </label>
        <label>Count limit
          <input aria-label="Policy count limit" type="number" min={0} step={1} value={draft.countLimit} onChange={(e) => setDraft({ ...draft, countLimit: e.target.value })} />
        </label>
        <label>Approval mode
          <select aria-label="Policy approval mode" value={draft.approvalMode} onChange={(e) => setDraft({ ...draft, approvalMode: e.target.value as ApprovalMode })}>
            {APPROVAL_MODES.map((mode) => <option key={mode} value={mode}>{mode.replaceAll('_', ' ')}</option>)}
          </select>
        </label>
        <label>Schedule note (optional)
          <input aria-label="Policy schedule note" value={draft.schedule} onChange={(e) => setDraft({ ...draft, schedule: e.target.value })} />
        </label>
        <button type="submit" disabled={pending}>{createMutation.isPending ? 'Creating…' : 'Create policy'}</button>
      </form>
    </section>
  )
}
