import { useMutation } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { DispatchGate, SimulateResponse } from './types'

const GATE_LABEL: Record<DispatchGate, string> = {
  clear: 'Clear -- would dispatch without a human decision',
  requires_approval: 'Requires approval before dispatch',
  policy_blocked: 'Blocked by policy',
  adapter_not_registered: 'Adapter not registered',
  not_applicable: 'Not applicable (routing step, no adapter)',
}

/**
 * `POST /workflows/{id}/simulate` results. UX-STATES.md requires simulation
 * results be "visually and structurally distinct from real run history at
 * every surface -- a simulated step can never be mistaken for one that
 * produced real side effects." Realized here with a persistent, always-
 * visible banner plus a dashed, tinted panel treatment used nowhere in
 * RunDetail's own real-step rendering (`.simulation-panel`, styles.css) --
 * never a subtle color-only difference.
 */
export default function SimulationView({ versionId }: { versionId: string }) {
  const simulateMutation = useMutation({
    mutationFn: () => apiRequest<SimulateResponse>(`/api/v1/automations/workflows/${versionId}/simulate`, { method: 'POST' }),
  })

  return (
    <section className="simulation-panel" aria-labelledby="automation-simulation-title">
      <p className="simulation-banner" role="status">
        SIMULATION -- preview only. No step below has produced or will produce a real side effect.
      </p>
      <h3 id="automation-simulation-title">Simulate this version</h3>
      <button type="button" disabled={simulateMutation.isPending} onClick={() => simulateMutation.mutate()}>
        {simulateMutation.isPending ? 'Simulating…' : 'Run simulation'}
      </button>

      {simulateMutation.isError ? (
        <div role="alert" className="inline-status error-panel">{simulateMutation.error instanceof Error ? simulateMutation.error.message : 'Simulation failed.'}</div>
      ) : null}

      {simulateMutation.data ? (
        <ol className="work-list" aria-label="Simulated steps">
          {simulateMutation.data.steps.map((step) => (
            <li key={step.step_index}>
              <div>
                <strong>[SIMULATED] {step.step_id}</strong>
                <small>{step.step_type}{step.action_ref ? ` · ${step.action_ref}` : ''}</small>
              </div>
              <div className="item-meta">
                <span>{GATE_LABEL[step.dispatch_gate]}</span>
                {step.policy_block_reason ? <span>{step.policy_block_reason.replaceAll('_', ' ')}</span> : null}
                {typeof step.reversible === 'boolean' ? <span>{step.reversible ? 'reversible' : 'irreversible'}</span> : null}
              </div>
              {step.high_impact_categories.length ? (
                <ul aria-label={`High-impact categories for ${step.step_id}`}>
                  {step.high_impact_categories.map((category) => <li key={category}>{category.replaceAll('_', ' ')}</li>)}
                </ul>
              ) : null}
              {step.compensate_ref ? <p>Compensated by: {step.compensate_ref}</p> : null}
              {step.error ? <p role="alert">{step.error}</p> : null}
              {step.preview ? (
                <details>
                  <summary>Simulated preview output</summary>
                  <pre>{JSON.stringify(step.preview, null, 2)}</pre>
                </details>
              ) : null}
            </li>
          ))}
        </ol>
      ) : null}
    </section>
  )
}
