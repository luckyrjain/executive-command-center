import { useId, useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { apiErrorMessage } from '../../api/errorMessage'
import { applyWizardFieldInvalidState, useWizardStepFocus } from '../../lib/wizardFocus'
import type { AdapterListResponse, ApprovalMode, Policy, PolicyListResponse } from './types'

const APPROVAL_MODES: ApprovalMode[] = ['preview_only', 'per_run', 'bounded_recurring']

type Draft = {
  workflowId: string
  actionTypes: string[]
  /** The highest data class the policy allows (an ordinal ceiling). */
  dataClass: string
  valueLimit: string
  countLimit: string
  approvalMode: ApprovalMode
  schedule: string
}

const emptyDraft: Draft = { workflowId: '', actionTypes: [], dataClass: '', valueLimit: '0', countLimit: '10', approvalMode: 'per_run', schedule: '' }
const CREATE_STEPS = ['scope', 'limits', 'review'] as const
const CREATE_STEP_LABELS: Record<(typeof CREATE_STEPS)[number], string> = { scope: 'Scope', limits: 'Limits', review: 'Review' }
const CREATE_ERROR_ID = 'create-policy-error'
const DATA_CLASS_LABEL = 'Highest data class allowed'

function createErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === 'INSUFFICIENT_ROLE') {
    return 'You can see this workflow but not change it, so you cannot attach a policy to it.'
  }
  return errorMessage(error)
}

function revokeErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === 'INSUFFICIENT_ROLE') {
    return 'You do not have permission to revoke this policy.'
  }
  return errorMessage(error)
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === 'POLICY_REVOKED') {
    const details = error.current as { revoked_at?: string } | undefined
    return `This policy was already revoked${details?.revoked_at ? ` at ${new Date(details.revoked_at).toLocaleString()}` : ''}.`
  }
  if (error instanceof ApiError && error.code === 'POLICY_EXPIRED') {
    const details = error.current as { expires_at?: string } | undefined
    return `This policy already expired${details?.expires_at ? ` at ${new Date(details.expires_at).toLocaleString()}` : ''} and cannot be revoked further.`
  }
  if (error instanceof ApiError && error.code === 'POLICY_SCOPE_UNKNOWN_VALUE') {
    const details = error.current as { field?: string; values?: string[] } | undefined
    return `Unknown ${details?.field === 'data_classes' ? 'data class' : 'action type'}: ${(details?.values ?? []).join(', ') || 'unrecognised value'}.`
  }
  return apiErrorMessage(error, {
    POLICY_SCOPE_EMPTY: 'Choose at least one action type and the highest data class this policy allows.',
    WORKFLOW_NOT_FOUND: 'No workflow with this ID exists in this workspace that you can see. Check the ID.',
    POLICY_NOT_FOUND: 'That policy no longer exists in this workspace, or you can no longer see its workflow.',
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
  const [createStepIndex, setCreateStepIndex] = useState(0)
  const createFormRef = useRef<HTMLFormElement>(null)
  const scopeIdPrefix = useId()
  const createStep = CREATE_STEPS[createStepIndex] ?? 'scope'
  const [invalidField, setInvalidField] = useState<string | null>(null)
  const createStepHeadingRef = useWizardStepFocus(
    () => applyWizardFieldInvalidState(createFormRef.current, invalidField, CREATE_ERROR_ID, createStepHeadingRef.current),
    [createStep, invalidField],
  )

  const query = useQuery({
    queryKey: ['automation', 'policies', workflowFilter],
    queryFn: () => apiRequest<PolicyListResponse>(`/api/v1/automations/policies${workflowFilter ? `?workflow_id=${encodeURIComponent(workflowFilter)}` : ''}`),
    retry: 1,
  })

  // The closed scope vocabularies; the registry is global, so this never
  // changes within a session.
  const adaptersQuery = useQuery({
    queryKey: ['automation', 'adapters'],
    queryFn: () => apiRequest<AdapterListResponse>('/api/v1/automations/adapters'),
    staleTime: Infinity,
    retry: 1,
  })
  const actionTypes = adaptersQuery.data?.action_types ?? []
  const dataClasses = adaptersQuery.data?.data_classes ?? []
  const adaptersByType = (actionType: string) =>
    (adaptersQuery.data?.adapters ?? []).filter((a) => a.action_type === actionType).map((a) => a.adapter_id)

  const createMutation = useMutation({
    mutationFn: (body: Record<string, unknown>) => apiRequest<Policy>('/api/v1/automations/policies', { method: 'POST', body }),
    onSuccess: () => {
      setDraft(emptyDraft)
      setCreateStepIndex(0)
      setInvalidField(null)
      void queryClient.invalidateQueries({ queryKey: ['automation', 'policies'] })
    },
  })

  const revokeMutation = useMutation({
    mutationFn: (policyId: string) => apiRequest<Policy>(`/api/v1/automations/policies/${policyId}/revoke`, { method: 'POST' }),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['automation', 'policies'] }) },
  })

  function fail(message: string, field: string, step: (typeof CREATE_STEPS)[number]) {
    setFormError(message)
    setInvalidField(field)
    setCreateStepIndex(CREATE_STEPS.indexOf(step))
  }
  // `createStep !== 'review'` guard is load-bearing, not defensive
  // redundancy -- see the identical guard's comment in
  // ConnectorHealthPanel.tsx's own attemptCreate. A step with exactly one
  // field and no submit button mounted (Continue/Back are both
  // type="button") still triggers the browser's implicit single-field
  // form submission on Enter; without this, an early step's Enter key
  // could run full terminal validation before the user ever reaches
  // Review.
  function attemptCreate(event: FormEvent) {
    event.preventDefault()
    if (createStep !== 'review') return
    if (!draft.workflowId.trim()) { fail('Workflow ID is required.', 'Workflow ID', 'scope'); return }
    if (draft.actionTypes.length === 0) { fail('Choose at least one action type this policy authorizes.', actionTypes[0] ?? 'Workflow ID', 'scope'); return }
    if (!draft.dataClass) { fail('Choose the highest data class this policy allows.', DATA_CLASS_LABEL, 'scope'); return }
    const valueLimit = Number(draft.valueLimit)
    const countLimit = Number(draft.countLimit)
    if (!Number.isFinite(valueLimit) || valueLimit < 0) { fail('Value limit must be zero or a positive number.', 'Value limit', 'limits'); return }
    if (!Number.isInteger(countLimit) || countLimit < 0) { fail('Count limit must be zero or a positive whole number.', 'Count limit', 'limits'); return }
    setFormError(null)
    setInvalidField(null)
    createMutation.mutate({
      workflow_id: draft.workflowId.trim(),
      action_types: draft.actionTypes,
      data_classes: [draft.dataClass],
      value_limit: valueLimit,
      count_limit: countLimit,
      approval_mode: draft.approvalMode,
      schedule: draft.schedule.trim() || null,
      rate_limit: null,
    })
  }
  function goCreateNext() { setInvalidField(null); setCreateStepIndex((i) => Math.min(i + 1, CREATE_STEPS.length - 1)) }
  function goCreateBack() { setInvalidField(null); setCreateStepIndex((i) => Math.max(i - 1, 0)) }

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
      <div className="field-form">
        <label>Filter by workflow ID
          <input value={workflowFilter} onChange={(e) => setWorkflowFilter(e.target.value)} />
        </label>
      </div>

      {query.isLoading ? <p role="status">Loading policies…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(query.error)}</div> : null}
      {query.data && policies.length === 0 ? <p className="empty-state">No policies recorded yet.</p> : null}
      {revokeMutation.isError ? <div role="alert" className="inline-status error-panel">{revokeErrorMessage(revokeMutation.error)}</div> : null}

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
              {policy.scope_enforced ? null : (
                <small className="status-badge is-degraded">Legacy scope: not enforced, expires {new Date(policy.expires_at).toLocaleDateString()}</small>
              )}
              <small>expires {new Date(policy.expires_at).toLocaleString()}{policy.revoked_at ? ` · revoked ${new Date(policy.revoked_at).toLocaleString()}` : ''}</small>
            </div>
            <div className="work-actions">
              {policy.status === 'active' ? (
                <button type="button" className="btn-destructive" aria-busy={revokeMutation.isPending && revokeMutation.variables === policy.id} disabled={pending} aria-label={`Revoke policy for ${policy.workflow_id}`} onClick={() => revokeMutation.mutate(policy.id)}>
                  {revokeMutation.isPending && revokeMutation.variables === policy.id ? 'Revoking…' : 'Revoke'}
                </button>
              ) : null}
            </div>
          </li>
        ))}
      </ol>

      <form ref={createFormRef} noValidate onSubmit={attemptCreate} aria-labelledby="create-policy-title">
        <h3 id="create-policy-title">Create a policy</h3>
        {formError ? <div id={CREATE_ERROR_ID} role="alert" className="inline-status error-panel">{formError}</div> : null}
        {createMutation.isError ? <div role="alert" className="inline-status error-panel">{createErrorMessage(createMutation.error)}</div> : null}

        <ol className="wizard-stepper" aria-label="Create policy progress">
          {CREATE_STEPS.map((step, i) => (
            <li className="wizard-step-node" key={step} aria-current={i === createStepIndex ? 'step' : undefined}>
              <span className={i < createStepIndex ? 'wizard-step-circle done' : i === createStepIndex ? 'wizard-step-circle active' : 'wizard-step-circle upcoming'}>{i < createStepIndex ? '✓' : i + 1}</span>
              <span className={i <= createStepIndex ? 'wizard-step-label on' : 'wizard-step-label'}>{CREATE_STEP_LABELS[step]}</span>
              {i < CREATE_STEPS.length - 1 ? <span className={i < createStepIndex ? 'wizard-step-line done' : 'wizard-step-line'} /> : null}
            </li>
          ))}
        </ol>

        {createStep === 'scope' ? (
          <div className="field-form">
            <p className="eyebrow">Step {createStepIndex + 1} of {CREATE_STEPS.length} · Scope</p>
            <h4 ref={createStepHeadingRef} tabIndex={-1}>What does this policy govern?</h4>
            <label>Workflow ID
              <input value={draft.workflowId} onChange={(e) => setDraft({ ...draft, workflowId: e.target.value })} />
            </label>
            {adaptersQuery.isLoading ? <p role="status">Loading action types…</p> : null}
            {adaptersQuery.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(adaptersQuery.error)}</div> : null}
            {actionTypes.length ? (
              <fieldset>
                <legend>Action types this policy authorizes</legend>
                {actionTypes.map((actionType) => (
                  <div key={actionType}>
                    <label>{actionType}
                      <input
                        type="checkbox"
                        aria-describedby={`${scopeIdPrefix}-${actionType}-adapters`}
                        checked={draft.actionTypes.includes(actionType)}
                        onChange={(e) => setDraft({
                          ...draft,
                          actionTypes: e.target.checked
                            ? [...draft.actionTypes, actionType]
                            : draft.actionTypes.filter((t) => t !== actionType),
                        })}
                      />
                    </label>
                    <small id={`${scopeIdPrefix}-${actionType}-adapters`}>{adaptersByType(actionType).join(', ')}</small>
                  </div>
                ))}
              </fieldset>
            ) : null}
            {dataClasses.length ? (
              <label><span id={`${scopeIdPrefix}-data-class-label`}>{DATA_CLASS_LABEL}</span>
                {/* aria-labelledby: a select nested in its label would otherwise
                    also take the selected option's text into its name. */}
                <select aria-labelledby={`${scopeIdPrefix}-data-class-label`} value={draft.dataClass} onChange={(e) => setDraft({ ...draft, dataClass: e.target.value })}>
                  <option value="">Choose a data class</option>
                  {dataClasses.map((dataClass) => <option key={dataClass} value={dataClass}>{dataClass}</option>)}
                </select>
              </label>
            ) : null}
            <div className="work-actions"><button type="button" className="btn-primary" onClick={goCreateNext}>Continue</button></div>
          </div>
        ) : createStep === 'limits' ? (
          <div className="field-form">
            <p className="eyebrow">Step {createStepIndex + 1} of {CREATE_STEPS.length} · Limits</p>
            <h4 ref={createStepHeadingRef} tabIndex={-1}>What are the bounds?</h4>
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
            <div className="work-actions"><button type="button" onClick={goCreateBack}>Back</button><button type="button" className="btn-primary" onClick={goCreateNext}>Continue</button></div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createStepIndex + 1} of {CREATE_STEPS.length} · Review</p>
            <h4 ref={createStepHeadingRef} tabIndex={-1}>Review and create</h4>
            <dl>
              <div><dt>Workflow ID</dt><dd className="is-machine-value">{draft.workflowId || '—'}</dd></div>
              <div><dt>Action types</dt><dd>{draft.actionTypes.join(', ') || '—'}</dd></div>
              <div><dt>{DATA_CLASS_LABEL}</dt><dd>{draft.dataClass || '—'}</dd></div>
              <div><dt>Value limit</dt><dd>{draft.valueLimit}</dd></div>
              <div><dt>Count limit</dt><dd>{draft.countLimit}</dd></div>
              <div><dt>Approval mode</dt><dd>{draft.approvalMode.replaceAll('_', ' ')}</dd></div>
              <div><dt>Schedule note</dt><dd>{draft.schedule || '—'}</dd></div>
            </dl>
            <div className="work-actions"><button type="button" onClick={goCreateBack}>Back</button><button type="submit" className="btn-primary" aria-busy={createMutation.isPending} disabled={pending}>{createMutation.isPending ? 'Creating…' : 'Create policy'}</button></div>
          </div>
        )}
      </form>
    </section>
  )
}
