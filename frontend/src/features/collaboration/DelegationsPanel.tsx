import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { ApiError, apiRequest } from '../../api/client'
import { collaborationErrorMessage, formatTimestamp } from './errors'
import { useMe } from './useMe'
import type { Delegation, DelegationListResponse } from './types'

function statusLabel(status: Delegation['status']): string {
  return status.charAt(0).toUpperCase() + status.slice(1)
}

function DelegationRow({
  delegation,
  myAccountId,
  onChanged,
}: {
  delegation: Delegation
  myAccountId: string
  onChanged: () => void
}) {
  const isDelegator = delegation.delegator_account_id === myAccountId
  const isRecipient = delegation.recipient_account_id === myAccountId

  const actionMutation = useMutation({
    mutationFn: (action: 'accept' | 'reject' | 'revoke' | 'complete') =>
      apiRequest<Delegation>(`/api/v1/delegations/${delegation.id}/${action}`, { method: 'POST' }),
    onSuccess: onChanged,
    // DELEGATION_NOT_PROPOSED/DELEGATION_NOT_ACCEPTED (409) mean this
    // row's status is already stale (someone else acted on it first) --
    // without a refetch here, the action buttons stay enabled against that
    // stale state, so a retry click just 409s again in a loop. Broad
    // status check, not a per-code list, since all four actions
    // (accept/reject/revoke/complete) each have their own precondition
    // error code but share the identical "stale state" shape.
    onError: (error) => { if (error instanceof ApiError && error.status === 409) onChanged() },
  })

  return (
    <li>
      <div>
        <strong>{delegation.expected_outcome}</strong>
        <small> · {delegation.obligation_type} {delegation.obligation_resource_id}</small>
      </div>
      <div role="status" className={['proposed', 'accepted'].includes(delegation.status) ? 'inline-status' : 'inline-status degraded-panel'}>
        {statusLabel(delegation.status)} · due {formatTimestamp(delegation.due_at)}
        {isDelegator ? ' · you delegated this' : isRecipient ? ' · delegated to you' : ''}
      </div>

      {delegation.status === 'proposed' && isRecipient ? (
        <div className="work-actions">
          <button type="button" disabled={actionMutation.isPending} onClick={() => actionMutation.mutate('accept')}>Accept</button>
          <button type="button" disabled={actionMutation.isPending} onClick={() => actionMutation.mutate('reject')}>Reject</button>
        </div>
      ) : null}
      {delegation.status === 'proposed' && isDelegator ? (
        <p className="empty-state">Awaiting a response. There is no way to withdraw a still-pending delegation -- wait for the recipient to accept/reject, or for it to expire.</p>
      ) : null}
      {delegation.status === 'accepted' && isRecipient ? (
        <div className="work-actions">
          <button type="button" disabled={actionMutation.isPending} onClick={() => actionMutation.mutate('complete')}>Mark complete</button>
        </div>
      ) : null}
      {delegation.status === 'accepted' && isDelegator ? (
        <div className="work-actions">
          <button type="button" disabled={actionMutation.isPending} onClick={() => actionMutation.mutate('revoke')}>Revoke</button>
        </div>
      ) : null}

      {actionMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(actionMutation.error)}</div> : null}

      {delegation.evidence.length > 0 ? (
        <details>
          <summary>Evidence ({delegation.evidence.length})</summary>
          <ul>
            {delegation.evidence.map((item) => <li key={`${item.resource_type}-${item.resource_id}`}>{item.resource_type} {item.resource_id}</li>)}
          </ul>
        </details>
      ) : null}
    </li>
  )
}

type ProposeForm = {
  recipientAccountId: string
  obligationType: string
  obligationResourceId: string
  expectedOutcome: string
  dueAt: string
}

const EMPTY_PROPOSE_FORM: ProposeForm = {
  recipientAccountId: '',
  obligationType: '',
  obligationResourceId: '',
  expectedOutcome: '',
  dueAt: '',
}

