import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { apiErrorMessage } from '../../api/errorMessage'
import type { KillSwitch, KillSwitchStatus } from './types'

/** Real, distinct, readable feedback for every error code the kill-switch
 * endpoints can actually return -- never a raw `error.message`, matching
 * `PolicyPanel.tsx`/`WorkflowDetail.tsx`'s own `errorMessage()` convention.
 *
 * The reachable code set is deliberately read off `kill_switches.py` rather
 * than guessed: neither `POST /kill_switch` nor `POST /workflows/{id}/
 * kill_switch` has any domain-specific rejection at all (a kill switch's
 * scope is an opaque string -- no `WORKFLOW_NOT_FOUND`; activate/deactivate
 * against an already-active/inactive scope is a documented idempotent no-op,
 * not an error), so what remains is the shared envelope: the idempotency
 * conflict `_load_cached` raises, `CsrfDep`'s two 403s, FastAPI request
 * validation (the `reason` field's own `max_length=2000`), auth expiry, and
 * `api/client.ts`'s own transport-level `OFFLINE`/`NETWORK_ERROR` codes --
 * which is also the entire error surface of the `GET .../kill_switch` read.
 */
function errorMessage(error: unknown): string {
  return apiErrorMessage(error, {
    OFFLINE: 'You are offline, so the kill-switch state could not be read or changed. Nothing was sent.',
    NETWORK_ERROR: 'Could not reach the server, so the kill-switch state could not be read or changed. Nothing was sent.',
    IDEMPOTENCY_CONFLICT: 'A different kill-switch request was already recorded under this request key. Nothing was changed by this attempt -- reload and retry.',
    CSRF_TOKEN_REQUIRED: 'Your session\'s security token is missing or stale, so this change was rejected. Reload the page and try again.',
    CSRF_TOKEN_INVALID: 'Your session\'s security token is missing or stale, so this change was rejected. Reload the page and try again.',
    VALIDATION_ERROR: 'The server rejected this kill-switch request as invalid -- the reason may be longer than the 2000 characters it accepts.',
    '401': 'Your session is no longer valid. Sign in again to read or change kill switches.',
    '403': 'You are not permitted to change kill switches in this workspace.',
  })
}

