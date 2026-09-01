import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiRequest } from '../../api/client'
import { sharingErrorMessage, formatTimestamp } from './errors'
import type { Action, Grant, GrantListResponse, GrantPreview } from './types'

type GrantState = 'active' | 'expired' | 'revoked'

function grantState(grant: Grant, now: Date): GrantState {
  if (grant.revoked_at) return 'revoked'
  if (grant.expires_at && new Date(grant.expires_at).getTime() <= now.getTime()) return 'expired'
  return 'active'
}

function grantStateLabel(state: GrantState): string {
  if (state === 'revoked') return 'Revoked'
  if (state === 'expired') return 'Expired'
  return 'Active'
}

function GrantRow({ grant, now, onChanged }: { grant: Grant; now: Date; onChanged: () => void }) {
  const state = grantState(grant, now)
  const revokeMutation = useMutation({
    mutationFn: () => apiRequest<Grant>(`/api/v1/sharing/grants/${grant.id}`, { method: 'DELETE' }),
    onSuccess: onChanged,
  })

  return (
    <li>
      <div>
        <strong>{grant.resource_type}</strong>
        <small> · resource {grant.resource_id} · grantee {grant.grantee_account_id} · actions: {grant.actions.join(', ')}</small>
      </div>
      <div role="status" className={state === 'active' ? 'inline-status' : 'inline-status degraded-panel'}>
        {grantStateLabel(state)}
        {grant.expires_at ? ` · expires ${formatTimestamp(grant.expires_at)}` : ' · no expiry'}
        {grant.revoked_at ? ` · revoked ${formatTimestamp(grant.revoked_at)}` : ''}
      </div>
      {state !== 'revoked' ? (
        <div className="work-actions">
          <button type="button" className="btn-destructive" disabled={revokeMutation.isPending} onClick={() => revokeMutation.mutate()}>Revoke</button>
        </div>
      ) : null}
      {revokeMutation.isError ? <div role="alert" className="inline-status error-panel">{sharingErrorMessage(revokeMutation.error)}</div> : null}
    </li>
  )
}

type FormState = {
  resourceType: string
  resourceId: string
  granteeAccountId: string
  actions: Action[]
}

function requestSignature(form: FormState): string {
  return JSON.stringify({
    resource_type: form.resourceType.trim(),
    resource_id: form.resourceId.trim(),
    grantee_account_id: form.granteeAccountId.trim(),
    actions: [...form.actions].sort(),
  })
}

const EMPTY_FORM: FormState = { resourceType: '', resourceId: '', granteeAccountId: '', actions: ['read'] }