export default function DelegationsPanel() {
  const queryClient = useQueryClient()
  const me = useMe()
  const [form, setForm] = useState<ProposeForm>(EMPTY_PROPOSE_FORM)

  const delegations = useQuery({
    queryKey: ['delegations'],
    queryFn: () => apiRequest<DelegationListResponse>('/api/v1/delegations'),
    retry: 1,
  })

  const proposeMutation = useMutation({
    mutationFn: (variables: ProposeForm) =>
      apiRequest<Delegation>('/api/v1/delegations', {
        method: 'POST',
        body: {
          recipient_account_id: variables.recipientAccountId.trim(),
          obligation_type: variables.obligationType.trim(),
          obligation_resource_id: variables.obligationResourceId.trim(),
          expected_outcome: variables.expectedOutcome.trim(),
          due_at: new Date(variables.dueAt).toISOString(),
          evidence: [],
        },
      }),
    onSuccess: () => {
      setForm(EMPTY_PROPOSE_FORM)
      onChanged()
    },
  })

  function onChanged() {
    void queryClient.invalidateQueries({ queryKey: ['delegations'] })
  }

  const items = delegations.data?.delegations ?? []
  const canPropose = form.recipientAccountId.trim() !== '' && form.obligationType.trim() !== '' && form.obligationResourceId.trim() !== '' && form.expectedOutcome.trim() !== '' && form.dueAt !== ''

  return (
    <section className="work-panel" aria-labelledby="delegations-title">
      <h2 id="delegations-title">Delegation inbox</h2>
      <p>Obligations you have delegated, and obligations delegated to you.</p>

      <form
        onSubmit={(event) => {
          event.preventDefault()
          proposeMutation.mutate(form)
        }}
      >
        <label>Recipient account ID
          <input aria-label="Recipient account ID" value={form.recipientAccountId} onChange={(event) => setForm((f) => ({ ...f, recipientAccountId: event.target.value }))} />
        </label>
        <label>Obligation resource type
          <input aria-label="Obligation resource type" value={form.obligationType} onChange={(event) => setForm((f) => ({ ...f, obligationType: event.target.value }))} />
        </label>
        <label>Obligation resource ID
          <input aria-label="Obligation resource ID" value={form.obligationResourceId} onChange={(event) => setForm((f) => ({ ...f, obligationResourceId: event.target.value }))} />
        </label>
        <label>Expected outcome
          <input aria-label="Expected outcome" value={form.expectedOutcome} onChange={(event) => setForm((f) => ({ ...f, expectedOutcome: event.target.value }))} />
        </label>
        <label>Due
          <input aria-label="Due date" type="datetime-local" value={form.dueAt} onChange={(event) => setForm((f) => ({ ...f, dueAt: event.target.value }))} />
        </label>
        <div className="work-actions">
          <button type="submit" disabled={!canPropose || proposeMutation.isPending}>Propose delegation</button>
        </div>
      </form>
      {proposeMutation.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(proposeMutation.error)}</div> : null}

      {me.isLoading || delegations.isLoading ? <p role="status">Loading delegations…</p> : null}
      {me.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(me.error)}</div> : null}
      {delegations.isError ? <div role="alert" className="inline-status error-panel">{collaborationErrorMessage(delegations.error)}</div> : null}
      {delegations.data && items.length === 0 ? <p className="empty-state">No delegations yet.</p> : null}

      {/* Gated on `me.data` too, not just `delegations.data` -- every row
          below needs `myAccountId` to decide which actions to show
          (Accept/Reject vs. Revoke vs. nothing); rendering the list while
          `me` is still loading would briefly show every row with no
          actions at all, alongside its own "Loading delegations…" status. */}
      {me.data ? (
        <ul className="work-list">
          {items.map((delegation) => (
            <DelegationRow key={delegation.id} delegation={delegation} myAccountId={me.data.account_id} onChanged={onChanged} />
          ))}
        </ul>
      ) : null}
    </section>
  )
}
