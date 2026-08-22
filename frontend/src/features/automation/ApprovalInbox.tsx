import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { apiErrorMessage } from '../../api/errorMessage'
import type { Approval, ApprovalListResponse, RunDetail } from './types'

function isExpired(approval: Approval, now: Date): boolean {
  return approval.status === 'pending' && new Date(approval.expires_at).getTime() <= now.getTime()
}

function errorMessage(error: unknown): string | { code: string; text: string } {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : 'Request failed.'
  const details = error.current as { expected_digest?: string; provided_digest?: string | null; decision?: string; decided_at?: string; expires_at?: string } | undefined
  if (error.code === 'DIGEST_MISMATCH') {
    return { code: 'DIGEST_MISMATCH', text: `The digest you entered does not match this approval's current action digest (expected ${details?.expected_digest ?? 'unknown'}). This step's input may have changed since the request was made -- do not approve blind.` }
  }
  if (error.code === 'APPROVAL_EXPIRED') {
    return { code: 'APPROVAL_EXPIRED', text: `This approval expired${details?.expires_at ? ` at ${new Date(details.expires_at).toLocaleString()}` : ''} and can no longer be decided.` }
  }
  if (error.code === 'APPROVAL_ALREADY_DECIDED') {
    return { code: 'APPROVAL_ALREADY_DECIDED', text: `This approval was already ${details?.decision ?? 'decided'}${details?.decided_at ? ` at ${new Date(details.decided_at).toLocaleString()}` : ''}.` }
  }
  return { code: error.code, text: apiErrorMessage(error) }
}

/** One approval request, expanded with the run/step context a human needs
 * before the approve action is reachable (UX-STATES.md: "the exact target,
 * payload summary, high-impact category, reversible status and expiry are
 * shown before the approve action is reachable"). `reversible` is not
 * exposed by any run/approval endpoint in this activation (only `/simulate`
 * declares it, and only for a version this run's own pinned version can be
 * looked up from over HTTP when it is still the workflow's current active
 * version) -- disclosed explicitly rather than fabricated; see this
 * component's own note below and IMPLEMENTATION-STATUS.md's judgment
 * calls. */
function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: () => void }) {
  const [digestInput, setDigestInput] = useState('')
  const [attemptedApprove, setAttemptedApprove] = useState(false)

  const runDetail = useQuery({
    queryKey: ['automation', 'run', approval.run_id],
    queryFn: () => apiRequest<RunDetail>(`/api/v1/automations/runs/${approval.run_id}`),
    retry: 1,
  })
  const step = runDetail.data?.steps.find((s) => s.step_index === approval.step_index) ?? null

  const decideMutation = useMutation({
    mutationFn: ({ decision, digest }: { decision: 'approve' | 'reject'; digest?: string }) =>
      apiRequest<Approval>(`/api/v1/automations/approvals/${approval.id}/${decision}`, {
        method: 'POST',
        body: decision === 'approve' ? { action_digest: digest } : undefined,
      }),
    onSuccess: onDecided,
  })

  const expired = isExpired(approval, new Date())
  const decided = approval.status !== 'pending'
  const pending = decideMutation.isPending
  const decisionError = decideMutation.isError ? errorMessage(decideMutation.error) : null
  const decisionErrorText = typeof decisionError === 'string' ? decisionError : decisionError?.text

  return (
    <li>
      <div>
        <strong>Run {approval.run_id} · step {approval.step_index}</strong>
        <small>
          requested {new Date(approval.requested_at).toLocaleString()} · expires {new Date(approval.expires_at).toLocaleString()}
          {expired ? ' · EXPIRED' : ''}{decided ? ` · ${approval.status}` : ''}
        </small>
      </div>

      {approval.high_impact_categories.length ? (
        <ul aria-label={`High-impact categories for run ${approval.run_id} step ${approval.step_index}`}>
          {approval.high_impact_categories.map((category) => <li key={category}>{category.replaceAll('_', ' ')}</li>)}
        </ul>
      ) : <p>No high-impact category declared for this step.</p>}

      <p>Reversibility is not exposed at this decision surface by this activation's data model -- check the workflow's Simulate view for this adapter's declared reversible/irreversible status before approving a step you are unsure about.</p>

      {runDetail.isLoading ? <p role="status">Loading step payload…</p> : null}
      {runDetail.isError ? <div role="alert" className="inline-status error-panel">Could not load this step's payload ({runDetail.error.message}). Do not approve blind -- retry loading before deciding.</div> : null}
      {step ? (
        <>
          <p>Target: <strong>{step.action_ref ?? 'unknown -- could not resolve this step\'s adapter'}</strong></p>
          <details>
            <summary>Payload summary (redacted)</summary>
            <pre>{JSON.stringify(step.input, null, 2)}</pre>
          </details>
        </>
      ) : null}

      <p>Action digest to authorize: <code>{approval.action_digest}</code></p>

      {decisionErrorText ? <div role="alert" className="inline-status error-panel">{decisionErrorText}</div> : null}

      {!decided ? (
        <form onSubmit={(event) => { event.preventDefault(); setAttemptedApprove(true); if (digestInput.trim()) decideMutation.mutate({ decision: 'approve', digest: digestInput.trim() }) }}>
          <label>Echo the action digest above to approve
            <input aria-label={`Echo action digest for run ${approval.run_id} step ${approval.step_index}`} value={digestInput} onChange={(e) => setDigestInput(e.target.value)} disabled={expired || pending} autoComplete="off" />
          </label>
          {attemptedApprove && !digestInput.trim() ? <p role="alert">Enter the exact action digest before approving.</p> : null}
          <div className="work-actions">
            <button type="submit" disabled={expired || pending}>Approve</button>
            <button type="button" disabled={expired || pending} onClick={() => decideMutation.mutate({ decision: 'reject' })}>Reject</button>
          </div>
        </form>
      ) : null}
    </li>
  )
}

export default function ApprovalInbox() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['automation', 'approvals', 'pending'],
    queryFn: () => apiRequest<ApprovalListResponse>('/api/v1/automations/approvals?status=pending'),
    retry: 1,
  })

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ['automation', 'approvals'] })
    void queryClient.invalidateQueries({ queryKey: ['automation', 'run'] })
  }

  const approvals = query.data?.approvals ?? []

  return (
    <section className="work-panel" aria-labelledby="automation-approvals-title">
      <h2 id="automation-approvals-title">Approval inbox</h2>
      <p>Every pending, digest-bound approval request. Nothing here is pre-filled or auto-decided -- read the target and payload before deciding.</p>

      {query.isLoading ? <p role="status">Loading approvals…</p> : null}
      {query.isError ? <div role="alert" className="inline-status error-panel">{query.error.message}</div> : null}
      {query.data && approvals.length === 0 ? <p className="empty-state">No approvals are waiting on you.</p> : null}

      <ol className="work-list">
        {approvals.map((approval) => (
          <ApprovalCard key={approval.id} approval={approval} onDecided={refresh} />
        ))}
      </ol>
    </section>
  )
}
