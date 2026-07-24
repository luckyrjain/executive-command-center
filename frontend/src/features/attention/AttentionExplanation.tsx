import { useEffect, useMemo, useRef, useState } from 'react'
import { useMutation } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import type { AttentionItem } from './AttentionQueue'

/** `phase-004/API-SCHEMAS.md`'s `AiRunResponse` shape (`runtime.py:_to_
 * response`) -- `output` is `null` on every non-`completed` outcome
 * (`runtime.py:fail` never sets it), matching UX-STATES.md's "invalid
 * output ... never shown as the raw model output". */
export type AiRunUsage = { prompt_tokens: number | null; output_tokens: number | null; cost: number }
export type AiExplainOutput = { explanation_text: string; cited_factor_codes: string[] }
export type AiRunStatus = 'running' | 'completed' | 'degraded' | 'failed' | 'cancelled'

export type AiRunResponse = {
  id: string
  task: string
  status: AiRunStatus
  data_class: string
  policy_version: number | null
  model_id: string | null
  provider: string | null
  prompt_id: string | null
  prompt_version: number | null
  evidence: string[]
  output: AiExplainOutput | null
  error_code: string | null
  usage: AiRunUsage
  attempts: number
  started_at: string
  completed_at: string | null
}

// Decision 5's per-model-call timeout -- the bound the progress indicator
// below actually reflects (UX-STATES.md: "a real, bounded progress
// indicator (the 20s budget, not an indefinite spinner)"), not a made-up
// animation length.
const MODEL_CALL_BUDGET_SECONDS = 20

/** `API-SCHEMAS.md`'s Errors section names the required codes; a few more
 * (`grounding_failed`, `not_found`, `provider_error`) are real outcomes
 * `runtime.py:execute_run` can also return -- every one gets its own
 * neutral, non-alarming copy rather than falling through to a raw code.
 * No anthropomorphic certainty language anywhere here (design doc /
 * UX-STATES.md's accessibility rule): copy states what happened, not how
 * confident/sorry the system is about it. */
const ERROR_COPY: Record<string, string> = {
  schema_invalid: 'The AI model could not produce a valid explanation this time.',
  tool_not_allowlisted: 'This request was rejected by a safety check before it reached the model.',
  budget_exceeded: 'This explanation exceeded its time or length budget.',
  timeout: 'The local AI model timed out before finishing.',
  circuit_open: 'The local AI model is temporarily unavailable after repeated failures.',
  remote_not_configured: 'Only local, on-device AI is available in this workspace.',
  feature_disabled: 'AI explanations are not available for this item right now.',
  grounding_failed: 'The AI model referenced something outside this item’s factors, so the explanation was discarded.',
  not_found: 'This item could not be found for an AI explanation.',
  provider_error: 'The local AI model returned an unexpected error.',
}

function errorCopy(code: string | null): string {
  if (!code) return 'Could not produce an AI explanation right now.'
  return ERROR_COPY[code] ?? 'Could not produce an AI explanation right now.'
}

/** UX-STATES.md's "stale result": the item was rescored (or otherwise
 * changed) after this explanation was generated -- the explanation may no
 * longer reflect the item's current factors, even though it was grounded
 * and valid at the time. Compared against the item's own `generated_at`
 * (Phase 3's freshness field), not re-derived from anything this component
 * has no access to. */
function isStale(item: AttentionItem, run: AiRunResponse): boolean {
  if (!run.completed_at) return false
  const generated = new Date(item.generated_at).getTime()
  const completed = new Date(run.completed_at).getTime()
  if (Number.isNaN(generated) || Number.isNaN(completed)) return false
  return generated > completed
}

type LocalState =
  | { kind: 'idle' }
  | { kind: 'run'; run: AiRunResponse }
  | { kind: 'cancelled' }
  | { kind: 'network_error' }

/** `phase-004/UX-STATES.md`'s "First surface: attention-item explanation":
 * an optional, clearly labelled, discardable affordance on an attention
 * item's existing (deterministic) factor list -- never replacing it, and
 * never changing the item's score/ranking (this component issues no
 * mutation against `attention_items` at all, only `POST /api/v1/ai/runs`).
 *
 * `aiEnabled` is a caller-supplied kill switch distinct from `AttentionQueue.
 * tsx`'s own choice of whether to mount this component at all: the queue
 * achieves "the existing Attention Queue is pixel-for-pixel/behaviorally
 * unaffected" (Task 6, Step 3) by not rendering this component at all when
 * AI explanations are globally off, so `aiEnabled={false}` is not the path
 * production ever takes -- it exists so this component's own "AI disabled"
 * state (one of UX-STATES.md's required states) has real, direct test
 * coverage independent of that integration choice.
 */