function timestamp(value: string | null): string {
  return value ? new Date(value).toLocaleString() : 'unknown time'
}

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

  /** Invalidate the whole `['automation', 'kill-switch']` prefix, never one
   * exact id. A per-workflow toggle can change what the *currently
   * displayed* lookup reports even when the two ids differ (a global switch
   * changes every workflow's `killed`/`active_global`; a per-workflow toggle
   * for a different id leaves the displayed one alone but is cheap to
   * refetch), and matching the mutated id against `lookupId` was exactly the
   * bug that made "type an id, click Activate for this workflow" produce no
   * DOM change at all. The prefix also covers `WorkflowDetail.tsx`'s own
   * `['automation', 'kill-switch', workflowId]` query, so a switch flipped
   * here is not stale there either. */
  function refreshKillSwitchState() {
    void queryClient.invalidateQueries({ queryKey: ['automation', 'kill-switch'] })
  }

  const globalMutation = useMutation({
    mutationFn: (active: boolean) => apiRequest<KillSwitch>('/api/v1/automations/kill_switch', { method: 'POST', body: { active, reason: reason.trim() || null } }),
    onSuccess: () => {
      setReason('')
      refreshKillSwitchState()
    },
  })

  const workflowMutation = useMutation({
    mutationFn: ({ id, active }: { id: string; active: boolean }) =>
      apiRequest<KillSwitch>(`/api/v1/automations/workflows/${encodeURIComponent(id)}/kill_switch`, { method: 'POST', body: { active, reason: reason.trim() || null } }),
    onSuccess: (_result, variables) => {
      setReason('')
      // The lookup display follows whatever id the user just acted on, so a
      // toggle for an id they typed but never pressed "Check current status"
      // for still shows its real resulting state instead of nothing.
      setLookupId(variables.id)
      refreshKillSwitchState()
    },
  })

  const pending = globalMutation.isPending || workflowMutation.isPending

  return (
    <section className="work-panel" aria-labelledby="automation-kill-switch-title">
      <h2 id="automation-kill-switch-title">Kill switches</h2>
      <p>A global kill switch stops every workflow in this workspace from starting a new run. A per-workflow switch stops only that workflow. Both take effect before the next not-yet-started step.</p>

      {/* No `aria-label` here on purpose: the wrapping label's own visible
          text is the accessible name, so a speech-input user saying what
          they can read activates the field (WCAG 2.5.3 Label in Name). The
          previous "Kill switch reason" accessible name did not contain this
          visible text at all. There is only one reason field in this panel,
          so no disambiguating context is needed on top of it. */}
      <label>Reason (optional, recorded on the switch)
        <input value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>

      {globalMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(globalMutation.error)}</div> : null}
      <div className="work-actions">
        <button type="button" className="btn-destructive" disabled={pending} onClick={() => globalMutation.mutate(true)}>Activate global kill switch</button>
        <button type="button" disabled={pending} onClick={() => globalMutation.mutate(false)}>Deactivate global kill switch</button>
      </div>
      {/* Read straight off the server's own response row for the global
          scope, which has no per-workflow lookup display of its own to
          refresh -- a real confirmation that the toggle took effect, never an
          optimistic guess. Worded as what the server recorded for *this*
          request, not as a live global-state readout: this activation has no
          "read the current global state" endpoint (see this component's
          docstring), so a live claim would be exactly the fabrication that
          docstring rules out. */}
      {globalMutation.isSuccess ? (
        <div role="status" className="inline-status">
          The server recorded the global kill switch as {globalMutation.data.active ? 'active' : 'inactive'}
          {globalMutation.data.reason ? ` (${globalMutation.data.reason})` : ''}
          {globalMutation.data.active
            ? ` -- activated at ${timestamp(globalMutation.data.activated_at)}.`
            : ` -- deactivated at ${timestamp(globalMutation.data.deactivated_at)}.`}
        </div>
      ) : null}

      <h3>Check or toggle a specific workflow</h3>
      {/* "Workflow ID to check" already starts with the exact visible label
          text ("Workflow ID"), so it satisfies WCAG 2.5.3 while keeping the
          disambiguation from this panel's other id-shaped inputs. */}
      <label>Workflow ID
        <input aria-label="Workflow ID to check" value={workflowId} onChange={(e) => setWorkflowId(e.target.value)} />
      </label>
      <div className="work-actions">
        <button type="button" onClick={() => setLookupId(workflowId.trim() || null)} disabled={!workflowId.trim()}>Check current status</button>
        <button type="button" className="btn-destructive" disabled={pending || !workflowId.trim()} onClick={() => workflowMutation.mutate({ id: workflowId.trim(), active: true })}>Activate for this workflow</button>
        <button type="button" disabled={pending || !workflowId.trim()} onClick={() => workflowMutation.mutate({ id: workflowId.trim(), active: false })}>Deactivate for this workflow</button>
      </div>
      {workflowMutation.isError ? <div role="alert" className="inline-status error-panel">{errorMessage(workflowMutation.error)}</div> : null}

      {status.isLoading ? <p role="status">Loading kill-switch status…</p> : null}
      {/* A failed lookup must never render as nothing: an empty panel reads
          as "not blocked", which is the opposite of what an unknown state
          means (UX-STATES.md). */}
      {status.isError ? (
        <div role="alert" className="inline-status error-panel">
          <p><strong>{lookupId}</strong>: kill-switch status could not be confirmed -- treat this workflow's block state as unknown, not as "not blocked".</p>
          <p>{errorMessage(status.error)}</p>
        </div>
      ) : null}
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
                  // `key={index}`: `KillSwitchStatusResponse.history` is a list of
                  // `KillSwitchResponse` (kill_switches.py's `_to_response`), which
                  // deliberately does not carry the row's own `id` -- there is no
                  // stable server-side identifier available on this shape, and the
                  // list is a fixed, server-ordered history that is never reordered
                  // or spliced client-side, so the index is the only key there is.
                  <li key={index}>
                    {entry.active ? 'activated' : 'deactivated'}{entry.reason ? `: ${entry.reason}` : ''}
                    {entry.active
                      ? ` at ${timestamp(entry.activated_at)}${entry.activated_by ? ` by ${entry.activated_by}` : ''}`
                      : ` at ${timestamp(entry.deactivated_at)}${entry.deactivated_by ? ` by ${entry.deactivated_by}` : ''} (activated at ${timestamp(entry.activated_at)})`}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}