export default function SharingReview() {
  const queryClient = useQueryClient()
  const [form, setForm] = useState<FormState>(EMPTY_FORM)
  const [preview, setPreview] = useState<GrantPreview | null>(null)
  const [previewedSignature, setPreviewedSignature] = useState<string | null>(null)

  // Mirrors `GrantsPanel.tsx`'s identical ref -- avoids a stale in-flight
  // mutation's `onSuccess` (rebound to the latest render's closures by
  // TanStack Query v5) clearing a different in-progress draft after the
  // user has changed the form while a request was still outstanding.
  const formRef = useRef(form)
  formRef.current = form

  const grants = useQuery({
    queryKey: ['sharing', 'grants'],
    queryFn: () => apiRequest<GrantListResponse>('/api/v1/sharing/grants'),
    retry: 1,
  })

  const previewMutation = useMutation({
    mutationFn: (variables: FormState) =>
      apiRequest<GrantPreview>('/api/v1/sharing/grants/preview', {
        method: 'POST',
        body: {
          resource_type: variables.resourceType.trim(),
          resource_id: variables.resourceId.trim(),
          grantee_account_id: variables.granteeAccountId.trim(),
          actions: variables.actions,
        },
      }),
    onSuccess: (data, variables) => {
      setPreview(data)
      setPreviewedSignature(requestSignature(variables))
    },
  })

  const createMutation = useMutation({
    mutationFn: (variables: { form: FormState; narrowVisibility: boolean }) =>
      apiRequest<Grant>('/api/v1/sharing/grants', {
        method: 'POST',
        body: {
          resource_type: variables.form.resourceType.trim(),
          resource_id: variables.form.resourceId.trim(),
          grantee_account_id: variables.form.granteeAccountId.trim(),
          actions: variables.form.actions,
          narrow_visibility: variables.narrowVisibility,
        },
      }),
    onSuccess: (_data, variables) => {
      if (requestSignature(formRef.current) === requestSignature(variables.form)) {
        setForm(EMPTY_FORM)
        setPreview(null)
        setPreviewedSignature(null)
      }
      void queryClient.invalidateQueries({ queryKey: ['sharing', 'grants'] })
    },
  })

  function refresh() {
    void queryClient.invalidateQueries({ queryKey: ['sharing', 'grants'] })
  }

  function toggleAction(action: Action) {
    setForm((current) => {
      const has = current.actions.includes(action)
      const actions = has ? current.actions.filter((a) => a !== action) : [...current.actions, action]
      return { ...current, actions }
    })
    setPreview(null)
    setPreviewedSignature(null)
  }

  function updateField<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }))
    setPreview(null)
    setPreviewedSignature(null)
  }

  const now = new Date()
  const items = grants.data?.grants ?? []
  const canPreview = form.resourceType.trim() !== '' && form.resourceId.trim() !== '' && form.granteeAccountId.trim() !== '' && form.actions.length > 0
  // "Share" only unlocks once the *current* form has actually been
  // previewed -- `UX-STATES.md`'s "sharing previews exactly what becomes
  // visible" requirement means the owner must have seen the real
  // consequence of *this exact* grant, not a stale preview of a form
  // they've since edited.
  const previewMatchesForm = preview !== null && previewedSignature === requestSignature(form)
  const losingCount = preview?.members_losing_default_access.length ?? 0

  return (
    <section className="work-panel" aria-labelledby="sharing-review-title">
      <h2 id="sharing-review-title">Sharing review</h2>
      <p>
        Preview exactly what becomes visible to whom before sharing a resource. A resource-picker that looks up
        member names lands with the members panel (Task 9) -- for now, enter the grantee&apos;s account ID directly.
      </p>

      <form
        className="field-form"
        onSubmit={(event) => {
          event.preventDefault()
          previewMutation.mutate(form)
        }}
      >
        <label>Resource type
          <input aria-label="Resource type" value={form.resourceType} onChange={(event) => updateField('resourceType', event.target.value)} />
        </label>
        <label>Resource ID
          <input aria-label="Resource ID" value={form.resourceId} onChange={(event) => updateField('resourceId', event.target.value)} />
        </label>
        <label>Grantee account ID
          <input aria-label="Grantee account ID" value={form.granteeAccountId} onChange={(event) => updateField('granteeAccountId', event.target.value)} />
        </label>
        <fieldset>
          <legend>Actions to grant</legend>
          <label className="field-checkbox">
            <input type="checkbox" checked={form.actions.includes('read')} onChange={() => toggleAction('read')} /> Read
          </label>
          <label className="field-checkbox">
            <input type="checkbox" checked={form.actions.includes('write')} onChange={() => toggleAction('write')} /> Write
          </label>
        </fieldset>
        <div className="work-actions">
          <button type="submit" disabled={!canPreview || previewMutation.isPending}>Preview sharing</button>
        </div>
      </form>
      {previewMutation.isError ? <div role="alert" className="inline-status error-panel">{sharingErrorMessage(previewMutation.error)}</div> : null}

      {previewMatchesForm && preview ? (
        <div className="inline-status" aria-live="polite">
          <p>
            Visibility: <strong>{preview.current_visibility}</strong> {preview.current_visibility !== preview.proposed_visibility ? <>&rarr; <strong>{preview.proposed_visibility}</strong></> : '(unchanged)'}
          </p>
          {preview.requires_narrow_visibility_confirmation ? (
            <p className="degraded-panel" role="alert">
              This narrows visibility: {losingCount === 0
                ? 'no other member currently has access to lose.'
                : `${losingCount} other member${losingCount === 1 ? '' : 's'} will lose their current default access.`}
            </p>
          ) : null}
          <p>
            {preview.grantee_already_has_access
              ? 'The grantee already has this access today.'
              : preview.grantee_gains_actions.length > 0
                ? `The grantee gains: ${preview.grantee_gains_actions.join(', ')}.`
                : 'This grant would have no additional effect.'}
          </p>
          <div className="work-actions">
            <button
              type="button"
              disabled={createMutation.isPending}
              onClick={() => createMutation.mutate({ form, narrowVisibility: preview.requires_narrow_visibility_confirmation })}
            >
              Confirm and share
            </button>
          </div>
        </div>
      ) : null}
      {createMutation.isError ? <div role="alert" className="inline-status error-panel">{sharingErrorMessage(createMutation.error)}</div> : null}

      {grants.isLoading ? <p role="status">Loading grants…</p> : null}
      {grants.isError ? <div role="alert" className="inline-status error-panel">{sharingErrorMessage(grants.error)}</div> : null}
      {grants.data && items.length === 0 ? <p className="empty-state">No grants shared yet.</p> : null}

      <ul className="work-list">
        {items.map((grant) => <GrantRow key={grant.id} grant={grant} now={now} onChanged={refresh} />)}
      </ul>
    </section>
  )
}