export default function AttentionExplanation({ item, aiEnabled = true }: { item: AttentionItem; aiEnabled?: boolean }) {
  const [state, setState] = useState<LocalState>({ kind: 'idle' })
  const abortRef = useRef<AbortController | null>(null)
  const [elapsedSeconds, setElapsedSeconds] = useState(0)

  const mutation = useMutation({
    mutationFn: (signal: AbortSignal) =>
      apiRequest<AiRunResponse>('/api/v1/ai/runs', {
        method: 'POST',
        signal,
        body: { task: 'attention.explain_item', attention_item_id: item.id, data_class: 'sensitive' },
      }),
    onSuccess: (data) => setState({ kind: 'run', run: data }),
    onError: (error: unknown) => {
      // A cancel-initiated abort surfaces here too (fetch rejects the
      // in-flight promise) -- distinguished from a genuine network failure
      // by abortRef itself having already been cleared by `cancel()` below,
      // so the cancelled branch there is what actually sets `state` for
      // that case; anything else reaching here is a real network error.
      if (abortRef.current === null) return
      abortRef.current = null
      setState({ kind: 'network_error' })
      void error
    },
  })

  const pending = mutation.isPending

  useEffect(() => {
    if (!pending) {
      setElapsedSeconds(0)
      return
    }
    const startedAt = Date.now()
    const interval = window.setInterval(() => {
      setElapsedSeconds(Math.min(MODEL_CALL_BUDGET_SECONDS, (Date.now() - startedAt) / 1000))
    }, 250)
    return () => window.clearInterval(interval)
  }, [pending])

  const requestExplanation = () => {
    const controller = new AbortController()
    abortRef.current = controller
    setState({ kind: 'idle' })
    mutation.mutate(controller.signal)
  }

  const cancel = () => {
    const controller = abortRef.current
    abortRef.current = null
    controller?.abort()
    setState({ kind: 'cancelled' })
  }

  const discard = () => {
    mutation.reset()
    setState({ kind: 'idle' })
  }

  // `GET /api/v1/ai/runs/{id}` / `POST /api/v1/ai/runs/{id}/cancel`: this
  // activation's `POST /ai/runs` executes synchronously
  // (`runtime.py:create_run` -> `execute_run`), so a real response is
  // always already terminal (`cancel_run`'s own docstring: "by the time
  // any request can reach this endpoint the row is already terminal in
  // every real exercise of this API") -- `status: "running"` is not
  // reachable against today's backend. This still wires both endpoints for
  // real, not as dead code: a `running` row (from a future async
  // execution mode, or a deliberately crafted test fixture) is polled via
  // `GET` until terminal, and its own `Cancel` button calls the real
  // `POST .../cancel` endpoint by id -- exercised in this component's own
  // test suite against mocked responses, per this task's sandbox-testing
  // convention (no live backend needed).
  const cancelRunMutation = useMutation({
    mutationFn: (runId: string) => apiRequest<AiRunResponse>(`/api/v1/ai/runs/${runId}/cancel`, { method: 'POST' }),
    onSuccess: (data) => setState({ kind: 'run', run: data }),
  })

  useEffect(() => {
    if (state.kind !== 'run' || state.run.status !== 'running') return
    const runId = state.run.id
    let stopped = false
    const poll = () => {
      apiRequest<AiRunResponse>(`/api/v1/ai/runs/${runId}`)
        .then((fresh) => { if (!stopped) setState({ kind: 'run', run: fresh }) })
        .catch(() => {
          // A transient poll failure does not fail the run -- retried on
          // the next tick rather than surfaced as a network error, since
          // the run itself may still complete successfully server-side.
        })
    }
    const interval = window.setInterval(poll, 1000)
    return () => { stopped = true; window.clearInterval(interval) }
  }, [state.kind, state.kind === 'run' ? state.run.id : null, state.kind === 'run' ? state.run.status : null])

  const citedFactors = useMemo(() => {
    if (state.kind !== 'run' || !state.run.output) return []
    const byCode = new Map(item.factors.map((factor) => [factor.code, factor.label]))
    return state.run.output.cited_factor_codes.map((code) => ({ code, label: byCode.get(code) ?? code }))
  }, [state, item.factors])

  if (!aiEnabled) {
    return (
      <div className="ai-explanation ai-explanation-disabled">
        <p className="empty-state">AI explanations are turned off for this workspace.</p>
      </div>
    )
  }

  // `state.kind` (an explicit local decision: the user cancelled, or a
  // network error was classified) always takes priority over the raw
  // `mutation.isPending` flag -- a deliberate cancellation must update the
  // UI immediately rather than waiting for the aborted fetch's rejection
  // to actually propagate back through react-query (which, in a real
  // browser, happens quickly, but must never be a precondition for the
  // cancelled state to render).
  if (state.kind === 'cancelled') {
    return (
      <div className="ai-explanation inline-status" role="status">
        <p>The AI explanation request was cancelled.</p>
        <button type="button" onClick={requestExplanation}>Try again</button>
      </div>
    )
  }

  if (state.kind === 'network_error') {
    return (
      <div className="ai-explanation inline-status error-panel" role="alert">
        <p>Could not reach the AI service. Deterministic factors above are unaffected.</p>
        <button type="button" onClick={requestExplanation}>Try again</button>
      </div>
    )
  }

  if (state.kind === 'idle') {
    if (pending) {
      return (
        <div className="ai-explanation" aria-labelledby={`ai-explanation-pending-${item.id}`}>
          <p id={`ai-explanation-pending-${item.id}`}>
            Generating an AI explanation (up to {MODEL_CALL_BUDGET_SECONDS}s)…
          </p>
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={MODEL_CALL_BUDGET_SECONDS}
            aria-valuenow={Math.round(elapsedSeconds)}
            aria-label={`Generating AI explanation, up to ${MODEL_CALL_BUDGET_SECONDS}s`}
            className="ai-explanation-progress"
          >
            <div
              className="ai-explanation-progress-fill"
              style={{ width: `${Math.min(100, (elapsedSeconds / MODEL_CALL_BUDGET_SECONDS) * 100)}%` }}
            />
          </div>
          <button type="button" aria-label="Cancel AI explanation request" onClick={cancel}>Cancel</button>
        </div>
      )
    }
    return (
      <div className="ai-explanation">
        <button type="button" onClick={requestExplanation} aria-label={`Explain "${item.explanation}" with AI`}>
          Explain with AI
        </button>
      </div>
    )
  }

  const run = state.run

  if (run.status === 'running') {
    return (
      <div className="ai-explanation" aria-labelledby={`ai-explanation-pending-${item.id}`}>
        <p id={`ai-explanation-pending-${item.id}`}>
          Generating an AI explanation (up to {MODEL_CALL_BUDGET_SECONDS}s)…
        </p>
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={MODEL_CALL_BUDGET_SECONDS}
          aria-valuenow={Math.round(elapsedSeconds)}
          aria-label={`Generating AI explanation, up to ${MODEL_CALL_BUDGET_SECONDS}s`}
          className="ai-explanation-progress"
        >
          <div
            className="ai-explanation-progress-fill"
            style={{ width: `${Math.min(100, (elapsedSeconds / MODEL_CALL_BUDGET_SECONDS) * 100)}%` }}
          />
        </div>
        <button
          type="button"
          aria-label="Cancel AI explanation request"
          disabled={cancelRunMutation.isPending}
          onClick={() => cancelRunMutation.mutate(run.id)}
        >
          Cancel
        </button>
      </div>
    )
  }

  if (run.status === 'cancelled') {
    return (
      <div className="ai-explanation inline-status" role="status">
        <p>The AI explanation request was cancelled.</p>
        <button type="button" onClick={requestExplanation}>Try again</button>
      </div>
    )
  }

  if (run.status === 'degraded') {
    return (
      <div className="ai-explanation inline-status degraded-panel" role="status">
        <p>This AI explanation is degraded ({errorCopy(run.error_code)}). Deterministic factors above remain accurate.</p>
        <button type="button" onClick={requestExplanation}>Try again</button>
      </div>
    )
  }

  if (run.status === 'failed') {
    return (
      <div className="ai-explanation inline-status error-panel" role="alert">
        <p>{errorCopy(run.error_code)}</p>
        <button type="button" onClick={requestExplanation}>Try again</button>
      </div>
    )
  }

  // run.status === 'completed'
  const stale = isStale(item, run)
  return (
    <div className="ai-explanation ai-explanation-result" aria-label="AI-generated explanation">
      <p className="ai-explanation-badge">AI-generated explanation — not a deterministic score input</p>
      {stale ? (
        <div className="inline-status degraded-panel" role="status">
          <p>This explanation may be stale — the item has changed since it was generated.</p>
        </div>
      ) : null}
      {run.output ? <p>{run.output.explanation_text}</p> : null}
      {citedFactors.length ? (
        <ul aria-label="Factors this explanation cites">
          {citedFactors.map((factor) => <li key={factor.code}>{factor.label}</li>)}
        </ul>
      ) : null}
      <small>
        Model {run.model_id ?? 'unknown'} · prompt v{run.prompt_version ?? '?'}
      </small>
      <p className="empty-state">This does not change the item’s score or ranking.</p>
      <div className="work-actions" role="group" aria-label="AI explanation actions">
        <button type="button" onClick={requestExplanation}>Regenerate</button>
        <button type="button" aria-label="Discard AI explanation" onClick={discard}>Discard</button>
      </div>
    </div>
  )
}
