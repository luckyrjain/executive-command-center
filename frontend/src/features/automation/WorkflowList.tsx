import { useRef, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { apiErrorMessage } from '../../api/errorMessage'
import { applyWizardFieldInvalidState, useWizardStepFocus } from '../../lib/wizardFocus'
import type { GraphStep, WorkflowListResponse, WorkflowVersion } from './types'

type StepDraft = {
  step_id: string
  step_type: GraphStep['step_type']
  action_ref: string
  input_mapping: string
  on_success: string
  on_failure: string
  compensate_ref: string
}

const emptyStep: StepDraft = {
  step_id: '',
  step_type: 'action',
  action_ref: '',
  input_mapping: '{}',
  on_success: 'succeeded',
  on_failure: 'failed',
  compensate_ref: '',
}

const STEP_TYPES: GraphStep['step_type'][] = ['action', 'approval_gate', 'condition', 'compensation']
const CREATE_STEPS = ['basics', 'build', 'review'] as const
const CREATE_STEP_LABELS: Record<(typeof CREATE_STEPS)[number], string> = { basics: 'Basics', build: 'Build', review: 'Review' }
const CREATE_ERROR_ID = 'draft-workflow-error'

function toGraphStep(draft: StepDraft): GraphStep {
  let input_mapping: Record<string, unknown> = {}
  try {
    const parsed: unknown = JSON.parse(draft.input_mapping || '{}')
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) input_mapping = parsed as Record<string, unknown>
  } catch {
    // Invalid JSON is caught by submit-time validation below; this fallback
    // only matters if a caller bypasses that check.
  }
  return {
    step_id: draft.step_id.trim(),
    step_type: draft.step_type,
    action_ref: draft.action_ref.trim() || null,
    input_mapping,
    on_success: draft.on_success.trim() || null,
    on_failure: draft.on_failure.trim() || null,
    compensate_ref: draft.compensate_ref.trim() || null,
  }
}

/** Real, distinct, readable feedback for every documented publish/create
 * error code (API-SCHEMAS.md) -- never raw JSON. */
function errorMessage(error: unknown): string {
  if (error instanceof ApiError && error.code === 'SCHEMA_INVALID') {
    const violations = (error.current as { violations?: string[] } | undefined)?.violations
    return violations?.length ? `Workflow graph is invalid: ${violations.join('; ')}` : 'Workflow graph is invalid.'
  }
  return apiErrorMessage(error, {
    WORKFLOW_VERSION_CONFLICT: 'Another version was created for this workflow at the same time. Reload and retry.',
    POLICY_NOT_FOUND: 'That policy ID does not exist in this workspace.',
    OFFLINE: 'You are offline, so workflows could not be read or created.',
    NETWORK_ERROR: 'Could not reach the server, so workflows could not be read or created.',
    '401': 'Your session is no longer valid. Sign in again to view workflows.',
  })
}

