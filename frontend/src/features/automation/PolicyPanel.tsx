import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { apiErrorMessage } from '../../api/errorMessage'
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
  if (error instanceof ApiError && error.code === 'POLICY_REVOKED') {
    const details = error.current as { revoked_at?: string } | undefined
    return `This policy was already revoked${details?.revoked_at ? ` at ${new Date(details.revoked_at).toLocaleString()}` : ''}.`
  }
  if (error instanceof ApiError && error.code === 'POLICY_EXPIRED') {
    const details = error.current as { expires_at?: string } | undefined
    return `This policy already expired${details?.expires_at ? ` at ${new Date(details.expires_at).toLocaleString()}` : ''} and cannot be revoked further.`
  }
  return apiErrorMessage(error, {
    WORKFLOW_NOT_FOUND: 'That workflow ID does not exist in this workspace yet -- draft the workflow first.',
    POLICY_NOT_FOUND: 'That policy no longer exists in this workspace.',
    OFFLINE: 'You are offline, so policies could not be read or changed.',
    NETWORK_ERROR: 'Could not reach the server, so policies could not be read or changed.',
    '401': 'Your session is no longer valid. Sign in again to review policies.',
  })
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

      {/* Every input in this panel drops its `aria-label` in favour of its own
          wrapping label's visible text (WCAG 2.5.3 Label in Name): the old
          names ("Filter policies by workflow ID", "Policy value limit", …)
          did not contain the visible text a speech-input user can read, and
          each visible label is already unique within this panel, so none of
          them needs extra disambiguating context. `Revoke policy for {id}` on
          the revoke button below stays -- there it is one visible "Revoke"
          per row and the visible text *is* a substring of the name. */}
      <label>Filter by workflow ID
        <input value={workflowFilter} onChange={(e) => setWorkflowFilter(e.target.value)} />
      </label>

      {query.isLoading ? <p role="status">Loading policies…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(query.error)}</div> : null}
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
          <input required value={draft.workflowId} onChange={(e) => setDraft({ ...draft, workflowId: e.target.value })} />
        </label>
        <label>Action types (comma separated)
          <input value={draft.actionTypes} onChange={(e) => setDraft({ ...draft, actionTypes: e.target.value })} />
        </label>
        <label>Data classes (comma separated)
          <input value={draft.dataClasses} onChange={(e) => setDraft({ ...draft, dataClasses: e.target.value })} />
        </label>
        <label>Value limit
          <input type="number" min={0} step="0.01" value={draft.valueLimit} onChange={(e) => setDraft({ ...draft, valueLimit: e.target.value })} />
        </label>
        <label>Count limit
          <input type="number" min={0} step={1} value={draft.countLimit} onChange={(e) => setDraft({ ...draft, countLimit: e.target.value })} />
        </label>
        <label>Approval mode
          <select value={draft.approvalMode} onChange={(e) => setDraft({ ...draft, approvalMode: e.target.value as ApprovalMode })}>
            {APPROVAL_MODES.map((mode) => <option key={mode} value={mode}>{mode.replaceAll('_', ' ')}</option>)}
          </select>
        </label>
        <label>Schedule note (optional)
          <input value={draft.schedule} onChange={(e) => setDraft({ ...draft, schedule: e.target.value })} />
        </label>
        <button type="submit" disabled={pending}>{createMutation.isPending ? 'Creating…' : 'Create policy'}</button>
      </form>
    </section>
  )
}