export default function WorkflowList({ onSelect }: { onSelect: (versionId: string) => void }) {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['automation', 'workflows'],
    queryFn: () => apiRequest<WorkflowListResponse>('/api/v1/automations/workflows'),
    retry: 1,
  })

  const [workflowId, setWorkflowId] = useState('')
  const [policyRef, setPolicyRef] = useState('')
  const [triggerRefs, setTriggerRefs] = useState('manual')
  const [steps, setSteps] = useState<StepDraft[]>([{ ...emptyStep }])
  const [formError, setFormError] = useState<string | null>(null)
  const [createStepIndex, setCreateStepIndex] = useState(0)
  const createFormRef = useRef<HTMLFormElement>(null)
  const createStep = CREATE_STEPS[createStepIndex] ?? 'basics'
  const [invalidField, setInvalidField] = useState<string | null>(null)
  const createStepHeadingRef = useWizardStepFocus(
    () => applyWizardFieldInvalidState(createFormRef.current, invalidField, CREATE_ERROR_ID, createStepHeadingRef.current),
    [createStep, invalidField],
  )

  const createMutation = useMutation({
    mutationFn: (body: { workflow_id: string; graph: { steps: GraphStep[] }; trigger_refs: string[]; policy_ref: string | null }) =>
      apiRequest<WorkflowVersion>('/api/v1/automations/workflows', { method: 'POST', body }),
    onSuccess: (created) => {
      setWorkflowId('')
      setPolicyRef('')
      setTriggerRefs('manual')
      setSteps([{ ...emptyStep }])
      setCreateStepIndex(0)
      void queryClient.invalidateQueries({ queryKey: ['automation', 'workflows'] })
      onSelect(created.id)
    },
    // A WORKFLOW_VERSION_CONFLICT means another version was created for
    // this workflow_id concurrently -- without a refetch here, the
    // workflows list doesn't reflect that new version, so the operator has
    // no visibility into what actually landed before deciding whether to
    // retry.
    onError: (error) => {
      if (error instanceof ApiError && error.code === 'WORKFLOW_VERSION_CONFLICT') {
        void queryClient.invalidateQueries({ queryKey: ['automation', 'workflows'] })
      }
    },
  })

  function updateStep(index: number, patch: Partial<StepDraft>) {
    setSteps((current) => current.map((step, i) => (i === index ? { ...step, ...patch } : step)))
  }

  function fail(message: string, field: string, step: (typeof CREATE_STEPS)[number]) {
    setFormError(message)
    setInvalidField(field)
    setCreateStepIndex(CREATE_STEPS.indexOf(step))
  }
  function attemptCreate(event: FormEvent) {
    event.preventDefault()
    if (!workflowId.trim()) {
      fail('Workflow ID is required.', 'Workflow ID', 'basics')
      return
    }
    for (let i = 0; i < steps.length; i += 1) {
      const step = steps[i]
      if (!step.step_id.trim()) {
        fail('Every step needs a step ID.', `Step ID for step ${i + 1}`, 'build')
        return
      }
      try {
        JSON.parse(step.input_mapping || '{}')
      } catch {
        fail(`Step "${step.step_id}": input mapping must be valid JSON.`, `Input mapping (JSON object) for step ${i + 1}`, 'build')
        return
      }
    }
    setFormError(null)
    setInvalidField(null)
    createMutation.mutate({
      workflow_id: workflowId.trim(),
      graph: { steps: steps.map(toGraphStep) },
      trigger_refs: triggerRefs.split(',').map((ref) => ref.trim()).filter(Boolean),
      policy_ref: policyRef.trim() || null,
    })
  }
  function goCreateNext() {
    setInvalidField(null)
    setCreateStepIndex((i) => Math.min(i + 1, CREATE_STEPS.length - 1))
  }
  function goCreateBack() {
    setInvalidField(null)
    setCreateStepIndex((i) => Math.max(i - 1, 0))
  }

  const pending = createMutation.isPending
  const workflows = query.data?.workflows ?? []

  return (
    <section className="work-panel" aria-labelledby="automation-workflow-list-title">
      <h2 id="automation-workflow-list-title">Workflows</h2>

      {query.isLoading ? <p role="status">Loading workflows…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(query.error)}</div> : null}
      {query.data && workflows.length === 0 ? <p className="empty-state">No workflows yet. Draft one below.</p> : null}

      <ol className="work-list">
        {workflows.map((summary) => (
          <li key={summary.workflow_id}>
            <div>
              <strong>{summary.workflow_id}</strong>
              <small>
                latest v{summary.latest_version} ({summary.latest_status})
                {summary.active_version ? ` · active v${summary.active_version}` : ' · no active version'}
              </small>
            </div>
            <div className="work-actions">
              <button type="button" onClick={() => onSelect(summary.latest_version_id)}>
                View latest (v{summary.latest_version})
              </button>
              {summary.active_version_id && summary.active_version_id !== summary.latest_version_id ? (
                <button type="button" onClick={() => onSelect(summary.active_version_id as string)}>
                  View active (v{summary.active_version})
                </button>
              ) : null}
            </div>
          </li>
        ))}
      </ol>

      <form ref={createFormRef} noValidate onSubmit={attemptCreate} aria-labelledby="draft-workflow-title">
        <h3 id="draft-workflow-title">Draft a new workflow (or a new version)</h3>
        <p>The builder is a structured step list, not a visual graph editor -- each step's type, action reference and routing are set explicitly below.</p>
        {formError ? <div id={CREATE_ERROR_ID} role="alert" className="inline-status error-panel">{formError}</div> : null}
        {createMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(createMutation.error)}</div> : null}

        <ol className="wizard-stepper" aria-label="Draft workflow progress">
          {CREATE_STEPS.map((step, i) => (
            <li className="wizard-step-node" key={step} aria-current={i === createStepIndex ? 'step' : undefined}>
              <span className={i < createStepIndex ? 'wizard-step-circle done' : i === createStepIndex ? 'wizard-step-circle active' : 'wizard-step-circle upcoming'}>
                {i < createStepIndex ? '✓' : i + 1}
              </span>
              <span className={i <= createStepIndex ? 'wizard-step-label on' : 'wizard-step-label'}>{CREATE_STEP_LABELS[step]}</span>
              {i < CREATE_STEPS.length - 1 ? <span className={i < createStepIndex ? 'wizard-step-line done' : 'wizard-step-line'} /> : null}
            </li>
          ))}
        </ol>

        {createStep === 'basics' ? (
          <div className="field-form">
            <p className="eyebrow">Step {createStepIndex + 1} of {CREATE_STEPS.length} · Basics</p>
            <h4 ref={createStepHeadingRef} tabIndex={-1}>Name and trigger this workflow</h4>
            {/* These three inputs take their accessible name from their own
                wrapping label's visible text (WCAG 2.5.3 Label in Name). "Trigger
                references"/"Policy ID" as `aria-label`s did *not* contain the
                visible "Trigger references (comma separated)"/"Policy ID (optional
                -- …)" text, and "Workflow ID" merely duplicated it. */}
            <label>Workflow ID
              <input value={workflowId} onChange={(e) => setWorkflowId(e.target.value)} placeholder="e.g. weekly-note-digest" />
            </label>
            <label>Trigger references (comma separated)
              <input value={triggerRefs} onChange={(e) => setTriggerRefs(e.target.value)} />
            </label>
            <label>Policy ID (optional -- attach after creating one from the Policies tab)
              <input value={policyRef} onChange={(e) => setPolicyRef(e.target.value)} placeholder="policy UUID" />
            </label>
            <div className="work-actions">
              <button type="button" onClick={goCreateNext}>Continue</button>
            </div>
          </div>
        ) : createStep === 'build' ? (
          <div className="field-form">
            <p className="eyebrow">Step {createStepIndex + 1} of {CREATE_STEPS.length} · Build</p>
            <h4 ref={createStepHeadingRef} tabIndex={-1}>Define the workflow steps</h4>
            <fieldset>
              <legend>Workflow steps</legend>
              {/* Each step field keeps an `aria-label` -- with several steps in one
                  form, "Step ID" alone would name every step's id field identically
                  -- but the label now *starts with* the field's own visible text and
                  only appends the step number after it ("Step ID for step 1", not
                  "Step 1 ID"), so the visible text is contained in the accessible
                  name as WCAG 2.5.3 requires. */}
              {steps.map((step, index) => (
                <div key={index} className="automation-step-editor field-form">
                  <label>Step ID
                    <input aria-label={`Step ID for step ${index + 1}`} value={step.step_id} onChange={(e) => updateStep(index, { step_id: e.target.value })} />
                  </label>
                  <label>Step type
                    <select aria-label={`Step type for step ${index + 1}`} value={step.step_type} onChange={(e) => updateStep(index, { step_type: e.target.value as GraphStep['step_type'] })}>
                      {STEP_TYPES.map((type) => <option key={type} value={type}>{type.replaceAll('_', ' ')}</option>)}
                    </select>
                  </label>
                  <label>Action reference
                    <input aria-label={`Action reference for step ${index + 1}`} value={step.action_ref} onChange={(e) => updateStep(index, { action_ref: e.target.value })} placeholder="e.g. local.create_note" />
                  </label>
                  <label>Input mapping (JSON object)
                    <textarea aria-label={`Input mapping (JSON object) for step ${index + 1}`} value={step.input_mapping} onChange={(e) => updateStep(index, { input_mapping: e.target.value })} />
                  </label>
                  <label>On success
                    <input aria-label={`On success for step ${index + 1}`} value={step.on_success} onChange={(e) => updateStep(index, { on_success: e.target.value })} placeholder="succeeded, failed, or another step_id" />
                  </label>
                  <label>On failure
                    <input aria-label={`On failure for step ${index + 1}`} value={step.on_failure} onChange={(e) => updateStep(index, { on_failure: e.target.value })} placeholder="succeeded, failed, or another step_id" />
                  </label>
                  <label>Compensate reference (action steps only)
                    <input aria-label={`Compensate reference (action steps only) for step ${index + 1}`} value={step.compensate_ref} onChange={(e) => updateStep(index, { compensate_ref: e.target.value })} placeholder="a compensation step_id" />
                  </label>
                  {steps.length > 1 ? (
                    <button type="button" onClick={() => setSteps((current) => current.filter((_, i) => i !== index))}>
                      Remove step {index + 1}
                    </button>
                  ) : null}
                </div>
              ))}
              <button type="button" onClick={() => setSteps((current) => [...current, { ...emptyStep }])}>Add step</button>
            </fieldset>
            <div className="work-actions">
              <button type="button" onClick={goCreateBack}>Back</button>
              <button type="button" onClick={goCreateNext}>Continue</button>
            </div>
          </div>
        ) : (
          <div className="wizard-review">
            <p className="eyebrow">Step {createStepIndex + 1} of {CREATE_STEPS.length} · Review</p>
            <h4 ref={createStepHeadingRef} tabIndex={-1}>Review and create</h4>
            <dl>
              <div><dt>Workflow ID</dt><dd className="is-machine-value">{workflowId || '—'}</dd></div>
              <div><dt>Trigger references</dt><dd className="is-machine-value">{triggerRefs || '—'}</dd></div>
              <div><dt>Policy ID</dt><dd className="is-machine-value">{policyRef || '—'}</dd></div>
            </dl>
            <ol className="work-list">
              {steps.map((step, index) => (
                <li key={index}>
                  <strong>{step.step_id || `Step ${index + 1}`}</strong>
                  <small>{step.step_type.replaceAll('_', ' ')}{step.action_ref ? ` · ${step.action_ref}` : ''}</small>
                </li>
              ))}
            </ol>
            <div className="work-actions">
              <button type="button" onClick={goCreateBack}>Back</button>
              <button type="submit" disabled={pending}>{pending ? 'Creating…' : 'Create draft'}</button>
            </div>
          </div>
        )}
      </form>
    </section>
  )
}
